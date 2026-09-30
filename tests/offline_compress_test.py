# -*- coding: utf-8 -*-
"""LZ4 压缩传输的离线测试：压缩往返、压缩大帧布局、能力位解析与升级闭环。

运行（monitor_tui 仓库根）：
    .venv\\Scripts\\python.exe tests\\offline_compress_test.py
退出码 0 = 全部通过。

闭环部分复用 offline_upgrade_test.py 的 FakeChannel/FakeModule 骨架：派生出
支持「压缩块受理」的假 Bootloader——收到 err=1 的 WRITE 大帧后按契约按 C 收口、
解压回 4096 octet 明文拼进模拟 Flash；err=0 的大帧仍按 4099 octet 直通受理。
"""
import os
import random
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from offline_upgrade_test import FakeClock, FakeModule, make_api  # noqa: E402

from monitor_tui import lz4  # noqa: E402
from monitor_tui.host_app import BootloaderError  # noqa: E402
from monitor_tui.upgrade import bl_protocol as P  # noqa: E402
from monitor_tui.upgrade import firmware as FW    # noqa: E402
from monitor_tui.upgrade.bl_client import pick_encoding  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  <- " + detail) if (detail and not ok) else ""),
          flush=True)


# ------------------------------------------------------------------ 1. 压缩往返
def test_lz4_roundtrip():
    print("0) lz4 往返：compress → decompress 逐字节还原（各类数据形态）")
    rng = random.Random(20260926)
    cases = {
        "全 0xFF": b"\xFF" * 4096,
        "全 0x00": b"\x00" * 4096,
        "随机": rng.randbytes(4096),
        "重复模式": (b"LZ4 bootloader transfer\x00" * 200)[:4096],
        "混合": bytes(rng.choice((0xFF, 0x00, rng.randrange(256))) for _ in range(4096)),
    }
    for name, data in cases.items():
        data = data[:4096]
        c = lz4.compress(data)
        back = lz4.decompress(c, len(data))
        check("lz4 往返 %s（%d -> %d）" % (name, len(data), len(c)), back == data)


def test_lz4_decode_errors():
    print("0b) lz4 参考解码失败路径：截断/非法 offset/长度不符都要抛 ValueError")
    good = lz4.compress(bytes(range(200)))
    for bad in (good[:-1], good[:3], b"\x00" * 4):
        try:
            lz4.decompress(bad, 200)
            check("坏流 %r 应拒绝" % bad[:6], False)
        except ValueError:
            check("坏流 %r 拒绝" % bad[:6], True)
    try:
        lz4.decompress(good, 201)   # 输出长度不符
        check("长度不符应拒绝", False)
    except ValueError:
        check("长度不符拒绝", True)


# ------------------------------------------------------------------ 3. 压缩大帧布局
def test_write_frame_compressed_layout():
    print("3) write_frame_compressed 字节布局与 sum8（手算一例断言）")
    # 手算：5A 03 00 01 02 03 BB，sum = 0x5A+3+0+1+2+3+0xBB = 286 → 0x1E
    f = P.write_frame_compressed(bytes((1, 2, 3)), last=True)
    check("小块 last=True 帧逐字节等于 5A0300010203BB1E",
          f == bytes.fromhex("5A0300010203BB1E"), f.hex().upper())
    # 手算：尾标换 0xAA，sum = 286 - 187 + 170 = 269 → 0x0D
    f2 = P.write_frame_compressed(bytes((1, 2, 3)), last=False)
    check("小块 last=False 帧逐字节等于 5A0300010203AA0D",
          f2 == bytes.fromhex("5A0300010203AA0D"), f2.hex().upper())
    # 300 字节：C_lo=0x2C C_hi=0x01，总长 C+5，sum8 覆盖前面全部字节
    c300 = b"\xAA" * 300
    f3 = P.write_frame_compressed(c300, last=True)
    check("长度低字节在前（C_lo=0x2C, C_hi=0x01）", f3[1:3] == b"\x2C\x01",
          f3[1:3].hex().upper())
    check("总长 = C + 5", len(f3) == 305, str(len(f3)))
    check("尾标/校验位正确且 sum8 覆盖全帧",
          f3[303] == P.MARK_LAST and f3[304] == P.sum8(f3[:304]))
    check("sum8 与 P.sum8 复算一致", f3[-1] == P.sum8(f3[:-1]))


# ------------------------------------------------------------------ 4. 能力位解析
def test_parse_info_capability():
    print("4) parse_info 能力位：byte5 = 附加算法支持位图")
    sup = bytes((0, 9, 1, 16, 13, 0x02, 0)); sup += bytes((P.sum8(sup),))
    info = P.parse_info(sup)
    check("byte5=0x02 → compress_flags=0x02（支持 LZ4）",
          info is not None and info["compress_flags"] == 0x02, repr(info))
    old = bytes((0, 9, 1, 16, 13, 0x00, 0)); old += bytes((P.sum8(old),))
    info0 = P.parse_info(old)
    check("byte5=0x00 → compress_flags=0x00（旧 BL 仅直通）",
          info0 is not None and info0["compress_flags"] == 0x00, repr(info0))
    check("原有字段解析不受影响",
          info["version"] == (0, 9, 1) and info["sector_nb"] == 16
          and info["param_sector"] == 13, repr(info))
    check("sum8 坏仍返回 None", P.parse_info(sup[:7] + b"\x00") is None)
    check("长度不足仍返回 None", P.parse_info(sup[:7]) is None)


# ------------------------------------------------------------------ 闭环：假设备
class LlzFakeModule(FakeModule):
    """支持压缩块受理的假 Bootloader，按 codec 模拟 BL 的编码能力：

    codec=None   仅直通（byte5=0x00，默认版本 1.0.0）
    codec="lz4"  算法 1 = LZ4 独立块（byte5=0x02），err=1 用 lz4 解码
    codec="lz4d" 算法 1 LZ4 + 算法 2 链式（byte5=0x06）；
                 err=2 解压带字典（= 已写入的模拟 Flash 尾部），C=0 空块跳过
                 （模拟 Flash 补 4096 个 0xFF = 擦除态）

    WRITE 大帧按契约 [5A][C_lo][C_hi][C octet][tail][sum8] 收口；err=0 按 4099
    octet 直通受理。三种块都按受理顺序回块号 ACK，与真固件同语义。"""

    _BYTE5 = {None: 0x00, "lz4": 0x02, "lz4d": 0x06}
    _VER = {None: (1, 0, 0), "lz4": (1, 2, 0), "lz4d": (1, 3, 0)}

    def __init__(self, *args, codec="lz4", bl_version=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.codec = codec
        self.bl_version = bl_version if bl_version is not None else self._VER[codec]
        self.flash = bytearray()
        self.enc_stats = {0: 0, 1: 0, 2: 0}     # 三种编码各受理了多少块
        self.skipped = 0                        # 空块跳过次数
        self.big_errors = []                    # 契约违反记录（帧头/尾标/sum8/解压）
        self._acc = {}                          # (cmd, err) → bytearray，按编码分桶

    def on_send(self, ch, arb_id, data, now):
        f = P.parse_id(arb_id)
        if f["dev"] == P.DEV_MODULE and self.in_bootloader:
            if f["cmd"] == P.CMD_INFO:
                maj, minor, patch = self.bl_version
                b5 = self._BYTE5[self.codec]
                self._reply(ch, P.CMD_INFO,
                            bytes((maj, minor, patch, 16, 13, b5, 0)), now)
                return
            if f["cmd"] == P.CMD_VERIFY:
                # 与真实 BL 同语义：对已写 Flash 内容算 CRC，不匹配不应答
                # （verify-first 探测依赖此语义：内容不一致时静默）
                buf = self._acc.setdefault((P.CMD_VERIFY, f["err"]), bytearray())
                buf += data
                if len(buf) < 13:
                    return
                del self._acc[(P.CMD_VERIFY, f["err"])]
                start_w = int.from_bytes(buf[0:4], "big")
                wc = int.from_bytes(buf[4:8], "big")
                crc = int.from_bytes(buf[8:12], "big")
                region = bytes(self.flash[start_w * 2:start_w * 2 + wc * 2])
                region = region.ljust(wc * 2, bytes([0xFF]))
                if P.crc32(region) == crc:
                    self._reply(ch, P.CMD_VERIFY, b"", now)
                return
            if f["cmd"] == P.CMD_WRITE:
                self._on_write(f["err"], bytes(data), ch, now)
                return
        super().on_send(ch, arb_id, data, now)

    def _on_write(self, err, data, ch, now):
        buf = self._acc.setdefault((P.CMD_WRITE, err), bytearray())
        buf += data
        if err == 0:
            need = 1 + P.BLOCK_OCTETS + 2          # 5A + 4096 + tail + sum8
            if len(buf) < need:
                return
            big = bytes(buf[:need])
            del self._acc[(P.CMD_WRITE, err)]
            if (big[0] != P.MARK_HEAD
                    or big[1 + P.BLOCK_OCTETS] not in (P.MARK_MORE, P.MARK_LAST)
                    or P.sum8(big[:-1]) != big[-1]):
                self.big_errors.append("直通大帧 帧头/尾标/sum8 不对")
            self.enc_stats[0] += 1
            self.flash += big[1:1 + P.BLOCK_OCTETS]
            self._ack_write(ch, now)
            return
        if len(buf) < 3:                            # 长度头还没到齐
            return
        c = buf[1] | (buf[2] << 8)                  # 按契约 C 提前收口
        need = c + 5
        if len(buf) < need:
            return
        big = bytes(buf[:need])
        del self._acc[(P.CMD_WRITE, err)]
        if (big[0] != P.MARK_HEAD
                or big[3 + c] not in (P.MARK_MORE, P.MARK_LAST)
                or P.sum8(big[:-1]) != big[-1]):
            self.big_errors.append("压缩大帧 帧头/尾标/sum8 不对")
        self.enc_stats[err] += 1
        if err == 2 and c == 0:                     # 空块跳过：擦除态即内容
            self.skipped += 1
            self.flash += b"\xFF" * P.BLOCK_OCTETS
            self._ack_write(ch, now)
            return
        try:
            if err == 2:
                self.flash += lz4.decompress_dict(
                    big[3:3 + c], P.BLOCK_OCTETS, bytes(self.flash)[-65535:])
            else:
                self.flash += lz4.decompress(big[3:3 + c], P.BLOCK_OCTETS)
        except ValueError as e:
            self.big_errors.append("解压失败: %s" % e)
            return
        self._ack_write(ch, now)

    def _ack_write(self, ch, now):
        self.blocks += 1
        idx = self.blocks - 1
        self._reply(ch, P.CMD_WRITE,
                    bytes((0xA5, (idx >> 8) & 0xFF, idx & 0xFF)), now)


def bl_write_errs(ch):
    """发出的 WRITE 大帧每帧 ID 的 err 段列表。"""
    return [P.parse_id(arb)["err"] for _, arb, _ in ch.sent
            if P.parse_id(arb)["dev"] == P.DEV_MODULE
            and P.parse_id(arb)["cmd"] == P.CMD_WRITE]


def make_fw(blocks, block_fn):
    octets = b"".join(block_fn(b) for b in range(blocks))
    return types.SimpleNamespace(octets=octets, size=len(octets), fmt="hex",
                                 base_addr=0x08400, crc32=0)


def pattern_block(b):
    """确定性可压块：周期 256 的渐变 + 逐块偏移，压缩后远小于 4094。"""
    return bytes(((i + b * 37) & 0xFF) for i in range(4096))


# ------------------------------------------------------------------ 2c. 链式往返
def test_lz4_chained_roundtrip():
    print("2c) 链式字典 LZ4：多块流往返 + 跨块匹配生效 + 首块退化为独立压缩")
    rng = random.Random(7)
    shared = bytes(rng.randrange(256) for _ in range(3000))
    blocks = [
        shared + rng.randbytes(1096),
        rng.randbytes(500) + shared + rng.randbytes(596),  # 块1 的 shared 只能跨块匹配
        b"\xFF" * 4096,
        bytes((i & 0xFF) for i in range(4096)),
    ]
    chainer = lz4.LZ4Chainer()
    chained = [chainer.compress(b) for b in blocks]
    indep = [lz4.compress(b) for b in blocks]
    check("首块无历史 → 与独立压缩逐字节相同", chained[0] == indep[0])
    flash = bytearray()
    for k, (c, b) in enumerate(zip(chained, blocks)):
        d = lz4.decompress_dict(c, 4096, bytes(flash)[-65535:])
        check("链式块 %d 往返一致（%d octet）" % (k, len(c)), d == b)
        flash += b
    check("跨块匹配生效：块1 链式显著更小", len(chained[1]) < len(indep[1]) - 1000,
          "%d vs %d" % (len(chained[1]), len(indep[1])))
    check("链式总字节 ≤ 独立总字节",
          sum(map(len, chained)) <= sum(map(len, indep)),
          "%d vs %d" % (sum(map(len, chained)), sum(map(len, indep))))
    # 字典越界必须拒绝：块1 拿空字典解（链式块1 有跨块引用，off 超出块内历史）
    try:
        lz4.decompress(chained[1], 4096)
        check("链式块1 用空字典解应抛 ValueError", False, "没抛异常")
    except ValueError:
        check("链式块1 用空字典解抛 ValueError（证明确实引用了前序历史）", True)


# ------------------------------------------------------------------ 5b. 闭环：链式
def test_closed_loop_chained():
    print("5b) 闭环：能力位 0x06 的假设备 + compress=True → 全链路走算法 2 链式")
    dev = LlzFakeModule(addr=0x01, in_bootloader=True, codec="lz4d")
    api = make_api(dev)
    fw = make_fw(15, pattern_block)
    api._flash_module(0x01, fw, compress=True)
    check("模拟 Flash 与原始镜像逐字节一致", bytes(dev.flash) == fw.octets,
          "%d vs %d octet" % (len(dev.flash), len(fw.octets)))
    check("15 块全部走链式（err=2），无直通无独立块",
          dev.enc_stats == {0: 0, 1: 0, 2: 15}, str(dev.enc_stats))
    check("无空块跳过（pattern_block 无纯填充块）", dev.skipped == 0)
    check("链式大帧契约无违反记录", dev.big_errors == [], str(dev.big_errors))
    errs = set(bl_write_errs(api._ch))
    check("线上 WRITE 帧 ID err 段只出现 2（链式）", errs == {2}, str(errs))
    check("完成消息显示实际编码（LZ4 链式）",
          "LZ4 链式" in api._progress["message"], api._progress["message"])


# ------------------------------------------------------------------ 8. 闭环：跳过
def test_closed_loop_skip_fill_blocks():
    print("8) 闭环：含纯 0xFF 填充块的镜像 → 空块跳过（C=0），Flash 仍逐字节一致")

    def block_fn(b):
        if b in (2, 5):
            return b"\xFF" * 4096
        return pattern_block(b)

    dev = LlzFakeModule(addr=0x01, in_bootloader=True, codec="lz4d")
    api = make_api(dev)
    fw = make_fw(8, block_fn)
    api._flash_module(0x01, fw, compress=True)
    check("模拟 Flash 与原始镜像逐字节一致（跳过块 = 擦除态 0xFF）",
          bytes(dev.flash) == fw.octets,
          "%d vs %d octet" % (len(dev.flash), len(fw.octets)))
    check("恰好跳过 2 个纯填充块（块 2、5）", dev.skipped == 2, str(dev.skipped))
    check("8 块全部受理（6 链式 + 2 跳过都计 err=2）",
          dev.enc_stats == {0: 0, 1: 0, 2: 8}, str(dev.enc_stats))
    check("跳过大帧契约无违反记录", dev.big_errors == [], str(dev.big_errors))
    n_frames = len(api._ch.bl_cmd_frames(P.CMD_WRITE))
    check("总线帧数因跳过显著减少（< 无跳过的 3/4）",
          n_frames < 8 * 513 // 4, str(n_frames))

    print("    —— 对照组：同一镜像发给只懂算法 1 的旧 BL ——")
    dev1 = LlzFakeModule(addr=0x01, in_bootloader=True)   # codec="lz4"：只懂算法 1 的 BL V1.2.x
    api1 = make_api(dev1)
    api1._flash_module(0x01, fw, compress=True)
    check("旧 BL 回退独立块（err=1），无跳过", dev1.skipped == 0
          and dev1.enc_stats == {0: 0, 1: 8, 2: 0}, str(dev1.enc_stats))
    check("旧 BL 整链也逐字节一致", bytes(dev1.flash) == fw.octets)


# ------------------------------------------------------------------ 5. 闭环：全压缩
def test_closed_loop_compressed():
    print("5) 闭环：能力位 0x02 的假设备 + compress=True 全链路还原镜像")
    dev = LlzFakeModule(addr=0x01, in_bootloader=True)
    api = make_api(dev)
    fw = make_fw(15, pattern_block)
    api._flash_module(0x01, fw, compress=True)
    check("模拟 Flash 与原始镜像逐字节一致", bytes(dev.flash) == fw.octets,
          "%d vs %d octet" % (len(dev.flash), len(fw.octets)))
    check("15 块全部受理且全部走压缩（err=1）",
          dev.blocks == 15 and dev.enc_stats == {0: 0, 1: 15, 2: 0},
          str(dev.enc_stats))
    check("大帧契约无违反记录", dev.big_errors == [], str(dev.big_errors))
    errs = set(bl_write_errs(api._ch))
    check("线上 WRITE 帧 ID err 段只出现 1（全压缩）", errs == {1}, str(errs))
    n_llz = len(api._ch.bl_cmd_frames(P.CMD_WRITE))
    check("压缩后总线帧数 %d（15 块）" % n_llz, n_llz < 15 * 513 // 4,
          str(n_llz))
    check("完成消息显示实际编码（LZ4）",
          "（LZ4，" in api._progress["message"], api._progress["message"])

    print("    —— 对照组：同一镜像 compress=False 直通 ——")
    dev0 = LlzFakeModule(addr=0x01, in_bootloader=True)
    api0 = make_api(dev0)
    api0._flash_module(0x01, fw, compress=False)
    n_plain = len(api0._ch.bl_cmd_frames(P.CMD_WRITE))
    check("直通对照也逐字节一致", bytes(dev0.flash) == fw.octets)
    check("直通全部走 err=0，帧数 %d" % n_plain,
          dev0.enc_stats == {0: 15, 1: 0, 2: 0}, str(dev0.enc_stats))
    check("压缩帧数明显少于直通（< 1/2）", n_llz * 2 < n_plain,
          "%d vs %d" % (n_llz, n_plain))
    check("直通完成消息显示不压缩",
          "不压缩" in api0._progress["message"], api0._progress["message"])


# ------------------------------------------------------------------ 6. 闭环：能力位 0
def test_closed_loop_no_capability():
    print("6) 闭环：能力位 0x00 的旧设备 + compress=True → 明确报错且不碰扇区")
    dev = LlzFakeModule(addr=0x01, in_bootloader=True, codec=None)
    api = make_api(dev)
    fw = make_fw(3, pattern_block)
    try:
        api._flash_module(0x01, fw, compress=True)
        check("_flash_module 抛 BootloaderError", False, "没抛异常")
    except BootloaderError as e:
        check("报错含「不支持压缩」", "不支持压缩" in str(e), str(e))
        check("报错提示去关开关或先升 Bootloader",
              "压缩传输" in str(e) and "Bootloader" in str(e), str(e))
    check("报错发生在擦除之前（没有 ERASE 帧）",
          len(api._ch.bl_cmd_frames(P.CMD_ERASE)) == 0)
    check("没有 WRITE 帧、模拟 Flash 未被写", dev.blocks == 0 and not dev.flash)

    print("    —— 同一台旧设备 compress=False 照常直通 ——")
    api._flash_module(0x01, fw, compress=False)
    check("旧设备 + 开关关 = 直通整链成功", bytes(dev.flash) == fw.octets
          and dev.enc_stats == {0: 3, 1: 0, 2: 0}, str(dev.enc_stats))


# ------------------------------------------------------------------ 7. 闭环：混合
def test_closed_loop_mixed_fallback():
    print("7) 闭环：块 0 随机不可压退直通，其余压缩——整链仍逐字节一致")
    rng = random.Random(42)
    rnd_block = rng.randbytes(4096)
    c = lz4.compress(rnd_block)
    check("前提：随机块压缩后 > 4094（确实该退直通）", len(c) > 4094,
          str(len(c)))

    def block_fn(b):
        return rnd_block if b == 0 else pattern_block(b)

    dev = LlzFakeModule(addr=0x01, in_bootloader=True)
    api = make_api(dev)
    fw = make_fw(15, block_fn)
    api._flash_module(0x01, fw, compress=True)
    check("混合整链成功且 Flash 与镜像逐字节一致",
          bytes(dev.flash) == fw.octets,
          "%d vs %d octet" % (len(dev.flash), len(fw.octets)))
    check("恰好块 0 退直通、其余 14 块压缩",
          dev.enc_stats == {0: 1, 1: 14, 2: 0}, str(dev.enc_stats))
    check("混合大帧契约无违反记录", dev.big_errors == [], str(dev.big_errors))

    print("    —— 链式设备上的同一混合镜像（不可压块仍退直通 err=0）——")
    dev2 = LlzFakeModule(addr=0x01, in_bootloader=True, codec="lz4d")
    api2 = make_api(dev2)
    api2._flash_module(0x01, fw, compress=True)
    check("链式混合整链逐字节一致", bytes(dev2.flash) == fw.octets)
    check("链式混合：块 0 直通、其余 14 块链式",
          dev2.enc_stats == {0: 1, 1: 0, 2: 14}, str(dev2.enc_stats))
    check("链式混合大帧契约无违反记录", dev2.big_errors == [], str(dev2.big_errors))


# ------------------------------------------------------------------ 9. 编码选择
def test_pick_encoding():
    print("9) pick_encoding：能力位 → 最优编码")
    cases = [
        ((1, 3, 0), 0x06, P.ENC_LZ4D, "链式"),
        ((1, 4, 0), 0x06, P.ENC_LZ4D, "未来版本同样链式"),
        ((1, 2, 0), 0x02, P.ENC_LZ4,  "独立块 LZ4"),
        ((1, 0, 0), 0x00, P.ENC_DIRECT, "仅直通"),
        ((1, 3, 0), 0x02, P.ENC_LZ4,  "宏裁剪掉链式的 1.3.0 回独立块"),
    ]
    for ver, cap, want, label in cases:
        got = pick_encoding({"version": ver, "compress_flags": cap})
        check("%s：byte5=0x%02X ver=%s → %s" % (label, cap, ver, want),
              got == want, "得到 %s" % got)


def main():
    print("=" * 64)
    print("LZ4 压缩传输离线测试（不加载 ControlCAN.dll、不打开任何设备）")
    print("=" * 64)
    for fn in (test_lz4_roundtrip, test_lz4_decode_errors,
               test_write_frame_compressed_layout, test_parse_info_capability,
               test_lz4_chained_roundtrip,
               test_closed_loop_compressed, test_closed_loop_chained,
               test_closed_loop_no_capability,
               test_closed_loop_mixed_fallback, test_closed_loop_skip_fill_blocks,
               test_pick_encoding):
        fn()
    fails = [r for r in RESULTS if not r[1]]
    print("-" * 64)
    print("合计 %d 项，通过 %d，失败 %d"
          % (len(RESULTS), len(RESULTS) - len(fails), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
