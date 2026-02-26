# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[
        ('C:\\ffmpeg\\bin\\ffmpeg.exe', 'core'),
        ('C:\\ffmpeg\\bin\\ffprobe.exe', 'core'),
        ('C:\\ffmpeg\\bin\\ffplay.exe', 'core')
    ],
    datas=[
        ('core', 'core'),
        ('ui', 'ui'),
        ('assets', 'assets')
    ],
    hiddenimports=[
        '_cffi_backend',
        'psutil',
        'PySide6.QtMultimedia',
        'PySide6.QtMultimediaWidgets',
        'PySide6.QtNetwork',
        'shiboken6',
        'playwright.sync_api'
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='ProTranscoder2026_v27.10.35',
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
    icon='assets\\icon.ico'
)
