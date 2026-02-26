# -*- mode: python ; coding: utf-8 -*-
import os

a = Analysis(
    ['dist/obf/main.py'],
    pathex=[],
    binaries=[
        ('C:\\ffmpeg\\bin\\ffmpeg.exe', 'core'), 
        ('C:\\ffmpeg\\bin\\ffprobe.exe', 'core'), 
        ('C:\\ffmpeg\\bin\\ffplay.exe', 'core')
    ],
    datas=[
        ('dist/obf/core', 'core'),
        ('dist/obf/ui', 'ui'),
        ('dist/obf/pyarmor_runtime_000000', 'pyarmor_runtime_000000'),
        ('redist', 'redist'),
        ('assets', 'assets')
    ],
    hiddenimports=[
        '_cffi_backend', 
        'pynacl', 'nacl', 'nacl.signing', 'nacl.encoding', 'nacl.exceptions',
        'win32api', 'win32file', 'psutil',
        'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets', 'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets',
        'core.settings', 'core.metadata', 'core.analyzer', 'core.transcoder', 'core.gpu_detector', 'core.preset_data', 'core.security',
        'ui.main_window', 'ui.player_widget', 'ui.lock_screen'
    ],
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
    name='金碼湛 ProTranscoder 2026',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon='assets/icon.ico',
)
