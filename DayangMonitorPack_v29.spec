# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['ALLPYCODE\\main_monitor_pack.py'],
    pathex=[],
    binaries=[],
    datas=[('monitor_schedules.json', '.')],
    hiddenimports=['pandas', 'openpyxl', 'schedule'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='DayangMonitorPack_v29',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
