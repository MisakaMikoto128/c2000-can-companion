# -*- coding: utf-8 -*-
"""升级探测与状态机的离线测试：全程不碰任何 CAN 设备。

运行（monitor_tui 仓库根）：
    .venv\\Scripts\\python.exe tests\\offline_upgrade_test.py
退出码 0 = 全部通过。

设备侧用 FakeChannel + FakeModule 替身：send 记录发出的帧；应答帧按到期时刻暂存，
虚拟时钟推进时投递给全部订阅信箱（与真机接收线程按订阅分发同构，架构 V2）。
HostAPI 因此可以在没有 USBCAN2 的机器上跑完整条升级状态机。
"""
import os
import sys
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from monitor_tui.host_app import HostAPI, UpgradeCancelled  # noqa: E402
from monitor_tui.protocol import (CAN_ADDR_HOST, CMD_ENTER_BL, CMD_PROBE,
                                  OWN_DEV, CanFrame, decode_probe,
                                  encode_enter_bl, encode_probe_query, own_id)
from monitor_tui.upgrade import bl_protocol as P  # noqa: E402
from monitor_tui import lz4  # noqa: E402
from monitor_tui.upgrade.bl_client import BootloaderError, FlashBootloader  # noqa: E402
from monitor_tui.upgrade.probe import (PROBE_TICK_S, DeviceProbe,  # noqa: E402
                                       bl_probe_addr, no_device_hint)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  <- " + detail) if (detail and not ok) else ""),
          flush=True)


class FakeClock:
    """虚拟时钟：调用可取值，advance 推进；推进时回调 on_advance（总线借此投递到期帧）。"""

    def __init__(self, t=1000.0):
        self.now = t
        self.on_advance = []

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += max(0.0, seconds)
        for cb in list(self.on_advance):
            cb()


class FakeModule:
    """一台模块的替身：在 App 时答 0x20 状态查询（B1=0x01）；收到单播 0x06 先受理，
    延时 0.5 秒（实测交接延时）后复位进 Bootloader。
    主机复查总在停机窗之后（真实时序 sleep 0.6s > 停机 0.5s），事件域里建模为
    「受理后收到的下一个 0x20 查询到来时已在 Bootloader」。
    reaches_bootloader=False 模拟「受理了但没复位成功」（停在保护里）。
    answer_probe=False 模拟「固件过旧不认识 0x20」：状态帧照发，查询不应答。"""

    def __init__(self, addr=0x01, version=(1, 2, 3), enter_bl_supported=True,
                 in_bootloader=False, status_period_s=0.3, shutdown_s=0.5,
                 reaches_bootloader=True, answer_probe=True):
        self.addr = addr
        self.version = version
        self.enter_bl_supported = enter_bl_supported
        self.in_bootloader = in_bootloader
        self.status_period_s = status_period_s
        self.shutdown_s = shutdown_s
        self.reaches_bootloader = reaches_bootloader
        self.answer_probe = answer_probe
        self.accepted = False          # 已经收到过升级请求
        self._reset_due = None         # 停机窗到点时刻（虚拟时钟）
        self.blocks = 0                # 已写入的块数
        self.written = {}              # 块号 → True（VERIFY 覆盖裁决用）
        self._acc = {}
        self._last_status = None

    def _tick_status(self, ch, now):
        """App 侧周期任务占位：演示固件没有主动上报帧，保持空实现。"""
        return

    def on_send(self, ch, arb_id, data, now):
        if (self.accepted and self.reaches_bootloader and not self.in_bootloader
                and self._reset_due is not None and now >= self._reset_due):
            self.in_bootloader = True      # 停机窗到点，看门狗复位进 Bootloader
        if not self.in_bootloader and not self.accepted:
            self._tick_status(ch, now)
        f = P.parse_id(arb_id)
        # dev 0x0C 统一段内按 cmd 分流：0x20 状态查询两侧都认（App/BL 都答），
        # 0x21~0x27 是 Bootloader 升级帧（仅 BL 受理），其余（0x01~0x07）是 App 侧命令
        if f["dev"] != P.DEV_MODULE:
            return
        if (self.accepted and self.reaches_bootloader and not self.in_bootloader
                and f["cmd"] == P.CMD_PROBE):
            # 受理后主机的复查 0x20 到来时停机窗已过：此时是 Bootloader 在应答
            self.in_bootloader = True
        if f["cmd"] == P.CMD_PROBE:
            # 状态查询：在 App 答 App 侧，在 BL 答 BL 侧（应答 byte1 区分）
            if self.in_bootloader:
                self._on_bl_frame(ch, f["cmd"], data, now, err=f["err"])
            else:
                self._on_app_frame(ch, f["cmd"], f["dest"], data, now)
            return
        if 0x21 <= f["cmd"] <= 0x27:
            if not self.in_bootloader:
                return
            self._on_bl_frame(ch, f["cmd"], data, now, err=f["err"])
            return
        self._on_app_frame(ch, f["cmd"], f["dest"], data, now)

    def _on_app_frame(self, ch, cmd, dest, data, now):
        if self.in_bootloader:
            return
        if cmd == CMD_PROBE and self.answer_probe:
            # App 侧应答 0x20：B1 恒为 0x01（受理后不应答的是 BL——见 on_send 的换侧）
            ch.push(now, own_id(CMD_PROBE, dest=CAN_ADDR_HOST, src=self.addr),
                    bytes((CMD_PROBE, 0x01, self.addr) + tuple(self.version) + (0, 0)))
        elif cmd == CMD_ENTER_BL and self.enter_bl_supported:
            if dest == self.addr:
                self.accepted = True
                self._reset_due = now + self.shutdown_s   # 到点才真的进 Bootloader

    def _on_bl_frame(self, ch, cmd, data, now, err=0):
        if err != 0 and cmd != P.CMD_WRITE:
            return   # 与真实 BL 一致：非 WRITE 命令的 err 段非 0 一律忽略
        totals = {P.CMD_PROBE: 4, P.CMD_ERASE: 8, P.CMD_VERIFY: 13,
                  P.CMD_WRITE: 4099, P.CMD_READ: 7}
        if cmd == P.CMD_WRITE and err == 1:
            # 压缩块（算法 1）：[5A][C_lo][C_hi][C 字节][尾标][sum8]，按 C 收满即 ACK
            buf = self._acc.setdefault(("W", 1), bytearray())
            buf += data
            if len(buf) >= 3:
                clen = buf[1] | (buf[2] << 8)
                if len(buf) >= clen + 5:
                    del self._acc[("W", 1)]
                    self.blocks += 1
                    idx = self.blocks - 1
                    self.written[idx] = True
                    self._reply(ch, cmd, bytes((0xA5, (idx >> 8) & 0xFF, idx & 0xFF)), now)
            return
        if cmd in totals:
            buf = self._acc.setdefault(cmd, bytearray())
            buf += data
            if len(buf) < totals[cmd]:
                return
            del self._acc[cmd]
            payload = bytes(buf[:totals[cmd]])
            if cmd == P.CMD_PROBE:
                # 状态查询（App/BL 都认）：B0=0x20、B1=0x02 BL、B2=地址、B3-5=BL 版本
                self._reply(ch, cmd, bytes((P.CMD_PROBE, 0x02, self.addr, 1, 2, 1)), now)
                return
            if cmd == P.CMD_WRITE:
                self.blocks += 1
                idx = self.blocks - 1
                self.written[idx] = True
                self._reply(ch, cmd, bytes((0xA5, (idx >> 8) & 0xFF, idx & 0xFF)), now)
                return
            if cmd == P.CMD_VERIFY:
                # 与真实 BL 同语义：VERIFY 按"校验范围是否全部已写"裁决——
                # 未写满不应答（verify-first 探测空 flash 必然超时转正常流程）
                start_w = (payload[0] << 24) | (payload[1] << 16) | (payload[2] << 8) | payload[3]
                wc = (payload[4] << 24) | (payload[5] << 16) | (payload[6] << 8) | payload[7]
                first_blk = start_w * 2 // P.BLOCK_OCTETS
                need = (wc * 2 + P.BLOCK_OCTETS - 1) // P.BLOCK_OCTETS
                if all((first_blk + k) in self.written for k in range(need)):
                    self._reply(ch, cmd, b"", now)
                return
            self._reply(ch, cmd, b"", now)
            return
        if cmd in (P.CMD_RUN, P.CMD_RESET, P.CMD_INFO):
            if cmd == P.CMD_INFO:
                self._reply(ch, cmd, bytes((0, 9, 1, 16, 13, 2, 0)), now)
            else:
                self._reply(ch, cmd, b"", now)

    def _reply(self, ch, cmd, payload, now):
        data = bytearray(payload)
        data.append(P.sum8(payload))
        data += bytearray(8 - len(data))
        ch.push(now, P.build_id(cmd, P.HOST_ADDR, src=self.addr), bytes(data))


class FakeMailbox:
    """与 upgrade.can_channel.Mailbox 同接口的替身：get 用推进虚拟时钟代替真实等待。"""

    def __init__(self, ch, pred):
        self._ch = ch
        self._pred = pred
        self._buf = []
        self._closed = False

    def _offer(self, frame):
        if not self._closed and (self._pred is None or self._pred(frame)):
            self._buf.append(frame)

    def get(self, timeout_s):
        deadline = self._ch.clock.now + max(0.0, timeout_s)
        while True:
            if self._buf:
                return self._buf.pop(0)
            if self._ch.clock.now >= deadline - 1e-9:
                return None
            # 推进到下一帧到期时刻或截止；投递在 advance 的 on_advance 回调里完成
            nxt = self._ch.rx[0][0] if self._ch.rx else deadline
            self._ch.clock.advance(min(nxt, deadline) - self._ch.clock.now)

    def close(self):
        self._closed = True
        if self in self._ch._mailboxes:
            self._ch._mailboxes.remove(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
        return False


class FakeChannel:
    """CAN 通道替身：与 CanChannel 同接口（send/send_many/subscribe/rx_count），不碰设备。

    帧调度：push 把帧按到期时刻暂存；虚拟时钟推进时把到期帧投递给全部订阅信箱
    （挂在时钟的 on_advance 上）——与真机接收线程按订阅分发同构（架构 V2）。"""

    def __init__(self, clock=None, device=None):
        self.clock = clock or FakeClock()
        self.device = device
        self.rx = []                  # (时刻, CanFrame)，按插入顺序取
        self.sent = []                # (时刻, 仲裁 ID, bytes)
        self.rx_count = 0
        self.tx_ok = 0
        self.tx_fail = 0
        self._mailboxes = []
        self.clock.on_advance.append(self._deliver_due)

    def push(self, at, arb_id, data, xtd=True):
        frame = CanFrame(id=arb_id, xtd=xtd, dlc=len(data), data=bytes(data), ts=at)
        for i, (due, _) in enumerate(self.rx):
            if due > at:
                self.rx.insert(i, (at, frame))
                break
        else:
            self.rx.append((at, frame))
        self.rx_count += 1

    def _deliver_due(self):
        while self.rx and self.rx[0][0] <= self.clock.now:
            _, frame = self.rx.pop(0)
            for mb in list(self._mailboxes):
                mb._offer(frame)

    def send(self, arb_id, data, xtd=True):
        self.sent.append((self.clock.now, arb_id, bytes(data)))
        self.tx_ok += 1
        if self.device is not None:
            self.device.on_send(self, arb_id, bytes(data), self.clock.now)
        return True

    def send_many(self, arb_id, chunks, xtd=True, retry_wait_s=0.0):
        """逐帧走 send，好让假模块继续按原来的顺序收到每一帧。"""
        n = 0
        for c in chunks:
            if self.send(arb_id, c, xtd):
                n += 1
        return n

    def subscribe(self, pred=None, maxsize=256):
        mb = FakeMailbox(self, pred)
        self._mailboxes.append(mb)
        return mb

    def close(self):
        pass

    def sent_own_cmd(self, cmd):
        """按自有协议 cmd 号取发出的帧：[(时刻, ID, 数据), ...]。"""
        return [(at, arb_id, data) for at, arb_id, data in self.sent
                if ((arb_id >> 22) & 0x0F) == OWN_DEV
                and ((arb_id >> 16) & 0x3F) == cmd]

    def bl_cmd_frames(self, cmd):
        return [(at, data) for at, arb_id, data in self.sent
                if P.parse_id(arb_id)["dev"] == P.DEV_MODULE
                and P.parse_id(arb_id)["cmd"] == cmd]


def probe_frames(ch):
    """发出的 Bootloader 探测广播帧（含被探测请求载荷）。"""
    return [(at, data) for at, arb_id, data in ch.sent
            if arb_id == P.build_id(P.CMD_PROBE, P.BROADCAST)]


def make_api(device=None, clock=None, blocks=15):
    """装好假通道与假固件的 HostAPI：不 connect，因此绝不打开设备。"""
    api = HostAPI()
    api._ch = FakeChannel(clock=clock or FakeClock(), device=device)
    api._probe_clock = api._ch.clock   # 探测走虚拟时钟，秒级窗口不真等
    api._fw = types.SimpleNamespace(octets=b"\xEE" * (blocks * P.BLOCK_OCTETS),
                                    size=blocks * P.BLOCK_OCTETS, fmt="hex",
                                    base_addr=0x08400, crc32=0)
    return api


def wait_until(pred, timeout_s=8.0):
    end = time.time() + timeout_s
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


# ------------------------------------------------------------------ 1. 探测：App
def test_probe_finds_app():
    print("1) 探测窗口内先收到状态帧再收到信息应答 → 判定 App")
    clk = FakeClock()
    dev = FakeModule(addr=0x07, version=(1, 0, 0))
    ch = FakeChannel(clock=clk, device=dev)
    res = DeviceProbe(ch, clock=clk).scan(20.0)
    check("判定为 App", res.state == "app", res.state)
    check("记下模块地址 0x07", res.app is not None and res.app.addr == 0x07,
          repr(res.app))
    check("记下 App 版本 1.0.0", res.app is not None and res.app.version == (1, 0, 0),
          repr(res.app.version if res.app else None))
    check("判定后即结束，没用满 20 秒", res.waited_s < 1.0, "%.3f" % res.waited_s)
    check("Bootloader 探测与信息查询同拍发出",
          len(probe_frames(ch)) >= 1 and len(ch.sent_own_cmd(CMD_PROBE)) ==
          len(probe_frames(ch)),
          "%d / %d" % (len(probe_frames(ch)), len(ch.sent_own_cmd(CMD_PROBE))))


# ------------------------------------------------------------------ 2. 探测：BL
def test_probe_finds_bootloader():
    print("2) 探测窗口内收到 Bootloader 应答 → 判定 BL 并给出地址")
    clk = FakeClock()
    dev = FakeModule(addr=0x03, in_bootloader=True)
    ch = FakeChannel(clock=clk, device=dev)
    res = DeviceProbe(ch, clock=clk).scan(20.0)
    check("判定为 Bootloader", res.state == "bl", res.state)
    check("返回模块地址列表 [0x03]", res.hit_addrs == [0x03], repr(res.hit_addrs))
    check("没把在 Bootloader 的设备说成 App", res.app is None)
    check("应答帧解析出源地址",
          bl_probe_addr(CanFrame(id=P.build_id(P.CMD_PROBE, P.HOST_ADDR, src=0x03),
                                 xtd=True, dlc=8,
                                 # 新布局：B0=0x20 回显、B1=0x02 BL 侧、B2=地址、B3-5 版本
                                 data=bytes((P.CMD_PROBE, 0x02, 0x03, 1, 2, 1, 0, 0)))) == 0x03)


# ------------------------------------------------------------------ 3. 探测超时
def test_probe_timeout_message():
    print("3) 窗口内什么都没有 → 超时，且文案不下武断结论")
    clk = FakeClock()
    ch = FakeChannel(clock=clk)
    res = DeviceProbe(ch, clock=clk).scan(10.0)
    check("判定为超时", res.state == "timeout", res.state)
    check("等满了窗口才报超时", 10.0 <= res.waited_s <= 10.16,
          "%.3f" % res.waited_s)
    hint = no_device_hint(res, 10.0, ch.rx_count)
    for banned in ("离线", "硬件故障", "坏了"):
        check("超时文案不含「%s」" % banned, banned not in hint, hint)
    check("超时文案给出排查动作",
          "USBCAN2" in hint and "上电" in hint and "125 kbps" in hint, hint)

    api = HostAPI()
    api._ch = FakeChannel()
    try:
        api._acquire_bootloader(None, 10.0)
        check("超时会抛错", False, "没抛异常")
    except BootloaderError as e:
        check("超时报错文案含「设备无应答」", "设备无应答" in str(e), str(e))
        check("超时报错文案不含「离线」", "离线" not in str(e), str(e))


# ------------------------------------------------------------------ 4. 确认→单播
def test_confirm_sends_unicast_enter_bl():
    print("4) App 分支确认后：0x43 必须单播、DLC=8、byte7 是目标地址")
    dev = FakeModule(addr=0x02, version=(1, 0, 0))
    api = make_api(dev)
    flashed = []
    api._flash_module = lambda addr, fw, compress=False, keep_blocks=None, erase_mask=None, skip_reason=None: flashed.append(addr)  # 擦写由真机验证，这里只走状态机
    r = api.start_upgrade("0x02", 20)
    check("start_upgrade 受理", r["success"], str(r))
    check("出现等待确认状态",
          wait_until(lambda: api.get_progress()["confirm"] is not None),
          str(api.get_progress()))
    p = api.get_progress()
    check("确认信息带地址 0x02", p["confirm"]["addr"] == "0x02", str(p["confirm"]))
    check("确认信息带 App 版本 1.0.0", p["confirm"]["version"] == "1.0.0",
          str(p["confirm"]))
    check("确认文案说清后果（复位、进入 Bootloader）",
          all(k in p["confirm"]["text"] for k in ("复位", "Bootloader")),
          p["confirm"]["text"])
    r = api.confirm_upgrade(True)
    check("答复被受理", r["success"], str(r))
    done = wait_until(lambda: not api.get_progress()["running"], timeout_s=15.0)
    p = api.get_progress()
    check("流程走完后 running 复位", done and not p["running"], str(p))
    check("确认后进入擦写阶段，目标是被确认的模块", flashed == [0x02], str(flashed))
    frames = api._ch.sent_own_cmd(CMD_ENTER_BL)
    check("恰好发出一次升级请求", len(frames) == 1, str(len(frames)))
    if frames:
        arb, data = frames[0][1], frames[0][2]
        check("升级请求 DLC=8", len(data) == 8, str(len(data)))
        check("升级请求是 0x06 cmd", ((arb >> 16) & 0x3F) == CMD_ENTER_BL)
        check("升级请求 ID dest 是目标地址 0x02", ((arb >> 8) & 0xFF) == 0x02)
        check("升级请求不是广播（ID dest != 0x3F）", ((arb >> 8) & 0xFF) != 0x3F)
        check("升级请求走自有协议 dev 0x0E 段", ((arb >> 22) & 0x0F) == OWN_DEV)
    check("模块确已转入 Bootloader 并被探测到", dev.in_bootloader)


# ------------------------------------------------------------------ 5. 用户取消
def test_decline_exits_clean():
    print("5) App 分支取消：不发 0x43，状态干净退出")
    dev = FakeModule(addr=0x02, version=(1, 0, 0))
    api = make_api(dev)
    api._flash_module = lambda addr, fw, compress=False: None
    api.start_upgrade("0x02", 20)
    check("出现等待确认状态",
          wait_until(lambda: api.get_progress()["confirm"] is not None))
    r = api.confirm_upgrade(False)
    check("拒绝被受理", r["success"], str(r))
    check("流程干净结束", wait_until(lambda: not api.get_progress()["running"], 5.0))
    p = api.get_progress()
    check("没有发出升级请求", api._ch.sent_own_cmd(CMD_ENTER_BL) == [])
    check("设备仍留在 App（未转 Bootloader）", not dev.in_bootloader)
    check("结束状态标记为取消", p["cancelled"] is True and p["success"] is False, str(p))
    check("结束后不再等待确认", p["confirm"] is None)
    check("取消后没把目标加进模块列表",
          "0x%02X" % dev.addr not in api._modules, repr(api._modules))


# ------------------------------------------------------------------ 6. 节拍
def test_probe_tick():
    print("6) 探测节拍约 150ms（虚拟时钟断言发送次数与间隔）")
    clk = FakeClock()
    ch = FakeChannel(clock=clk)
    res = DeviceProbe(ch, clock=clk, tick_s=PROBE_TICK_S).scan(10.0)
    times = [at for at, _ in probe_frames(ch)]
    check("静默窗口内持续重发（不是只发一次）", len(times) >= 60, str(len(times)))
    # 每拍同 ID 两帧（BL 探测广播 + 自有 0x20 查询，dest 都是 0x3F）：按时刻去重再算间隔
    ticks = sorted(set(times))
    gaps = [round(b - a, 6) for a, b in zip(ticks, ticks[1:])]
    check("相邻两拍间隔等于节拍 %g 秒" % PROBE_TICK_S,
          all(abs(g - PROBE_TICK_S) < 1e-6 for g in gaps), repr(gaps[:5]))
    check("信息查询与探测同拍", len(ch.sent_own_cmd(CMD_PROBE)) == len(times),
          "%d / %d" % (len(ch.sent_own_cmd(CMD_PROBE)), len(times)))
    check("窗口结束时等满超时", res.state == "timeout"
          and 10.0 <= res.waited_s <= 10.16, "%.3f" % res.waited_s)


# ------------------------------------------------------------------ 7. E05 解析
def test_info_codec():
    print("7) 设备信息查询与应答帧的逐字段解析")
    raw = bytes((CMD_PROBE, 0x01, 0x0A, 0x01, 0x02, 0x03, 0x00, 0x00))
    info = decode_probe(raw)
    check("解析出运行模式 0x01", info is not None and info.mode == 0x01, repr(info))
    check("解析出模块地址 0x0A", info is not None and info.addr == 0x0A)
    check("解析出版本三元组 1.2.3",
          info is not None and info.version == (1, 2, 3) and info.version_str == "1.2.3")
    check("保留字节非 0 也照样解析",
          decode_probe(bytes((CMD_PROBE, 0x02, 0x01, 2, 3, 4, 0xFF, 0xAA))) is not None)
    check("模式 0x02 表示已接受升级请求",
          decode_probe(bytes((CMD_PROBE, 0x02, 0x01, 1, 0, 0, 0, 0))).mode == 0x02)
    check("长度不足返回 None", decode_probe(raw[:7]) is None)
    check("byte0 不是 0x05 时不当作应答",
          decode_probe(bytes((0x04, 0x01, 0x0A, 0, 0, 0, 0, 0))) is None)
    check("广播查询 byte0=0x20 且 byte7=0x3F", encode_probe_query() == bytes.fromhex("200000000000003F"),
          encode_probe_query().hex().upper())
    check("单播查询 byte7=目的地址", encode_probe_query(0x05)[-1] == 0x05)
    check("升级请求样例字节 06 00 00 00 00 00 00 01",
          encode_enter_bl(0x01) == bytes.fromhex("0600000000000001"),
          encode_enter_bl(0x01).hex().upper())
    for bad in (0x00, 0xFF, -1, 0x1FF):
        try:
            encode_enter_bl(bad)
            check("拒绝非单播地址 %s" % bad, False, "没抛 ValueError")
        except ValueError:
            check("拒绝非单播地址 %s" % bad, True)


# ------------------------------------------------------------------ 8. 全链路
def test_upgrade_end_to_end():
    print("8) 全链路离线走通：App → 确认 → Bootloader → 擦写校验 → RUN")
    dev = FakeModule(addr=0x01, version=(2, 1, 0))

    class FastAPI(HostAPI):
        """写入节奏不在离线测试里真等：pace_s=0。"""
        def _bl(self, addr):
            return FlashBootloader(self._ch, addr=addr, pace_s=0.0)

    api = make_api(dev)
    # 这台假设备是旧 BL（INFO byte5=0，无压缩能力位），显式关压缩钉住直通链路；
    # 压缩链路的闭环由 tests/offline_compress_test.py 覆盖
    r = api.start_upgrade("", 20, False)
    check("start_upgrade 受理（没预选地址）", r["success"], str(r))
    check("探测到 App 后等待确认",
          wait_until(lambda: api.get_progress()["confirm"] is not None),
          str(api.get_progress()))
    api.confirm_upgrade(True)
    done = wait_until(lambda: not api.get_progress()["running"], timeout_s=60.0)
    p = api.get_progress()
    check("全链路结束", done, str(p))
    check("升级成功", p["success"] is True, p["message"])
    check("写入按 15 块上报", p["blocks_total"] == 15 and p["blocks_done"] == 15,
          "%d/%d" % (p["blocks_done"], p["blocks_total"]))
    check("模块收到 15 块写入", dev.blocks == 15, str(dev.blocks))
    check("擦除已下发", len(api._ch.bl_cmd_frames(P.CMD_ERASE)) >= 1)
    check("校验已下发", len(api._ch.bl_cmd_frames(P.CMD_VERIFY)) >= 1)
    check("最后用 RUN 跳转（不是 RESET）",
          len(api._ch.bl_cmd_frames(P.CMD_RUN)) == 1
          and len(api._ch.bl_cmd_frames(P.CMD_RESET)) == 0,
          "run=%d reset=%d" % (len(api._ch.bl_cmd_frames(P.CMD_RUN)),
                               len(api._ch.bl_cmd_frames(P.CMD_RESET))))
    check("目标地址进了模块列表", "0x01" in api._modules, repr(api._modules))
    check("文案不教用户重新上电", "重新上电" not in p["message"], p["message"])


# ------------------------------------------------------------------ 9. 老 App
def test_status_only_old_app():
    print("9) 0x20 不应答（固件过旧）→ 报「设备无应答」而不是含糊结论")
    dev = FakeModule(addr=0x09, version=(0, 9, 0), answer_probe=False)
    api = make_api(dev)
    try:
        api._acquire_bootloader(0x09, 20.0)
        check("会抛错", False, "没抛异常")
    except UpgradeCancelled as e:
        check("不应按用户取消处理", False, str(e))
    except BootloaderError as e:
        check("文案含「设备无应答」", "设备无应答" in str(e), str(e))
        check("文案点出固件过旧的可能性", "固件过旧" in str(e), str(e))
        check("文案不含「离线」", "离线" not in str(e), str(e))


# ------------------------------------------------------------------ 10. 不支持升级请求
def test_post_request_probe_cadence():
    print("10) 设备不认识 0x06：复查时仍在 App → 报错且没有擦写（单发单查，无窗口期重发）")
    dev = FakeModule(addr=0x05, version=(1, 0, 0), enter_bl_supported=False)
    api = make_api(dev)
    api.start_upgrade("0x05", 60)
    check("探测后进入确认",
          wait_until(lambda: api.get_progress()["confirm"] is not None),
          str(api.get_progress()))
    api.confirm_upgrade(True)
    check("流程走完", wait_until(lambda: not api.get_progress()["running"], 10.0),
          str(api.get_progress()))
    p = api.get_progress()
    check("发出恰好一次升级请求", len(api._ch.sent_own_cmd(CMD_ENTER_BL)) == 1,
          str(len(api._ch.sent_own_cmd(CMD_ENTER_BL))))
    check("报错说明设备仍在 App", "仍在 App" in p["message"], p["message"])
    check("失败而非取消", p["success"] is False and p["cancelled"] is False, str(p))
    check("设备没进 Bootloader、没有擦写", not dev.in_bootloader and dev.blocks == 0)


# ------------------------------------------------------------------ 11. 受理但未复位
def test_accepted_but_no_reset():
    print("11) 设备受理了 0x06 但始终没进 Bootloader → 复查仍在 App，不擦写")
    dev = FakeModule(addr=0x06, version=(1, 0, 0), reaches_bootloader=False)
    api = make_api(dev)
    api.start_upgrade("0x06", 10)
    check("探测后进入确认",
          wait_until(lambda: api.get_progress()["confirm"] is not None),
          str(api.get_progress()))
    api.confirm_upgrade(True)
    check("窗口在虚拟时钟内跑完",
          wait_until(lambda: not api.get_progress()["running"], 10.0),
          str(api.get_progress()))
    p = api.get_progress()
    check("文案说明设备仍在 App", "仍在 App" in p["message"], p["message"])
    check("模块确实受理了", dev.accepted)
    check("设备没被误判进 Bootloader 去擦写", dev.blocks == 0)


def main():
    print("=" * 64)
    print("升级探测与状态机离线测试（不加载 ControlCAN.dll、不打开任何设备）")
    print("=" * 64)
    for fn in (test_probe_finds_app, test_probe_finds_bootloader,
               test_probe_timeout_message, test_confirm_sends_unicast_enter_bl,
               test_decline_exits_clean, test_probe_tick, test_info_codec,
               test_upgrade_end_to_end, test_status_only_old_app,
               test_post_request_probe_cadence, test_accepted_but_no_reset):
        fn()
    fails = [r for r in RESULTS if not r[1]]
    print("-" * 64)
    print("合计 %d 项，通过 %d，失败 %d"
          % (len(RESULTS), len(RESULTS) - len(fails), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
