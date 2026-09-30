# -*- coding: utf-8 -*-
"""Bootloader 传输协议：ID 构造/解析、CRC32、sum8、大帧拆装、命令载荷。

物理层：125kbps、29 位扩展帧、DLC=8。
ID = err[28:26] | dev[25:22]=0x0C | cmd[21:16] | dest[15:8] | src[7:0]。
"""
import zlib

from monitor_tui.protocol import OWN_DEV as DEV_MODULE  # 自有协议统一段（唯一来源 protocol.OWN_DEV）
HOST_ADDR = 0xF0
BROADCAST = 0x3F

CMD_PROBE = 0x20
CMD_ERASE = 0x21
CMD_WRITE = 0x22
CMD_VERIFY = 0x23
CMD_READ = 0x24
CMD_RUN = 0x25
CMD_INFO = 0x26
CMD_RESET = 0x27
# 注意：dev 0x0C 里 0x28 是自有协议遥测、0x2A 是 walk 配置——ALOAD/ARUN 曾占
# 0x28/0x29 与遥测撞号（遥测帧被 BL 误拼大帧回错误应答，且刷新判跳计时），
# 已挪到 0x2B/0x2C；固件侧受理同步改为命令白名单（boot_protocol.c）

ERR_OK = 0
ERR_FAIL = 1

BLOCK_OCTETS = 4096            # 每块 octet 数 = 2048 字
TARGET_SECTOR_NB = 12          # 目标区（App 区 SEC4~15）逻辑扇段数，与固件 boot_jump.h 同源
TARGET_BLOCK_NB = TARGET_SECTOR_NB * 2   # 目标区总块数 = 24（INFO byte3 是芯片总扇区数 16，不作此用）
FRAME_DATA_LEN = 8
FRAME_GAP_S = 0.010            # 大帧帧间隔超时

MARK_HEAD = 0x5A
MARK_MORE = 0xAA
MARK_LAST = 0xBB

PROBE_REQ = bytes((0xA5, 0xA1, 0xB1))
PROBE_RSP = bytes((0xA5, 0xAA, 0xBB))


def build_id(cmd, dest, src=HOST_ADDR, err=0, dev=DEV_MODULE):
    return ((err & 0x7) << 26) | ((dev & 0xF) << 22) | ((cmd & 0x3F) << 16) \
        | ((dest & 0xFF) << 8) | (src & 0xFF)


def parse_id(arbitration_id):
    return {
        "err": (arbitration_id >> 26) & 0x7,
        "dev": (arbitration_id >> 22) & 0xF,
        "cmd": (arbitration_id >> 16) & 0x3F,
        "dest": (arbitration_id >> 8) & 0xFF,
        "src": arbitration_id & 0xFF,
    }


def sum8(data):
    return sum(data) & 0xFF


def crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def chunk_frames(data):
    """octet 流 → 8 字节帧列表。"""
    return [data[i:i + FRAME_DATA_LEN] for i in range(0, len(data), FRAME_DATA_LEN)]


def u32_be(v):
    return bytes(((v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF))


def u16_be(v):
    return bytes(((v >> 8) & 0xFF, v & 0xFF))


def probe_request():
    p = bytearray(PROBE_REQ)
    p.append(sum8(p))
    return bytes(p)


def erase_payload(mode, mask):
    p = bytearray((mode & 0xFF, (mask >> 8) & 0xFF, mask & 0xFF, 0, 0, 0, 0))
    p.append(sum8(p))
    return bytes(p)


def write_frame(block_octets, last):
    """块大帧：[5A] + 数据(4096) + 尾标 + sum8。"""
    p = bytearray((MARK_HEAD,))
    p += block_octets
    p.append(MARK_LAST if last else MARK_MORE)
    p.append(sum8(p))
    return bytes(p)


COMPRESS_LZ4 = 1                # 算法 1：LZ4 独立块
COMPRESS_CAP_LZ4 = 0x02         # INFO byte5 能力位：bit1 = 支持算法 1 压缩块
COMPRESS_LZ4D = 2               # 算法 2：LZ4 链式字典（窗口跨块，字典 = Flash 前序明文）
COMPRESS_CAP_LZ4D = 0x04        # INFO byte5 能力位：bit2 = 支持 LZ4 链式 + 空块跳过

# Bootloader 自升级（RAM 烧录代理，见协议文档 §agent）：INFO byte6 位图

# 传输编码选择（写块时的线上编码）：与算法 ID 的映射——
# direct → 算法 0 直通；llz/lz4 → 算法 1（同 ID 两代编码，按 INFO 版本段区分：
# BL V1.1.0 解 LLZ、≥V1.2.0 解 LZ4）；lz4d → 算法 2 链式字典 + 空块跳过。
ENC_DIRECT = "direct"
ENC_LZ4 = "lz4"
ENC_LZ4D = "lz4d"


def write_frame_compressed(compressed_octets, last):
    """压缩块大帧：[5A][C_lo][C_hi][C octet 压缩数据][尾标][sum8]，总长 C+5。

    C 是压缩后长度。算法 1/2 都按 C 提前收口，解压回 4096 octet 明文后写扇区，
    ACK/块号/扇区映射与直通完全一致。算法 2（链式）另定义 C=0 为空块跳过标记：
    整块纯 0xFF 填充时不烧写、只推进块号（扇区擦除态即正确内容）——C=0 在
    算法 1 下是非法帧，只有协商过链式能力（COMPRESS_CAP_LZ4D）才能发。"""
    c = len(compressed_octets)
    p = bytearray((MARK_HEAD, c & 0xFF, (c >> 8) & 0xFF))
    p += compressed_octets
    p.append(MARK_LAST if last else MARK_MORE)
    p.append(sum8(p))
    return bytes(p)


def verify_payload(start_word, word_count, expected_crc32):
    p = bytearray(u32_be(start_word) + u32_be(word_count) + u32_be(expected_crc32))
    p.append(sum8(p))
    return bytes(p)


    """ARUN 单帧载荷：前 4 字节是已收全部代理块（含 0xFF 补齐）的 CRC32。
    长度不上线：装载块数×4096 就是双方共同的覆盖口径。与 RUN/INFO 一样
    不附 sum8（单帧直收，不经过大帧拆装）。"""
    return u32_be(expected_crc32) + bytes(4)


def read_payload(start_word, word_count):
    p = bytearray(u32_be(start_word) + u16_be(word_count))
    p.append(sum8(p))
    return bytes(p)


def parse_ack(frame_data):
    """ACK 载荷 [A5 hi lo sum8] → (block_index, sum_ok)。"""
    if len(frame_data) < 4 or frame_data[0] != 0xA5:
        return None
    idx = (frame_data[1] << 8) | frame_data[2]
    return idx, sum8(frame_data[:3]) == frame_data[3]


def parse_info(frame_data):
    """INFO 载荷 [maj min patch 扇区数 参数扇区 压缩能力位 自升级位 sum8]。

    byte5 是附加算法支持位图：bit1(0x02) = LZ4 独立块，bit2(0x04) = LZ4 链式
    字典 + 空块跳过；旧 Bootloader 该字节恒 0（仅直通）。
    byte6 是自升级能力位图：bit0(0x01) = 支持 ALOAD/ARUN，bit1(0x02) = 本端是
    RAM 烧录代理；BL <V1.4.0 该字节恒 0。"""
    if len(frame_data) < 8 or sum8(frame_data[:7]) != frame_data[7]:
        return None
    return {
        "version": (frame_data[0], frame_data[1], frame_data[2]),
        "sector_nb": frame_data[3],
        "param_sector": frame_data[4],
        "compress_flags": frame_data[5],
        "self_flags": frame_data[6],
    }
