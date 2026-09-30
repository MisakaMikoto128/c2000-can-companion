#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包入口：编译产物默认进 pywebview GUI。

源码运行时等价于 `python -m monitor_tui.host_app`。
"""
import sys

from monitor_tui.host_app import main

if __name__ == "__main__":
    sys.exit(main())
