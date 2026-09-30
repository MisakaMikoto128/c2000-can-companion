# -*- coding: utf-8 -*-
"""固件文件 → 小端 octet 流：.out/.elf 经 hex2000 转 Intel HEX，.hex 直接解析。

octet 流口径：每个 16 位字按 [低字节, 高字节] 摆放（小端），不足补 0xFF。
hex2000 的 Intel HEX 地址字段与扩展段均以 16 位字计，解析时 ×2 转 octet 地址。
"""
import os
import subprocess
import tempfile

# .out/.elf 转 HEX 依赖 TI hex2000 (CCS 自带, 不随发布包分发);
# 未安装 hex2000 时可用环境变量 HEX2000 指到实际安装路径, .hex/ELF 文件则无需它。
HEX2000 = os.environ.get(
    "HEX2000",
    r"C:\ti\ccs1260\ccs\tools\compiler\ti-cgt-c2000_22.6.1.LTS\bin\hex2000.exe")

FMT_OUT = "out"
FMT_ELF = "elf"
FMT_HEX = "hex"


class FirmwareError(Exception):
    pass


class FirmwareImage:
    def __init__(self, path, fmt, octets, base_addr):
        from monitor_tui.upgrade import bl_protocol as P
        self.path = path
        self.fmt = fmt
        self.octets = octets
        self.base_addr = base_addr
        self.size = len(octets)
        self.crc32 = P.crc32(octets if len(octets) % 2 == 0 else octets + b"\xFF")


def detect_format(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".out":
        return FMT_OUT
    if ext == ".elf":
        return FMT_ELF
    if ext == ".hex":
        return FMT_HEX
    raise FirmwareError("不支持的固件格式 %s（仅 .out/.elf/.hex）" % ext)


def hex_to_octets(hex_path, fill_value=0xFF):
    """Intel HEX（C28x 字地址）→ 小端 octet 流。"""
    memory = {}
    ext_addr = 0
    min_addr = None
    max_addr = None

    with open(hex_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or not line.startswith(":"):
                continue
            try:
                count = int(line[1:3], 16)
                address = int(line[3:7], 16)
                rectype = int(line[7:9], 16)
                data = bytes.fromhex(line[9:9 + count * 2])
            except ValueError:
                raise FirmwareError("HEX 记录损坏: %s" % line[:20])

            if rectype == 0x00:
                word = ext_addr + address
                full = word * 2
                # hex2000 --order=MS 每字 [高,低] 摆放，交换为小端 [低,高]
                for i in range(0, len(data) - 1, 2):
                    memory[full + i] = data[i + 1]
                    memory[full + i + 1] = data[i]
                if len(data) % 2:
                    memory[full + len(data) - 1] = data[-1]
                min_addr = full if min_addr is None or full < min_addr else min_addr
                end = full + len(data) - 1
                max_addr = end if max_addr is None or end > max_addr else max_addr
            elif rectype == 0x01:
                break
            elif rectype == 0x04:
                ext_addr = int.from_bytes(data[:2], "big") << 16

    if not memory:
        raise FirmwareError("HEX 无数据记录")

    out = bytearray((fill_value,) * (max_addr - min_addr + 1))
    for addr, b in memory.items():
        out[addr - min_addr] = b
    return bytes(out), min_addr


def out_to_octets(out_path):
    """C28x .out/.elf → 小端 octet 流（先经 hex2000 转 HEX）。"""
    if not os.path.exists(HEX2000):
        raise FirmwareError("hex2000.exe 未找到: " + HEX2000)
    fd, hex_path = tempfile.mkstemp(suffix=".hex")
    os.close(fd)
    try:
        cmd = [HEX2000, "--memwidth=16", "--order=MS", "--romwidth=16",
               "--diag_wrap=off", "--intel", "-o", hex_path, out_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise FirmwareError("hex2000 转换失败（文件不是有效的 C28x 可执行格式？）: "
                                + result.stderr.strip()[:200])
        return hex_to_octets(hex_path)
    finally:
        if os.path.exists(hex_path):
            os.remove(hex_path)


APP_BASE_OCTET = 0x108000   # App 入口 0x084000 字地址 × 2 = octet 基址


def load_firmware(path):
    """任意支持格式 → FirmwareImage；失败抛 FirmwareError。

    基址守卫：CAN 升级只能烧 App 区入口（0x084000 字 = 0x108000 octet）起的镜像。
    FLASH_Standalone 配置（0x080000 起）或任何错配置产物在此直接拒绝——写下去
    既跑不起来还会覆盖 Bootloader。"""
    if not os.path.exists(path):
        raise FirmwareError("文件不存在: " + path)
    fmt = detect_format(path)
    if fmt == FMT_HEX:
        octets, base = hex_to_octets(path)
    else:
        octets, base = out_to_octets(path)
    if base != APP_BASE_OCTET:
        raise FirmwareError(
            "固件基址 0x%X 不是 App 区入口（应为 0x108000，即字地址 0x084000）。"
            "请使用与 Bootloader 配套（App 在 0x084000 链接）的构建产物；"
            "独立运行的 App 镜像（0x080000 起）不能走 CAN 升级，会覆盖 Bootloader" % base)
    return FirmwareImage(path, fmt, octets, base)
