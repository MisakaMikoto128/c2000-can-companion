# -*- coding: utf-8 -*-
"""CAN Companion 自有协议定义与编解码（dev 0x0C 段）。

与固件侧 can_dev.h / can_app.c 同一套约定：
  - 帧 ID[28:0] = err[28:26] | dev[25:22] | cmd[21:16] | dest[15:8] | src[7:0]
  - 寻址：设备 0x01~0x3B、广播 0x3F、上位机 0xF0
  - 数据域一律小端，DLC=8
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

OWN_DEV = 0x0C        # 自有协议统一设备段（唯一来源：固件 can_dev.h）
CAN_ADDR_HOST = 0xF0  # 上位机地址
BROADCAST = 0x3F      # 广播地址

CMD_ENTER_BL = 0x06    # 请求进入升级模式（仅单播）
CMD_BUILD_TIME = 0x07  # 查询固件编译时刻（App/BL 都答，byte1 区分侧）
CMD_PROBE = 0x20       # 状态查询（App/BL 都答，byte1 区分侧）

# 应答 byte1 侧标识（0x06 受理应答 / 0x07 编译时刻 / 0x20 状态查询共用）
SIDE_APP = 0x01    # App 正常运行
SIDE_BL = 0x02     # Bootloader
INFO_MODE_APP = SIDE_APP
INFO_MODE_ENTER_BL = SIDE_BL


def own_id(cmd: int, dest: int, src: int = CAN_ADDR_HOST, err: int = 0) -> int:
    """构造自有协议（dev 0x0C 统一段）的 29 位扩展帧 ID。"""
    return (((err & 0x07) << 26) | ((OWN_DEV & 0x0F) << 22) | ((cmd & 0x3F) << 16)
            | ((dest & 0xFF) << 8) | (src & 0xFF)) & 0x1FFFFFFF


def own_cmd_of(arb_id: int) -> Optional[int]:
    """ID -> cmd 号；不属于自有段（dev != 0x0C）返回 None。"""
    if ((arb_id >> 22) & 0x0F) != OWN_DEV:
        return None
    return (arb_id >> 16) & 0x3F


@dataclass
class CanFrame:
    """一帧 CAN 报文（与固件 HDL_CAN_Frame_t 对应, data 为 8 字节缓冲截断到 DLC）。"""
    id: int
    xtd: bool          # True = 扩展帧（29-bit）, False = 标准帧（11-bit）
    dlc: int           # 0..8
    data: bytes        # 长度 = dlc
    ts: float = 0.0    # time.time() 墙钟秒（含毫秒, 用于显示）


@dataclass
class DeviceInfo:
    """0x20 状态查询应答解码结果。"""
    mode: int              # INFO_MODE_APP / INFO_MODE_ENTER_BL
    addr: int              # 设备 CAN 地址
    version: tuple         # App 版本 (主, 次, 修订)

    @property
    def version_str(self) -> str:
        return "%d.%d.%d" % self.version


def decode_probe(data: bytes) -> Optional[DeviceInfo]:
    """解码 0x20 状态查询应答帧（dev 0x0C cmd 0x20，App/BL 都答）。

    布局（8 字节）：B0=0x20（cmd 回显）、B1=侧标识（0x01 App / 0x02 Bootloader）、
    B2=本机地址、B3-5=版本 (maj, min, patch)、B6=能力位（bit0 = 调试内存读写
    命令组已编译）、B7 保留。
    App 应答给 App 版本；BL 应答给 Bootloader 版本。"""
    if len(data) < 8 or data[0] != CMD_PROBE:
        return None
    return DeviceInfo(mode=data[1], addr=data[2], version=(data[3], data[4], data[5]))


def encode_probe_query(addr: int = BROADCAST) -> bytes:
    """组 0x20 状态查询（DLC=8）。addr=0x3F 广播，App/BL 各自回一帧应答（byte1 区分侧）。"""
    return bytes((CMD_PROBE & 0xFF, 0, 0, 0, 0, 0, 0, addr & 0xFF))


def encode_build_time_query(addr: int = BROADCAST) -> bytes:
    """组编译时刻查询（dev 0x0C cmd 0x07，DLC=8）。addr=0x3F 广播，App/BL 各自回一帧应答。"""
    return bytes((CMD_BUILD_TIME & 0xFF, 0, 0, 0, 0, 0, 0, addr & 0xFF))


def decode_build_time(data: bytes):
    """解码 0x07 编译时刻应答 -> (侧标识, Unix 时间戳秒)。

    布局（8 字节）：B0=0x07（cmd 回显）、B1=侧标识（0x01 App / 0x02 BL）、
    B2-5=Unix 秒（小端）、B6-7 保留。长度不足或 B0 不符返回 None。"""
    if len(data) < 8 or data[0] != CMD_BUILD_TIME:
        return None
    ts = data[2] | (data[3] << 8) | (data[4] << 16) | (data[5] << 24)
    return (data[1], ts)


def encode_enter_bl(addr: int) -> bytes:
    """组升级请求（dev 0x0C cmd 0x06，DLC=8），目标设备跳进 Bootloader 等待烧写。

    只允许单播：固件只受理 dest 等于本机地址的帧，广播（0x3F）直接忽略，
    所以地址必须给定且落在 0x01~0x3B 的设备地址段。"""
    if not 0x01 <= addr <= 0x3B:
        raise ValueError("升级请求必须单播到 0x01~0x3B 的设备地址，收到 0x%02X" % addr)
    return bytes((CMD_ENTER_BL & 0xFF, 0, 0, 0, 0, 0, 0, addr & 0xFF))


def fmt_frame(frame: CanFrame, direction: str, summary: str = "") -> str:
    """原始帧单行文本: 时间 方向 ID 类型 DLC hex [摘要]。"""
    hexs = " ".join("%02X" % b for b in frame.data[:frame.dlc]) or "-"
    typ = "EXT" if frame.xtd else "STD"
    line = "%s %s  %08X  %s  DLC=%d  %s" % (time_str(frame.ts), direction, frame.id,
                                            typ, frame.dlc, hexs)
    if summary:
        line += "  | " + summary
    return line


def time_str(ts: float) -> str:
    """墙钟秒 -> HH:MM:SS.mmm。"""
    lt = time.localtime(ts)
    return "%02d:%02d:%02d.%03d" % (lt.tm_hour, lt.tm_min, lt.tm_sec,
                                    int(round(ts % 1.0 * 1000)) % 1000)
