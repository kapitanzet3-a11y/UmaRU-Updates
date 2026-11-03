# -*- mode: python ; coding: utf-8 -*-
"""Portable PyInstaller spec; build_setup.py supplies absolute paths via env."""
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

source = Path(os.environ['UMARU_SOURCE_ROOT'])
player = source / 'player'
datas, binaries, hiddenimports = collect_all('sv_ttk')
a = Analysis(
    [str(player / 'umaru_app.py')],
    pathex=[str(player), str(source)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['PIL', 'numpy'], noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name='UmaRU',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
    version=os.environ['UMARU_VERSION_FILE'], icon=[os.environ['UMARU_ICON_FILE']],
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True, upx_exclude=[], name='UmaRU')
