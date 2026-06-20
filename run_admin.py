"""PyInstaller 打包入口。打包出的可执行文件运行它,等价于 `python3 -m admin`。"""

import sys

from admin.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
