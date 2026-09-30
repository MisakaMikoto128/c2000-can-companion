# -*- coding: utf-8 -*-
"""变量观察（WATCH）离线测试：符号解析、合并规划、值编解码、超时公式、假通道轮询。

运行（monitor_tui 仓库根）：
    .venv\\Scripts\\python.exe tests\\offline_watch_test.py
退出码 0 = 全部通过。

符号解析对仓库自带 demo/watch_demo.out 断言（文件不在则跳过该组）；
协议事务用假通道走 WatchSession/WatchManager 的完整轮询与写入路径，不碰设备。
"""
import os
import struct
import sys
import threading
import time
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import monitor_tui.watch as W  # noqa: E402
from monitor_tui.watch import (APP_BASE_WORD, WatchEntry, WatchError,
                               WatchManager, WatchSession, decode_scalar,
                               encode_scalar, plan_blocks, parse_symbols,
                               read_timeout_s, resolve_path)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                         ("： " + detail) if detail else ""))


REAL_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "demo", "watch_demo.out")


class FakeChannel:
    """假 CAN 通道：按 WatchSession 的帧序回 0x31/0x34/0x35；记录全部发出帧。"""

    def __init__(self, mem=None):
        self.sent = []
        self.mem = mem or {}          # 字地址 -> 字值（读返回值，写即生效）
        self.rxq = []                 # 待收帧队列（subscribe 直接挂引用）
        self.subscribes = 0           # 订阅次数（read_image 单订阅断言用）
        self.drop_next_0x30 = False   # 置位时吞掉下一帧 0x30（模拟段丢失触发重发）
        self.silent_0x35 = False      # 置位时忽略 0x35（模拟旧固件白名单外整帧丢弃）
        self._wr = None

    class _Mb:
        def __init__(self, rxq):
            self._rxq = rxq

        def get(self, timeout_s):
            return self._rxq.pop(0) if self._rxq else None

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def subscribe(self, pred=None, maxsize=256):
        self.subscribes += 1
        return self._Mb(self.rxq)

    def _mem_bytes(self, addr, words):
        octets = bytearray()
        for k in range(words):
            v = self.mem.get(addr + k, 0)
            octets += bytes((v & 0xFF, (v >> 8) & 0xFF))
        return bytes(octets)

    def send(self, arb_id, data, xtd=True):
        self.sent.append((arb_id, bytes(data)))
        cmd = (arb_id >> 16) & 0x3F
        seq = data[6] if cmd in (0x30, 0x32) else 0
        if cmd == 0x30:
            if self.drop_next_0x30:
                self.drop_next_0x30 = False   # 吞掉请求：无应答 → 上位机超时重发
                return True
            addr = int.from_bytes(data[0:4], "little")
            words = data[4]
            # 7 字节/帧切片（协议 0.2：帧 i 覆盖偏移 [7i, 7i+7)，末帧不足补 0）
            octets = self._mem_bytes(addr, words)
            for i in range(0, len(octets), 7):
                chunk = bytes(octets[i:i + 7])
                chunk += bytes(7 - len(chunk))
                self.rxq.append(_rx(0x31, bytes((seq,)) + chunk))
        elif cmd == 0x32:
            self._wr = (int.from_bytes(data[0:4], "little"), data[4], seq)
        elif cmd == 0x33:
            waddr, words, seq = self._wr
            for k in range(words):
                self.mem[waddr + k] = data[2 * k] | (data[2 * k + 1] << 8)
            self._wr = None
            reply = bytes((seq, 0)) + waddr.to_bytes(4, "little") \
                + (words * 2).to_bytes(2, "little")
            self.rxq.append(_rx(0x34, reply))
        elif cmd == 0x35:
            if self.silent_0x35:
                return True   # 旧固件：0x35 不在白名单，整帧丢弃无应答
            # 固件 0x35 的最小复刻：算 CRC 前先做与 can_dbg_crc 相同的范围校验
            addr = int.from_bytes(data[0:4], "little")
            words = data[4] | (data[5] << 8)
            if words == 0 or words > 24576:
                reply = bytes(4) + bytes((1, 0, 0, 0))
            else:
                crc = zlib.crc32(self._mem_bytes(addr, words))
                reply = crc.to_bytes(4, "little") + bytes((0, 0, 0, 0))
            self.rxq.append(_rx(0x35, reply))
        return True


def _rx(cmd, data):
    """模块→上位机应答帧（dev 0x0C，dest=0xF0，src=0x01）。"""
    from monitor_tui.protocol import CanFrame
    arb = (0x0C << 22) | (cmd << 16) | (0xF0 << 8) | 0x01
    return CanFrame(id=arb, xtd=True, dlc=8, data=data, ts=time.time())


def main():
    # ---- A. 合并规划 ----
    frags = [(0x100, 2, 1, 0), (0x102, 4, 2, 0), (0x120, 26, 3, 0), (0x300, 2, 4, 0)]
    blocks = plan_blocks(frags, 16)
    check("A1 相邻合并", len(blocks) == 3 and blocks[0][1] == 6,
          str([(b[0], b[1]) for b in blocks]))
    check("A2 超阈不并", len(plan_blocks(frags, 4)) == 3)
    check("A3 不合并", len(plan_blocks(frags, None)) == 4)
    big = plan_blocks([(0x100, 600, 1, 0)], 16)
    check("A4 超 255 字切分", [b[1] for b in big] == [255, 255, 90],
          str([b[1] for b in big]))

    # ---- B. 值编解码 ----
    f32 = {"k": "num", "w": 2, "enc": 4, "name": "float"}
    check("B1 float 往返", decode_scalar(encode_scalar("3.14", f32), 0, f32) == "3.14")
    u16 = {"k": "num", "w": 1, "enc": 7, "name": "unsigned int"}
    check("B2 u16 十六进制输入", decode_scalar(encode_scalar("0xBEEF", u16), 0, u16) == "48879")
    i16 = {"k": "num", "w": 1, "enc": 5, "name": "int"}
    check("B3 i16 负数", decode_scalar(encode_scalar("-2", i16), 0, i16) == "-2")
    try:
        encode_scalar("70000", u16)
        check("B4 范围拒绝", False)
    except ValueError:
        check("B4 范围拒绝", True)
    boolean = {"k": "bool", "w": 1}
    check("B5 bool", decode_scalar(encode_scalar("true", boolean), 0, boolean) == "true")
    enum = {"k": "enum", "w": 1, "vals": {0: "OFF", 1: "ON"}}
    check("B6 枚举名写入", decode_scalar(encode_scalar("ON", enum), 0, enum) == "ON (1)")
    e1 = WatchEntry(9, "st", {"addr": 0xB380, "words": 1, "type": enum, "writable": True})
    check("B7 枚举行带下拉值", e1.build_rows()["enumVals"] == ["OFF", "ON"])

    # ---- C. 超时公式（协议 0.6） ----
    check("C1 255 字超时", abs(read_timeout_s(255) - 0.335) < 0.01,
          "%.0fms" % (read_timeout_s(255) * 1000))
    check("C2 短读下限 200ms", read_timeout_s(8) == 0.2)

    # ---- D. 真机产物符号解析（文件存在才跑） ----
    if os.path.isfile(REAL_OUT):
        t0 = time.time()
        sf = parse_symbols(REAL_OUT)
        check("D1 解析耗时", time.time() - t0 < 3.0, "%.2fs" % (time.time() - t0))
        g = sf.symbols.get("g_demo_status")
        check("D2 g_demo_status 结构体", g is not None and g["type"]["k"] == "struct"
              and g["type"]["w"] == 8 and len(g["type"]["members"]) == 4,
              "addr=0x%X w=%d members=%d" % (g["addr"], g["words"],
                                             len(g["type"]["members"])) if g else "缺失")
        check("D3 成员字偏移", g["type"]["members"][1][1] == 2,
              str([m[1] for m in g["type"]["members"][:3]]))
        check("D4 CRC 镜像窗口", 0 < sf.image_words() <= 0x10000,
              "%d 字" % sf.image_words())
        check("D5 Flash 符号只读", all(not s["writable"] for n, s in sf.symbols.items()
                                       if 0x080000 <= s["addr"] < 0x090000))
        e = WatchEntry(1, "g_demo_status", g)
        row = e.build_rows()
        check("D6 树形行展开", len(e.nodes) == 5 and "_children" in row,
              "%d 行" % len(e.nodes))
        off = [n for n in e.nodes if n.endswith("gain")]
        check("D7 gain 成员行存在", len(off) == 1)
    else:
        print("[SKIP] D 真机产物不在本机")

    # ---- E. 假通道事务：读/写/轮询/写入/序号过滤 ----
    ch = FakeChannel()
    ch.mem = {0xB618: 0x1234, 0xB619: 0x5678}
    sess = WatchSession(ch, 0x01)
    raw = sess.read_words(0xB618, 2)
    check("E1 读事务", raw == bytes.fromhex("34127856"), raw.hex() if raw else "None")

    err, _a, n = sess.write_words(0xB630, bytes.fromhex("10515293"))
    check("E2 写事务", err == 0 and n == 4 and ch.mem.get(0xB630) == 0x5110
          and ch.mem.get(0xB631) == 0x9352)
    raw = sess.read_words(0xB618, 2)
    seqs = [d[6] for i, d in ch.sent if (i >> 16) & 0x3F in (0x30, 0x32)]
    check("E3 序号递增", seqs == sorted(seqs) and len(set(seqs)) == len(seqs),
          str(seqs))

    # 旧序号残帧污染：信箱里塞一帧旧 seq 的 0x31，新读必须丢弃它并收齐自己的
    stale = _rx(0x31, bytes((0xEE, 1, 2, 3, 4, 5, 6, 7)))
    ch.rxq.append(stale)
    raw = sess.read_words(0xB618, 2)
    check("E4 残帧过滤", raw == bytes.fromhex("34127856") and sess.stale_frames == 1)

    # ---- E5~E8. 校验路径：0x35 快路径 / 单订阅 read_image / 段重发 / 作废 ----
    old_to = W.CRC_VERIFY_TIMEOUT_S
    W.CRC_VERIFY_TIMEOUT_S = 0.05   # 无应答用例提速，不改被测逻辑
    try:
        r = sess.crc_verify(0xB618, 2)
        check("E5 0x35 快路径", r is not None and r[0] == 0
              and r[1] == zlib.crc32(bytes.fromhex("34127856")),
              str(r))
        r = sess.crc_verify(0xB618, 0)
        check("E6 0x35 范围非法", r == (1, 0), str(r))
        n_sent = sum(1 for i, _ in ch.sent if (i >> 16) & 0x3F == 0x35)
        ch.silent_0x35 = True   # 旧固件无 0x35：超时回 None，调用方回退逐段读
        r = sess.crc_verify(0xB618, 2)
        check("E7 0x35 无应答回 None（回退信号）",
              r is None and sum(1 for i, _ in ch.sent if (i >> 16) & 0x3F == 0x35) == n_sent + 1,
              "请求已发出、无应答")
        ch.silent_0x35 = False
    finally:
        W.CRC_VERIFY_TIMEOUT_S = old_to

    ch3 = FakeChannel()
    ch3.mem = {APP_BASE_WORD + i: (i * 7 + 1) & 0xFFFF for i in range(300)}
    sess3 = WatchSession(ch3, 0x01)
    img = sess3.read_image(300)
    check("E8 read_image 单订阅拼回", img == ch3._mem_bytes(APP_BASE_WORD, 300)
          and ch3.subscribes == 1,
          "订阅 %d 次" % ch3.subscribes)
    n_req = sess3.requests
    try:
        sess3.read_image(300, alive=lambda: False)
        check("E9 作废守卫", False, "未抛 WatchError")
    except WatchError:
        check("E9 作废守卫（校验中断开停止后续段）",
              sess3.requests == n_req + 1, "req=%d" % sess3.requests)

    ch4 = FakeChannel()
    ch4.mem = dict(ch3.mem)
    ch4.drop_next_0x30 = True   # 吞掉段 1 第一次请求 → 超时退避重发
    sess4 = WatchSession(ch4, 0x01)
    img4 = sess4.read_image(300)
    check("E10 段超时退避重发", img4 == ch3._mem_bytes(APP_BASE_WORD, 300)
          and sess4.requests == 3 and sess4.timeouts == 1,
          "req=%d to=%d" % (sess4.requests, sess4.timeouts))

    # ---- F. WatchManager 轮询（离线全链路） ----
    ch2 = FakeChannel()
    ch2.mem = {0xB618 + i: (i * 257) for i in range(26)}
    mgr = WatchManager.__new__(WatchManager)   # 不经 HostAPI，手动装配最小状态
    mgr._host = type("H", (), {"_log": lambda s, m: None, "_bump": lambda s: None})()
    mgr._lock = threading.RLock()
    mgr._symfile = type("SF", (), {"symbols": {}, "path": "fake.out",
                                   "fmt": "EABI"})()
    mgr._st = "ready"
    mgr._msg = ""
    mgr._addr = 0x01
    mgr._sess = WatchSession(ch2, 0x01)
    mgr._entries = []
    mgr._next_eid = 1
    mgr._poll_ms = 50
    mgr._eff_poll_ms = 50
    mgr._merge = True
    mgr._snapshot = False
    mgr._gap = 16
    mgr._running = False
    mgr._stop = threading.Event()
    mgr._thread = None
    mgr._verify_prog = 0.0
    mgr._last_rows = None
    mgr._est_fps = 0
    mgr._toast = ""
    mgr._toast_seq = 0
    mgr._wave_pins = []
    mgr._wave_hist = {}
    mgr._wave_t0 = None
    sym = {"addr": 0xB618, "words": 2,
           "type": {"k": "num", "w": 1, "enc": 7, "name": "unsigned int"},
           "writable": True}
    mgr._symfile.symbols["counter"] = sym
    r = mgr.add("counter")
    check("F1 添加变量", r["success"] and r["row"]["id"] == "e1",
          r.get("message", ""))
    mgr.start_poll()
    time.sleep(0.3)
    mgr.stop_poll(None)
    rows = mgr._last_rows or []
    check("F2 轮询出值", rows and rows[0]["id"] == "e1"
          and rows[0]["val"] == str(ch2.mem[0xB618]),
          rows[0]["val"] if rows else "无")
    check("F3 快照统计", mgr.snapshot()["stats"]["req"] >= 3,
          str(mgr.snapshot()["stats"]))
    snap = mgr.snapshot()
    check("F4 预算帧率", snap["fps"] > 0 and snap["fps"] < 300, str(snap["fps"]))

    # ---- F5. 升级完成提醒：ready 态推一次性 toast，其余态不推 ----
    mgr.notify_firmware_updated()
    snap = mgr.snapshot()
    ok = (snap["toast_seq"] == 1 and "固件已更新" in snap["toast"])
    mgr._st = "file"
    mgr.notify_firmware_updated()
    snap = mgr.snapshot()
    check("F5 升级完成提醒", ok and snap["toast_seq"] == 1,
          "seq=%d" % snap["toast_seq"])
    mgr._st = "ready"

    # ---- F6. 符号缺失：轮询跳过、_replan 预算剔除、右键移除对表达式行是 no-op ----
    mgr._entries[0].set_missing(True)
    mgr._replan()
    check("F6 缺失剔除预算", mgr._per_tick == 0 and mgr._est_fps == 0,
          "per_tick=%d" % mgr._per_tick)
    n_sent = len(ch2.sent)
    check("F7 缺失跳过读", mgr._tick() is True and len(ch2.sent) == n_sent,
          "无总线动作")
    mgr._entries[0].set_missing(False)
    mgr._replan()
    r = mgr.remove("x1")   # 表达式行 rowid 不匹配任何变量根：no-op 成功
    check("F8 表达式行移除 no-op", r["success"] and len(mgr._entries) == 1,
          str(r))
    mgr.stop_poll(None)

    # ---- G. 换符号文件重映射：同名重绑 / 缺失标记 / 行数据带 missing 字段 ----
    sym_v2 = {"addr": 0xB700, "words": 3,
              "type": {"k": "num", "w": 2, "enc": 5, "name": "int"},
              "writable": True}
    e = WatchEntry(7, "counter", sym)
    row = e.build_rows()
    e.last_vals = {"e7": "99"}
    e.rebind(sym_v2)
    check("G1 rebind 更新地址类型宽度", e.addr == 0xB700 and e.words == 3
          and e.type["w"] == 2 and e.last_vals == {},
          "addr=0x%X w=%d" % (e.addr, e.words))
    check("G2 rebind 重建行", row["id"] == "e7" and e.rows[0]["addr"] == "0xB700"
          and e.rows[0]["missing"] is False and len(e.nodes) == 1, str(e.nodes))
    e.set_missing(True)
    check("G3 缺失标记到行", e.missing is True and e.rows[0]["missing"] is True)
    e.set_missing(False)
    check("G4 解除缺失", e.rows[0]["missing"] is False)

    # ---- H. 成员路径解析（点运算符） ----
    st_t = {"k": "struct", "w": 4, "members": [
        ["a", 0, {"k": "num", "w": 1, "enc": 7, "name": "unsigned int"}],
        ["b", 1, f32],
        ["inner", 3, {"k": "struct", "w": 1, "members": [
            ["c", 0, {"k": "num", "w": 1, "enc": 7, "name": "unsigned int"}]]}]]}
    un_t = {"k": "union", "w": 2, "members": [["u1", 0, f32], ["u2", 0, u16]]}
    starr_t = {"k": "array", "w": 12, "n": 3, "elem": st_t}
    syms = {
        "obj": {"addr": 0xB600, "words": 4, "type": st_t, "writable": True},
        "un": {"addr": 0xB610, "words": 2, "type": un_t, "writable": True},
        "sarr": {"addr": 0xB620, "words": 12, "type": starr_t, "writable": False},
        "scalar": {"addr": 0xB640, "words": 1,
                   "type": {"k": "num", "w": 1, "enc": 7, "name": "unsigned int"},
                   "writable": True},
    }
    check("H1 顶层直查", resolve_path(syms, "scalar")["addr"] == 0xB640)
    p = resolve_path(syms, "obj.b")
    check("H2 成员偏移", p is not None and p["addr"] == 0xB601 and p["words"] == 2
          and p["writable"] is True, "0x%X" % p["addr"] if p else "None")
    p = resolve_path(syms, "obj.inner.c")
    check("H3 嵌套成员", p is not None and p["addr"] == 0xB603,
          "0x%X" % p["addr"] if p else "None")
    p = resolve_path(syms, "sarr[1].b")
    check("H4 数组下标+成员", p is not None and p["addr"] == 0xB625
          and p["writable"] is False, "0x%X" % p["addr"] if p else "None")
    p = resolve_path(syms, "un.u2")
    check("H5 union 成员零偏移", p is not None and p["addr"] == 0xB610,
          "0x%X" % p["addr"] if p else "None")
    check("H6 非法路径拒绝", all(resolve_path(syms, n) is None for n in
          ("nope.x", "obj.zz", "obj.", "obj.a.x", "obj[0]", "sarr[3].a", "")),
          "")
    p = resolve_path(syms, "obj.b")
    e9 = WatchEntry(9, "obj.b", p)
    row = e9.build_rows()
    check("H7 路径条目建行", row["id"] == "e9" and row["name"] == "obj.b"
          and row["addr"] == "0xB601" and row["editable"] is True
          and row["words"] == 2, str(row)[:100])

    # ---- I. 路径观察 + 期望/实际周期分离（走 WatchManager 公开接口） ----
    mgr._symfile.symbols["obj"] = {"addr": 0xB600, "words": 4, "type": st_t,
                                   "writable": True}
    r = mgr.add("obj.b")
    check("I1 路径添加", r["success"] and r["row"]["id"] == "e2"
          and r["row"]["addr"] == "0xB601", r.get("message", ""))
    check("I2 路径重复添加拒绝", not mgr.add("obj.b")["success"])
    check("I3 未知路径拒绝", not mgr.add("obj.zz")["success"])
    arr100 = {"k": "array", "w": 100, "n": 100,
              "elem": {"k": "num", "w": 1, "enc": 7, "name": "unsigned int"}}
    mgr._symfile.symbols["bigarr"] = {"addr": 0xB800, "words": 100,
                                      "type": arr100, "writable": True}
    mgr.add("bigarr")
    mgr.set_options(poll_ms=50)
    snap = mgr.snapshot()
    exp_eff = -(-mgr._per_tick * 1000 // 300)
    check("I4 超预算放大实际周期", snap["poll_ms"] == 50 and snap["eff_ms"] == exp_eff
          and exp_eff > 50 and snap["fps"] <= 300,
          "eff=%d fps=%d" % (snap["eff_ms"], snap["fps"]))
    mgr.remove("e3")   # bigarr 移除：实际周期随变量列表变化回缩
    snap = mgr.snapshot()
    check("I5 移除后实际周期回缩", snap["poll_ms"] == 50 and snap["eff_ms"] == 50
          and snap["fps"] <= 300, "eff=%d fps=%d" % (snap["eff_ms"], snap["fps"]))

    # ---- J. 浮点全精度 raw + load_file 回传路径 ----
    e11 = WatchEntry(11, "fv", {"addr": 0xB600, "words": 2, "type": f32,
                                "writable": True})
    e11.build_rows()
    vals, raws = e11.decode(struct.pack("<f", 12.9984))
    check("J1 浮点 raw 全精度", vals["e11"] == "12.9984"
          and raws.get("e11", "").startswith("12.9983997"),
          "%s / %s" % (vals.get("e11"), raws.get("e11")))
    vals, raws = e11.decode(struct.pack("<f", 0.5))
    check("J2 raw 与显示相同则不记", vals["e11"] == "0.5" and "e11" not in raws,
          str(raws))
    if os.path.isfile(REAL_OUT):
        mgr2 = WatchManager.__new__(WatchManager)
        mgr2._host = type("H", (), {"_log": lambda s, m: None,
                                    "_bump": lambda s: None})()
        mgr2._lock = threading.RLock()
        mgr2._symfile = None
        mgr2._st = "idle"
        mgr2._msg = ""
        mgr2._entries = []
        mgr2._next_eid = 1
        mgr2._running = False
        mgr2._thread = None
        mgr2._stop = threading.Event()
        mgr2._sess = None
        mgr2._addr = None
        mgr2._last_rows = None
        r = mgr2.load_file(REAL_OUT)
        check("J3 load_file 回传路径", r.get("success") and r.get("path") == REAL_OUT,
              str(r.get("path")))

    # ---- K. 波形环形缓冲 ----
    r = mgr.wave_pins(["e1", "e2", "e99"])
    check("K1 通道登记过滤", r["pins"] == ["e1", "e2"], str(r["pins"]))
    mgr._tick()
    d = mgr.wave_data()
    check("K2 记录首点", set(d) == {"e1", "e2"}
          and len(d["e1"]["pts"]) == 1 and d["e1"]["pts"][0][0] == 0.0
          and d["e1"]["name"] == "counter",
          str(d))
    time.sleep(0.005)   # 真实轮询间隔 ≥50ms，两 tick 不会落在同一毫秒
    mgr._tick()
    d = mgr.wave_data()
    check("K3 逐 tick 追加", len(d["e1"]["pts"]) == 2
          and d["e1"]["pts"][1][0] > d["e1"]["pts"][0][0], str(d["e1"]["pts"]))
    mgr.wave_clear()
    d = mgr.wave_data()
    check("K4 清除缓冲", d["e1"]["pts"] == [] and d["e2"]["pts"] == [])
    mgr.remove("e1")
    d = mgr.wave_data()
    check("K5 移除变量剔除通道", set(d) == {"e2"}, str(list(d)))

    # ---- L. 清单独立于符号文件：idle 态添加未解析条目，载入后解析 ----
    ch5 = FakeChannel()
    mgr5 = WatchManager.__new__(WatchManager)
    mgr5._host = type("H", (), {"_log": lambda s, m: None, "_bump": lambda s: None})()
    mgr5._lock = threading.RLock()
    mgr5._symfile = None
    mgr5._st = "idle"
    mgr5._msg = ""
    mgr5._addr = None
    mgr5._sess = None
    mgr5._entries = []
    mgr5._next_eid = 1
    mgr5._poll_ms = 100
    mgr5._eff_poll_ms = 100
    mgr5._merge = True
    mgr5._snapshot = False
    mgr5._gap = 16
    mgr5._running = False
    mgr5._thread = None
    mgr5._stop = threading.Event()
    mgr5._verify_prog = 0.0
    mgr5._last_rows = None
    mgr5._est_fps = 0
    mgr5._per_tick = 0
    mgr5._toast = ""
    mgr5._toast_seq = 0
    mgr5._wave_pins = []
    mgr5._wave_hist = {}
    mgr5._wave_t0 = None
    r = mgr5.add("g_demo_status")
    check("L1 idle 态添加为未解析", r["success"] and r["row"]["missing"] is True
          and r["row"]["addr"] == "—" and r["row"]["words"] == "—",
          str(r.get("message", "")))
    check("L2 重复添加拒绝", not mgr5.add("g_demo_status")["success"])
    r = mgr5.add("no_such_sym")
    check("L3 未知名同样登记待解析", r["success"] and r["row"]["missing"] is True)
    check("L4 未解析不产生读请求", mgr5._per_tick == 0, str(mgr5._per_tick))
    if os.path.isfile(REAL_OUT):
        r = mgr5.load_file(REAL_OUT)
        ents = {e.name: e for e in mgr5._entries}
        check("L5 载入后清单自动解析", ents["g_demo_status"].missing is False
              and ents["g_demo_status"].addr > 0
              and ents["g_demo_status"].rows[0]["missing"] is False,
              str(ents["g_demo_status"].rows[0])[:80])
        check("L6 清单内未知名保持缺失标注", ents["no_such_sym"].missing is True
              and ents["no_such_sym"].rows[0]["addr"] == "—")
        check("L7 已载入后加未知名拒绝", not mgr5.add("no_such_sym")["success"])

    print("\n%d/%d 通过%s" % (len(RESULTS) - sum(1 for _, ok in RESULTS if not ok),
                             len(RESULTS),
                             "" if all(ok for _, ok in RESULTS) else "（有失败）"))
    return 0 if all(ok for _, ok in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
