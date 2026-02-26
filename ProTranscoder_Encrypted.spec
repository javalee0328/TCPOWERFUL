# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['c:\\Users\\jerry.lee\\PycharmProjects\\pythonProject\\dist\\obf\\main.py'],
    pathex=['c:\\Users\\jerry.lee\\PycharmProjects\\pythonProject\\dist\\obf'],
    binaries=[],
    datas=[],
    hiddenimports=['PySide6', 'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets', 'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets', 'nacl', 'nacl.secret', 'nacl.utils', 'nacl.signing', 'nacl.encoding', 'nacl.exceptions', 'nacl.bindings', 'cffi', '_cffi_backend', 'ctypes', 'logging', 'json', 'shutil', 'subprocess', 'hashlib', 'win32api', 'win32file', 'win32con', 'pythoncom', 'pywintypes', 'pyarmor_runtime', 'core.analyzer', 'core.gpu_detector', 'core.metadata', 'core.preset_data', 'core.security', 'core.settings', 'core.transcoder', 'ui.close_event_snippet', 'ui.lock_screen', 'ui.main_window', 'ui.player_widget', 'ui.playlist_widget'],
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
    name='ProTranscoder_Encrypted',
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
