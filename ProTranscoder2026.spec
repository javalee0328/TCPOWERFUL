# -*- mode: python ; coding: utf-8 -*-


import os

# Check for license files to bundle
extra_datas = [('core', 'core'), ('ui', 'ui'), ('assets', 'assets'), ('redist', 'redist')]
if os.path.exists('dist/license.dat'):
    extra_datas.append(('dist/license.dat', '.'))
elif os.path.exists('license.dat'):
    extra_datas.append(('license.dat', '.'))

a = Analysis(
    ['main.py'],
    version='v27.3',
    pathex=[],
    binaries=[('C:\\ffmpeg\\bin\\ffmpeg.exe', 'core'), ('C:\\ffmpeg\\bin\\ffprobe.exe', 'core'), ('C:\\ffmpeg\\bin\\ffplay.exe', 'core')],
    datas=extra_datas,
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
    name='ProTranscoder2026_v27.3',
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
