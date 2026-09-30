# -*- coding: utf-8 -*-
"""升级前的设备探测：在超时窗口内按固定节拍分辨设备在 Bootloader 还是在 App。

Bootloader 上电后只等 1.5 秒无主机活动就跳回 App，一次广播加短窗口抓不到晚
上电的设备，所以探测在整段超时窗口内每 150ms 重发一轮（BL 探测广播 + 自有
协议 0x20 状态查询），直到出现明确身份或者窗口结束。

三类角色的判据（0x20 两侧同 cmd，按 B1 侧标识区分）：
  Bootloader —— 0x20 应答 B1=0x02（ID 里 cmd=0x20、src=模块地址）。
  App        —— 0x20 应答 B1=0x01（带模块地址和 App 版本）；只收到 0x01
                状态帧而没有应答，说明对面的 App 不认识状态查询。
  无设备     —— 窗口内两者都没出现。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from monitor_tui.protocol import (CMD_PROBE, own_cmd_of, own_id,
                                  DeviceInfo, decode_probe, encode_probe_query)
from monitor_tui.upgrade import bl_protocol as P

PROBE_TICK_S = 0.15                    # 发送节拍：探测广播与信息查询同拍
PROBE_TIMEOUTS_S = (10.0, 20.0, 60.0)  # 界面可选的超时窗口
PROBE_TIMEOUT_DEFAULT_S = 20.0


@dataclass
class ScanResult:
    """一次探测扫描的结论。字段记录窗口内看到的事实，结论交给调用方判断。"""
    state: str = "timeout"        # bl / app / timeout / cancelled
    bl_addrs: List[int] = field(default_factory=list)   # 所有应答探测的模块地址
    hit_addrs: List[int] = field(default_factory=list)  # 其中符合本次目标的
    app: Optional[DeviceInfo] = None       # 最近一次应答信息查询的模块
    apps: List[DeviceInfo] = field(default_factory=list)
    waited_s: float = 0.0
    rx_frames: int = 0            # 窗口内收到的帧数，用来区分总线静默与别家报文


def bl_probe_addr(frame) -> Optional[int]:
    """帧是 Bootloader 的 0x20 状态查询应答则返回模块地址，否则返回 None。

    新布局（2026-09-27 统一）：B0=0x20（cmd 回显）、B1=0x02（Bootloader 侧标识）、
    B2=本机地址、B3-5=Bootloader 版本。旧格式（A5 AA BB + sum8）已废弃。"""
    if len(frame.data) < 6:
        return None
    f = P.parse_id(frame.id)
    if (f["dev"] != P.DEV_MODULE or f["cmd"] != P.CMD_PROBE
            or f["dest"] != P.HOST_ADDR or f["err"] != P.ERR_OK):
        return None
    d = bytes(frame.data)
    if d[0] != P.CMD_PROBE or d[1] != 0x02:  # B0=cmd 回显、B1=BL 侧标识
        return None
    return f["src"]


class DeviceProbe:
    """按节拍扫描总线，分辨 Bootloader / App / 无设备。"""

    def __init__(self, channel, expect_addr=None, tick_s=PROBE_TICK_S,
                 clock: Callable[[], float] = time.monotonic):
        self.ch = channel
        self.expect = expect_addr    # 界面已选定目标时只认它，别的模块另行记录
        self.tick_s = tick_s
        self._clock = clock

    def scan(self, window_s: float, decide_on_app=True, on_progress=None,
             abort: Optional[Callable[[], bool]] = None) -> ScanResult:
        """扫一个超时窗口。on_progress(已等秒数, 窗口秒数) 每拍回调一次，
        abort 返回 True 时提前结束。

        decide_on_app=False 用于已经发出升级请求之后：那时 App 的应答只说明
        "还没复位"，不能当成"设备在 App"的新结论把用户再问一遍。"""
        res = ScanResult()
        start = self._clock()
        deadline = start + window_s
        base_rx = self.ch.rx_count
        # 整个扫描窗口共用一个信箱：订阅先于首次发送，应答不可能落在订阅之前。
        # 谓词按 ID 粗筛：探测应答（两侧 0x20 同 cmd）
        def pred(frame):
            if not frame.xtd:
                return False
            if own_cmd_of(frame.id) == CMD_PROBE:
                return True
            f = P.parse_id(frame.id)
            return (f["dev"] == P.DEV_MODULE and f["cmd"] == P.CMD_PROBE
                    and f["dest"] == P.HOST_ADDR)
        with self.ch.subscribe(pred) as mb:
            while True:
                if abort is not None and abort():
                    res.state = "cancelled"
                    break
                now = self._clock()
                if now >= deadline:
                    break
                self._send_tick()
                res.waited_s = self._clock() - start
                if on_progress is not None:
                    on_progress(res.waited_s, window_s)
                if self._collect(mb, min(now + self.tick_s, deadline), res, decide_on_app):
                    break
        res.waited_s = self._clock() - start
        res.rx_frames = self.ch.rx_count - base_rx
        if res.state == "timeout" and res.apps:
            res.state = "app"
        return res

    def _send_tick(self) -> None:
        """发一轮探测：Bootloader 探测广播 + 设备信息查询（已知目标则单播查询）。"""
        self.ch.send(P.build_id(P.CMD_PROBE, P.BROADCAST), P.probe_request())
        self.ch.send(own_id(CMD_PROBE, dest=0x3F if self.expect is None else self.expect),
                     encode_probe_query(0x3F if self.expect is None else self.expect))

    def _collect(self, mb, until: float, res: ScanResult, decide_on_app: bool) -> bool:
        """从信箱收帧到 until 时刻，返回是否已能定下结论。"""
        while True:
            remain = until - self._clock()
            if remain <= 0:
                return False
            frame = mb.get(remain)
            if frame is None:
                return False
            addr = bl_probe_addr(frame)
            if addr is not None:
                if addr not in res.bl_addrs:
                    res.bl_addrs.append(addr)
                if self.expect is None or addr == self.expect:
                    if addr not in res.hit_addrs:
                        res.hit_addrs.append(addr)
                    res.state = "bl"     # Bootloader 优先：它已在等烧写，不用再问用户
                    return True
                continue
            if not frame.xtd:
                continue
            if own_cmd_of(frame.id) == CMD_PROBE:
                info = decode_probe(bytes(frame.data))
                # 0x20 应答两侧同 cmd 号：B1=0x01 是 App、B1=0x02 是 BL；
                # BL 应答已被 bl_probe_addr 先拦下，这里只收 App 应答
                if info is not None and info.mode == 0x01 and (self.expect is None
                                         or info.addr == self.expect):
                    if info not in res.apps:
                        res.apps.append(info)
                    res.app = info
                    # App 应答不立即定结论：0x20 统一后 App 和 BL 都答同一 cmd，
                    # 先收 App 应答就定 "app" 会漏掉后到的 BL 应答。BL 优先：
                    # 再收一个短窗口（0.5s）看有没有 BL 应答，没有就定 "app"。
                    if decide_on_app:
                        res.state = "app"
                        short_deadline = self._clock() + 0.5
                        while True:
                            f2 = mb.get(short_deadline - self._clock())
                            if f2 is None:
                                break
                            addr2 = bl_probe_addr(f2)
                            if addr2 is not None:
                                if addr2 not in res.bl_addrs:
                                    res.bl_addrs.append(addr2)
                                if self.expect is None or addr2 == self.expect:
                                    if addr2 not in res.hit_addrs:
                                        res.hit_addrs.append(addr2)
                                    res.state = "bl"
                                    return True
                        return True


def no_device_hint(res: ScanResult, window_s: float, rx_total: int) -> str:
    """探测没有结论时的排查提示：只说窗口内看到了什么，不下"设备坏了"的结论。"""
    other = "（另外还看到地址 %s 的模块在 Bootloader，不是你选的目标）" % \
            "、".join("0x%02X" % a for a in res.bl_addrs) if res.bl_addrs else ""
    return ("%g 秒内没探测到目标设备：没有 Bootloader 应答，也没有状态查询应答"
            "%s。请确认上位机已连接 USBCAN2、设备已上电、CAN 线 H/H 与 L/L 接对、"
            "波特率 125 kbps。本窗口收到 %d 帧（总线累计 %d 帧）：%s"
            % (window_s, other, res.rx_frames, rx_total,
               "总线完全静默" if res.rx_frames == 0 else
               "总线上有报文，但没有上位机认得的应答"))
