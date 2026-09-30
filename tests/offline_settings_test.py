# -*- coding: utf-8 -*-
"""设置页与自动烧录的离线测试：固件重载、连接配置、看门狗触发。全程不碰设备。

运行（monitor_tui 仓库根）：
    .venv\\Scripts\\python.exe tests\\offline_settings_test.py
退出码 0 = 全部通过。

复用 offline_upgrade_test 的 FakeChannel/FakeModule 替身与 make_api 工厂；
固件用临时 Intel HEX（C28x 字地址），改硬盘内容即可验证「烧前强制重载」。
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import monitor_tui.host_app as HA  # noqa: E402
from monitor_tui.canbus import BAUD_TIMING, UsbCan2Bus  # noqa: E402
from monitor_tui.host_app import HostAPI  # noqa: E402
from monitor_tui.upgrade import bl_protocol as P  # noqa: E402
from monitor_tui.upgrade.can_channel import CanChannel  # noqa: E402

from offline_upgrade_test import (RESULTS, FakeChannel, FakeModule, check,  # noqa: E402
                                  make_api, wait_until)


# ------------------------------------------------------------------ 工具
def hex_record(count, addr, rectype, data):
    rec = bytes((count, (addr >> 8) & 0xFF, addr & 0xFF, rectype)) + data
    return ":" + rec.hex().upper() + "%02X" % P.sum8(rec)


def write_hex(path, octets):
    """把 4 字节 octet 内容写成最小 Intel HEX（字地址 0x10 起，--order=MS）。"""
    assert len(octets) % 2 == 0
    ms = bytearray()
    for i in range(0, len(octets), 2):     # 解析端把 [高,低] 交换回小端
        ms.append(octets[i + 1])
        ms.append(octets[i])
    with open(path, "w") as f:
        f.write(hex_record(2, 0x0000, 0x04, b"\x00\x08") + "\n")
        f.write(hex_record(len(ms), 0x4000, 0x00, bytes(ms)) + "\n")
        f.write(hex_record(0, 0, 0x01, b"") + "\n")


def touch(path, serial):
    """改 mtime（ns 单调递增）模拟编译器重写产物；serial 保证每次快照必不同。"""
    t = time.time_ns() + serial * 10 ** 9
    os.utime(path, ns=(t, t))


class SettingsAPI(HostAPI):
    """connect/disconnect 换成替身：set_link_config 的断开重连可以离线跑。"""

    def __init__(self):
        super().__init__()
        self.connect_calls = []
        self.disconnect_count = 0

    def connect(self, baud=None, channel=None):
        self.connect_calls.append((baud, channel))
        self._ch = FakeChannel()   # 只表示"已连接"，替身不碰任何硬件
        return {"success": True, "modules": []}

    def disconnect(self):
        self.disconnect_count += 1
        self._ch = None
        return {"success": True}


# ------------------------------------------------------------------ a. 重载
def test_reload_before_upgrade():
    print("a) load_file 记忆路径；start_upgrade 起线程前强制从硬盘重载")
    tmp = tempfile.mkdtemp(prefix="cc_set_")
    try:
        path = os.path.join(tmp, "app.hex")
        write_hex(path, b"\x11\x22\x33\x44")
        dev = FakeModule(addr=0x01, in_bootloader=True)
        api = make_api(dev)
        stale = api._fw          # make_api 塞的占位固件，升级时应被重载顶掉
        r = api.load_file(path)
        check("load_file 成功", r["success"], str(r))
        check("记忆了完整路径", api._fw_path == path, repr(api._fw_path))
        check("解析出所写内容", api._fw.octets == b"\x11\x22\x33\x44",
              api._fw.octets.hex())
        check("基址 = 0x108000（App 入口）", api._fw.base_addr == 0x108000,
              hex(api._fw.base_addr))

        # 硬盘内容变了：升级必须拿到新内容（重载发生在起线程之前，返回即可断言）
        write_hex(path, b"\xAA\xBB\xCC\xDD")
        r2 = api.start_upgrade("0x01", 20, False)
        check("start_upgrade 受理", r2["success"], str(r2))
        check("重载顶掉了旧固件对象", api._fw is not stale)
        check("烧的是硬盘上的新内容", api._fw.octets == b"\xAA\xBB\xCC\xDD",
              api._fw.octets.hex() if api._fw else "None")
        done = wait_until(lambda: not api.get_progress()["running"], 60.0)
        p = api.get_progress()
        check("升级全链路走完", done and p["success"] is True, str(p))
        check("4 octet 恰好 1 块", p["blocks_total"] == 1 and dev.blocks == 1,
              "%d/%d dev=%d" % (p["blocks_done"], p["blocks_total"], dev.blocks))
        check("完成消息带新内容 CRC32",
              "CRC32=0x%08X" % P.crc32(b"\xAA\xBB\xCC\xDD") in p["message"],
              p["message"])

        # restore_firmware：文件在 → 重载并附 restored 标记
        r3 = api.restore_firmware(path)
        check("restore_firmware 成功", r3["success"] and r3.get("restored") is True,
              str(r3))
        check("restore 后路径仍记忆", api._fw_path == path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ b. 缺失
def test_reload_missing_file():
    print("b) 固件文件被移走：升级拒绝并给出明确文案；restore 静默报 missing")
    tmp = tempfile.mkdtemp(prefix="cc_set_")
    try:
        path = os.path.join(tmp, "app.hex")
        write_hex(path, b"\x01\x02\x03\x04")
        api = make_api()         # 无设备：本组不应有任何总线动作
        check("先正常加载一次", api.load_file(path)["success"])
        os.remove(path)
        r = api.start_upgrade("0x01", 20, False)
        check("升级被拒绝", r["success"] is False, str(r))
        check("文案指明重载失败", "固件重载失败" in r["message"], r["message"])
        check("文案提示重新选择固件", "重新选择固件" in r["message"], r["message"])
        check("失败时路径记忆清空", api._fw_path is None, repr(api._fw_path))
        check("流程没有启动", not api.get_progress()["running"])
        logs_before = len(api._logs)   # start_upgrade 自己的报错已落账，从这里起算静默
        rr = api.restore_firmware(path)
        check("restore 报 missing", rr["success"] is False and rr.get("missing") is True,
              str(rr))
        check("missing 不写日志（静默）", len(api._logs) == logs_before,
              str(api._logs[logs_before:]))
        # 文件回来了 → restore 成功
        write_hex(path, b"\x01\x02\x03\x04")
        rr2 = api.restore_firmware(path)
        check("文件回来后 restore 成功", rr2["success"] and rr2.get("restored") is True,
              str(rr2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ c. 连接配置
def test_link_config():
    print("c) set_link_config：校验、断开重连；UsbCan2Bus 非法波特率不碰硬件")
    check("波特率表 125k 保持现值", BAUD_TIMING[125000] == (0x03, 0x1C),
          repr(BAUD_TIMING.get(125000)))
    check("波特率表四挡齐全",
          set(BAUD_TIMING) == {125000, 250000, 500000, 1000000},
          repr(sorted(BAUD_TIMING)))
    api = SettingsAPI()
    r = api.set_link_config(9600, 0)
    check("非法波特率拒绝", r["success"] is False and api._baud == 125000, str(r))
    r = api.set_link_config(250000, 2)
    check("非法通道拒绝", r["success"] is False and api._channel == 0, str(r))
    r = api.set_link_config("abc", 0)
    check("非数字波特率拒绝", r["success"] is False, str(r))
    r = api.set_link_config(250000, 1)
    check("未连接时只存值不重连",
          r["success"] and r["reconnected"] is False
          and api._baud == 250000 and api._channel == 1,
          str(r))
    check("未连接时没碰 connect/disconnect",
          api.connect_calls == [] and api.disconnect_count == 0)
    api.connect()            # 手动连上（替身），模拟"已连接"状态
    r = api.set_link_config(500000, 0)
    check("已连接时断开重连", r["success"] and r["reconnected"] is True, str(r))
    check("断开恰一次、重连走存值",
          api.disconnect_count == 1 and api.connect_calls[-1] == (None, None)
          and len(api.connect_calls) == 2,
          "%d %r" % (api.disconnect_count, api.connect_calls))
    m = api.get_status()
    check("get_status 带回 baud/channel", m["baud"] == 500000 and m["channel"] == 0,
          "%r %r" % (m.get("baud"), m.get("channel")))
    api._set_progress(running=True)
    r = api.set_link_config(125000, 0)
    check("升级进行中拒绝改配置",
          r["success"] is False and "升级进行中" in r["message"]
          and api._baud == 500000, str(r))
    api._set_progress(running=False)

    errs = []
    bus = UsbCan2Bus(on_error=errs.append)
    check("UsbCan2Bus.start 非法波特率返回 False", bus.start(baud=9600) is False)
    check("报错走了 on_error 且点明数值", errs and "9600" in errs[0], str(errs))
    check("校验在 LoadLibrary 之前，没碰驱动", bus._dll is None)
    ch = CanChannel(on_error=errs.append)
    check("CanChannel.start 透传 baud（非法同样拒绝）", ch.start(baud=12345) is False)


# ------------------------------------------------------------------ d. 自动烧录
def test_auto_flash_watchdog():
    print("d) set_auto_flash：看门狗触发自动升级（跳过确认）；进行中/未连接跳过本轮")
    old_poll = HA.AUTOFLASH_POLL_S
    HA.AUTOFLASH_POLL_S = 0.05     # 注入快轮询；稳定等待 0.4s 保持真实值
    tmp = tempfile.mkdtemp(prefix="cc_set_")
    try:
        path = os.path.join(tmp, "app.hex")
        write_hex(path, b"\x11\x11\x22\x22")
        dev = FakeModule(addr=0x02, version=(1, 0, 0))   # 在跑 App
        api = make_api(dev)
        check("先加载固件", api.load_file(path)["success"])
        check("非法地址被拒", api.set_auto_flash(True, "zz")["success"] is False)
        r = api.set_auto_flash(True, "0x02", compress=True)
        check("开关受理且解析出单播目标",
              r["success"] and api._auto_flash_addr == 0x02, str(r))
        th = api._af_thread
        check("看门狗线程起来了", th is not None and th.is_alive(), repr(th))
        check("线程名 fw-watch", th is not None and th.name == "fw-watch")

        # 触发轮 1：升级"进行中"→ 只记日志并跳过，不动设备
        api._set_progress(running=True)
        touch(path, 1)
        check("进行中跳过本轮并留日志",
              wait_until(lambda: any("跳过本轮" in l for l in api._logs), 5.0),
              str(api._logs[-3:]))
        check("设备没被动过", dev.blocks == 0 and not dev.in_bootloader)
        api._set_progress(running=False)

        # 触发轮 2：自动升级全链路 App→BL，auto 跳过人工确认
        touch(path, 2)
        ok = wait_until(lambda: api.get_progress()["success"] is True, 60.0)
        p = api.get_progress()
        check("自动升级全链路成功", ok and p["success"] is True, str(p))
        check("模块收到 1 块写入", dev.blocks == 1, str(dev.blocks))
        check("日志写明跳过人工确认",
              any("跳过人工确认" in l for l in api._logs),
              str([l for l in api._logs if "自动" in l][-3:]))
        check("全程没有挂起确认弹窗", p["confirm"] is None, str(p.get("confirm")))

        # 触发轮 3：CAN 未连接 → 跳过本轮
        api._ch = None
        touch(path, 3)
        check("未连接也跳过本轮",
              wait_until(lambda: sum("跳过本轮" in l for l in api._logs) >= 2, 5.0),
              str(api._logs[-3:]))
        api._ch = FakeChannel()

        r = api.set_auto_flash(False, None)
        check("关闭受理", r["success"] and api._auto_flash is False, str(r))
        check("看门狗线程退出",
              wait_until(lambda: api._af_thread is None
                         or not api._af_thread.is_alive(), 3.0))
        # 稳定等待防抖：连续两次写文件只应触发一次（第二次 mtime 在稳定窗内仍在变）
    finally:
        HA.AUTOFLASH_POLL_S = old_poll
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=" * 64)
    print("设置页与自动烧录离线测试（不加载 ControlCAN.dll、不打开任何设备）")
    print("=" * 64)
    test_reload_before_upgrade()
    test_reload_missing_file()
    test_link_config()
    test_auto_flash_watchdog()
    fails = [r for r in RESULTS if not r[1]]
    print("-" * 64)
    print("合计 %d 项，通过 %d，失败 %d"
          % (len(RESULTS), len(RESULTS) - len(fails), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
