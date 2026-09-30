# -*- coding: utf-8 -*-
"""增量升级——独立于压缩算法的升级方式，可整体裁剪。

命名（业界实打实用词，OTA 领域通用的两个相对术语）：
- 增量升级（incremental）= 只传有变化的块（本模块）；
- 全量升级（full）= 传整个镜像（原版路径）。
不用"差分/delta"——那是 bsdiff 类补丁算法的专名，本流程传的是新镜像原文块。
界面与日志一律显示"增量升级/全量升级"；代码标识 SELECTIVE_* 与协议能力位
（CAP_SELECTIVE，BL 1.5.0 已入板）保持原名，不改已定协议。

规划基线 = 设备 Flash 现存内容，不依赖本机保存的任何镜像文件，版本号不参与
判断（版本串可造假，仅用于界面显示）。上位机对新固件逐块发 0x23 VERIFY 请求
（该命令支持任意范围），Bootloader 对实际 Flash 内容算 CRC 后应答 ok/err，
ok 即该块与新固件一致、可以跳过。写后最终 VERIFY 对实际 Flash 内容全区算
CRC：任何错误跳过的块都会被抓住并自动转全量重写。

两级门控，任一不满足即退回原版全量升级（一行不改的旧路径）：
  1. SELECTIVE_ENABLED —— 上位机本地裁剪总开关，False = 永远全量；
  2. 设备能力位 —— INFO byte5 bit3（CAP_SELECTIVE，BL 1.5.0 起上报）。
     旧 BL、以及 BOOT_COMPRESS_ENABLE=0 的小 Flash MCU 构建都不置位，
     上位机自动退全量；BL 尺寸零增量。

线上语义（BL 源码 boot/boot_protocol.c + 真机实证 2026-09-28，diag_r4 与
隔离实验双通过；此前失败根因 = 掩码映射错误，与抽查读无关）：
- ERASE mode=1：mask 位 i = 目标区逻辑扇段 i（8KB = 块 2i 与 2i+1）；位为 0
  的段不擦，保留 Flash 原内容；
- WRITE C=0（算法 2）：保持块——BL 不擦不写只推进块号，Flash 原内容即目标
  内容。调用方必须保证该块未被本次擦除且新旧内容一致；同一逻辑扇段的两个
  块必须同判定（整段一致才整段保留）；
- VERIFY：对实际 Flash 内容算 CRC——保持块以真实内容参与校验。
"""
from monitor_tui.upgrade import bl_protocol as P

SELECTIVE_ENABLED = True          # 裁剪总开关：False = 永远走全量升级
CAP_SELECTIVE = 0x08              # INFO byte5 bit3：设备上报增量升级能力
SELECTIVE_ERASE_MODE = 1          # ERASE mode=1 = 按 mask 擦目标区逻辑扇段


def capability_ok(compress_flags):
    """设备侧能力位门控（INFO byte5）。"""
    return bool(compress_flags & CAP_SELECTIVE)


def plan(matches):
    """按逐块一致性结果做扇段规划 → (keep_blocks, erase_mask)。

    matches[k] = 设备 Flash 现存内容与新固件块 k 是否逐字节一致（上位机对
    每块发 VERIFY 请求，应答 ok = True / err 或超时 = False）。覆盖目标区
    全部块（含新固件长度之外的 0xFF 填充块——设备上旧镜像的残尾因此同样
    被判差异并擦除）。
    粒度 = 逻辑扇段（块 k ∈ 段 k//2，每段 2 块）：擦除按段进行，段内任一块
    有差异就整段擦除重写，所以只保留整段一致的段。
    全部一致时返回全保持（keep=所有块、mask=0：0 擦 0 写，仅最终 VERIFY
    做硬件校验确认）。"""
    total = len(matches)
    keep = set()
    erase_mask = 0
    for pr in range((total + 1) // 2):
        b0 = 2 * pr
        b1 = 2 * pr + 1
        if matches[b0] and (b1 >= total or matches[b1]):
            keep.add(b0)
            if b1 < total:
                keep.add(b1)
        else:
            erase_mask |= 1 << pr       # 位 i = 段 i = 块 2i 与 2i+1
    return keep, erase_mask
