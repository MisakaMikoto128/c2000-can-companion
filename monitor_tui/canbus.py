"""CAN 总线层: USBCAN2（ZLG VCI / ControlCAN.dll）实机驱动。

I/O 模型（厂商文档实证）：VCI_Receive 的 WaitTime 是保留参数、永不阻塞
（《接口函数库使用说明书》2.2.10），所以收发同线程——单个 I/O 线程独占 DLL：
发送方只把帧存进发送队列（_txq），I/O 线程排空队列发帧、再非阻塞收帧分发；
调用线程的 send()/send_many() 保留同步外观（入队后等完成事件拿结果）。
所有帧都经 on_frame 回调（运行在 I/O 线程）上报。
"""
from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import time
from typing import Optional

from monitor_tui.protocol import CanFrame

DLL_PATH_DEFAULT = r"C:\ZLG\ControlCAN.dll"   # 常见安装位置兜底


def resolve_dll_path(explicit: Optional[str] = None) -> str:
    """ControlCAN.dll 查找顺序: 调用方指定 > 发布包自带(exe/包目录旁) > 开发机默认路径。

    发布目录里 ControlCAN.dll 与 exe 同层; 源码运行时 exe 是 venv 解释器,
    命不中, 落回 DLL_PATH_DEFAULT (常见安装位置)。
    """
    if explicit:
        return explicit
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "ControlCAN.dll"),
        os.path.join(here, "ControlCAN.dll"),
        os.path.join(os.path.dirname(here), "ControlCAN.dll"),
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return DLL_PATH_DEFAULT

VCI_USBCAN2 = 4
STATUS_OK = 1
# send_many 连续这么多次一帧都没排进设备缓冲就放弃（每次间隔 retry_wait_s）
SEND_MANY_IDLE_TRIES = 200

# 125 kbps（对照固件 HDL_CAN.h CAN_BAUD_KHZ=125, 与 can_test.py 同一组时序参数）
TIMING0_125K = 0x03
TIMING1_125K = 0x1C

# 波特率 → (TIMING0, TIMING1)：ZLG USBCAN2 标准 BTR 值。125k 保持与固件一致的现值；
# 其余挡位为将来下位机改波特率后上位机对频预留（设置页切换）。
BAUD_TIMING = {125000: (TIMING0_125K, TIMING1_125K),
               250000: (0x01, 0x1C),
               500000: (0x00, 0x1C),
               1000000: (0x00, 0x14)}


class VCI_INIT_CONFIG(ctypes.Structure):
    _fields_ = [("AccCode", ctypes.c_uint),
                ("AccMask", ctypes.c_uint),
                ("Reserved", ctypes.c_uint),
                ("Filter", ctypes.c_ubyte),
                ("Timing0", ctypes.c_ubyte),
                ("Timing1", ctypes.c_ubyte),
                ("Mode", ctypes.c_ubyte)]


class VCI_CAN_OBJ(ctypes.Structure):
    _fields_ = [("ID", ctypes.c_uint),
                ("TimeStamp", ctypes.c_uint),
                ("TimeFlag", ctypes.c_ubyte),
                ("SendType", ctypes.c_ubyte),
                ("RemoteFlag", ctypes.c_ubyte),
                ("ExternFlag", ctypes.c_ubyte),
                ("DataLen", ctypes.c_ubyte),
                ("Data", ctypes.c_ubyte * 8),
                ("Reserved", ctypes.c_ubyte * 3)]


class UsbCan2Bus:
    """USBCAN2 通道包装：start() 打开设备并起 I/O 线程（收发同线程），close() 关闭。

    send()/send_many() 对调用线程是同步外观：帧进 _txq 队列，I/O 线程取出后发上
    总线，调用方等完成事件拿到实际发送结果。DLL 只被 I/O 线程触碰。
    """

    # I/O 线程空闲拍：队列空且总线无帧时才睡这么久（收发延迟上限即此值）
    IO_IDLE_S = 0.001
    # 调用方等 I/O 线程完成发送的上限（线程死了/设备掉了时别让调用方挂死）
    TX_WAIT_S = 2.0

    def __init__(self, dll_path: Optional[str] = None, channel: int = 0,
                 on_frame: Optional[Callable[[CanFrame], None]] = None,
                 on_error: Optional[Callable[[str], None]] = None) -> None:
        self.dll_path = resolve_dll_path(dll_path)
        self.channel = channel
        self.on_frame = on_frame
        self.on_error = on_error
        self.tx_ok = 0
        self.tx_fail = 0
        self.rx_count = 0
        self._dll: Optional[ctypes.CDLL] = None
        self._rx_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._rx_n = 2500
        self._rx_buf = (VCI_CAN_OBJ * self._rx_n)()
        self._rx_ptr = ctypes.cast(self._rx_buf, ctypes.POINTER(VCI_CAN_OBJ))
        # 发送队列：元素 = (帧列表, retry_wait_s, 完成事件, 结果盒)
        self._txq: "queue.Queue" = queue.Queue()

    # ---- 生命周期 -------------------------------------------------------
    def start(self, baud: Optional[int] = None) -> bool:
        """打开设备并起收帧线程；baud 缺省 125 kbps，非法值走 on_error 并返回 False。

        波特率校验放在 LoadLibrary 之前：选错挡位不必碰硬件就能当场报错。"""
        baud = 125000 if baud is None else int(baud)
        timing = BAUD_TIMING.get(baud)
        if timing is None:
            self._err("不支持的波特率 %d（可选 %s）"
                      % (baud, "、".join("%dk" % (b // 1000)
                                         for b in sorted(BAUD_TIMING))))
            return False
        timing0, timing1 = timing
        try:
            self._dll = ctypes.windll.LoadLibrary(self.dll_path)
        except OSError as exc:
            self._err("驱动文件 %s 加载失败（文件不存在或依赖缺失）: %s" % (self.dll_path, exc))
            return False
        # VCI_OpenDevice 的第二参数是设备索引（0=第一台 USBCAN2），不是通道号；
        # 通道号只在 VCI_InitCAN/VCI_StartCAN 里用。设备索引固定 0（单台设备）。
        ret = self._dll.VCI_OpenDevice(VCI_USBCAN2, 0, 0)
        if ret != STATUS_OK:
            # ControlCAN.dll 对"设备没插"和"已被别的程序独占打开"给出同一个失败码，
            # 这一层区分不了两种原因，只能把返回码报出去由上层给排查建议。
            self._dll = None
            self._err("VCI_OpenDevice 失败（返回码 %d）" % ret)
            return False
        cfg = VCI_INIT_CONFIG(0x00000000, 0xFFFFFFFF, 0, 0,
                              timing0, timing1, 0)  # AccMask 全 1 = 全收
        if self._dll.VCI_InitCAN(VCI_USBCAN2, 0, self.channel, ctypes.byref(cfg)) != STATUS_OK:
            self._close_device()
            self._err("VCI_InitCAN 失败（通道参数或设备固件异常）")
            return False
        if self._dll.VCI_StartCAN(VCI_USBCAN2, 0, self.channel) != STATUS_OK:
            self._close_device()
            self._err("VCI_StartCAN 失败")
            return False
        self._stop.clear()
        self._rx_thread = threading.Thread(target=self._io_loop, name="usbcan2-io", daemon=True)
        self._rx_thread.start()
        return True

    def _close_device(self) -> None:
        if self._dll is not None:
            self._dll.VCI_CloseDevice(VCI_USBCAN2, 0)   # 设备索引 0，与 OpenDevice 对应
            self._dll = None

    def close(self) -> None:
        self._stop.set()
        if self._rx_thread is not None:
            self._rx_thread.join(timeout=1.0)
            self._rx_thread = None
        # 积压的发送项全部标失败放行，调用方不挂死
        while True:
            try:
                _, _, done, res = self._txq.get_nowait()
            except queue.Empty:
                break
            res.append(0)
            done.set()
        self._close_device()

    @property
    def running(self) -> bool:
        return self._rx_thread is not None and self._rx_thread.is_alive()

    # ---- I/O 线程：收发同线程，独占 DLL -----------------------------------
    def _io_loop(self) -> None:
        dll = self._dll  # 本地引用：close() 把 self._dll 置 None 时本线程可能还没退出
        while not self._stop.is_set():
            busy = False
            # 1) 排空发送队列：一个队列项（含 513 帧的大帧）整块发完再取下一条，
            #    结构上没有别的帧能插进大帧中间。仲裁/失败由适配器自动重发
            #    （SendType=0），主机侧不需要帧间隔纪律
            while True:
                try:
                    frames, retry_wait_s, done, res = self._txq.get_nowait()
                except queue.Empty:
                    break
                res.append(self._transmit(frames, retry_wait_s))
                done.set()
                busy = True
            # 2) 非阻塞收一遍（厂商文档：WaitTime 保留参数，函数不阻塞）
            ret = dll.VCI_Receive(VCI_USBCAN2, 0, self.channel,
                                  self._rx_ptr, self._rx_n, 0)
            for i in range(max(ret, 0)):
                f = self._rx_buf[i]
                dlc = f.DataLen
                if dlc > 8:
                    dlc = 8
                frame = CanFrame(id=f.ID, xtd=bool(f.ExternFlag), dlc=dlc,
                                 data=bytes(f.Data[:dlc]), ts=time.time())
                self.rx_count += 1
                if self.on_frame is not None:
                    try:
                        self.on_frame(frame)
                    except Exception:  # noqa: BLE001 - 回调异常不杀 I/O 线程
                        pass
            if ret > 0:
                busy = True
            # 3) 队列空且总线无帧才睡一拍（收发延迟上限 1ms，换取不热自旋）
            if not busy:
                time.sleep(self.IO_IDLE_S)

    # ---- 发送（调用线程：入队 + 等完成，不碰 DLL） ---------------------------
    def send(self, frame: CanFrame) -> bool:
        """发一帧; 返回是否成功（实际发送由 I/O 线程执行，本方法等它的结果）。"""
        return self.send_many([frame]) == 1

    def _transmit(self, frames, retry_wait_s: float) -> int:
        """I/O 线程内：把多帧交给驱动发送队列，返回成功排队的帧数。

        VCI_Transmit 的末参就是帧数，驱动收进缓冲之后自己按线速发，所以上位机不需要
        逐帧 sleep——那样反而比线速慢，把总线利用率压到七成（实测 1.70 ms/帧，
        而 125 kbps 下一个 8 字节扩展帧的线上时间是 1.216 ms）。
        设备缓冲装不下时返回的数量小于请求数，剩下的等 retry_wait_s 之后补发；
        连续 SEND_MANY_IDLE_TRIES 次一帧都没排进去才放弃。"""
        n = len(frames)
        if n == 0:
            return 0
        obj_size = ctypes.sizeof(VCI_CAN_OBJ)
        buf = (VCI_CAN_OBJ * n)()
        for k, frame in enumerate(frames):
            data = bytes(frame.data[:8]).ljust(8, b"\x00")
            # SendType=0（正常发送）：仲裁输了/发送失败由适配器自动重发（1s 窗口）。
            # 曾用 1（单次不重发）：设备应答与主机突发争线时主机帧仲裁输了就丢——
            # 实测 BL/App 两侧 0ms 背靠背突发都恰好丢一半（文档 §13 与总线日志互证）
            buf[k] = VCI_CAN_OBJ(frame.id, 0, 0, 0, 0, 1 if frame.xtd else 0,
                                 frame.dlc, (ctypes.c_ubyte * 8)(*data),
                                 (ctypes.c_ubyte * 3)(0, 0, 0))
        sent = 0
        idle = 0
        while sent < n and idle < SEND_MANY_IDLE_TRIES:
            ret = self._dll.VCI_Transmit(
                VCI_USBCAN2, 0, self.channel,   # 设备索引恒 0，通道号是第三参
                ctypes.byref(buf, sent * obj_size), n - sent)
            if ret <= 0:
                idle += 1
                if retry_wait_s > 0:
                    time.sleep(retry_wait_s)
                continue
            sent += ret
            idle = 0
        self.tx_ok += sent
        self.tx_fail += n - sent
        return sent

    def send_many(self, frames, retry_wait_s: float = 0.002) -> int:
        """把多帧排进发送队列并等 I/O 线程发完，返回成功排队的帧数。
        设备未打开或 I/O 线程没在跑时立即返回 0。"""
        frames = list(frames)
        if self._dll is None or self._rx_thread is None or not frames:
            self.tx_fail += len(frames)
            return 0
        done = threading.Event()
        res = []
        self._txq.put((frames, retry_wait_s, done, res))
        if not done.wait(self.TX_WAIT_S):
            self.tx_fail += len(frames)
            return 0
        return res[0]

    def _err(self, msg: str) -> None:
        if self.on_error is not None:
            self.on_error(msg)
