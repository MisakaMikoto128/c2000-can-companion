# -*- coding: utf-8 -*-
"""CAN 固件升级包：Bootloader 协议客户端 + 固件格式转换 + pywebview UI。

协议与主仓库 docs/协议/03_自有协议标准.md §5 逐字节一致：
125kbps 29 位扩展帧，ID = err|dev=0x0C|cmd|dest|src，大帧同 ID 连续帧 + 尾 sum8。
"""
