# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[
        ('C:/ffmpeg/bin/ffmpeg.exe', '.'), 
        ('C:/ffmpeg/bin/ffprobe.exe', '.'), 
        ('C:/ffmpeg/bin/ffplay.exe', '.')
    ],
    datas=[
        ('core', 'core'),
        ('ui', 'ui'),
        ('assets', 'assets')
    ],
    hiddenimports=['_cffi_backend', 'psutil', 'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets', 'PySide6.QtNetwork'],
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
    name='ProTranscoder2026_v27.10.51',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets\\icon.ico',
)
