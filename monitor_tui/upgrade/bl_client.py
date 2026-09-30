# -*- coding: utf-8 -*-
"""CAN Bootloader 客户端：probe/info/erase/write/verify/read/run/reset 流程。"""
import time

from monitor_tui import lz4
from monitor_tui.upgrade import bl_protocol as P
from monitor_tui.upgrade.can_channel import CanChannel
from monitor_tui.upgrade.probe import bl_probe_addr

LZ4_MAX_C = 4094               # 压缩块长度上限，超过退直通（含 ==4096 的不划算情形）


class BootloaderError(Exception):
    pass


def pick_encoding(info):
    """按 INFO 应答的能力位选最优传输编码：链式 > LZ4 > 直通。

    升级页只有「压缩/不压缩」两个选项；选了压缩后具体用哪种编码由本函数
    按下位机能力自动决定（插件式：新算法只需在这里加一行分支）。"""
    cap = info["compress_flags"]
    if cap & P.COMPRESS_CAP_LZ4D:
        return P.ENC_LZ4D
    if cap & P.COMPRESS_CAP_LZ4:
        return P.ENC_LZ4
    return P.ENC_DIRECT


class FlashBootloader:
    """按模块地址操作的 Bootloader 客户端。"""

    def __init__(self, channel: CanChannel, addr=0x01, pace_s=0.001):
        """pace_s 是设备发送缓冲满时的重试等待，不是逐帧节拍。

        大帧整块交给驱动按线速发（见 CanChannel.send_many），逐帧 sleep 反而比线速慢：
        实测那样是 1.70 ms/帧，而 125 kbps 下一个 8 字节扩展帧的线上时间只要 1.216 ms。"""
        self.ch = channel
        self.addr = addr & 0xFF
        self.pace_s = pace_s

    # ---- 底层 -----------------------------------------------------------
    def _tx_cmd(self, cmd, payload, dest=None, broadcast=False):
        dest = P.BROADCAST if broadcast else self.addr
        return self.ch.send(P.build_id(cmd, dest), payload)

    def _reply_pred(self, cmd):
        def pred(frame):
            f = P.parse_id(frame.id)
            return (f["dev"] == P.DEV_MODULE and f["dest"] == P.HOST_ADDR
                    and f["src"] == self.addr and f["cmd"] == cmd)
        return pred

    def _transact(self, cmd, payload, timeout_s, broadcast=False, tries=1):
        """先订阅后发送（应答不可能落在订阅之前），信箱阻塞等该命令的应答帧。
        错误应答也算应答（由调用方判 err）；窗口内没有帧返回 None。
        tries>1 用于幂等查询（INFO）：紧贴探测流量发出的命令帧可能撞上设备
        收发占线窗口被丢弃，重试一次代价是几百 ms，比失败强。"""
        with self.ch.subscribe(self._reply_pred(cmd)) as mb:
            for _ in range(tries):
                if not self._tx_cmd(cmd, payload, broadcast=broadcast):
                    return None
                frame = mb.get(timeout_s)
                if frame is not None:
                    return frame
        return None

    def _wait_ack(self, cmd, timeout_s, payload):
        frame = self._transact(cmd, payload, timeout_s)
        if frame is None:
            return None
        f = P.parse_id(frame.id)
        if f["err"] != P.ERR_OK:
            raise BootloaderError("cmd 0x%02X err=%d 载荷=%s"
                                  % (cmd, f["err"], bytes(frame.data).hex()))
        return frame

    def _transact_big(self, cmd, big_data, ack_timeout_s, tries, on_retry=None,
                      enc_err=0):
        """发大帧（每帧都带命令 ID）并等 ACK。ACK 超时或模块回错误应答都重发整个大帧。

        enc_err 进 ID 的 err[28:26] 段（传输编码算法 ID）：0=直通，1=算法 1
        （LZ4 独立块），2=LZ4 链式字典。
        重发永远重发同一个大帧（同编码），ACK/块号语义与编码无关。
        on_retry(attempt, tries, kind) 在每次重发前回调，kind 为 "错误应答" 或
        "ACK 超时"，供上层把自愈过程显式化（别让用户以为进度卡死）。"""
        frames = P.chunk_frames(big_data)
        arb_id = P.build_id(cmd, self.addr, err=enc_err)
        reason = "ACK 超时"
        kind = "ACK 超时"
        # 整个重发循环共用一个信箱：订阅先于首次发送，迟到的 ACK 只会被当成
        # 当前次的 ACK（语义等价：模块确实确认了这一块）
        with self.ch.subscribe(self._reply_pred(cmd)) as mb:
            for attempt in range(1, tries + 1):
                if attempt > 1 and on_retry is not None:
                    on_retry(attempt, tries, kind)
                sent = self.ch.send_many(arb_id, frames, retry_wait_s=self.pace_s)
                if sent < len(frames):
                    raise BootloaderError("CAN 发送失败（%d/%d 帧排进设备发送缓冲）"
                                          % (sent, len(frames)))
                frame = mb.get(ack_timeout_s)
                if frame is None:
                    reason = "ACK 超时"
                    kind = "ACK 超时"
                    continue
                f = P.parse_id(frame.id)
                if f["err"] == P.ERR_OK:
                    return frame
                reason = "err=%d 载荷=%s" % (f["err"], bytes(frame.data).hex())
                kind = "错误应答"
        raise BootloaderError("cmd 0x%02X %s（重发 %d 次仍失败）" % (cmd, reason, tries))

    # ---- 命令 -----------------------------------------------------------
    def probe(self, broadcast=True, window_s=2.0, tick_s=0.15):
        """探测（广播收多机应答 / 单播收单机），返回应答的模块地址列表。

        窗口内每 tick_s 重发一次请求：模块从上电到跑进 Bootloader 需要时间，
        只发一次会错过晚起来的模块。"""
        found = []
        deadline = time.monotonic() + window_s
        # 谓词先按 ID 段粗筛（dev/cmd/dest/err），载荷校验在取帧后做
        def pred(frame):
            f = P.parse_id(frame.id)
            return (f["dev"] == P.DEV_MODULE and f["cmd"] == P.CMD_PROBE
                    and f["dest"] == P.HOST_ADDR and f["err"] == P.ERR_OK)
        with self.ch.subscribe(pred) as mb:
            while time.monotonic() < deadline:
                self._tx_cmd(P.CMD_PROBE, P.probe_request(), broadcast=broadcast)
                slice_end = min(time.monotonic() + tick_s, deadline)
                while True:
                    frame = mb.get(slice_end - time.monotonic())
                    if frame is None:
                        break
                    addr = bl_probe_addr(frame)
                    if addr is not None and addr not in found:
                        found.append(addr)
        return found

    def info(self):
        frame = self._transact(P.CMD_INFO, bytes(8), 0.4, tries=3)
        if frame is None:
            raise BootloaderError("INFO 无应答")
        info = P.parse_info(bytes(frame.data))
        if info is None:
            raise BootloaderError("INFO 校验失败")
        return info

    def erase(self, mode=0, mask=0):
        ack = self._transact_big(P.CMD_ERASE, P.erase_payload(mode, mask),
                                 ack_timeout_s=5.0, tries=3)
        return P.parse_ack(bytes(ack.data))

    def write(self, octets, progress=None, on_retry=None, encoding=P.ENC_DIRECT,
              keep_blocks=None):
        """按块写入 octet 流（自动补齐），返回 (块数, 总 octet 数)。

        encoding 是线上传输编码（P.ENC_*，通常由 pick_encoding 按 INFO 能力
        选出）：direct=直通；lz4=算法 1 独立块压缩；lz4d=算法 2 链式字典，
        整块纯 0xFF 的填充块改发 C=0 空块跳过——擦除态即正确内容，不烧写
        只推进块号。
        压缩后 ≤ 4094 octet 才发压缩大帧，否则该块退直通——逐块独立决策，
        混传完全合法（链式字典只认明文内容，与编码形态无关）。
        进度口径不变：块数始终按明文块计。

        keep_blocks：保持原样的块号集合。这些块发 C=0 空块帧——Bootloader
        不写不擦直接推进块号，Flash 保留原内容。调用方必须保证三件事：
        该块未被本次擦除（erase mask 已排除其所在逻辑扇段）；设备现存内容
        与新镜像该块逐字节相同；同一逻辑扇段的另一块按相同方式判定（协议块
        k → SEC4+k/2，每物理扇段 2 块，擦除按段进行，段内两块必须同判定）。
        仅 LZ4D 编码下合法（C=0 在算法 1 是帧错误）。keep 块同样推进链式
        编码器：解码端字典是已写 Flash 的前序明文，跳过块的真实内容就在
        Flash 上，编码历史序列必须连续。"""
        if len(octets) % 2:
            octets += bytes([0xFF])
        if keep_blocks and encoding != P.ENC_LZ4D:
            raise BootloaderError("keep_blocks 保持块仅在 LZ4D 编码下合法（C=0 在算法 1 是帧错误）")
        nb = (len(octets) + P.BLOCK_OCTETS - 1) // P.BLOCK_OCTETS
        chainer = lz4.LZ4Chainer() if encoding == P.ENC_LZ4D else None
        full_ff = b"\xFF" * P.BLOCK_OCTETS
        for k in range(nb):
            block = octets[k * P.BLOCK_OCTETS:(k + 1) * P.BLOCK_OCTETS]
            block = block.ljust(P.BLOCK_OCTETS, b"\xFF")
            last = k == nb - 1
            enc_err = 0
            if keep_blocks is not None and k in keep_blocks:
                chainer.compress(block)  # 推进编码器历史：解码端字典含此块明文
                big = P.write_frame_compressed(b"", last=last)
                enc_err = P.COMPRESS_LZ4D
            elif encoding == P.ENC_LZ4D:
                if block == full_ff:
                    chainer.compress(block)  # 推进编码器历史（后续块可引用），结果不上线
                    big = P.write_frame_compressed(b"", last=last)
                    enc_err = P.COMPRESS_LZ4D
                else:
                    c = chainer.compress(block)
                    if len(c) <= LZ4_MAX_C:
                        big = P.write_frame_compressed(c, last=last)
                        enc_err = P.COMPRESS_LZ4D
                    else:    # 压缩不划算（膨胀或几乎无收益），该块退直通
                        big = P.write_frame(block, last=last)
            elif encoding == P.ENC_LZ4:
                c = lz4.compress(block)
                if len(c) <= LZ4_MAX_C:
                    big = P.write_frame_compressed(c, last=last)
                    enc_err = P.COMPRESS_LZ4   # 算法 1
                else:            # 压缩不划算（膨胀或几乎无收益），该块退直通
                    big = P.write_frame(block, last=last)
            else:
                big = P.write_frame(block, last=last)
            blk_retry = None
            if on_retry is not None:
                blk_retry = lambda attempt, tries, kind: on_retry(k + 1, attempt, tries, kind)
            self._transact_big(P.CMD_WRITE, big, ack_timeout_s=0.2, tries=3,
                               on_retry=blk_retry, enc_err=enc_err)
            if progress is not None:
                progress(k + 1, nb)
        return nb, len(octets)

    def verify(self, octets):
        """全区校验：设备目标区前 len(octets)//2 字的实际内容 CRC 与新固件一致。"""
        if len(octets) % 2:
            octets += bytes([0xFF])
        crc = P.crc32(octets)
        self.verify_range(0, len(octets) // 2, crc)
        return True, crc

    def verify_range(self, start_word, word_count, expect_crc):
        """按范围校验：ok = 设备 Flash 该范围实际内容的 CRC 与 expect 一致；
        内容不一致（err）或无应答都抛 BootloaderError，调用方按差异处理。
        增量升级按块探测用：块 k 的范围 = [k*2048, 2048) 字。tries=1——
        err 是明确应答，重发结果相同；无应答误判为差异只会多擦写一块，
        偏安全方向。"""
        self._transact_big(P.CMD_VERIFY,
                           P.verify_payload(start_word, word_count, expect_crc),
                           ack_timeout_s=2.0, tries=1)
        return True

    def read(self, start_word, word_count):
        expect = word_count * 2
        got = bytearray()
        deadline = time.monotonic() + 10.0
        with self.ch.subscribe(self._reply_pred(P.CMD_READ)) as mb:
            self._tx_cmd(P.CMD_READ, P.read_payload(start_word, word_count))
            while len(got) < expect:
                frame = mb.get(deadline - time.monotonic())
                if frame is None:
                    break
                got += bytes(frame.data)
        return bytes(got[:expect])

    def run(self):
        return self._wait_ack(P.CMD_RUN, 2.0, bytes(8)) is not None

    def reset(self):
        return self._wait_ack(P.CMD_RESET, 2.0, bytes(8)) is not None
