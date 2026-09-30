# -*- coding: utf-8 -*-
"""增量升级（跳过相同块）的离线测试：FlashModel 建模真协议语义，验证合成结果。

运行（monitor_tui 仓库根）：
    .venv\\Scripts\\python.exe tests\\offline_selective_test.py
退出码 0 = 全部通过。

规划基线 = 设备 Flash 现存内容（上位机对每块发 VERIFY 请求，按 ok/err 判定），
不依赖本机保存的任何镜像文件，版本号不参与判断。FlashModel 按 §5.4 建模
Bootloader 的 Flash 行为：
- App 区 98304 octet 字节数组，初始 = 预置内容（设备现存任意内容，与上位机无关）
- ERASE mode=0 全擦 24 块；mode=1 按 mask 擦逻辑扇段（mask 位 i = 段 i =
  块 2i/2i+1，真机实证语义；另提供 mode1_as_fullerase 故障注入 = 掩码被当全擦）
- WRITE 大帧：编码算法 ID 在 CAN ID err[28:26]（0=直通 4099，2=LZ4D 变长）；
  C=0 保持块帧不写只 ACK；写前目标物理扇段（2 块）未擦则先自擦再写（§5.4）
- 算法 2 数据用 lz4.decompress_dict 真解压，字典 = flash 前序明文（与固件
  LZ4_DecodeDict 直读已写区同构）
- VERIFY：按请求范围对 flash 实际内容算 CRC32 比对，一致回 ACK、不一致回
  错误应答（真机 boot_reply_err 语义）
- RUN：切回 App 态，0x20 状态查询上报新版本

最终裁决 = 烧写完成后 flash 前 nb 块与新镜像逐字节相同。
"""
import hashlib
import os
import sys
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from monitor_tui.host_app import HostAPI  # noqa: E402
from monitor_tui import lz4  # noqa: E402
from monitor_tui.protocol import CMD_ENTER_BL  # noqa: E402
from monitor_tui.upgrade import bl_protocol as P  # noqa: E402
from monitor_tui.protocol import CAN_ADDR_HOST, own_id  # noqa: E402
from offline_upgrade_test import FakeClock, FakeChannel, check, RESULTS  # noqa: E402

BLOCK = P.BLOCK_OCTETS
NB = 15                                                     # 与演示 App 相同的块数
FF1 = bytes([0xFF])   # 无反义序列写法：信道曾把反斜杠转义解码坏


def make_image(mutate=None):
    """合成 15 块镜像：伪随机主体 + 尾部 0xFF 填充。"""
    body_len = NB * BLOCK - 12000
    body = bytearray()
    seed = 0
    while len(body) < body_len:
        seed = (seed + 1) & 0xFF
        body += hashlib.sha256(bytes([seed]) + b"fw").digest()
    img = bytearray(body[:body_len]) + FF1 * (NB * BLOCK - body_len)
    if mutate:
        mutate(img)
    return bytes(img)


class FlashModel:
    """一台模块：App/BL 双态 + 真 Flash 行为（docstring 见文件头）。"""

    def __init__(self, addr, version, flash_init, mode1_as_fullerase=False,
                 new_version=None, info_flags=0x0E):
        self.addr = addr
        self.version = version
        self.info_flags = info_flags
        self.new_version = new_version or version   # RUN 跳转后上报的版本
        self.flash = bytearray(flash_init)          # 98304 = 24 块
        self.in_bl = False
        self.mode1_as_fullerase = mode1_as_fullerase
        self.erase_calls = []                       # (mode, mask) 记录
        self.wire_data = 0                          # WRITE 大帧线上字节累计
        self.written_blocks = 0                     # 实际写 Flash 的块数
        self._erased = set()                        # 已擦物理扇段对号（软件记账）
        self._acc = {}
        self._blk = 0                               # 内部块计数（ERASE 归零）

    # ---- CAN 收帧入口（FakeChannel.device.on_send） ----
    def on_send(self, ch, arb_id, data, now):
        f = P.parse_id(arb_id)
        if f["dev"] != P.DEV_MODULE:
            return
        cmd, dest = f["cmd"], f["dest"]
        if cmd == P.CMD_PROBE:
            if self.in_bl:
                # BL PROBE：6 字节载荷 + sum8，由 _reply 补齐到 8
                self._reply(ch, cmd, bytes((P.CMD_PROBE, 0x02, self.addr, 1, 6, 0)), now)
            else:
                # App 态 0x20 = 自有协议状态帧：8 字节完整帧（B6/B7 保留 0），裸发
                v = tuple(int(x) for x in self.version.split("."))
                ch.push(now, own_id(P.CMD_PROBE, dest=CAN_ADDR_HOST, src=self.addr),
                        bytes((P.CMD_PROBE, 0x01, self.addr) + v + (0, 0)))
            return
        if not self.in_bl:
            if cmd == CMD_ENTER_BL and dest == self.addr:
                self.in_bl = True                   # 简化：受理即进 BL
            return
        if cmd == P.CMD_INFO:
            # [maj min patch 扇区总数16 参数区3 能力 自升级 sum8]；能力 0x0E =
            # LZ4|LZ4D|SELECTIVE（镜像 BL 1.5.0+ 的 INFO byte5；byte3=16 与
            # 真机一致——芯片总扇区数，目标区扇段数由上位机常量持有）
            self._reply(ch, cmd, bytes((1, 6, 0, 16, 3, self.info_flags, 0x01)), now)
            return
        if cmd == P.CMD_ERASE:
            buf = self._acc.setdefault("E", bytearray())
            buf += data
            if len(buf) < 8:
                return
            del self._acc["E"]
            mode = buf[0]
            mask = (buf[1] << 8) | buf[2]           # erase_payload 大端：B1=hi, B2=lo
            self.erase_calls.append((mode, mask))
            self._blk = 0                           # 协议：擦除把块计数归零
            self._erased = set()
            if mode == 0 or (mode == 1 and self.mode1_as_fullerase):
                self._erase_range(0, 24)
                self._erased = set(range(12))
            elif mode == 1:
                # mask 位 i = 目标区逻辑扇段 i（8KB = 块 2i,2i+1）——真机实证语义
                # （diag_r4：bit5=块 10,11 擦对差异段 VERIFY 过）
                for i in range(12):
                    if mask & (1 << i):
                        self._erase_range(2 * i, 2)
                        self._erased.add(i)
            self._reply(ch, cmd, b"", now)
            return
        if cmd == P.CMD_WRITE:
            enc = f["err"]                          # 编码算法 ID 在 err[28:26]
            buf = self._acc.setdefault(("W", enc), bytearray())
            buf += data
            if enc == 0:
                need = 1 + BLOCK + 2
            else:
                if len(buf) < 3:
                    return
                c = buf[1] | (buf[2] << 8)
                need = 3 + c + 2
            if len(buf) < need:
                return
            del self._acc[("W", enc)]
            self.wire_data += need
            k = self._blk
            self._blk += 1
            if enc == 2:
                c = buf[1] | (buf[2] << 8)
                if c == 0:                          # 保持块/填充块跳过：不写只 ACK
                    self._ack_block(ch, cmd, k, now)
                    return
                plain = lz4.decompress_dict(
                    bytes(buf[3:3 + c]), BLOCK,
                    dict_bytes=bytes(self.flash[max(0, k * BLOCK - 65535):k * BLOCK]))
            else:
                plain = bytes(buf[1:1 + BLOCK])
            self._write_block_autoerase(k, plain)
            self.written_blocks += 1
            self._ack_block(ch, cmd, k, now)
            return
        if cmd == P.CMD_VERIFY:
            buf = self._acc.setdefault("V", bytearray())
            buf += data
            if len(buf) < 13:
                return
            del self._acc["V"]
            # verify_payload 无帧头：[start u32][wc u32][crc u32][sum8]
            start_word = int.from_bytes(buf[0:4], "big")
            wc = int.from_bytes(buf[4:8], "big")
            crc = int.from_bytes(buf[8:12], "big")
            actual = P.crc32(bytes(self.flash[start_word * 2:(start_word + wc) * 2]))
            if actual == crc:
                self._reply(ch, cmd, b"", now)
            else:
                self._reply_err(ch, cmd, now)       # 不一致回错误应答（真机语义）
            return
        if cmd == P.CMD_READ:
            # read_payload 无帧头：[start u32][wc u16][sum8]
            start_word = int.from_bytes(data[0:4], "big")
            wc = int.from_bytes(data[4:6], "big")
            chunk = bytes(self.flash[start_word * 2:(start_word + wc) * 2])
            for i in range(0, len(chunk), 8):
                # READ 数据流 = 纯数据按 8 字节连续帧，无帧内 sum（上位机按
                # frame.data 顺序拼接后与 expect 截断比对）
                ch.push(now, P.build_id(P.CMD_READ, P.HOST_ADDR, src=self.addr),
                        chunk[i:i + 8])
            return
        if cmd in (P.CMD_RUN, P.CMD_RESET):
            if cmd == P.CMD_RUN:
                self.in_bl = False
                self.version = self.new_version     # 新固件上线，版本随变
            self._reply(ch, cmd, b"", now)
            return

    def _write_block_autoerase(self, k, plain):
        """§5.4「写入前检查该扇段已擦除，未擦先擦再写」——"已擦"是 BL 的软件
        记账状态（擦除命令/自擦置位，同扇段后续块直接写），不是"内容全 FF"。
        物理扇段 = 块 k 与同段另一块（k^1）所在 2 块。"""
        pair = k // 2
        if pair not in self._erased:
            self._erase_range(pair * 2, 2)
            self._erased.add(pair)
        self.flash[k * BLOCK:(k + 1) * BLOCK] = plain

    def _ack_block(self, ch, cmd, k, now):
        self._reply(ch, cmd, bytes((0xA5, (k >> 8) & 0xFF, k & 0xFF)), now)

    def _erase_range(self, start, nb):
        for i in range(start, start + nb):
            self.flash[i * BLOCK:(i + 1) * BLOCK] = FF1 * BLOCK

    def _reply(self, ch, cmd, payload, now):
        data = bytearray(payload)
        data.append(P.sum8(payload))
        data += bytearray(8 - len(data))
        ch.push(now, P.build_id(cmd, P.HOST_ADDR, src=self.addr), bytes(data))

    def _reply_err(self, ch, cmd, now):
        p = bytes((0xAA, 0xEE, 0xEE, 0, 0, 0, 0))   # 真机 boot_send_err 载荷
        data = bytearray(p) + bytearray((P.sum8(p),))
        ch.push(now, P.build_id(cmd, P.HOST_ADDR, src=self.addr, err=1), bytes(data))


def make_api(model, new_octets):
    api = HostAPI()
    api._ch = FakeChannel(clock=FakeClock(), device=model)
    api._probe_clock = api._ch.clock
    api._fw = types.SimpleNamespace(octets=new_octets, size=len(new_octets),
                                    base_addr=0x08400, crc32=0)
    return api


def run_upgrade(api, addr=0x07, compress=True):
    api._upgrade_worker(addr, window_s=0.5, auto=True, compress=compress)


def flash_eq(model, expect):
    nb = len(expect) // BLOCK
    return bytes(model.flash[:nb * BLOCK]) == expect


def test_same_content_no_local_copy():
    print("1) 重烧同内容（本机无任何镜像文件）：逐块探测全一致 → 全保持，0 擦 0 写")
    same = make_image()
    dev = FlashModel(0x07, "9.9.9", same.ljust(24 * BLOCK, FF1))
    api = make_api(dev, same)                   # 不预置任何本地镜像
    run_upgrade(api)
    check("flash 前 15 块 == 镜像（逐字节）", flash_eq(dev, same))
    check("零擦除", dev.erase_calls == [], str(dev.erase_calls))
    check("零写入", dev.written_blocks == 0, str(dev.written_blocks))
    check("设备跳回 App 态", not dev.in_bl)


def test_one_byte_diff_no_local_copy():
    print("2) 1 字节差异（本机无任何镜像文件）：擦 1 扇段写 2 块，其余 22 块跳过")
    old = make_image()
    new = bytearray(old)
    new[4 * BLOCK + 100] ^= 0xFF              # 只改块 4（扇段 2）一个字节
    new = bytes(new)
    dev = FlashModel(0x07, "9.9.9", old.ljust(24 * BLOCK, FF1),
                     new_version="1.0.0")
    api = make_api(dev, new)                    # 不预置任何本地镜像
    t0 = time.perf_counter()
    run_upgrade(api)
    dt = time.perf_counter() - t0
    check("flash 前 15 块 == 新镜像（逐字节）", flash_eq(dev, new))
    check("写了 2 块（差异块 + 同扇段连带块）", dev.written_blocks == 2,
          str(dev.written_blocks))
    check("擦除 1 段（mode=1，mask 位=段对号 2）", dev.erase_calls == [(1, 1 << 2)],
          str(dev.erase_calls))
    check("线上字节显著小于全量（< 3 块大帧）", dev.wire_data <= 3 * 4099,
          str(dev.wire_data))
    check("设备跳回 App 态", not dev.in_bl)
    print("   [计时] 离线全流程 %.2f s（虚拟时钟，仅供流程冒烟）" % dt)


def test_stale_tail_cleaned():
    print("3) 设备残留旧镜像残尾（新镜像更短）：残尾被判差异擦除，尾部回到擦除态")
    old = make_image()
    new = make_image()                          # 与设备现存内容无关的镜像
    mark = b"STALE-RESIDUE-FROM-OLDER-LONGER-IMAGE...."
    tail = mark + FF1 * (BLOCK - len(mark))     # 块 15 前部残尾，其余擦除态
    flash_init = old + tail + FF1 * (8 * BLOCK)                  # 设备现存 16 块 + 擦除态
    dev = FlashModel(0x07, "9.9.9", flash_init, new_version="1.0.0")
    api = make_api(dev, new)
    run_upgrade(api)
    check("flash 前 15 块 == 新镜像", flash_eq(dev, new))
    check("块 15~23 全部回到擦除态（残尾被清除）",
          bytes(dev.flash[15 * BLOCK:]) == FF1 * (9 * BLOCK))
    check("擦除掩码含扇段 7（块 14,15）", (1, 1 << 7) in dev.erase_calls,
          str(dev.erase_calls))


def test_mask_broken_fallback():
    print("4) 掩码擦除被当全擦（黑盒故障注入）：验证失败转全量，结果仍正确")
    old = make_image()
    new = bytearray(old)
    new[4 * BLOCK + 100] ^= 0xFF
    new = bytes(new)
    dev = FlashModel(0x07, "9.9.9", old.ljust(24 * BLOCK, FF1),
                     mode1_as_fullerase=True)
    api = make_api(dev, new)
    run_upgrade(api)
    check("flash 前 15 块 == 新镜像（降级后仍正确）", flash_eq(dev, new))
    check("检测到擦写结果异常后补了全擦", (0, 0) in dev.erase_calls,
          str(dev.erase_calls))
    check("非 FF 块全写（累计 15 = 增量 2 + 转全量 13，尾 FF 块 C=0）",
          dev.written_blocks == 15, str(dev.written_blocks))


def test_capability_gate_fallback():
    print("5) 设备未上报增量升级能力位（旧 BL/小 Flash 构建）：门控拦截转全量")
    old = make_image()
    new = bytearray(old)
    new[4 * BLOCK + 100] ^= 0xFF
    new = bytes(new)
    dev = FlashModel(0x07, "9.9.9", old.ljust(24 * BLOCK, FF1),
                     new_version="1.0.0", info_flags=0x06)   # 无 0x08 位
    api = make_api(dev, new)
    run_upgrade(api)
    check("flash 前 15 块 == 新镜像（门控退全量仍正确）", flash_eq(dev, new))
    check("能力位缺失走全量擦", dev.erase_calls == [(0, 0)], str(dev.erase_calls))
    check("非 FF 块全写（13，门控转全量后尾 FF 块 C=0）",
          dev.written_blocks == 13, str(dev.written_blocks))


def main():
    test_same_content_no_local_copy()
    test_one_byte_diff_no_local_copy()
    test_stale_tail_cleaned()
    test_capability_gate_fallback()
    test_mask_broken_fallback()
    fails = [n for n, ok in RESULTS if not ok]
    print("-" * 64)
    print("合计 %d 项，通过 %d，失败 %d" % (len(RESULTS), len(RESULTS) - len(fails), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
