# -*- coding: utf-8 -*-
"""变量观察（WATCH）：固件符号解析 + 调试内存读写（dev 0x0C cmd 0x30~0x34）。

与固件侧 can_dbg_rd/wr/crc 同一套帧格式。两个全文通用的口径：
- 地址与宽度一律按 C28x 字（16 位）计。TI 编译器把 sizeof 单位直接写进 DWARF，
  byte_size 是字数；ELF st_size 是 8 位字节数，换算字数 = st_size // 2。
- 帧载荷一律小端字节：字 k 低字节在前，32 位量 = 相邻两字小端拼接。

符号来源两级：EABI（标准 ELF）走 pyelftools 全功能（类型树/结构体展开）；
COFF 走 CGT 的 ofd2000.exe -x 转 XML，只有符号级（无类型树，按无符号字解码）。
"""
import os
import re
import struct
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zlib
from collections import deque

from monitor_tui.protocol import CAN_ADDR_HOST, encode_probe_query, own_id

APP_BASE_WORD = 0x080000      # 演示固件独立运行，App 区从 Flash 首字开始（字地址）
APP_WINDOW_WORDS = 0x10000    # CRC 镜像窗口（字）= 整个 Flash（16 扇段 × 4096 字）；
                              # App 与 Bootloader 共存时按实际 App 区大小收紧
MAX_READ_WORDS = 255        # 0x30 单次读上限
MAX_WRITE_WORDS = 4         # 0x32 单次写上限
FRAME_MS = 1.216            # 125k 下 8 字节扩展帧线上时间（BL 链路实测）
WRITE_ACK_TIMEOUT_S = 0.8   # 等 0x34 的上限（0x32 的 B5 填 100ms，再加余量）
CRC_VERIFY_TIMEOUT_S = 1.0  # 等 0x35 的上限（固件毫秒级算完即回，余量给总线竞争）
VERIFY_RETRIES = 2          # 校验逐段回退路径：每段超时后的重发次数
VERIFY_RETRY_GAP_S = 0.1    # 重发前的退避间隔
DEFAULT_GAP_WORDS = 16      # 合并的地址空隙阈值（字）：空隙超过它宁可分开读
MIN_POLL_MS = 50            # 轮询周期下限；0 = 停止
MAX_FAIL_STREAK = 20        # 连续失败 tick 数上限，超过自动停轮询（设备已不在）
BUS_BUDGET_FPS = 300        # 总线预算：读数据帧 ≤300 帧/秒（协议 0.8.3），超出自动放大周期
EXPAND_DEPTH = 4            # 结构体/数组展开深度上限
EXPAND_ARRAY_MAX = 32       # 数组展开元素数上限
WAVE_POINTS = 600           # 波形环形缓冲点数上限（每通道）

ERR_TEXT = {1: "Flash 区，拒绝写入", 2: "长度不符", 3: "写入超时"}

# ELF 节标志位
SHF_WRITE = 0x1


class WatchError(Exception):
    pass


def read_timeout_s(words, gap_ms=0):
    """协议 0.6 超时公式（本模块轮询恒 gap=0，按线速连发）。"""
    n = -(-words * 2 // 7)  # 期望 0x31 帧数
    base = n * (gap_ms + FRAME_MS) if gap_ms else n * FRAME_MS
    return max(200.0, base + 100.0 + 2.0 * n) / 1000.0


def plan_blocks(frags, gap_words=None):
    """观察片段合并成读请求块。frags: [(addr, words, entry_id, frag_off)]。

    gap_words=None 不合并（超 255 字仍强制切分）；否则按起始地址排序线性扫描：
    空隙 ≤ gap_words 并入当前块、超过封口，块总长超 MAX_READ_WORDS 强制切开。
    返回 [(块地址, 块字数, [(片地址, 片字数, entry_id, 片偏移), ...])]。"""
    pending = []
    for addr, words, eid, off in frags:
        for k in range(0, words, MAX_READ_WORDS):
            pending.append((addr + k, min(MAX_READ_WORDS, words - k), eid, off + k))
    if gap_words is None:
        return [(a, w, [(a, w, e, o)]) for a, w, e, o in pending]
    pending.sort(key=lambda x: x[0])
    blocks = []
    for addr, words, eid, off in pending:
        merged = False
        if blocks:
            b = blocks[-1]
            end = b[0] + b[1]
            if addr - end <= gap_words and max(end, addr + words) - b[0] <= MAX_READ_WORDS:
                b[1] = max(end, addr + words) - b[0]
                b[2].append((addr, words, eid, off))
                merged = True
        if not merged:
            blocks.append([addr, words, [(addr, words, eid, off)]])
    return [(a, w, owners) for a, w, owners in blocks]


# ==================== 符号解析 ====================

# 行的类型描述（可 JSON 化，前端只显示不解释），w 单位一律字：
#   {"k":"num","w":..,"enc":DWARF编码,"name":..}   整型/浮点标量
#   {"k":"bool",..} / {"k":"enum","w":..,"vals":{值:名}} / {"k":"ptr","w":..}
#   {"k":"struct"|"union","w":..,"name":..,"members":[(名,字偏移,type)]}
#   {"k":"array","w":总字数,"n":元素数,"elem":type}

_WRAP_TAGS = ("DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type",
              "DW_TAG_restrict_type",
              "DW_TAG_lo_user")  # TI 扩展限定符（如 far）：带 DW_AT_type 时原样透传


class SymbolFile:
    """一份 .out/.elf 的解析结果：符号表 + App 区 CRC 镜像。"""

    def __init__(self, path, fmt, symbols, image):
        self.path = path
        self.fmt = fmt            # "EABI" / "COFF"
        self.symbols = symbols    # {名: {"addr":字地址,"words":字数,"type":..,"writable":b}}
        self.image = image        # CRC 镜像字节（基址 APP_BASE_WORD，长度 ≤ 2×窗口）
        self.crc32 = zlib.crc32(image)

    def image_words(self):
        return len(self.image) // 2


def _is_flash_word(addr):
    return 0x080000 <= addr < APP_BASE_WORD + APP_WINDOW_WORDS


def _type_name(t):
    if t["k"] == "struct":
        return "struct " + t["name"] if t.get("name") else "struct"
    if t["k"] == "union":
        return "union " + t["name"] if t.get("name") else "union"
    return t.get("name") or t["k"]


def _spec_source(die, names):
    """经 DW_AT_specification/abstract_origin 找到真正带名字的 DIE。"""
    for n in names:
        if die.attributes.get(n):
            try:
                return die.get_DIE_from_attribute(n)
            except KeyError:
                return None
    return None


def _attr_str(die, name):
    a = die.attributes.get(name)
    return a.value.decode("utf-8", "replace") if a is not None else None


def _attr_int(die, name, default=None):
    a = die.attributes.get(name)
    if a is None:
        return default
    v = a.value
    if isinstance(v, list):        # exprloc（如 [DW_OP_plus_uconst, n]）取常量操作数
        return v[1] if len(v) > 1 else default
    return v


def _type_of(die, depth):
    """DIE → 类型描述；超深/未知形态返回 None（调用方跳过该符号）。"""
    if die is None or depth > 8:
        return None
    tag = die.tag
    w = _attr_int(die, "DW_AT_byte_size")
    if tag in _WRAP_TAGS:
        inner = None
        if die.attributes.get("DW_AT_type"):
            try:
                inner = _type_of(die.get_DIE_from_attribute("DW_AT_type"), depth + 1)
            except KeyError:
                return None
        if tag == "DW_TAG_typedef" and inner is not None:
            inner = dict(inner)
            inner.setdefault("name", _attr_str(die, "DW_AT_name"))
        return inner
    if tag == "DW_TAG_base_type":
        enc = _attr_int(die, "DW_AT_encoding")
        return {"k": "bool" if enc == 2 else "num", "w": max(1, w or 1), "enc": enc,
                "name": _attr_str(die, "DW_AT_name") or "int"}
    if tag == "DW_TAG_enumeration_type":
        vals = {}
        for en in die.iter_children():
            ename = _attr_str(en, "DW_AT_name")
            ev = _attr_int(en, "DW_AT_const_value")
            if ename and ev is not None:
                vals[ev] = ename
        return {"k": "enum", "w": max(1, w or 1), "vals": vals,
                "name": _attr_str(die, "DW_AT_name") or "enum"}
    if tag == "DW_TAG_pointer_type":
        if not die.attributes.get("DW_AT_type"):
            return None                    # void*：无被指类型宽度，跳过
        return {"k": "ptr", "w": max(1, w or 1)}
    if tag in ("DW_TAG_structure_type", "DW_TAG_union_type"):
        members = []
        for m in die.iter_children():
            mname = _attr_str(m, "DW_AT_name")
            if m.attributes.get("DW_AT_declaration") or not mname \
                    or not m.attributes.get("DW_AT_type"):
                continue
            try:
                mt = _type_of(m.get_DIE_from_attribute("DW_AT_type"), depth + 1)
            except KeyError:
                continue
            if mt is None:
                continue
            members.append([mname, _attr_int(m, "DW_AT_data_member_location", 0), mt])
        return {"k": "struct" if tag == "DW_TAG_structure_type" else "union",
                "w": max(1, w or 1), "members": members,
                "name": _attr_str(die, "DW_AT_name")}
    if tag == "DW_TAG_array_type":
        try:
            elem = _type_of(die.get_DIE_from_attribute("DW_AT_type"), depth + 1)
        except KeyError:
            return None
        if elem is None:
            return None
        n = None
        for sub in die.iter_children():
            if sub.tag == "DW_TAG_subrange_type":
                cnt = _attr_int(sub, "DW_AT_count")
                ub = _attr_int(sub, "DW_AT_upper_bound")
                n = cnt if cnt is not None else (ub + 1 if ub is not None else None)
        if n is None:
            n = (w // elem["w"]) if w else 1
        return {"k": "array", "w": elem["w"] * max(1, n), "n": max(1, n), "elem": elem}
    return None


def parse_symbols(path):
    """入口：EABI 走 pyelftools，COFF 走 ofd2000。抛 WatchError。"""
    if not os.path.isfile(path):
        raise WatchError("文件不存在: %s" % path)
    with open(path, "rb") as f:
        magic = f.read(4)
    return _parse_eabi(path) if magic == b"\x7fELF" else _parse_coff(path)


def _parse_eabi(path):
    from elftools.elf.elffile import ELFFile
    with open(path, "rb") as f:
        elf = ELFFile(f)
        # App 区 CRC 镜像：按 PT_LOAD 段的 p_paddr（加载地址 = Flash 实际存放地址）
        # 拷入窗口，空洞与尾部保持 0xFF（擦除态）。不能用节头 sh_addr——
        # .TI.ramfunc 之类“Load=Flash/Run=RAM”的节 sh_addr 是 RAM 运行地址，
        # 它在 Flash 里的加载映像只有 p_paddr 指得出（实测该节 0x00A000/0x085000）。
        img = bytearray(b"\xFF" * (APP_WINDOW_WORDS * 2))
        covered = 0
        for seg in elf.iter_segments():
            h = seg.header
            if h["p_type"] != "PT_LOAD" or not h["p_filesz"] \
                    or not APP_BASE_WORD <= h["p_paddr"] < APP_BASE_WORD + APP_WINDOW_WORDS:
                continue
            data = seg.data()
            off = (h["p_paddr"] - APP_BASE_WORD) * 2
            n = min(len(data), len(img) - off)
            if n < len(data):
                # 超出窗口的段数据被丢弃：CRC 仍会对窗口内内容通过，
                # 必须显式暴露，不能静默截断
                self._host._log("✕ 固件段超出 App 区窗口（0x%06X），镜像不完整"
                                % h["p_paddr"])
            img[off:off + n] = data[:n]
            covered = max(covered, off + n)
        image = bytes(img[:covered // 2 * 2])

        # 符号可写性按节标志判定（节在符号解析里仍以 sh_addr 定位——
        # 运行期读写的地址就是运行地址）
        sections = []          # (字地址, 字数, 可写)
        for sec in elf.iter_sections():
            h = sec.header
            if h["sh_size"] and h["sh_flags"] & 0x2 \
                    and APP_BASE_WORD <= h["sh_addr"] < APP_BASE_WORD + APP_WINDOW_WORDS:
                sections.append((h["sh_addr"], h["sh_size"] // 2,
                                 bool(h["sh_flags"] & SHF_WRITE)))

        def word_writable(addr):
            for sa, sw, wr in sections:
                if sa <= addr < sa + sw:
                    return wr
            return not _is_flash_word(addr)

        symbols = {}
        dwarf = elf.get_dwarf_info()
        for cu in dwarf.iter_CUs():
            for die in cu.iter_DIEs():
                if die.tag != "DW_TAG_variable" or die.attributes.get("DW_AT_declaration"):
                    continue
                loc = die.attributes.get("DW_AT_location")
                if not loc or loc.form != "DW_FORM_exprloc" or len(loc.value) < 5 \
                        or loc.value[0] != 0x03:
                    continue
                ref = _spec_source(die, ("DW_AT_specification", "DW_AT_abstract_origin"))
                name = _attr_str(die, "DW_AT_name") or (ref and _attr_str(ref, "DW_AT_name"))
                if not name or name in symbols:
                    continue
                try:
                    tdie = die.get_DIE_from_attribute("DW_AT_type")
                except KeyError:
                    continue
                t = _type_of(tdie, 0)
                if t is None:
                    continue
                addr = int.from_bytes(bytes(loc.value[1:5]), "little")
                symbols[name] = {"addr": addr, "words": max(1, t["w"]), "type": t,
                                 "writable": word_writable(addr)}
    return SymbolFile(path, "EABI", symbols, image)


def _find_ofd2000():
    """ofd2000 与 hex2000 同目录（CGT bin）；HEX2000 环境变量同样生效。"""
    from monitor_tui.upgrade import firmware as FW
    cand = os.path.join(os.path.dirname(FW.HEX2000), "ofd2000.exe")
    return cand if os.path.exists(cand) else None


def _parse_coff(path):
    """COFF 历史产物：ofd2000 -x 转 XML 取符号级信息（无类型树，按无符号字解码）。"""
    exe = _find_ofd2000()
    if exe is None:
        raise WatchError("COFF 产物解析需要 TI 编译器自带的 ofd2000.exe（未找到），"
                         "请改用 EABI 配置的 .out/.elf")
    try:
        r = subprocess.run([exe, "-x", path], capture_output=True, timeout=60)
    except OSError as e:
        raise WatchError("ofd2000 运行失败: %s" % e)
    if r.returncode != 0:
        raise WatchError("ofd2000 转换失败（文件不是有效的 TI 目标文件？）")
    root = ET.fromstring(r.stdout.decode("ISO-8859-1", "replace"))

    def txt(node, tag, default=None):
        e = node.find(tag)
        return e.text if e is not None and e.text else default

    symbols = {}
    for sym in root.iter("elf32_sym"):
        if txt(sym, "st_type") != "STT_OBJECT" \
                or txt(sym, "st_shndx") in (None, "SHN_UNDEF"):
            continue
        name = txt(sym, "st_name_string")
        addr = int(txt(sym, "st_value", "0"), 16)
        words = max(1, int(txt(sym, "st_size", "0"), 16) // 2)
        if not name or name in symbols:
            continue
        symbols[name] = {"addr": addr, "words": words,
                         "type": {"k": "num", "w": words, "enc": 7,
                                  "name": "unsigned int" if words == 1 else "unsigned long"},
                         "writable": not _is_flash_word(addr)}
    # CRC 镜像复用升级链路的 hex2000 通路（对 COFF 同样适用，含基址守卫）
    from monitor_tui.upgrade import firmware as FW
    fw = FW.load_firmware(path)
    n = min(len(fw.octets), APP_WINDOW_WORDS * 2)
    return SymbolFile(path, "COFF", symbols, fw.octets[:n // 2 * 2])


# ==================== 值解码 / 编码 ====================

_SCALAR_FMT = {5: {1: "<h", 2: "<i", 4: "<q"},          # 有符号整型
               6: {1: "<h", 2: "<i", 4: "<q"},          # 有符号字符
               7: {1: "<H", 2: "<I", 4: "<Q"},          # 无符号整型
               8: {1: "<H", 2: "<I", 4: "<Q"}}          # 无符号字符


def _scalar_fmt(t):
    if t.get("enc") == 4:
        return "<d" if t["w"] >= 4 else "<f"
    return _SCALAR_FMT.get(t.get("enc", 7), {}).get(t["w"], "<H")


def decode_scalar(data, byte_off, t):
    """缓冲区（小端字节流）按类型解码出一个标量显示串。"""
    if t["k"] == "ptr":
        v = int.from_bytes(data[byte_off:byte_off + t["w"] * 2], "little")
        return "0x%0*X" % (t["w"] * 4, v)
    v = struct.unpack_from(_scalar_fmt(t), data, byte_off)[0]
    if t["k"] == "bool":
        return "true" if v else "false"
    if t["k"] == "enum":
        return "%s (%d)" % (t["vals"][v], v) if v in t.get("vals", {}) else str(v)
    if t.get("enc") == 4:
        return "%g" % v
    return str(v)


def decode_raw(data, byte_off, t):
    """浮点全精度显示串：%.9g 保证 float32 十进制往返一致；double 用 %.17g。"""
    v = struct.unpack_from("<d" if t["w"] >= 4 else "<f", data, byte_off)[0]
    return "%.17g" % v if t["w"] >= 4 else "%.9g" % v


def encode_scalar(text, t):
    """用户输入文本 → 按类型宽度的小端字节流（非法输入抛 ValueError）。"""
    text = str(text).strip()
    if t["k"] == "bool":
        low = text.lower()
        if low in ("0", "false", "off"):
            return b"\x00" * (t["w"] * 2)
        if low in ("1", "true", "on"):
            return b"\x01" + b"\x00" * (t["w"] * 2 - 1)
        raise ValueError("布尔量填 0/1")
    if t["k"] == "enum":
        for val, name in t.get("vals", {}).items():
            if text == name:
                text = str(val)
                break
    if t.get("enc") == 4:
        return struct.pack("<d" if t["w"] >= 4 else "<f", float(text))
    v = int(text, 0)
    nbytes = t["w"] * 2
    if t.get("enc", 7) in (5, 6):
        lo, hi = -(1 << (nbytes * 8 - 1)), (1 << (nbytes * 8 - 1)) - 1
    else:
        lo, hi = 0, (1 << (nbytes * 8)) - 1
    if not lo <= v <= hi:
        raise ValueError("超出 %d 字范围 [%d, %d]" % (t["w"], lo, hi))
    return (v & ((1 << (nbytes * 8)) - 1)).to_bytes(nbytes, "little")


def leaf_kind(t):
    """标量才可解码显示/编辑；struct/union/array 行只作容器。"""
    return t["k"] in ("num", "bool", "enum", "ptr")


def row_walk(t, depth):
    """类型一层展开行：[(成员名, 字偏移, type, 路径后缀)]。"""
    if t is None or depth >= EXPAND_DEPTH:
        return []
    if t["k"] == "struct":
        return [(m[0], m[1], m[2], "." + m[0]) for m in t["members"]]
    if t["k"] == "union":
        return [(m[0], 0, m[2], "." + m[0]) for m in t["members"]]
    if t["k"] == "array":
        return [("[%d]" % i, i * t["elem"]["w"], t["elem"], "[%d]" % i)
                for i in range(min(t["n"], EXPAND_ARRAY_MAX))]
    return []


# ==================== 成员路径解析 ====================

_PATH_SEG = re.compile(r"\.(\w+)|\[(\d+)\]")


def resolve_path(symbols, name):
    """观察名 → 符号条目：顶层名直查；「符号.成员[下标]…」路径沿类型树逐级
    换算字地址（成员偏移/元素宽与 build_rows 同为字单位）。解析失败返回
    None。COFF 无类型树（标量），路径自然解析失败。可写性沿用根符号。"""
    sym = symbols.get(name)
    if sym is not None:
        return sym
    root = re.match(r"[A-Za-z_]\w*", name)
    if root is None or root.end() == len(name):
        return None
    sym = symbols.get(root.group())
    if sym is None:
        return None
    addr, t, pos = sym["addr"], sym["type"], root.end()
    while pos < len(name):
        seg = _PATH_SEG.match(name, pos)
        if seg is None:
            return None
        if seg.group(2) is not None:              # [i] 数组下标
            if t["k"] != "array" or int(seg.group(2)) >= t["n"]:
                return None
            addr += int(seg.group(2)) * t["elem"]["w"]
            t = t["elem"]
        else:                                     # .member 成员名
            if t["k"] not in ("struct", "union"):
                return None
            for nm, off, mt in t["members"]:
                if nm == seg.group(1):
                    addr += off                   # union 成员偏移恒 0（row_walk 同口径）
                    t = mt
                    break
            else:
                return None
        pos = seg.end()
    return {"addr": addr, "words": max(1, t["w"]), "type": t,
            "writable": sym["writable"]}


# ==================== 事务（协议 0x30~0x34） ====================

class WatchSession:
    """对单模块地址的调试内存读写会话。单槽纪律：全部事务经 _txn 串行。"""

    def __init__(self, channel, addr):
        self.ch = channel
        self.addr = addr & 0xFF
        self._seq = 0
        self._txn = threading.Lock()
        self.requests = 0       # 0x30/0x32/0x35 发起数（含超时重发前的每次新请求）
        self.data_frames = 0    # 收到的 0x31 数据帧数
        self.stale_frames = 0   # 序号不符被丢弃的残帧数
        self.timeouts = 0       # 读超时 + 写无应答次数

    def _next_seq(self):
        self._seq = (self._seq + 1) & 0xFF
        return self._seq

    def _reply_pred(self, cmd, seq=None):
        def pred(frame):
            if not frame.xtd or ((frame.id >> 22) & 0x0F) != 0x0C:
                return False
            if ((frame.id >> 16) & 0x3F) != cmd \
                    or ((frame.id >> 8) & 0xFF) != CAN_ADDR_HOST \
                    or (frame.id & 0xFF) != self.addr:
                return False
            return seq is None or (frame.dlc >= 1 and frame.data[0] == seq)
        return pred

    def probe_capability(self, tries=2, timeout_s=0.5):
        """0x20 探测：确认在跑 App（B1=0x01）且 B6 bit0=1（调试命令组已编译）。"""
        def pred(frame):
            if not self._reply_pred(0x20)(frame) or frame.dlc < 8:
                return False
            return frame.data[0] == 0x20 and frame.data[1] == 0x01
        with self.ch.subscribe(pred) as mb:
            for _ in range(tries):
                if not self.ch.send(own_id(0x20, self.addr),
                                    encode_probe_query(self.addr)):
                    return False
                f = mb.get(timeout_s)
                if f is not None:
                    return bool(f.data[6] & 0x01)
        return False

    def read_words(self, addr, words, direct=True):
        """0x30+0x31 读 words 字，返回小端字节流；超时/收不齐返回 None。"""
        with self._txn:
            with self.ch.subscribe(self._reply_pred(0x31)) as mb:
                return self._read_seg(mb, addr, words, direct=direct)

    def _read_seg(self, mb, addr, words, direct=True, retries=0):
        """在既有订阅内读一段：发 0x30，收满期望帧数才返回（绝不带缺帧数据）；
        超时退避重发当前段（序号新取，旧残帧按序号整帧丢弃），重试耗尽返回
        None。调用方需已持有 _txn（单槽：等锁时间不占超时预算，序号严格递增）。"""
        n = -(-words * 2 // 7)
        for attempt in range(retries + 1):
            seq = self._next_seq()
            payload = bytes((addr & 0xFF, (addr >> 8) & 0xFF, (addr >> 16) & 0xFF,
                             (addr >> 24) & 0xFF, words, 0, seq, 1 if direct else 0))
            deadline = time.monotonic() + read_timeout_s(words)
            self.requests += 1
            if not self.ch.send(own_id(0x30, self.addr), payload):
                return None
            got = []
            while len(got) < n:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    break
                f = mb.get(remain)
                if f is None:
                    break
                if f.dlc < 8:
                    continue
                if f.data[0] != seq:
                    self.stale_frames += 1   # 旧事务残帧，按序号整帧丢弃（协议 0.3）
                    continue
                self.data_frames += 1
                got.append(bytes(f.data[1:8]))
            if len(got) >= n:
                return b"".join(got)[:words * 2]
            self.timeouts += 1
            if attempt < retries:
                time.sleep(VERIFY_RETRY_GAP_S)
        return None

    def write_words(self, addr, data):
        """0x32+0x33 背靠背写（≤4 字），等 0x34。返回 (err, 地址回显, 字节数)；
        err=None 表示无应答。"""
        words = len(data) // 2
        if not 1 <= words <= MAX_WRITE_WORDS:
            raise WatchError("单次写 1~%d 字" % MAX_WRITE_WORDS)
        with self._txn:   # seq 在单槽内取，与读事务共享同一严格递增计数
            seq = self._next_seq()
            start = bytes((addr & 0xFF, (addr >> 8) & 0xFF, (addr >> 16) & 0xFF,
                           (addr >> 24) & 0xFF, words, 100, seq, 0))
            self.requests += 1
            with self.ch.subscribe(self._reply_pred(0x34, seq)) as mb:
                if not self.ch.send(own_id(0x32, self.addr), start):
                    return None, addr, 0
                self.ch.send(own_id(0x33, self.addr), bytes(data))
                f = mb.get(WRITE_ACK_TIMEOUT_S)
        if f is None or f.dlc < 8:
            self.timeouts += 1
            return None, addr, 0
        return f.data[1], int.from_bytes(bytes(f.data[2:6]), "little"), \
            int.from_bytes(bytes(f.data[6:8]), "little")

    def crc_verify(self, addr, words):
        """0x35 单帧 CRC 校验：固件对 [addr, addr+words) 同步算 CRC32。
        返回 (状态码, CRC32)；发送失败/无应答返回 None（旧固件白名单外整帧
        丢弃 0x35，调用方以此回退逐段读回路径）。"""
        payload = struct.pack("<IH", addr, words) + b"\x00\x00"
        with self._txn:
            self.requests += 1
            with self.ch.subscribe(self._reply_pred(0x35)) as mb:
                if not self.ch.send(own_id(0x35, self.addr), payload):
                    return None
                f = mb.get(CRC_VERIFY_TIMEOUT_S)
        if f is None or f.dlc < 5:
            self.timeouts += 1
            return None
        return f.data[4], int.from_bytes(bytes(f.data[0:4]), "little")

    def read_image(self, words, on_progress=None, alive=None):
        """按 ≤255 字分段读回 App 区（直读；Flash 内容静态，无撕裂问题）。

        整个镜像共用一次订阅与单槽：段 k 收满期望帧数后立即在订阅内发段 k+1
        请求，消除逐段订阅/退订往返；超时口径不变（每段独立计时），超时退避
        重发当前段。alive 返回 False（断开/换目标）即中止后续段抛 WatchError。"""
        out = bytearray()
        off = 0
        with self._txn:
            with self.ch.subscribe(self._reply_pred(0x31)) as mb:
                while off < words:
                    n = min(MAX_READ_WORDS, words - off)
                    raw = self._read_seg(mb, APP_BASE_WORD + off, n, direct=True,
                                         retries=VERIFY_RETRIES)
                    if raw is None:
                        raise WatchError("读取校验区失败（0x%06X 起）" % (APP_BASE_WORD + off))
                    out += raw
                    off += n
                    if on_progress is not None:
                        on_progress(off / words)
                    if alive is not None and not alive():
                        raise WatchError("校验已作废")
        return bytes(out)


# ==================== 观察会话管理（HostAPI 的后端） ====================

class WatchEntry:
    """一个被观察变量：根行 + 展开行；每轮刷新产出全部行的值字符串。
    sym=None 为未解析条目（清单先于符号文件存在）：行只有名称与缺失
    标注，载入符号文件后由 load_file 重映射解析。"""

    def __init__(self, eid, name, sym=None):
        self.id = eid
        self.name = name
        self.missing = sym is None
        if sym is not None:
            self.addr = sym["addr"]
            self.words = sym["words"]
            self.type = sym["type"]
            self.writable = sym["writable"]
        else:
            self.addr = 0
            self.words = 0
            self.type = None
            self.writable = False
        self.rows = []           # Tabulator 行数据（根行含 _children 嵌套）
        self.nodes = {}          # rowid -> (字地址, type, writable)
        self.last_vals = {}      # rowid -> 上一轮值串（变化标色基准）

    def build_rows(self):
        self.nodes = {}

        def add(rid, disp, addr, t, writable, depth):
            if t is None:   # 未解析条目：仅有名称与缺失标注
                self.nodes[rid] = (0, None, False)
                return {"id": rid, "name": disp, "addr": "—", "words": "—",
                        "tname": "—", "val": "—", "editable": False,
                        "writable": False, "changed": False, "err": "",
                        "missing": True}
            row = {"id": rid, "name": disp, "addr": "0x%04X" % addr,
                   "words": t["w"], "tname": _type_name(t), "val": "—",
                   "editable": leaf_kind(t), "writable": writable,
                   "changed": False, "err": "", "missing": self.missing}
            if t["k"] == "enum" and t.get("vals"):
                # 枚举成员名按值排序下发，前端值栏换下拉编辑（写入接受枚举名）
                row["enumVals"] = [nm for _, nm in sorted(t["vals"].items())]
            self.nodes[rid] = (addr, t, writable)
            kids = row_walk(t, depth)
            if kids:
                row["_children"] = [add(rid + suffix, disp + suffix if t["k"] == "array"
                                        else nm, addr + off, mt, writable, depth + 1)
                                    for nm, off, mt, suffix in kids]
            return row

        self.rows = [add("e%d" % self.id, self.name, self.addr, self.type,
                         self.writable, 0)]
        return self.rows[0]

    def set_missing(self, flag):
        """标记/解除符号缺失：行数据（含子行）补 missing 字段供前端上警告色。
        缺失行保留旧地址显示，轮询跳过其分片。"""
        self.missing = flag

        def stamp(rows):
            for r in rows:
                r["missing"] = flag
                stamp(r.get("_children") or [])
        stamp(self.rows)

    def rebind(self, sym):
        """换符号文件后按同名符号重绑：更新地址/类型/宽度并重建行表；
        旧值对比基准作废（新地址新值）。"""
        self.addr = sym["addr"]
        self.words = sym["words"]
        self.type = sym["type"]
        self.writable = sym["writable"]
        self.last_vals = {}
        self.build_rows()

    def decode(self, data):
        """整块小端字节流 → ({rowid: 值串}, {rowid: 浮点全精度串})；
        未解析行与行宽超出数据的行跳过，全精度串与值串相同则不记。"""
        vals = {}
        raws = {}
        for rid, (addr, t, _wr) in self.nodes.items():
            if t is None:
                continue
            off = (addr - self.addr) * 2
            if leaf_kind(t) and 0 <= off and off + t["w"] * 2 <= len(data):
                try:
                    vals[rid] = decode_scalar(data, off, t)
                    if t.get("enc") == 4:
                        raw = decode_raw(data, off, t)
                        if raw != vals[rid]:
                            raws[rid] = raw
                except Exception:  # noqa: BLE001 - 个别行解码失败不影响其余行
                    pass
        return vals, raws


class WatchManager:
    """状态机 idle → file → verifying → ready/mismatch；ready 下可轮询与写入。
    观察清单独立于符号文件：未载入符号时可添加，条目以未解析（missing）
    形式存在，load_file 重映射时解析或保持缺失标注。"""

    def __init__(self, host):
        self._host = host
        self._lock = threading.RLock()
        self._symfile = None
        self._st = "idle"
        self._msg = ""
        self._addr = None
        self._sess = None
        self._entries = []
        self._next_eid = 1
        self._poll_ms = 100       # 期望轮询周期（用户输入，预算逻辑不改写）
        self._eff_poll_ms = 100   # 实际轮询周期（超出总线预算时放大，_replan 重算）
        self._merge = True
        self._snapshot = False
        self._gap = DEFAULT_GAP_WORDS
        self._running = False
        self._stop = threading.Event()
        self._thread = None
        self._verify_prog = 0.0
        self._last_rows = None
        self._est_fps = 0
        self._per_tick = 0
        self._toast = ""        # 一次性提醒文案（随快照推给前端，seq 供去重）
        self._toast_seq = 0
        self._wave_pins = []    # 波形记录通道（rowid，保序）
        self._wave_hist = {}    # rowid -> deque[(t_s, val)]，容量 WAVE_POINTS
        self._wave_t0 = None    # 波形时间基准（monotonic 秒），清除后重置

    # ---- 状态与推送 ------------------------------------------------------
    def table_rows(self):
        """整表行数据（页面刷新后前端重建表格用；快照只带值补丁）。"""
        with self._lock:
            return [e.rows[0] for e in self._entries]

    def snapshot(self):
        with self._lock:
            if self._st == "idle":
                return None
            snap = {"st": self._st, "msg": self._msg, "prog": -1,
                    "file": os.path.basename(self._symfile.path) if self._symfile else None,
                    "fmt": self._symfile.fmt if self._symfile else "",
                    "nsym": len(self._symfile.symbols) if self._symfile else 0,
                    "addr": ("0x%02X" % self._addr) if self._addr else "",
                    "poll": self._running, "poll_ms": self._poll_ms,
                    "eff_ms": self._eff_poll_ms,
                    "merge": self._merge, "snapshot": self._snapshot,
                    "gap": self._gap, "fps": self._est_fps,
                    "stats": self._stats(), "rows": self._last_rows,
                    "toast": self._toast, "toast_seq": self._toast_seq}
            if self._st == "verifying":
                snap["prog"] = int(self._verify_prog * 100)
            return snap

    def _stats(self):
        sess = self._sess
        base = {"req": 0, "frm": 0, "stale": 0, "to": 0}
        if sess is not None:
            base = {"req": sess.requests, "frm": sess.data_frames,
                    "stale": sess.stale_frames, "to": sess.timeouts}
        return base

    def _log(self, msg):
        self._host._log(msg)

    def _set(self, st=None, msg=None):
        with self._lock:
            if st is not None:
                self._st = st
            if msg is not None:
                self._msg = msg
        self._host._bump()

    # ---- 文件与连接 ------------------------------------------------------
    def load_file(self, path):
        try:
            sf = parse_symbols(path)
        except WatchError as e:
            self._log("✕ 符号解析失败: %s" % e)
            return {"success": False, "message": str(e)}
        except Exception as e:  # noqa: BLE001 - 解析器深层异常拦成用户文案
            msg = "符号解析失败: %s" % e
            self._log("✕ " + msg)
            return {"success": False, "message": msg}
        if not sf.image_words():
            # 镜像为空则 CRC 恒等通过（0==0），非 App 产物（如 Bootloader）会
            # 绕过版本校验直接放行——必须在入口拒绝
            msg = "文件里没有 App 区镜像，请选择 App 编译产物"
            self._log("✕ " + msg)
            return {"success": False, "message": msg}
        self.stop_poll(None)
        with self._lock:
            self._symfile = sf
            # 观察列表不清空：按变量名重映射——新符号表里有同名符号的条目更新
            # 地址/类型/宽度继续观察；没有的标记符号缺失（行保留、轮询跳过），
            # 加载回原固件自动恢复
            missing = []
            for e in self._entries:
                sym = resolve_path(sf.symbols, e.name)
                if sym is None:
                    e.set_missing(True)
                    missing.append(e.name)
                else:
                    e.set_missing(False)
                    e.rebind(sym)
            table = [e.rows[0] for e in self._entries]
            self._last_rows = None
            if self._st not in ("idle", "file"):
                self._sess = None
                self._addr = None
        if missing:
            self._log("符号缺失: %s（行保留并标记，轮询跳过；加载回原固件自动恢复）"
                      % "、".join(missing))
        self._set("file", "已解析 %d 个符号，等待校验" % len(sf.symbols))
        self._log("观察符号就绪: %s（%s，%d 符号，镜像 CRC32 0x%08X）"
                  % (os.path.basename(path), sf.fmt, len(sf.symbols), sf.crc32))
        return {"success": True, "path": path,
                "file": {"name": os.path.basename(path),
                         "fmt": sf.fmt, "nsym": len(sf.symbols)},
                "symbols": sorted(sf.symbols), "table": table, "missing": missing}

    def connect(self, addr):
        if self._symfile is None:
            return {"success": False, "message": "先选择符号文件"}
        if self._host._ch is None:
            return {"success": False, "message": "CAN 未连接"}
        if self._host._progress["running"]:
            return {"success": False, "message": "升级进行中"}
        if not 0x01 <= addr <= 0x3B:
            return {"success": False, "message": "需要单播地址 0x01~0x3B"}
        self.stop_poll(None)
        with self._lock:
            # 观察列表带入新会话（换文件重映射、重新校验不清空）；只作废变化
            # 标色基准与上一轮值，避免旧会话残留数据冒充新读数
            for e in self._entries:
                e.last_vals = {}
            self._last_rows = None
        self._set("verifying", "校验中…")
        self._verify_prog = 0.0
        self._addr = addr
        self._host._jobq.put(lambda: self._do_connect(addr))
        return {"success": True, "pending": True}

    def _connect_alive(self, addr):
        """校验发起后目标未变且仍在校验态；断开/换目标即作废在途校验结果。"""
        with self._lock:
            return self._st == "verifying" and self._addr == addr

    def _do_connect(self, addr):
        if not self._connect_alive(addr):
            return
        sess = WatchSession(self._host._ch, addr)
        if not sess.probe_capability():
            if self._connect_alive(addr):
                self._set("mismatch", "模块无应答，或固件未编入调试命令")
                self._log("✕ 观察连接失败: 0x%02X 无 App 应答或 B6 bit0=0" % addr)
            return
        with self._lock:
            sf = self._symfile
        words = sf.image_words()
        t0 = time.monotonic()
        # 快路径：0x35 单帧 CRC 校验（App 1.2.5 起，毫秒级）。旧固件白名单外
        # 整帧丢弃 0x35，无应答时回退逐段读回路径
        fast = sess.crc_verify(APP_BASE_WORD, words)
        if fast is not None and fast[0] == 0:
            if not self._connect_alive(addr):
                return
            if fast[1] == sf.crc32:
                self._verify_done(sess, addr, sf, t0, "0x35 单帧")
            else:
                self._set("mismatch", "固件不一致：文件与板上固件不同，已拒绝观察")
                self._log("✕ 固件校验不一致（0x35 单帧，文件 0x%08X / 板上 0x%08X），"
                          "拒绝观察与写入" % (sf.crc32, fast[1]))
            return
        try:
            remote = sess.read_image(words, lambda p: setattr(self, "_verify_prog", p),
                                     alive=lambda: self._connect_alive(addr))
        except WatchError as e:
            if self._connect_alive(addr):
                self._set("mismatch", str(e))
                self._log("✕ 固件校验中断: %s" % e)
            return
        if not self._connect_alive(addr):
            return
        if zlib.crc32(remote) == sf.crc32:
            self._verify_done(sess, addr, sf, t0, "逐段读回")
        else:
            self._set("mismatch", "固件不一致：文件与板上固件不同，已拒绝观察")
            self._log("✕ 固件校验不一致（逐段读回，文件 0x%08X / 板上 0x%08X），拒绝观察与写入"
                      % (sf.crc32, zlib.crc32(remote)))

    def _verify_done(self, sess, addr, sf, t0, path):
        with self._lock:
            self._sess = sess
        self._set("ready", "校验一致，可以观察")
        self._log("观察已连接: 0x%02X，固件校验一致（%s，%.1f 秒，CRC32 0x%08X）"
                  % (addr, path, time.monotonic() - t0, sf.crc32))

    def disconnect(self):
        self.stop_poll(None)
        with self._lock:
            self._sess = None
            self._addr = None
            self._entries = []      # 会话结束：观察列表一并清空（与前端表格同步）
            self._last_rows = None
        if self._st not in ("idle", "file"):
            self._set("file", "已断开，可重新连接校验")

    def close(self):
        """进程退出/断开 CAN：停线程并回到 idle。"""
        self.stop_poll(None)
        with self._lock:
            self._sess = None
            self._addr = None
            self._symfile = None
            self._st = "idle"
            self._msg = ""

    def notify_firmware_updated(self):
        """升级 worker 烧录成功后的提醒：WATCH 处于已校验态时推一次性 toast。
        不打断观察、不断开会话；前端按 toast_seq 去重，只弹一次。"""
        with self._lock:
            if self._st != "ready":
                return
            self._toast = "固件已更新，若与所选符号文件不一致请重新选择校验"
            self._toast_seq += 1
        self._host._bump()

    # ---- 观察列表 --------------------------------------------------------
    def add(self, name):
        """清单添加：任何状态都可加（清单独立于符号文件）。已载入符号表时
        立即解析（解析不到拒绝，防手误）；未载入时登记为未解析条目，
        load_file 重映射时再解析。"""
        with self._lock:
            sym = None
            if self._symfile is not None:
                sym = resolve_path(self._symfile.symbols, name)
                if sym is None:
                    return {"success": False, "message": "符号表里没有 %s" % name}
            if any(e.name == name for e in self._entries):
                return {"success": False, "message": "%s 已在观察列表" % name}
            entry = WatchEntry(self._next_eid, name, sym)
            self._next_eid += 1
            self._entries.append(entry)
            row = entry.build_rows()   # 锁内建行：轮询解码不会撞上半建好的行表
        self._replan()
        if sym is not None:
            self._log("观察添加: %s（0x%04X，%d 字）" % (name, entry.addr, entry.words))
        else:
            self._log("观察添加: %s（未解析，等待载入符号文件）" % name)
        return {"success": True, "row": row}

    def remove(self, rowid):
        """按任意行 rowid 移除所属变量（子行一并移除）。"""
        with self._lock:
            keep = []
            for e in self._entries:
                root = "e%d" % e.id
                if rowid == root or rowid.startswith(root + ".") \
                        or rowid.startswith(root + "["):
                    continue
                keep.append(e)
            self._entries = keep
        self._replan()
        return {"success": True}

    def clear(self):
        with self._lock:
            self._entries = []
        self._replan()
        return {"success": True}

    def _replan(self):
        """按当前列表与选项重算每 tick 帧预算与实际轮询周期（协议 0.8.3：
        ≤300 帧/秒，期望周期超预算时放大为实际周期）。期望周期与变量列表
        任一变化都会经过这里（set_options/add/remove/clear），移除变量后
        实际周期随之回缩。符号缺失的条目跳过（不发无地址的读）。"""
        with self._lock:
            frags = []
            for e in self._entries:
                if e.missing:
                    continue
                for k in range(0, e.words, MAX_READ_WORDS):
                    frags.append((e.addr + k, min(MAX_READ_WORDS, e.words - k), e.id, k))
            blocks = plan_blocks(frags, self._gap if self._merge else None)
            self._per_tick = len(blocks) + sum(-(-w * 2 // 7) for _, w, _ in blocks)
            self._eff_poll_ms = self._poll_ms
            if self._poll_ms and self._per_tick:
                est = self._per_tick * 1000.0 / self._poll_ms
                if est > BUS_BUDGET_FPS:
                    self._eff_poll_ms = max(MIN_POLL_MS,
                                            -(-self._per_tick * 1000 // BUS_BUDGET_FPS))
            self._est_fps = int(self._per_tick * 1000
                                / max(MIN_POLL_MS, self._eff_poll_ms))
        self._host._bump()

    # ---- 选项与轮询 ------------------------------------------------------
    def set_options(self, poll_ms=None, merge=None, gap=None, snapshot=None):
        with self._lock:
            if poll_ms is not None:
                try:
                    poll_ms = int(poll_ms)
                except (TypeError, ValueError):
                    return {"success": False, "message": "轮询周期需数字 ms"}
                if poll_ms != 0 and poll_ms < MIN_POLL_MS:
                    return {"success": False,
                            "message": "周期最小 %d ms（0 = 停止）" % MIN_POLL_MS}
                self._poll_ms = poll_ms
            if merge is not None:
                self._merge = bool(merge)
            if snapshot is not None:
                self._snapshot = bool(snapshot)
            if gap is not None:
                try:
                    self._gap = max(0, int(gap))
                except (TypeError, ValueError):
                    return {"success": False, "message": "空隙阈值需数字（字）"}
        self._replan()
        if self._poll_ms == 0:
            self.stop_poll(None)
        return {"success": True, "poll_ms": self._poll_ms,
                "merge": self._merge, "snapshot": self._snapshot, "gap": self._gap}

    def start_poll(self):
        with self._lock:
            if self._st != "ready" or self._running:
                return {"success": False, "message": "先连接校验并添加变量"}
            if not [e for e in self._entries if not e.missing]:
                return {"success": False, "message": "没有可观察的变量（符号缺失条目已跳过）"}
            self._replan()   # 重算每 tick 帧预算与实际周期
            want, eff = self._poll_ms, self._eff_poll_ms
            self._running = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll_loop, name="watch-poll",
                                            daemon=True)
            self._thread.start()
        if eff != want:
            self._log("观察轮询开始：期望 %d ms，实际 %d ms（超出总线预算）" % (want, eff))
        else:
            self._log("观察轮询开始（%d ms）" % want)
        return {"success": True}

    def stop_poll(self, reason):
        thread = None
        with self._lock:
            if self._running:
                self._running = False
                self._stop.set()
                thread = self._thread
                self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)   # 轮询线程自动停时不能 join 自己
        if reason:
            self._log("观察轮询停止: %s" % reason)
            self._set(msg="轮询已停止（%s）" % reason)
        return {"success": True}

    def _poll_loop(self):
        fail = 0
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                ok = self._tick()
            except Exception as e:  # noqa: BLE001 - 轮询线程不炸宿主
                self._log("✕ 观察轮询异常: %s" % e)
                ok = False
            fail = 0 if ok else fail + 1
            if fail >= MAX_FAIL_STREAK:
                self.stop_poll("连续读取失败")
                return
            period = max(MIN_POLL_MS, self._eff_poll_ms) / 1000.0
            remain = period - (time.monotonic() - t0)
            if remain > 0 and self._stop.wait(remain):
                return

    def _tick(self):
        """一轮刷新：合并成块读回，逐变量解码。返回本轮是否全部成功。
        符号缺失的条目整轮跳过（不发无地址的读，也不计失败）。"""
        with self._lock:
            sess = self._sess
            entries = [e for e in self._entries if not e.missing]
            gap = self._gap if self._merge else None
            if sess is None or not entries:
                return True
            frags = []
            for e in entries:
                for k in range(0, e.words, MAX_READ_WORDS):
                    frags.append((e.addr + k, min(MAX_READ_WORDS, e.words - k), e.id, k))
        ok_all = True
        bufs = {}   # entry_id -> {片偏移: 片字节}
        direct = not self._snapshot
        for addr, words, owners in plan_blocks(frags, gap):
            raw = sess.read_words(addr, words, direct=direct)
            if raw is None:
                ok_all = False
                continue
            for fa, fw, eid, fo in owners:
                s = (fa - addr) * 2
                bufs.setdefault(eid, {})[fo] = raw[s:s + fw * 2]
        last_rows = []
        with self._lock:
            for e in entries:
                b = bufs.get(e.id)
                if not b:
                    continue
                data = b"".join(b[o] for o in sorted(b))
                if len(data) < e.words * 2:
                    ok_all = False
                    continue
                vals, raws = e.decode(data)
                changed = {rid for rid, v in vals.items()
                           if rid in e.last_vals and e.last_vals[rid] != v}
                e.last_vals.update(vals)
                self._wave_record(e, vals)
                for rid, (_a, _t, _w) in e.nodes.items():
                    v = vals.get(rid)
                    if v is not None:
                        row = {"id": rid, "val": v,
                               "ch": 1 if rid in changed else 0, "err": ""}
                        raw = raws.get(rid)
                        if raw is not None:
                            row["raw"] = raw
                        last_rows.append(row)
            self._last_rows = last_rows
        return ok_all

    # ---- 写入 ------------------------------------------------------------
    def write(self, rowid, text):
        if self._st != "ready":
            return {"success": False, "message": "先连接校验通过"}
        with self._lock:
            for e in self._entries:
                if rowid in e.nodes:
                    addr, t, wr = e.nodes[rowid]
                    entry_id = e.id
                    entry_missing = e.missing
                    break
            else:
                return {"success": False, "message": "观察列表里没有该行"}
        if entry_missing:
            return {"success": False, "message": "该变量的符号在当前固件中缺失，拒绝写入"}
        if not wr:
            return {"success": False, "message": "该变量在 Flash 区，不能写"}
        if self._host._progress["running"]:
            return {"success": False, "message": "升级进行中"}
        self._host._jobq.put(lambda: self._do_write(entry_id, rowid, addr, t, text))
        return {"success": True, "pending": True}

    def _do_write(self, entry_id, rowid, addr, t, text):
        with self._lock:
            sess = self._sess
            entry = next((e for e in self._entries if e.id == entry_id), None)
            disp = (entry.name + rowid[len("e%d" % entry.id):]) if entry else rowid
        if sess is None:
            return
        try:
            data = encode_scalar(text, t)
        except (ValueError, WatchError) as ex:
            self._row_error(rowid, str(ex), disp)
            return
        err, _echo, _n = sess.write_words(addr, data)
        if err is None:
            self._row_error(rowid, "写入无应答", disp)
        elif err != 0:
            self._row_error(rowid, ERR_TEXT.get(err, "写入失败（%d）" % err), disp)
        else:
            self._log("观察写入: %s = %s（0x%04X）" % (disp, text, addr))
            with self._lock:
                if entry is not None:
                    entry.last_vals.pop(rowid, None)   # 下轮刷新重新对比
            if not self._running:
                self._readback_row(rowid, addr, t, entry)

    def _readback_row(self, rowid, addr, t, entry):
        """轮询停止时的写入确认：读回刚写的字刷新该行（轮询中由下一 tick 刷新）。"""
        with self._lock:
            sess = self._sess
        raw = sess.read_words(addr, t["w"], direct=True) if sess is not None else None
        if raw is None:
            return
        try:
            v = decode_scalar(raw, 0, t)
            full = decode_raw(raw, 0, t) if t.get("enc") == 4 else None
        except Exception:  # noqa: BLE001 - 回读解码失败不掩盖写入成功
            return
        row = {"id": rowid, "val": v, "ch": 0, "err": ""}
        if full is not None and full != v:
            row["raw"] = full
        with self._lock:
            if entry is not None:
                entry.last_vals[rowid] = v
            rows = [r for r in (self._last_rows or []) if r["id"] != rowid]
            rows.append(row)
            self._last_rows = rows
        self._host._bump()

    def _row_error(self, rowid, msg, disp=None):
        with self._lock:
            rows = [r for r in (self._last_rows or []) if r["id"] != rowid]
            rows.append({"id": rowid, "val": None, "ch": 0, "err": msg})
            self._last_rows = rows
        self._log("✕ %s: %s" % (msg, disp or rowid))

    # ---- 波形（轮询值的环形缓冲） ----------------------------------------
    def wave_pins(self, rowids):
        """设置波形记录通道（全量替换，保序）。rowid 不在当前观察列表的丢弃。"""
        with self._lock:
            alive = set()
            for e in self._entries:
                alive.update(e.nodes)
            pins = []
            for rid in rowids or []:
                rid = str(rid)
                if rid in alive and rid not in pins:
                    pins.append(rid)
            self._wave_pins = pins
            self._wave_hist = {rid: self._wave_hist.get(
                rid, deque(maxlen=WAVE_POINTS)) for rid in pins}
        return {"success": True, "pins": self._wave_pins}

    def wave_data(self):
        """拉取波形缓冲：{rowid: {name: 显示名, pts: [[t_s, val], ...]}}。
        通道的行已随列表移除时自动剔除（历史一并失效）。"""
        with self._lock:
            alive = set()
            for e in self._entries:
                alive.update(e.nodes)
            self._wave_pins = [r for r in self._wave_pins if r in alive]
            out = {}
            for rid in self._wave_pins:
                dq = self._wave_hist.get(rid)
                if dq is not None:
                    out[rid] = {"name": self._wave_name(rid),
                                "pts": [[t, v] for t, v in dq]}
            return out

    def wave_clear(self):
        with self._lock:
            for dq in self._wave_hist.values():
                dq.clear()
            self._wave_t0 = None
        return {"success": True}

    def _wave_name(self, rowid):
        """rowid 的显示名 = 变量名 + 路径后缀（与写入日志口径一致）。"""
        for e in self._entries:
            if rowid in e.nodes:
                return e.name + rowid[len("e%d" % e.id):]
        return rowid

    def _wave_record(self, entry, vals):
        """把本 tick 中属于记录通道的数值行追加进缓冲（时间基准为
        perf_counter 秒相对首点——monotonic 在 Windows 上分辨率约 15.6ms，
        不够波形用；数值口径与表达式代入一致，非数值不计）。"""
        if not self._wave_pins:
            return
        root = "e%d" % entry.id
        now = time.perf_counter()
        if self._wave_t0 is None:
            self._wave_t0 = now
        t = round(now - self._wave_t0, 3)
        for rid in self._wave_pins:
            if rid != root and not rid.startswith(root + ".") \
                    and not rid.startswith(root + "["):
                continue
            v = vals.get(rid)
            if v is None:
                continue
            try:
                fv = float(v)
            except ValueError:
                continue
            dq = self._wave_hist.get(rid)
            if dq is not None:
                dq.append((t, fv))
