# -*- coding: utf-8 -*-
"""CAN Companion 上位机（pywebview）：CAN 固件升级 + CAN 变量观察（WATCH）。

HostAPI 暴露给 JS：连接状态/升级进度经后端推送线程下发（onPush）；
升级与 CAN 往返命令（探测/INFO/RUN）在后台线程跑——js_api 只做投递，
UI 线程不阻塞。

升级流程在后台线程里按两种情况走同一条路：探测（0x20 状态查询）
→ 已在 Bootloader 就直接擦写；在跑 App 就把结论交给用户确认，同意后
单播升级请求，设备直接跳进 Bootloader（不经复位），上位机再继续等应答。
等待确认的状态通过推送的 progress 结构传给前端，用户的答复由
confirm_upgrade() 投递回后台线程。

注意：js_api 的公开非方法属性会被 pywebview 递归遍历，挂窗口/驱动对象
会导致启动期访问未就绪窗口而崩溃——内部字段一律 `_` 前缀。

启动：python -m monitor_tui.host_app [--parent-pid N]

同名互斥量保证整机只有一个 host_app 实例（USBCAN2 是独占设备，多开只会让后开的那个
打不开设备）。--parent-pid 供脚本/自动化拉起时使用：目标进程一旦消失就关闭 CAN 并结束
本进程，防止没人关窗导致设备被长期占用。传调用方自身的 PID；若误传了 .venv 启动器的
PID（它会等待本进程，盯它永远不会触发），启动时会自动改盯启动器的父进程。
"""
import argparse
import atexit
import ctypes
import json
import os
import queue
import re
import sys
import threading
import time

import webview

from monitor_tui._version import APP_VERSION
from monitor_tui.canbus import BAUD_TIMING
from monitor_tui.protocol import (CMD_BUILD_TIME, CMD_ENTER_BL, CMD_PROBE,
                                  BROADCAST, INFO_MODE_APP, INFO_MODE_ENTER_BL,
                                  OWN_DEV, own_cmd_of, own_id,
                                  decode_build_time, decode_probe,
                                  encode_build_time_query,
                                  encode_enter_bl, encode_probe_query)
from monitor_tui.upgrade import bl_protocol as P
from monitor_tui.upgrade import selective
from monitor_tui.upgrade import firmware as FW
from monitor_tui.upgrade.bl_client import (BootloaderError, FlashBootloader,
                                           pick_encoding)
from monitor_tui.upgrade.can_channel import CanChannel
from monitor_tui.upgrade.probe import (PROBE_TIMEOUTS_S, DeviceProbe,
                                       PROBE_TICK_S, PROBE_TIMEOUT_DEFAULT_S,
                                       bl_probe_addr, no_device_hint)
from monitor_tui.watch import WatchManager

CONFIRM_TIMEOUT_S = 120.0  # 确认弹窗没人答复就干净退出，不给设备发任何东西

# 传输编码的显示叫法（升级日志与完成消息用；编码选择见 bl_client.pick_encoding）
_ENCODING_CN = {P.ENC_DIRECT: "不压缩（直通）",
                P.ENC_LZ4D: "LZ4 链式（含填充块跳过）", P.ENC_LZ4: "LZ4"}
DISCOVER_QUIET_S = 2.0   # 批量发现的静默退出：最后一条新地址后再等这么久就收手
                         # 一个设备都没等到时仍等满窗口，保住设备后上电的场景
AUTOFLASH_POLL_S = 1.0   # 自动烧录看门狗的轮询间隔（模块级，离线测试可注入）
AUTOFLASH_STABLE_S = 0.4  # 变化后复核一次的稳定等待，避开编译器半写的文件

MUTEX_NAME = "Local\\CAN_Companion_HostApp"
ERROR_ALREADY_EXISTS = 183
EXIT_ALREADY_RUNNING = 3

_mutex_handle = None


class UpgradeCancelled(BootloaderError):
    """升级流程被用户取消或超时结束：不是设备故障，界面按中性提示显示。"""


def acquire_single_instance() -> bool:
    """抢命名互斥量；成功后句柄不关闭，进程终止时由系统统一释放。"""
    global _mutex_handle
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex_handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return False
    # 互斥量创建本身失败（极少见）时放行，不能让系统 API 异常把用户挡在门外。
    return True


def _parent_process_gone(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        # ERROR_ACCESS_DENIED(5) 说明进程还在、只是无权查询，其余失败码按已消失处理。
        return kernel32.GetLastError() != 5
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return True
        return code.value != 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(h)


def _watch_parent(api: "HostAPI", pid: int) -> None:
    while True:
        time.sleep(2.0)
        if _parent_process_gone(pid):
            api._log("盯的目标进程 %d 已退出，关闭 CAN 并结束本进程" % pid)
            api.disconnect()
            sys.stdout.flush()
            # 这里直接结束进程，不走窗口关闭事件：调用方是脚本时没人去点窗口；
            # os._exit 不等待其它线程，即使有线程滞留也照样能退出去。
            os._exit(0)


def _process_parent(pid: int):
    """Toolhelp32 快照查 pid 的父 PID；查不到返回 None。"""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                    ("th32ProcessID", ctypes.c_ulong),
                    ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                    ("th32ParentProcessID", ctypes.c_ulong),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_ulong),
                    ("szExeFile", ctypes.c_wchar * 260)]

    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snap in (None, ctypes.c_void_p(-1).value):
        return None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.th32ProcessID == pid:
                return entry.th32ParentProcessID
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        return None
    finally:
        kernel32.CloseHandle(snap)


def resolve_parent_watch_target(pid: int):
    """把 --parent-pid 修正为真正会先消失的进程，返回要盯的 PID；无可盯目标返回 None。

    真身跑在 .venv 启动器之下（判据：sys.executable 是 venv 路径而基础解释器在别处），
    此时调用方若把启动器 PID 传进来，启动器会等待真身退出，盯它永远不会触发；
    改盯启动器的父进程（拉起 .bat 的那个调用方）。sys.executable 等于基础解释器时
    （直接用系统 python 运行，父进程就是调用方本身）保持原值不动。
    """
    if pid != os.getppid():
        return pid
    try:
        redirected = sys.executable != sys._base_executable
    except AttributeError:
        redirected = False
    if not redirected:
        return pid
    upper = _process_parent(pid)
    if upper and upper != pid:
        print("传入的 --parent-pid %d 是 .venv 启动器（它等待本进程退出），"
              "已改盯它的父进程 %d" % (pid, upper), flush=True)
        return upper
    return None


class HostAPI:
    def __init__(self):
        self._window = None
        self._ch = None
        self._fw = None
        self._fw_path = None      # 最近一次成功 load_file 的路径（升级前强制重载用）
        self._modules = []
        self._bl_info = None
        self._dev_state = None     # 最近一轮探测的结论：bl / app / timeout
        self._app_info = None      # 在跑 App 时的 {"addr","version"}，供升级页显示
        self._cancel = threading.Event()
        self._thread = None
        self._progress = self._new_progress()
        self._upgrade_all = None      # 批量升级进度：{"devices": [...], "current": "0x.."}
        self._logs = []
        self._confirm_evt = threading.Event()
        self._confirm_answer = None    # True=用户同意进入升级模式，False=拒绝，None=未答复
        self._confirm_seq = 0          # 弹窗序号：前端据此判断是新请求还是同一次的重绘
        self._probe_clock = time.monotonic  # 探测节拍时钟，离线测试可注入虚拟时钟
        self._poll_clock = time.monotonic   # 轮询节拍时钟，离线测试可注入虚拟时钟
        self._jobq = queue.Queue()
        threading.Thread(target=self._job_loop, name="can-jobs", daemon=True).start()
        self._build_time = None              # 固件编译时刻（Unix 秒），0x07 查询回填
        self._app_build_time = None          # App 侧编译时刻（B1=0x01 应答）
        self._baud = 125000                  # 连接配置存值（设置页修改，connect 缺省用）
        self._channel = 0
        self._auto_flash = False             # 自动烧录（仅单机）：固件文件变化即重载+升级
        self._auto_flash_addr = None         # None = 由探测决定（界面地址空/广播时）
        self._auto_flash_compress = True
        self._af_thread = None               # fw-watch 看门狗线程
        self._af_stop = threading.Event()
        self._af_snapshot = None             # 看门狗上次看到的固件 (mtime_ns, size)
        # 后端推送：状态版本号（任何状态/进度/日志变化 +1），推送线程 50ms 一拍，
        # 版本变了才把快照推给前端——前端不再有状态轮询，更新延迟 = 推送拍
        self._state_ver = 0
        self._push_thread = None
        self._push_stop = threading.Event()
        self._watch = WatchManager(self)     # WATCH 页后端：符号/轮询/写入

    # ---- 内部 -----------------------------------------------------------
    def _new_progress(self):
        return {"running": False, "percent": 0, "stage": "",
                "blocks_done": 0, "blocks_total": 0,
                "message": "", "success": None, "cancelled": False,
                "confirm": None,      # 非空表示流程停在等待用户答复，内容供弹窗显示
                "upgrade_mode": ""}   # 本轮升级方式：增量升级（跳过 N/M 块）/ 全量升级

    def _log(self, msg):
        # 控制台编码可能容不下消息里的符号（如失败标记 ✕）：回显降级即可，
        # 前端日志面板读的 self._logs 必须始终拿到原文。
        stamp = time.strftime("%H:%M:%S")
        try:
            print("[%s] %s" % (stamp, msg), flush=True)
        except UnicodeEncodeError:
            enc = getattr(sys.stdout, "encoding", None) or "ascii"
            print("[%s] %s" % (stamp, msg.encode(enc, "replace").decode(enc)), flush=True)
        self._logs.append(msg)
        if len(self._logs) > 500:
            self._logs = self._logs[-500:]
        self._bump()

    def _set_progress(self, **kw):
        p = dict(self._progress)
        p.update(kw)
        self._progress = p
        self._bump()

    # ---- 后端推送（前端不轮询状态，版本变了 ≤50ms 推过去） -------------------
    def _bump(self):
        self._state_ver += 1

    def start_push(self):
        """窗口就绪后启动推送线程。快照组装复用 get_status/get_progress
        （与前端展示同源同构）。"""
        if self._push_thread is not None and self._push_thread.is_alive():
            return
        self._push_stop.clear()
        self._push_thread = threading.Thread(target=self._push_loop,
                                             name="ui-push", daemon=True)
        self._push_thread.start()

    def _push_loop(self):
        last = -1
        while not self._push_stop.wait(0.05):
            win = self._window
            if win is None or self._state_ver == last:
                continue
            last = self._state_ver
            try:
                payload = {"s": self.get_status(), "p": self.get_progress(),
                           "w": self._watch.snapshot()}
                js = json.dumps(payload, ensure_ascii=False)
                # U+2028/2029 在 JS 字符串字面量里是非法行分隔符，转义掉
                js = js.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
                win.evaluate_js("onPush(%s)" % js)
            except Exception:  # noqa: BLE001 - 窗口关闭中/桥忙，下一拍再试
                pass

    def _bl(self, addr):
        if self._ch is None:
            raise BootloaderError("CAN 未连接")
        return FlashBootloader(self._ch, addr=addr)

    # ---- 后台任务队列（CAN 往返命令离开 UI 线程） ------------------------
    def _job_loop(self):
        while True:
            fn = self._jobq.get()
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - 任务线程不炸宿主
                self._log("✕ 后台任务异常: %s" % e)

    def _submit(self, fn):
        """投递后台 CAN 任务；结果经推送的 logs 回前端。"""
        if self._progress["running"]:
            return {"success": False, "message": "升级进行中，请稍后再试"}
        self._jobq.put(fn)
        return {"success": True, "pending": True}

    # ---- 总线旁路（接收线程回调，只做轻量摄取） --------------------------
    def _on_bus_frame(self, frame):
        """总线旁路：捕获 0x07 编译时刻应答（App/BL 侧标识区分）。"""
        if frame.xtd and own_cmd_of(frame.id) == CMD_BUILD_TIME:
            bt = decode_build_time(bytes(frame.data))
            if bt is not None:
                side, ts = bt
                if side == 2 and ts != self._build_time:
                    self._build_time = ts
                if side == 1 and ts != self._app_build_time:
                    self._app_build_time = ts
                self._log("固件编译时刻: %s（Unix %d）"
                          % (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)), ts))
        self._bump()

    # ---- 固件文件 -------------------------------------------------------
    def pick_file(self):
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=("固件文件 (*.out;*.elf;*.hex)", "所有文件 (*.*)"))
        if not result:
            return {"success": False, "message": "已取消"}
        return self.load_file(result[0])

    def load_file(self, path):
        try:
            self._fw = FW.load_firmware(path)
        except FW.FirmwareError as e:
            self._fw = None
            self._fw_path = None
            self._log("固件解析失败: %s" % e)
            return {"success": False, "message": str(e)}
        self._fw_path = path
        if self._auto_flash:
            # 手动选择/启动恢复是用户主动加载，不算"文件变化"；
            # 快照对齐后看门狗只认这之后的外部改动（如编译器重写）
            self._af_snapshot = self._fw_stat(path)
        self._log("固件就绪: %s（%s，%d octet，基址 0x%X，CRC32 0x%08X，修改 %s）"
                  % (os.path.basename(path), self._fw.fmt.upper(),
                     self._fw.size, self._fw.base_addr, self._fw.crc32,
                     self._fw_mtime_str(path)))
        return {"success": True, "file": self._fw_info()["file"]}

    @staticmethod
    def _fw_mtime_str(path):
        """固件文件最新修改时间（本地时间字符串）；读不到返回 None。"""
        if not path:
            return None
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.localtime(os.stat(path).st_mtime))
        except OSError:
            return None

    def _fw_info(self):
        """当前固件的展示信息（load_file 与看门狗重载刷新共用）。"""
        fw, path = self._fw, self._fw_path
        if fw is None:
            return None
        return {"file": {"name": os.path.basename(path), "path": path,
                          "format": fw.fmt.upper(), "size": fw.size,
                          "base_addr": "0x%X" % fw.base_addr,
                          "crc32": "0x%08X" % fw.crc32,
                          "mtime": self._fw_mtime_str(path)}}

    def restore_firmware(self, path):
        """启动时静默恢复上次选择的固件：文件还在就重载（成功附 restored=True）；
        不在则返回 missing=True 且不写日志——跨启动文件被清理是正常情况。"""
        if not path or not os.path.isfile(path):
            return {"success": False, "missing": True, "message": "固件文件不存在: %s" % path}
        r = self.load_file(path)
        if r.get("success"):
            r["restored"] = True
        return r

    # ---- 连接配置（设置页） ----------------------------------------------
    def set_link_config(self, baud, channel):
        """设置页：更新波特率/通道存值。已连接时先断开再按新配置重连
        （升级进行中拒绝——断开会 abort 升级流程）；未连接则下次连接生效。"""
        try:
            baud = int(baud)
        except (TypeError, ValueError):
            return {"success": False, "message": "波特率非法"}
        try:
            channel = int(channel)
        except (TypeError, ValueError):
            return {"success": False, "message": "通道号非法"}
        if baud not in BAUD_TIMING:
            return {"success": False, "message": "不支持的波特率 %d（可选 %s）"
                    % (baud, "、".join("%dk" % (b // 1000)
                                       for b in sorted(BAUD_TIMING)))}
        if channel not in (0, 1):
            return {"success": False, "message": "通道号仅支持 0 或 1"}
        if self._progress["running"]:
            return {"success": False, "message": "升级进行中，不能改连接配置"}
        was_connected = self._ch is not None
        self._baud, self._channel = baud, channel
        self._log("连接配置更新: USBCAN2 通道 %d，%d kbps" % (channel, baud // 1000))
        if not was_connected:
            return {"success": True, "reconnected": False}
        self.disconnect()
        r = self.connect()   # connect 缺省就用刚更新的存值
        if not r.get("success"):
            return {"success": False, "reconnected": False,
                    "message": "按新配置重连失败: %s" % r.get("message", "")}
        return {"success": True, "reconnected": True}

    # ---- 自动烧录（仅单机） ----------------------------------------------
    def set_auto_flash(self, enabled, addr=None, compress=True, silent=False):
        """开关自动烧录看门狗：开着时固件文件一变化就自动重载并升级（单机场景）。

        addr 走 _parse_target 校验；空/0x3F 视为交给探测决定。
        silent=True 表示启动时随持久化恢复，日志从简。"""
        target = self._parse_target(addr)
        if target < 0:
            return {"success": False,
                    "message": "目标地址非法（0x3F 广播或 0x01~0x3B 单播）"}
        enabled = bool(enabled)
        self._auto_flash = enabled
        self._auto_flash_addr = None if target == 0x3F else target
        self._auto_flash_compress = bool(compress)
        if enabled:
            self._af_snapshot = self._fw_stat(self._fw_path)
            self._af_stop.clear()
            if self._af_thread is None or not self._af_thread.is_alive():
                self._af_thread = threading.Thread(target=self._auto_flash_watch,
                                                   name="fw-watch", daemon=True)
                self._af_thread.start()
            if silent:
                self._log("自动烧录已随启动恢复（固件更新后自动写入，仅单机）")
            else:
                self._log("自动烧录开: 盯 %s，变化即重载并升级（目标 %s，仅单机）"
                          % (self._fw_path or "（尚未选择固件）",
                             "0x%02X" % target if target != 0x3F else "由探测决定"))
        else:
            self._af_stop.set()
            self._af_thread = None
            self._log("自动烧录关")
        return {"success": True, "enabled": enabled, "addr": self._auto_flash_addr}

    @staticmethod
    def _fw_stat(path):
        """固件文件的 (mtime_ns, size) 快照；文件读不到返回 None。"""
        if not path:
            return None
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _auto_flash_watch(self):
        """fw-watch 看门狗线程：每 AUTOFLASH_POLL_S 查一次固件文件 (mtime_ns, size)。

        防抖两道：1) 快照与上次不同后等 AUTOFLASH_STABLE_S 再取一次，两次一致才
        认为编译器写完了；2) 触发前 CAN 未连接或升级进行中就记日志、把快照对齐到
        当前文件并跳过本轮，等下一次变化再试。真正执行投进 _jobq 单工人队列，
        与手动任务天然串行。"""
        while not self._af_stop.wait(AUTOFLASH_POLL_S):
            path = self._fw_path
            if not path:
                continue
            snap = self._fw_stat(path)
            if snap is None or snap == self._af_snapshot:
                continue
            time.sleep(AUTOFLASH_STABLE_S)   # 稳定等待：文件还在写就放弃本轮
            snap2 = self._fw_stat(path)
            if snap2 is None or snap2 != snap:
                continue
            self._af_snapshot = snap2
            if self._ch is None or self._progress["running"]:
                why = "CAN 未连接" if self._ch is None else "升级进行中"
                self._log("自动烧录: 固件已变化，但当前%s，跳过本轮（快照已对齐）" % why)
                continue
            addr = self._auto_flash_addr
            self._log("自动烧录: 固件文件变化，自动重载并开始升级%s"
                      % ("（目标 0x%02X）" % addr if addr else ""))
            self._jobq.put(lambda a=addr: self._auto_flash_job(a))

    def _auto_flash_job(self, addr):
        """看门狗投进 _jobq 的任务：复用 start_upgrade 的参数组装（含硬盘重载）。"""
        r = self.start_upgrade(addr=("0x%02X" % addr) if addr else None,
                               compress=getattr(self, "_auto_flash_compress", True),
                               auto=True)
        if not r.get("success"):
            self._log("自动烧录未启动: %s" % r.get("message", ""))
        elif getattr(self, "_window", None) is not None:
            # 硬盘重载后把升级页的固件 chip（含最新修改时间）刷到当前
            try:
                info = self._fw_info()
                if info:
                    self._window.evaluate_js("showFileChip(%s)" % json.dumps(info))
            except Exception:
                pass

    # ---- 连接与设备 -----------------------------------------------------
    def connect(self, baud=None, channel=None):
        """打开 CAN。baud/channel 不传就用设置页存值（见 set_link_config）。"""
        if self._ch is not None:
            return {"success": True, "modules": self._modules}
        try:
            baud = self._baud if baud is None else int(baud)
        except (TypeError, ValueError):
            baud = self._baud
        try:
            channel = self._channel if channel is None else int(channel)
        except (TypeError, ValueError):
            channel = self._channel
        errors = []
        ch = CanChannel(channel=channel,
                        on_snoop=self._on_bus_frame,
                        on_error=errors.append)
        if not ch.start(baud=baud):
            detail = "（%s）" % "；".join(errors) if errors else ""
            msg = ("CAN 打开失败%s。USBCAN2 同一时刻只允许一个程序打开设备，"
                   "请先确认没有另一个程序或 ZLG 测试工具正占着这块板卡，"
                   "确认没有再检查 USB 线是否插好，然后点重连。" % detail)
            self._log(msg)
            return {"success": False, "message": msg}
        self._ch = ch
        self._baud, self._channel = baud, channel
        self._jobq.put(lambda: self._ch.send(own_id(CMD_BUILD_TIME, BROADCAST),
                                             encode_build_time_query(0x3F))
                       if self._ch else None)
        self._log("CAN 已连接（USBCAN2 通道 %d，%d kbps）" % (channel, baud // 1000))
        self._jobq.put(self._do_reprobe)
        return {"success": True, "modules": self._modules}

    def reprobe(self):
        """投递后台探测任务；结果经推送下发前端。"""
        if self._ch is None:
            return {"success": False, "message": "CAN 未连接"}
        return self._submit(self._do_reprobe)

    def _do_reprobe(self):
        """广播一轮 0x20 状态查询（双形态各一帧），300ms 收集全部应答：
        BL 设备答 B1=0x02 进 _modules，App 设备答 B1=0x01 进 _app_info。

        单发即够（设备应答实测 <20ms；BL 广播应答按地址×10ms 错峰，300ms 窗口
        覆盖 0x1E 以内全部地址），第二拍只是丢帧冗余。"""
        if self._ch is None:
            return
        bl, apps = [], {}

        def pred(frame):
            return frame.xtd and own_cmd_of(frame.id) == CMD_PROBE

        with self._ch.subscribe(pred) as mb:
            for _ in range(2):
                self._ch.send(P.build_id(P.CMD_PROBE, P.BROADCAST), P.probe_request())
                self._ch.send(own_id(CMD_PROBE, BROADCAST),
                              encode_probe_query(BROADCAST))
                deadline = time.monotonic() + 0.15
                while True:
                    f = mb.get(deadline - time.monotonic())
                    if f is None:
                        break
                    d = decode_probe(bytes(f.data))
                    if d is None:
                        continue
                    if d.mode == INFO_MODE_ENTER_BL:
                        if d.addr not in bl:
                            bl.append(d.addr)
                    elif d.mode == INFO_MODE_APP:
                        apps[d.addr] = d
        bl.sort()
        self._modules = ["0x%02X" % a for a in bl]
        first_app = apps[sorted(apps)[0]] if apps else None
        self._dev_state = "bl" if bl else ("app" if apps else "timeout")
        self._app_info = ({"addr": "0x%02X" % first_app.addr,
                           "version": first_app.version_str} if first_app else None)
        if bl:
            self._log("Bootloader 探测: %s" % self._modules)
        if apps:
            self._log("在跑 App 的设备: %s"
                      % "、".join("0x%02X（版本 %s）" % (a.addr, a.version_str)
                                  for a in (apps[k] for k in sorted(apps))))
        if not bl and not apps:
            self._log("总线上无探测应答——设备未上电或未运行固件")

    def disconnect(self):
        # 窗口关闭、webview.start 返回、atexit 都会走到这里，只在真正关过设备时报一声。
        self._abort_pending()
        self._watch.close()
        self._push_stop.set()
        if self._ch is not None:
            self._ch.close()
            self._ch = None
            self._log("CAN 已断开")
        self._modules = []
        self._bl_info = None
        self._dev_state = None
        self._app_info = None
        return {"success": True}

    def _abort_pending(self):
        """叫醒一切还在等总线或等用户答复的后台流程，别让它们抱着已关闭的设备。"""
        self._cancel.set()
        self._confirm_evt.set()

    def get_info(self, addr):
        if self._ch is None:
            return {"success": False, "message": "CAN 未连接"}
        self._bl_info = None
        return self._submit(lambda: self._do_get_info(addr))

    def _do_get_info(self, addr):
        try:
            info = self._bl(addr).info()
        except BootloaderError as e:
            self._bl_info = None
            self._log("INFO 查询失败: %s" % e)
            return
        v = info["version"]
        self._log("Bootloader v%d.%d.%d，扇区 %d，参数区 SEC%d"
                  % (v[0], v[1], v[2], info["sector_nb"], info["param_sector"]))
        self._bl_info = {"version": "v%d.%d.%d" % v,
                         "sector_nb": info["sector_nb"],
                         "param_sector": info["param_sector"]}

    # ---- 目标地址 --------------------------------------------------------
    @staticmethod
    def _parse_target(addr):
        """把界面上的目标地址转成取值：None/空/0 = 广播 0x3F，0x01~0x3B 单播。

        0x3C~0x3E/0xFF 非法。返回 -1 表示非法（调用方拒绝下发）。地址随每条
        命令携带，不在后台存状态，避免与升级线程共享可变目标。"""
        if addr in (None, ""):
            return 0x3F
        if isinstance(addr, str):
            try:
                addr = int(addr.strip(), 16) if addr.strip().lower().startswith("0x") \
                    else int(addr.strip())
            except ValueError:
                return -1
        addr = int(addr)
        if not 0x01 <= addr <= 0x3B:
            return -1
        return addr

    # ---- 增量升级（独立流程能力，语义与门控见 upgrade/selective.py）----
    def _plan_selective(self, addr, new_octets):
        """增量升级规划：以设备 Flash 现存内容为基线挑出可跳过的块，全量升级
        就是规划出零保持的特例。基线通过 0x23 VERIFY 按块请求取得——该命令
        支持任意范围，Bootloader 对实际 Flash 内容算 CRC 后应答 ok/err；不依赖
        本机保存的任何镜像文件，版本号不参与判断（版本串仅用于界面显示）。
        探测覆盖目标区全部块（设备自报扇段数 ×2）：新固件长度之外的块按全
        0xFF 参与比对，旧镜像残尾同样被判差异并擦除。
        规划正确性由写后最终 VERIFY（对实际 Flash 内容全区算 CRC）保证：任何
        错误跳过的块都会被抓住并自动转全量重写。
        返回 (keep_blocks, erase_mask, reason)，reason 供门控退出时显示。"""
        if not selective.SELECTIVE_ENABLED:
            return None, None, "增量升级开关已关闭"
        bl = self._bl(addr)
        info = bl.info()
        if not selective.capability_ok(info["compress_flags"]):
            return None, None, "设备未上报增量升级能力"
        blk_nb = P.TARGET_BLOCK_NB             # 目标区总块数（SEC4~15 = 12 扇段 × 2）
        padded = new_octets.ljust(blk_nb * P.BLOCK_OCTETS, bytes([0xFF]))
        matches = []
        for k in range(blk_nb):
            chunk = padded[k * P.BLOCK_OCTETS:(k + 1) * P.BLOCK_OCTETS]
            try:
                bl.verify_range(k * (P.BLOCK_OCTETS // 2), P.BLOCK_OCTETS // 2,
                                P.crc32(chunk))
                matches.append(True)
            except BootloaderError:
                matches.append(False)
        keep, erase_mask = selective.plan(matches)
        self._log("增量升级: 与设备现存内容逐块比对，%d/%d 块相同跳过（差异扇段 %d/%d）"
                  % (len(keep), blk_nb, bin(erase_mask).count("1"),
                     (blk_nb + 1) // 2))
        return keep, erase_mask, ""

    # ---- 升级 -----------------------------------------------------------
    def start_upgrade(self, addr=None, timeout_s=PROBE_TIMEOUT_DEFAULT_S,
                      compress=True, auto=False):
        """开始升级：先探测设备在哪一侧，再决定要不要请求它进 Bootloader。

        addr 是界面上已选定的目标设备地址（"0x01" 或整数），没选就交给探测决定。
        compress 打开 LZ4 压缩传输（逐块决策，设备不支持时在烧写前明确报错）。
        auto=True 表示自动烧录触发：App 侧跳过人工确认（写日志留痕）。

        起线程之前强制从硬盘重载 _fw_path：保证烧的永远是最新编译产物，
        而不是上次点选文件时驻留内存的旧内容。"""
        if self._fw is None:
            return {"success": False, "message": "请先选择固件文件"}
        if self._ch is None:
            return {"success": False, "message": "请先连接 CAN"}
        if self._progress["running"]:
            return {"success": False, "message": "升级进行中"}
        target = self._parse_addr(addr)
        window = self._parse_window(timeout_s)
        if self._fw_path:
            r = self.load_file(self._fw_path)
            if not r.get("success"):
                msg = "固件重载失败: %s，请重新选择固件" % (r.get("message") or "未知原因")
                self._log("✕ " + msg)
                return {"success": False, "message": msg}
        self._cancel.clear()
        self._confirm_answer = None
        self._confirm_evt.clear()
        self._progress = self._new_progress()
        self._set_progress(running=True, stage="probe",
                           message="正在探测设备（超时 %g 秒）…" % window)
        self._thread = threading.Thread(target=self._upgrade_worker,
                                        args=(target, window, bool(compress),
                                              bool(auto)),
                                        name="fw-upgrade", daemon=True)
        self._thread.start()
        return {"success": True}

    @staticmethod
    def _parse_addr(addr):
        """把界面上的目标地址（"0x01" 或整数）转成 int；没选或选中广播返回 None。"""
        if isinstance(addr, str):
            try:
                addr = int(addr.strip() or "0", 16)
            except ValueError:
                return None
        return addr or None

    @staticmethod
    def _parse_window(timeout_s):
        """只接受界面上可选的 10 / 20 / 60 秒，其余取值按默认 20 秒处理。"""
        try:
            window = float(timeout_s)
        except (TypeError, ValueError):
            return PROBE_TIMEOUT_DEFAULT_S
        return window if window in PROBE_TIMEOUTS_S else PROBE_TIMEOUT_DEFAULT_S

    def confirm_upgrade(self, accept):
        """回答确认弹窗：accept 非 0 表示同意进入 Bootloader。答复投给后台线程。"""
        if self._progress.get("confirm") is None:
            return {"success": False, "message": "当前没有等待答复的确认"}
        self._confirm_answer = bool(accept) and not self._cancel.is_set()
        self._confirm_evt.set()
        return {"success": True}

    def _wait_user_ok(self, info):
        """把探测结论摆给用户等一句答复；不同意或没人答复就抛错退出。"""
        text = ("检测到设备在跑应用固件（版本 %s，地址 0x%02X）。继续升级会让设备"
                "复位并进入 Bootloader 停机等待烧写。是否继续？"
                % (info.version_str, info.addr))
        self._confirm_seq += 1
        self._confirm_answer = None
        self._confirm_evt.clear()
        self._set_progress(stage="confirm", message="等待确认：是否让设备进入升级模式",
                           confirm={"seq": self._confirm_seq,
                                    "addr": "0x%02X" % info.addr,
                                    "version": info.version_str, "text": text})
        answered = self._confirm_evt.wait(CONFIRM_TIMEOUT_S)
        self._set_progress(confirm=None)
        if self._cancel.is_set():
            raise UpgradeCancelled("已取消")
        if not answered:
            raise UpgradeCancelled("等待确认超过 %g 秒，已退出升级流程" % CONFIRM_TIMEOUT_S)
        if not self._confirm_answer:
            raise UpgradeCancelled("已取消：设备继续在跑当前固件，没有改动")

    def _request_enter_bl(self, addr):
        """单播升级请求（cmd 0x06）：设备直接跳进 Bootloader 并停在那里等烧写。"""
        self._log("→ 升级请求（cmd 0x06，单播 0x%02X）" % addr)
        try:
            payload = encode_enter_bl(addr)
        except ValueError as e:
            raise BootloaderError(str(e))
        if not self._ch.send(own_id(CMD_ENTER_BL, addr), payload):
            raise BootloaderError("升级请求发送失败（CAN 未就绪）")

    def _query_side(self, addr):
        """单播 0x20 状态查询：发一帧问设备在哪一侧（App/BL），200ms 等应答。

        addr=None 时退化为广播查询（dest 0x3F）：接受第一个应答的设备——
        升级页没预选地址时靠它定位唯一的在总线设备。
        返回 DeviceInfo（mode=0x01 App / 0x02 BL）或 None（无应答）。
        先订阅后发送：信箱在 send 之前注册，应答不可能被漏收或被别处消费。

        0x20 请求有两种载荷形态（与 DeviceProbe._send_tick 同）：自有格式 8 字节
        （B0=0x20 回显，App 认这个）；BL 格式 4 字节（P.probe_request()，BL 的
        帧长检查只认这个——用 8 字节格式问 BL 会吃 err=1 的 AA EE EE 错误应答）。
        不知道设备在哪一侧正是本查询的目的，所以两种形态各发一帧，
        App/BL 各自认自己的那帧应答。"""
        if self._ch is None:
            return None
        addr = self._parse_addr(addr)   # "0x01" 字符串 -> int 0x01
        dest = BROADCAST if addr is None else addr

        def pred(frame):
            return frame.xtd and own_cmd_of(frame.id) == CMD_PROBE

        with self._ch.subscribe(pred) as mb:
            self._ch.send(P.build_id(P.CMD_PROBE, dest), P.probe_request())
            self._ch.send(own_id(CMD_PROBE, dest), encode_probe_query(dest))
            deadline = time.monotonic() + 0.2   # 设备应答实测 <50ms，留余量
            while True:
                f = mb.get(deadline - time.monotonic())
                if f is None:
                    return None
                info = decode_probe(bytes(f.data))
                if info is not None and (addr is None or info.addr == addr):
                    return info

    def _acquire_bootloader(self, target_addr, window_s, auto=False):
        """探测设备在哪一侧，交回一个可烧写的 Bootloader 模块地址。

        auto=True（自动烧录）跳过 App 侧的人工确认：写日志留痕后直接发升级请求。"""
        # 奥卡姆剃刀：直接单播 0x20 查询（50ms 等应答），不走窗口期重发
        self._set_progress(stage="probe", message="正在探测设备…")
        info = self._query_side(target_addr)
        if info is None:
            raise BootloaderError(
                "设备无应答——未上电、不在总线上，或固件过旧不认识 0x20 状态查询。"
                "请确认设备已上电、CAN 接线 H/H 与 L/L 接对、波特率 125 kbps")
        if info.mode == INFO_MODE_ENTER_BL:  # 0x02 = 已在 Bootloader
            return info.addr
        if info.mode != INFO_MODE_APP:  # 0x01 = 在 App
            raise BootloaderError("设备应答了但侧标识异常（0x%02X）" % info.mode)
        if auto:
            self._log("自动烧录: 跳过人工确认（设备 0x%02X，App 版本 %s）"
                      % (info.addr, info.version_str))
        else:
            self._wait_user_ok(info)
        self._request_enter_bl(info.addr)
        # 进 BL 后确认：先等 0.6s 交接窗，再单播 0x20 复查是否已进 BL。
        # 交接的实测耗时有抖动，一次查询撞上空窗会误判失败：
        # 每 200ms 复查一次、最多 5 次（最坏 ≈1s 后报错），仍是单发单查。
        self._set_progress(stage="probe", message="设备正在进入 Bootloader…")
        time.sleep(0.6)
        info2 = None
        for _ in range(5):
            info2 = self._query_side(info.addr)
            if info2 is not None:
                break
            time.sleep(0.1)
        if info2 is None:
            raise BootloaderError("设备无应答——没有如期进入 Bootloader")
        if info2.mode == INFO_MODE_ENTER_BL:  # 0x02 = 已在 Bootloader
            return info2.addr
        raise BootloaderError(
            "升级请求已发出，但设备仍在 App（侧标识 0x%02X）——固件没按请求交接进 "
            "Bootloader" % info2.mode)

    def _upgrade_worker(self, target_addr, window_s, compress=False, auto=False):
        # 结束态由本函数统一落笔：流程各分支只管写 message/success，
        # running 一定在 finally 里复位，前端轮询不会看到永远在跑的流程。
        self._watch.stop_poll("升级开始")   # App 即将停机跳 BL，观察读事务先行消亡
        try:
            addr = self._acquire_bootloader(target_addr, window_s, auto=auto)
            chip = "0x%02X" % addr
            if chip not in self._modules:
                # 换一个新列表：轮询线程可能正在读旧的
                self._modules = self._modules + [chip]
            try:
                keep_blocks, erase_mask, skip_reason = self._plan_selective(
                    addr, self._fw.octets)
            except Exception as e:  # noqa: BLE001 - 增量规划失败不挡升级
                self._log("增量升级规划异常，转全量: %s" % e)
                keep_blocks, erase_mask, skip_reason = None, None, "规划异常"
            try:
                self._flash_module(addr, self._fw, compress=compress,
                                   keep_blocks=keep_blocks, erase_mask=erase_mask,
                                   skip_reason=skip_reason)
            except BootloaderError as e:
                if keep_blocks is None:
                    raise   # 全量本身失败，无退路
                # 增量路径失败（如设备对 C=0 保持块的校验与预期不符）：
                # 设备已在 Bootloader 且 App 区处于已擦/半写状态，直接补一轮
                # 全量擦写兜底——用户侧永远是一次成功的升级。
                self._log("增量路径失败（%s），自动转全量重试…" % str(e)[:80])
                self._set_progress(stage="erase", percent=2, message="转全量重试…",
                                   upgrade_mode="全量升级（增量路径失败转全量）")
                self._flash_module(addr, self._fw, compress=compress)
        except UpgradeCancelled as e:
            self._set_progress(success=False, cancelled=True, message=str(e))
            self._log("升级流程结束: %s" % e)
        except BootloaderError as e:
            self._set_progress(success=False, message=str(e))
            self._log("✕ 升级失败: %s" % e)
        except Exception as e:  # noqa: BLE001 - 工作线程不炸宿主
            self._set_progress(success=False, message="异常: %s" % e)
            self._log("✕ 升级异常: %s" % e)
        else:
            self._set_progress(success=True)
        finally:
            done = dict(self._progress)   # 先快照结束态，再复位 running（期间可能开新一轮）
            self._set_progress(running=False)
            self._watch.notify_firmware_updated()   # WATCH 已校验态推一次重校验提醒

    def _flash_module(self, addr, fw, progress_hook=None, compress=False,
                      keep_blocks=None, erase_mask=None, skip_reason=None):
        """对已在 Bootloader 的模块执行（能力查询→）擦除→写入→校验→跳转 App。

        progress_hook 缺省走 _set_progress（单机升级路径，行为不变）；批量升级
        传入回调，把各阶段 (stage/percent/blocks_done/blocks_total/message)
        写进批量进度里对应地址的设备条目。
        compress=True 时先发一帧 INFO 查传输编码能力位（放擦除之前，避免白擦）：
        不支持 LZ4 就明确报错，提示用户关开关或先升级 Bootloader。"""
        bl = self._bl(addr)
        progress = progress_hook if progress_hook is not None else self._set_progress
        t0 = time.monotonic()   # 烧写计时起点：进入擦写流程（不含探测/确认等待）

        if compress:
            info = bl.info()
            cap = info["compress_flags"]
            encoding = pick_encoding(info)
            if encoding == P.ENC_DIRECT:
                raise BootloaderError(
                    "该 Bootloader 不支持压缩传输（能力位 0x%02X）。"
                    "请在升级页关闭「压缩传输」后重试，或先升级 Bootloader" % cap)
            self._log("Bootloader %d.%d.%d 能力位 0x%02X：按 %s 编码传输"
                      % (info["version"][0], info["version"][1], info["version"][2],
                         cap, _ENCODING_CN[encoding]))
        else:
            encoding = P.ENC_DIRECT
            cap = 0
        if keep_blocks and (not compress or encoding != P.ENC_LZ4D
                            or not selective.capability_ok(cap)):
            # 门控的第二级：设备 INFO byte5 bit3。
            # 设备不置位时自动退全量（第一级开关在
            # selective.SELECTIVE_ENABLED，_plan_selective 已先行过滤）。
            self._log("增量升级前置不满足（需 LZ4D 编码 + 设备能力位），转全量")
            keep_blocks, erase_mask = None, None

        way = "增量升级" if keep_blocks is not None else "全量升级"   # 界面/日志显示的升级方式名
        if keep_blocks is not None:
            # 常驻显示：升级页「升级方式」行，探测规划后即出现
            nb_disp = (fw.size + P.BLOCK_OCTETS - 1) // P.BLOCK_OCTETS
            kept_disp = len([k for k in keep_blocks if k < nb_disp])
            umode = "增量升级（跳过 %d/%d 块）" % (kept_disp, nb_disp)
            if kept_disp == nb_disp:
                umode = "增量升级（与设备现存内容一致，0 擦 0 写）"
            progress(upgrade_mode=umode)
        else:
            umode = "全量升级" + ("（%s）" % skip_reason if skip_reason else "")
            progress(upgrade_mode=umode)
        if keep_blocks is not None and erase_mask == 0:
            # 全保持：全部块与设备现存内容一致，不擦不写，后续逐块 C=0 跳过
            self._log("%s: 全部块与设备现存内容一致，不擦不写" % way)
        elif keep_blocks is not None:
            # 增量路径：只擦差异扇段，其余扇段保留 Flash 原内容
            progress(stage="erase", percent=2, message="%s：擦除差异扇段…" % way)
            self._log("%s: 按掩码擦扇段（mask=0x%04X，%d 段）…" %
                      (way, erase_mask, bin(erase_mask).count("1")))
            bl.erase(mode=selective.SELECTIVE_ERASE_MODE, mask=erase_mask)
        else:
            progress(stage="erase", percent=2, message="%s：擦除 App 区…" % way)
            self._log("%s: 擦除 App 区…" % way)
            bl.erase()

        def on_block(done, total):
            if self._cancel.is_set():
                raise UpgradeCancelled("已取消")
            pct = 5 + int(85.0 * done / total)
            progress(stage="write", percent=pct,
                     blocks_done=done, blocks_total=total,
                     message="写入 %d/%d 块" % (done, total))

        total_blocks = (fw.size + P.BLOCK_OCTETS - 1) // P.BLOCK_OCTETS

        def on_retry(block_no, attempt, tries, kind):
            # 重发是协议在自愈，不静默：让用户在进度条上看见是哪一块、第几次、为什么，
            # 前端同时把该块格标成琥珀底（blocks_retry）。
            progress(
                stage="write", blocks_retry=block_no - 1,
                message="写入 %d/%d 块·第 %d 块重发（%s，第 %d/%d 次）"
                        % (block_no - 1, total_blocks, block_no,
                           "收到错误应答" if kind == "错误应答" else "等 ACK 超时",
                           attempt, tries))
        self._log("写入固件（%d octet%s）…" % (fw.size, "，LZ4 压缩传输" if compress else ""))
        nb, total = bl.write(fw.octets, progress=on_block, on_retry=on_retry,
                             encoding=encoding, keep_blocks=keep_blocks)

        progress(stage="verify", percent=95, message="校验 CRC32…")
        self._log("校验 CRC32…")
        _, crc = bl.verify(fw.octets)

        progress(stage="run", percent=100, message="跳转 App…")
        if not bl.run():
            raise BootloaderError("RUN 无应答：固件已写入但设备没有跳转，可点「运行 App」重试")
        elapsed = time.monotonic() - t0
        progress(message="%s：升级完成，烧写用时 %.1f 秒（%s，CRC32=0x%08X，%d 块/%d octet），已跳转 App"
                 % (way, elapsed, _ENCODING_CN[encoding], crc, nb, total))
        self._log("✓ 0x%02X %s完成，用时 %.1f 秒（%s），CRC32=0x%08X，已跳转 App"
                  % (addr, way, elapsed, _ENCODING_CN[encoding], crc))

    # ---- 批量升级（升级全部设备，逐台执行） ------------------------------
    _STAGE_CN = {"erase": "擦除", "write": "写入", "verify": "校验", "run": "运行"}

    def upgrade_all(self, timeout_s=PROBE_TIMEOUT_DEFAULT_S, compress=True,
                    auto=False):
        """批量升级：发现总线上全部设备，逐台请进 Bootloader，再按地址升序逐台烧写。

        auto 形参仅为与 start_upgrade 签名对齐而保留：批量路径没有挂起式确认
        （前端已有两遍确认弹窗），且自动烧录只针对单机，前端在自动烧录开启时
        会禁用本入口。"""
        if self._fw is None:
            return {"success": False, "message": "请先选择固件文件"}
        if self._ch is None:
            return {"success": False, "message": "请先连接 CAN"}
        if self._progress["running"]:
            return {"success": False, "message": "升级进行中"}
        window = self._parse_window(timeout_s)
        if self._fw_path:
            r = self.load_file(self._fw_path)
            if not r.get("success"):
                msg = "固件重载失败: %s，请重新选择固件" % (r.get("message") or "未知原因")
                self._log("✕ " + msg)
                return {"success": False, "message": msg}
        self._cancel.clear()
        self._confirm_answer = None
        self._confirm_evt.clear()
        self._progress = self._new_progress()
        self._upgrade_all = {"devices": [], "current": None}
        self._set_progress(running=True, stage="probe",
                           message="正在发现总线上的设备（超时 %g 秒）…" % window)
        self._thread = threading.Thread(target=self._upgrade_all_worker,
                                        args=(window, bool(compress)),
                                        name="fw-upgrade-all",
                                        daemon=True)
        self._thread.start()
        return {"success": True}

    def _discover_devices(self, window_s):
        """发现循环（不用 DeviceProbe.scan，它单命中即返回）：发现窗口内每
        150ms 同拍重发 BL 探测广播与 0x20 状态查询（0x3F 广播），收集全部应答者。

        返回 (bl_addrs, apps)：bl_addrs = 已停在 Bootloader 的设备地址（升序），
        apps = 应答 0x20 的 App 侧设备 {addr: DeviceInfo}（按地址去重，保留最后一帧）；
        取消返回 (None, None)。整个窗口共用一个信箱，订阅先于首次发送。"""
        bl, apps = set(), {}
        last_new = None    # 最近一次发现新地址的时刻；静默 DISCOVER_QUIET_S 后提前收手
        deadline = self._probe_clock() + window_s

        def pred(frame):
            if not frame.xtd:
                return False
            if own_cmd_of(frame.id) == CMD_PROBE:
                return True
            f = P.parse_id(frame.id)
            return (f["dev"] == P.DEV_MODULE and f["cmd"] == P.CMD_PROBE
                    and f["dest"] == P.HOST_ADDR)

        with self._ch.subscribe(pred) as mb:
            while self._probe_clock() < deadline:
                if self._cancel.is_set():
                    return None, None
                if last_new is not None and self._probe_clock() - last_new >= DISCOVER_QUIET_S:
                    break
                self._ch.send(P.build_id(P.CMD_PROBE, P.BROADCAST), P.probe_request())
                self._ch.send(own_id(CMD_PROBE, BROADCAST), encode_probe_query(0x3F))
                until = min(self._probe_clock() + PROBE_TICK_S, deadline)
                while True:
                    frame = mb.get(until - self._probe_clock())
                    if frame is None:
                        break
                    addr = bl_probe_addr(frame)
                    if addr is not None:
                        if addr not in bl:
                            bl.add(addr)
                            last_new = self._probe_clock()   # 只在真正的新地址时刷新静默计时
                        continue
                    if frame.xtd and own_cmd_of(frame.id) == CMD_PROBE:
                        info = decode_probe(bytes(frame.data))
                        if info is not None and info.mode == INFO_MODE_APP:
                            if info.addr not in apps:
                                last_new = self._probe_clock()
                            apps[info.addr] = info   # 已知地址的重复应答只刷新内容，不刷新静默
            return sorted(bl), apps

    def _batch_device(self, chip):
        return {"addr": chip, "state": "等待", "percent": 0, "message": ""}

    def _batch_update(self, chip, **kw):
        """批量升级的进度回调：把 (stage/percent/blocks_done/blocks_total/message)
        写进对应地址条目，同时镜像到全局进度条（stage 轴复用单机语义，
        前端步进条与块格照常动）。"""
        for d in self._upgrade_all["devices"]:
            if d["addr"] == chip:
                if "stage" in kw:
                    d["state"] = self._STAGE_CN.get(kw["stage"], d["state"])
                if "percent" in kw:
                    d["percent"] = kw["percent"]
                if "message" in kw:
                    d["message"] = kw["message"]
                break
        mirror = {k: v for k, v in kw.items()
                  if k in ("stage", "percent", "blocks_done", "blocks_total",
                           "blocks_retry", "message")}
        if "message" in mirror:
            mirror["message"] = "批量 %s：%s" % (chip, mirror["message"])
        self._set_progress(**mirror)

    def _batch_hook(self, chip):
        """给 _flash_module 的 progress_hook：把该台设备的阶段进度写进批量表。"""
        def hook(**kw):
            self._batch_update(chip, **kw)
        return hook

    def _upgrade_all_worker(self, window_s, compress=False):
        # 结束态由本函数统一落笔（与单机 _upgrade_worker 同约定）：
        # 流程各分支只写 message/success，running 一定在 finally 里复位。
        self._watch.stop_poll("升级开始")
        devices = self._upgrade_all["devices"]
        try:
            bl_addrs, apps = self._discover_devices(window_s)
            if bl_addrs is None:
                raise UpgradeCancelled("已取消")
            if not bl_addrs and not apps:
                raise BootloaderError(
                    "%g 秒内没有发现任何设备：没有 Bootloader 探测应答，也没有状态查询应答。"
                    "请确认设备已上电、CAN 接线 H/H 与 L/L 接对、波特率 125 kbps" % window_s)
            for a in bl_addrs:
                d = self._batch_device("0x%02X" % a)
                d["message"] = "已在 Bootloader"
                devices.append(d)
            for a in sorted(apps):
                devices.append(self._batch_device("0x%02X" % a))
            self._log("批量升级发现 %d 台设备（BL %s / App %s）"
                      % (len(devices),
                         "、".join("0x%02X" % a for a in bl_addrs) or "无",
                         "、".join("0x%02X" % a for a in sorted(apps)) or "无"))
            # 阶段一：App 设备按地址升序逐台单播 0x06，等它进 Bootloader
            for a in sorted(apps):
                chip = "0x%02X" % a
                if self._cancel.is_set():
                    break
                self._set_progress(stage="probe",
                                   message="批量升级：请求 %s 进入 Bootloader…" % chip)
                self._request_enter_bl(a)
                res = DeviceProbe(self._ch, expect_addr=a,
                                  clock=self._probe_clock).scan(
                    window_s, decide_on_app=False, abort=self._cancel.is_set)
                if res.state == "cancelled":
                    break
                entry = next(d for d in devices if d["addr"] == chip)
                if res.state == "bl":
                    entry["message"] = "已进入 Bootloader"
                else:
                    entry["state"] = "进入 Bootloader 失败"
                    entry["message"] = ("窗口内没有等到 %s 的 Bootloader 应答" % chip
                                        if res.state != "timeout" else
                                        no_device_hint(res, window_s, self._ch.rx_count))
                    self._log("✕ %s 进入 Bootloader 失败，跳过该台继续" % chip)
            # 阶段二：全部就绪的设备按地址升序逐台烧写
            ready = [d for d in devices if d["state"] == "等待"]
            ready.sort(key=lambda d: int(d["addr"], 16))
            done = 0
            for d in ready:
                chip = d["addr"]
                if self._cancel.is_set():
                    d["state"] = "已跳过（已取消）"
                    continue
                self._upgrade_all["current"] = chip
                d["state"] = "擦除"
                try:
                    self._flash_module(int(chip, 16), self._fw,
                                       progress_hook=self._batch_hook(chip),
                                       compress=compress)
                except UpgradeCancelled as e:
                    d["state"] = "失败"
                    d["message"] = str(e)
                    self._log("✕ %s 升级中止: %s" % (chip, e))
                    break
                except BootloaderError as e:
                    d["state"] = "失败"
                    d["message"] = str(e)
                    self._log("✕ %s 升级失败: %s" % (chip, e))
                except Exception as e:  # noqa: BLE001 - 单台异常不拖垮其余设备
                    d["state"] = "失败"
                    d["message"] = "异常: %s" % e
                    self._log("✕ %s 升级异常: %s" % (chip, e))
                else:
                    d["state"] = "完成"
                    d["percent"] = 100
                    done += 1
            if self._cancel.is_set():
                for d in devices:      # 中断后没轮到的设备统一标跳过
                    if d["state"] == "等待":
                        d["state"] = "已跳过（已取消）"
            self._upgrade_all["current"] = None
            failed = [d for d in devices
                      if d["state"] in ("失败", "进入 Bootloader 失败")]
            if self._cancel.is_set():
                msg = "批量升级已取消（完成 %d/%d 台）" % (done, len(ready))
                self._set_progress(success=False, cancelled=True, message=msg)
                self._log("批量升级结束: %s" % msg)
            else:
                msg = "批量升级完成 %d/%d 台" % (done, len(ready))
                if failed:
                    msg += "，失败 %d 台（%s）" % (len(failed),
                                                  "、".join(d["addr"] for d in failed))
                self._set_progress(success=not failed, message=msg)
                self._log(("✓ " if not failed else "✕ ") + msg)
                if done:
                    self._watch.notify_firmware_updated()   # WATCH 已校验态推一次重校验提醒
        except UpgradeCancelled as e:
            self._upgrade_all["current"] = None
            self._set_progress(success=False, cancelled=True, message=str(e))
            self._log("批量升级结束: %s" % e)
        except BootloaderError as e:
            self._upgrade_all["current"] = None
            self._set_progress(success=False, message=str(e))
            self._log("✕ 批量升级失败: %s" % e)
        except Exception as e:  # noqa: BLE001 - 工作线程不炸宿主
            self._upgrade_all["current"] = None
            self._set_progress(success=False, message="异常: %s" % e)
            self._log("✕ 批量升级异常: %s" % e)
        finally:
            self._set_progress(running=False)

    def run_app(self, addr):
        if self._ch is None:
            return {"success": False, "message": "CAN 未连接"}
        return self._submit(lambda: self._do_run_app(addr))

    def _do_run_app(self, addr):
        try:
            ok = self._bl(addr).run()
        except BootloaderError as e:
            self._log("✕ 运行 App 失败: %s" % e)
            return
        if not ok:
            self._log("✕ 运行 App 无应答")
            return
        self._log("已跳转 App")

    def cancel(self):
        """中止升级：正在探测或等用户答复时立刻结束，正在写入时按块边界停下。"""
        self._abort_pending()
        return {"success": True}

    # ---- 变量观察（WATCH 页）：解析快/慢操作分离，CAN 往返全部走 _jobq -------
    def watch_pick_file(self, initial_dir=None):
        """选择符号文件。initial_dir = 上次选择的文件夹（前端 localStorage 记忆，
        对话框初始目录）；完整路径随应答回传，供观察会话持久化与启动恢复。"""
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            directory=initial_dir if initial_dir and os.path.isdir(initial_dir) else "",
            file_types=("符号文件 (*.out;*.elf)", "所有文件 (*.*)"))
        if not result:
            return {"success": False, "message": "已取消"}
        path = result[0]
        r = self._watch.load_file(path)
        if r.get("success"):
            r["dir"] = os.path.dirname(path)
        return r

    def watch_load_file(self, path):
        return self._watch.load_file(path)

    def watch_connect(self, addr):
        return self._watch.connect(self._parse_target(addr))

    def watch_disconnect(self):
        self._watch.disconnect()
        return {"success": True}

    def watch_add(self, name):
        return self._watch.add(name)

    def watch_remove(self, rowid):
        return self._watch.remove(rowid)

    def watch_clear(self):
        return self._watch.clear()

    def watch_set_options(self, poll_ms=None, merge=None, gap=None, snapshot=None):
        return self._watch.set_options(poll_ms, merge, gap, snapshot)

    def watch_start(self):
        return self._watch.start_poll()

    def watch_stop(self):
        return self._watch.stop_poll(None)

    def watch_write(self, rowid, value):
        return self._watch.write(rowid, value)

    def watch_status(self):
        return self._watch.snapshot()

    def watch_table(self):
        """观察表整表行数据：页面刷新后快照只有值补丁，前端用它重建表格。"""
        return {"success": True, "table": self._watch.table_rows()}

    def watch_wave_pins(self, rowids):
        return self._watch.wave_pins(rowids)

    def watch_wave_data(self):
        return self._watch.wave_data()

    def watch_wave_clear(self):
        return self._watch.wave_clear()

    def watch_wave_export(self, text, default_name="watch_wave.csv"):
        """波形数据 CSV 导出：保存对话框 + 写文件。"""
        result = self._window.create_file_dialog(
            webview.SAVE_DIALOG, directory="", save_filename=default_name)
        if not result:
            return {"success": False, "message": "已取消"}
        path = result if isinstance(result, str) else result[0]
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        except OSError as e:
            return {"success": False, "message": "写入失败: %s" % e}
        self._log("波形数据已导出: %s" % path)
        return {"success": True, "path": path}

    # ---- 状态与轮询 -------------------------------------------------------
    def get_status(self):
        s = {"connected": self._ch is not None,
             "modules": list(self._modules),
             "dev_state": self._dev_state,
             "app_info": self._app_info,
             "app_version": APP_VERSION,
             "baud": self._baud,
             "channel": self._channel,
             "bl_info": self._bl_info,
             "build_time": (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(self._build_time))
                            if self._build_time is not None else None),
             "app_build_time": (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(self._app_build_time))
                                if self._app_build_time is not None else None)}
        if self._ch is not None:
            s["rx_total"] = self._ch.rx_count
            s["tx_ok"] = self._ch.tx_ok
            s["tx_fail"] = self._ch.tx_fail
        else:
            s["rx_total"] = s["tx_ok"] = s["tx_fail"] = 0
        return s

    def get_progress(self):
        p = dict(self._progress)
        p["logs"] = list(self._logs)
        if self._upgrade_all is not None:
            # 浅拷贝给轮询线程：后台线程还在原地更新设备条目，序列化期间不撕裂
            p["upgrade_all"] = {"devices": [dict(d) for d in self._upgrade_all["devices"]],
                                "current": self._upgrade_all["current"]}
        return p


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m monitor_tui.host_app",
        description="CAN Companion（pywebview GUI）")
    parser.add_argument("--parent-pid", type=int, default=None,
                        help="给定时，该进程消失后本程序自动关闭 CAN 并退出；"
                             "传调用方自身的 PID（自动化拉起用）")
    parser.add_argument("--version", action="version",
                        version="CAN Companion " + APP_VERSION)
    args = parser.parse_args(argv)

    if not acquire_single_instance():
        msg = ("上位机已经在运行了，直接用那个窗口；如果找不到窗口，"
               "用任务管理器结束程序进程后重试。")
        print(msg, flush=True)
        # 打包版禁用了控制台, print 无人可见, 补一个弹窗告知。
        if getattr(sys, "frozen", False):
            ctypes.windll.user32.MessageBoxW(None, msg, "CAN Companion", 0x40)
        return EXIT_ALREADY_RUNNING

    api = HostAPI()
    atexit.register(api.disconnect)
    if args.parent_pid:
        watch_pid = resolve_parent_watch_target(args.parent_pid)
        if watch_pid is None:
            print("无法确定 --parent-pid 要盯的进程，看门狗未启用。", flush=True)
        else:
            threading.Thread(target=_watch_parent, args=(api, watch_pid),
                             name="parent-watch", daemon=True).start()

    static_dir = os.path.join(os.path.dirname(__file__), "static_host")
    # 每次启动把 index 副本里的 ?v= 全部改写成启动时间戳（_index_live.html），浏览器
    # 缓存永远命不中旧资源——手改 ?v= 的纪律漏过一次，整批 UI 改动就全部不可见。
    stamp = time.strftime("%Y%m%d%H%M%S")
    with open(os.path.join(static_dir, "index.html"), encoding="utf-8") as f:
        live_html = re.sub(r"\?v=\d+", "?v=" + stamp, f.read())
    live_path = os.path.join(static_dir, "_index_live.html")
    with open(live_path, "w", encoding="utf-8") as f:
        f.write(live_html)
    window = webview.create_window(
        title="CAN Companion " + APP_VERSION,
        url=live_path,
        js_api=api,
        width=1280, height=860,
        resizable=True,
        background_color="#EEF1F6")
    api._window = window
    api.start_push()   # 后端推送：状态变化 ≤50ms 推给前端（前端不再轮询状态）

    def on_closed():
        api.disconnect()
    window.events.closed += on_closed

    dbg = os.environ.get("CAN_COMPANION_DEBUG_PORT")
    if dbg:
        webview.settings["REMOTE_DEBUGGING_PORT"] = int(dbg)
    try:
        # private_mode 默认 True（WebView2 全内存，localStorage 不落盘），
        # 用户习惯（地址/目标/周期）全靠 localStorage，必须关掉隐私模式才能跨启动保留
        webview.start(debug=bool(dbg), private_mode=False)
    finally:
        api.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
