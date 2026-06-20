# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 单文件打包。public/ 静态前端一起打进去;运行时从解包目录读取。
# 用法:pyinstaller ptadmin.spec   产物在 dist/ptadmin(.exe)

from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ['run_admin.py'],
    pathex=[],
    binaries=[],
    datas=[('public', 'public')],           # 把前端打进可执行文件
    hiddenimports=collect_submodules('admin'),
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ptadmin',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,                            # 控制台窗口里打印监听地址/日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
