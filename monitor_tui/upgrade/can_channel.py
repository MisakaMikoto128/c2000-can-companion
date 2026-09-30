# -*- coding: utf-8 -*-
"""CAN 通道封装：复用 monitor_tui 的 USBCAN2 驱动，收帧按订阅分发。

架构（docs/消息体系架构_V2.md）：帧只被投递，不被取走。驱动接收线程的
_on_frame 是唯一入口，每帧同步回调全部 listener，并按谓词复制进每个订阅者
的私有 Mailbox；消费者在自有信箱上阻塞等待。没有共享接收队列，因此不存在
多执行流抢帧，也不需要独占会话/drain/重放。
"""
import queue
import threading

from monitor_tui.canbus import resolve_dll_path, UsbCan2Bus
from monitor_tui.protocol import CanFrame


class Mailbox:
    """一个订阅者的私有收帧信箱：pred 匹配的帧由通道分发进来，get 阻塞取。"""

    def __init__(self, channel, pred, maxsize):
        self._ch = channel
        self._pred = pred
        self._q = queue.Queue(maxsize)
        self._closed = False

    def _offer(self, frame):
        """分发入口（接收线程内）：谓词匹配则入队；满则丢最旧，保证最新应答可见。"""
        if self._closed or (self._pred is not None and not self._pred(frame)):
            return
        try:
            self._q.put_nowait(frame)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(frame)
            except queue.Full:
                pass

    def get(self, timeout_s):
        """取一帧，超时返回 None。queue 的超时内部走单调时钟。"""
        try:
            return self._q.get(timeout=max(0.0, timeout_s))
        except queue.Empty:
            return None

    def close(self):
        self._closed = True
        self._ch._remove_mailbox(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
        return False


class CanChannel:
    """start() 打开设备；send() 发帧；subscribe() 订阅收帧；add_listener() 全帧旁听。"""

    def __init__(self, channel=0, dll_path=None, on_snoop=None, on_tx=None, on_error=None):
        self._bus = UsbCan2Bus(channel=channel,
                               dll_path=resolve_dll_path(dll_path),
                               on_frame=self._on_frame,
                               on_error=on_error)
        self._lock = threading.Lock()
        self._listeners = [on_snoop] if on_snoop is not None else []
        self._mailboxes = []
        self._on_tx = on_tx        # 发送成功回调（帧表记录用）

    def _on_frame(self, frame):
        """唯一收帧入口：先分发信箱，再回调 listener 链。
        信箱里睡着等应答的命令线程（延迟敏感）；listener 是监控簿记（摄取/帧表/
        CSV，对延迟不敏感）——簿记不能挡在应答前面。
        单个 listener 抛异常不影响其余 listener 与信箱投递。"""
        with self._lock:
            listeners = list(self._listeners)
            mailboxes = list(self._mailboxes)
        for mb in mailboxes:
            mb._offer(frame)
        for cb in listeners:
            try:
                cb(frame)
            except Exception:  # noqa: BLE001 - 监听方异常不炸接收线程
                pass

    # ---- 订阅 ------------------------------------------------------------
    def subscribe(self, pred=None, maxsize=256):
        """注册信箱，从注册后的下一帧开始按谓词投递。
        先订阅后发送，应答就不会落在订阅注册之前。"""
        mb = Mailbox(self, pred, maxsize)
        with self._lock:
            self._mailboxes.append(mb)
        return mb

    def _remove_mailbox(self, mb):
        with self._lock:
            if mb in self._mailboxes:
                self._mailboxes.remove(mb)

    # ---- 旁听（每帧回调，与订阅无关） --------------------------------------
    def add_listener(self, cb):
        with self._lock:
            if cb not in self._listeners:
                self._listeners.append(cb)

    def remove_listener(self, cb):
        with self._lock:
            if cb in self._listeners:
                self._listeners.remove(cb)

    # ---- 设备 ------------------------------------------------------------
    def start(self, baud=None):
        """打开设备；baud 缺省 125 kbps，透传给 UsbCan2Bus.start。"""
        return self._bus.start(baud=baud)

    def close(self):
        self._bus.close()

    def send(self, arbitration_id, data, xtd=True):
        ok = self._bus.send(CanFrame(id=arbitration_id, xtd=xtd,
                                     dlc=len(data), data=bytes(data)))
        if ok and self._on_tx is not None:
            self._on_tx(arbitration_id, xtd, bytes(data))
        return ok

    def send_many(self, arbitration_id, chunks, xtd=True, retry_wait_s=0.002):
        """同一个 ID 的多个载荷一次性交给驱动；返回成功排队的帧数。

        大帧（WRITE 一块 513 帧）走这条路：逐帧发会被上位机的调用开销拖到线速以下。"""
        frames = [CanFrame(id=arbitration_id, xtd=xtd, dlc=len(c), data=bytes(c))
                  for c in chunks]
        sent = self._bus.send_many(frames, retry_wait_s)
        if self._on_tx is not None:
            for c in chunks[:sent]:
                self._on_tx(arbitration_id, xtd, bytes(c))
        return sent

    @property
    def rx_count(self):
        return self._bus.rx_count

    @property
    def tx_ok(self):
        return self._bus.tx_ok

    @property
    def tx_fail(self):
        return self._bus.tx_fail
