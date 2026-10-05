# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、无控制台窗口、带图标。

路径策略：全部基于 **本 spec 文件所在目录** 计算，
不要写死临时目录（临时目录会被系统清理，导致打包突然报
"Unable to find ... when adding binary and data files"）。
"""
import os

SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
PNG = os.path.join(SPEC_DIR, 'app.png')
ICO = os.path.join(SPEC_DIR, 'app.ico')

# 图标缺失时自动重新生成（make_icon.py 用 Pillow 画）
if not (os.path.isfile(PNG) and os.path.isfile(ICO)):
    try:
        import subprocess
        import sys as _sys
        subprocess.run([_sys.executable,
                        os.path.join(SPEC_DIR, 'make_icon.py'), SPEC_DIR],
                       check=False)
    except Exception:
        pass

_datas = []
if os.path.isfile(PNG):
    _datas.append((PNG, '.'))

block_cipher = None

a = Analysis(
    [os.path.join(SPEC_DIR, 'main.py')],
    pathex=[SPEC_DIR],
    binaries=[],
    # 内嵌界面图标（侧栏月亮用；打包后释放到 sys._MEIPASS）
    datas=_datas,
    hiddenimports=['PIL._tkinter_finder'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'numpy', 'pandas', 'scipy', 'pytest',
              'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'test'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='AutoDragon',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 关键：无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICO if os.path.isfile(ICO) else None,
)
