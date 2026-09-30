# -*- coding: utf-8 -*-
"""LZ4 block 传输压缩（算法 1/2）：压缩与参考解码。

与固件 LIBs/lz4_dec.c 是同一份格式的两端。格式 = LZ4 官方 block 格式
（非 frame 包装）：
- 序列 = token(1B) + 字面量 + (2 字节小端 offset)；token 高 4 位字面量长度
  （15 = 转义：后续每字节 255 继续、最后非 255 计入），低 4 位匹配长度-4
  （15 = 转义同上），min match 4；
- 最后一条序列只有字面量（无 offset 部），输入耗尽即块结束；
- 算法 1（独立块）：无虚拟窗口，匹配只引用本块内已输出的历史；
- 算法 2（链式字典，LZ4Chainer）：匹配窗口跨块，可引用距当前位置 ≤ 65535 的
  前序明文；每块仍是合法独立 LZ4 block（末 5 字节字面量、末匹配起点距块尾
  ≥ 12 的官方压缩端约束按块套用）。解码端（固件 LZ4_DecodeDict）把字典指向
  已写入 Flash 的前序明文直读，RAM 零增量。

compress 遵守官方压缩端约束（末 5 字节字面量、末匹配起点距块尾 ≥ 12）。
decompress / decompress_dict 是参考解码，离线测试用它们对拍固件解码器的行为
（真机 CRC 一致是最终裁决）。压缩比不划算时由调用方对该块退直通
（算法 ID 0），本模块不做该决策。
"""
from __future__ import annotations

MIN_MATCH = 4
MAX_OFFSET = 65535          # LZ4 offset 字段上限（2 字节）


def compress(data: bytes) -> bytes:
    """压缩一段明文（一个块），返回 LZ4 block 字节流（不可压时可能比原文长）。"""
    return LZ4Chainer().compress(data)


class LZ4Chainer:
    """链式字典 LZ4 压缩器（算法 2）：窗口跨块，字典 = 前序明文。

    按块顺序喂入：c = chainer.compress(block)。每块输出独立成帧；块间共享
    索引与历史（窗口封到 LZ4 offset 上限 65535）。块重发由上层重发同一份
    压缩结果（本器状态只随 compress 前进，不回退）。
    """

    def __init__(self, window: int = MAX_OFFSET):
        self._window = min(window, MAX_OFFSET)
        self._stream = bytearray()
        self._index: dict[bytes, list[int]] = {}

    def compress(self, data: bytes) -> bytes:
        """压缩一块明文并推进链式历史，返回 LZ4 block 字节流。"""
        stream = self._stream
        index = self._index
        base = len(stream)
        stream += data
        n = len(data)
        out = bytearray()
        i = 0
        lit_start = 0

        def emit_seq(lit_end: int, match) -> None:
            """match=None → 末序列只有字面量；否则 (off, ml)，ml≥4。"""
            lit = stream[base + lit_start:base + lit_end]
            ll = len(lit)
            if match is None:
                out.append(min(ll, 15) << 4)
            else:
                off, ml = match
                out.append((min(ll, 15) << 4) | min(ml - MIN_MATCH, 15))
            rest = ll - 15
            while rest >= 0:
                out.append(255 if rest >= 255 else rest)
                if rest < 255:
                    break
                rest -= 255
            out.extend(lit)
            if match is not None:
                off, ml = match
                out.append(off & 0xFF)
                out.append((off >> 8) & 0xFF)
                fld = ml - MIN_MATCH
                if fld >= 15:
                    rest = fld - 15
                    while True:
                        out.append(255 if rest >= 255 else rest)
                        if rest < 255:
                            break
                        rest -= 255

        while i < n:
            best_len = 0
            best_off = 0
            gi = base + i
            # 官方压缩端约束：末 5 字节只做字面量，匹配不得从距块尾 12 字节内发起
            if i + MIN_MATCH <= n and i <= n - 12:
                cands = index.get(bytes(stream[gi:gi + MIN_MATCH]))
                if cands:
                    lo = gi - self._window
                    max_l = n - 5 - i
                    tried = 0
                    for p in reversed(cands):
                        if p < lo or tried >= 64:
                            break
                        tried += 1
                        l = MIN_MATCH
                        while l < max_l and stream[p + l] == stream[gi + l]:
                            l += 1
                        if l > best_len:
                            best_len, best_off = l, gi - p
                            if l == max_l:
                                break
            if best_len >= MIN_MATCH:
                emit_seq(i, (best_off, best_len))
                for j in range(best_len):
                    p = gi + j
                    if p + MIN_MATCH <= len(stream):
                        index.setdefault(bytes(stream[p:p + MIN_MATCH]), []).append(p)
                i += best_len
                lit_start = i
            else:
                if gi + MIN_MATCH <= len(stream):
                    index.setdefault(bytes(stream[gi:gi + MIN_MATCH]), []).append(gi)
                i += 1
        emit_seq(n, None)
        return bytes(out)


def decompress(data: bytes, out_len: int) -> bytes:
    """参考解码：与固件 LZ4_Decode 同语义（算法 1，无字典）。失败抛 ValueError。"""
    return decompress_dict(data, out_len)


def decompress_dict(data: bytes, out_len: int, dict_bytes: bytes = b"") -> bytes:
    """参考解码（usingDict 语义）：与固件 LZ4_DecodeDict 同语义。

    off 越出本块已输出部分时从 dict_bytes 末尾继续回看（字典逻辑上排在输出
    之前）；匹配跨字典/块内边界时逐字节切换来源。失败抛 ValueError。"""
    out = bytearray()
    pos = 0
    n = len(data)
    dn = len(dict_bytes)

    def hist(k: int) -> int:
        """输出流回看第 k 个字节（k<0 进字典）。"""
        return dict_bytes[dn + k] if k < 0 else out[k]

    while pos < n:
        tok = data[pos]
        pos += 1
        ll = tok >> 4
        if ll == 15:
            while True:
                if pos >= n:
                    raise ValueError("LZ4: 字面量长度扩展截断")
                b = data[pos]
                pos += 1
                ll += b
                if b != 255:
                    break
        if pos + ll > n:
            raise ValueError("LZ4: 字面量截断")
        out += data[pos:pos + ll]
        pos += ll
        if len(out) > out_len:
            raise ValueError("LZ4: 字面量越出块尾")
        if pos == n:
            break                        # 末序列只有字面量
        if pos + 2 > n:
            raise ValueError("LZ4: offset 截断")
        off = data[pos] | (data[pos + 1] << 8)
        pos += 2
        if off == 0 or off > len(out) + dn:
            raise ValueError("LZ4: offset 越出字典+块内历史")
        ml = (tok & 0xF) + MIN_MATCH
        if (tok & 0xF) == 15:
            while True:
                if pos >= n:
                    raise ValueError("LZ4: 匹配长度扩展截断")
                b = data[pos]
                pos += 1
                ml += b
                if b != 255:
                    break
        if ml > len(out) and off > len(out) + dn:
            raise ValueError("LZ4: 匹配引用越出字典+块尾")
        for _ in range(ml):
            if len(out) >= out_len:
                raise ValueError("LZ4: 匹配复制越出块尾")
            out.append(hist(len(out) - off))
    if len(out) != out_len:
        raise ValueError("LZ4: 输出长度不符")
    return bytes(out)
