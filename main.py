import sys
import os
import PySide6.QtMultimedia
import PySide6.QtMultimediaWidgets
import PySide6.QtNetwork

# Ensure project root is in path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from ui.main_window import ModernTranscoderUI
import logging
import time
from datetime import datetime

# [v27.9.13] File logging setup
log_file = os.path.join(os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))), 'debug.log')
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file, mode='w', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

def debug_log(msg):
    """Global debug logging function"""
    logging.info(msg)
    print(msg)

# Global Mutex Reference to prevent Garbage Collection
_app_mutex = None

if __name__ == "__main__":
    # [v27.10.16] Fix Ghost Windows (Recursive Spawning)
    import multiprocessing
    multiprocessing.freeze_support()

    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    
    # Set application icon
    from PySide6.QtGui import QIcon
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.ico")
    if hasattr(sys, '_MEIPASS'):
        icon_path = os.path.join(sys._MEIPASS, "assets", "icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    
    # --- Single Instance Check (Triple-Lock: Mutex + LockFile + SharedMem) ---
    # [DISABLED] Single Instance Check - User requested to allow multiple instances
    # Keeping the code commented for reference
    import ctypes
    from PySide6.QtWidgets import QMessageBox
    from PySide6.QtCore import Qt, QLockFile, QDir, QSharedMemory
    
    # 1. Mutex Check (Primary for Windows)
    MUTEX_NAME = "ProTranscoder_Single_Instance_Mutex"
    kernel32 = ctypes.windll.kernel32
    try:
        _app_mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        last_error = kernel32.GetLastError()
        print(f"DEBUG: Mutex Handle: {_app_mutex}, Last Error: {last_error}")
    except Exception as e:
        print(f"Mutex Error: {e}")
        last_error = 0
    
    ERROR_ALREADY_EXISTS = 183
    is_running = (last_error == ERROR_ALREADY_EXISTS)

    # 2. LockFile Check (Backup) with Stale Lock Cleanup
    # Bind to app to ensure lifetime
    app._lock_file = QLockFile(QDir.temp().filePath("ProTranscoder.lock"))
    app._lock_file.setStaleLockTime(30000)  # 30 seconds stale timeout
    
    if not is_running: 
        if not app._lock_file.tryLock(100):
            # 鎖定失敗，嘗試清理過期鎖
            print("DEBUG: Lock file locked, attempting to remove stale lock...")
            if app._lock_file.removeStaleLockFile():
                print("DEBUG: Stale lock removed, retrying...")
                # 重試一次
                if not app._lock_file.tryLock(100):
                    is_running = True
                    print("DEBUG: Still locked after stale removal, another instance is running.")
                else:
                    print("DEBUG: Lock acquired after stale removal.")
            else:
                # 無法清理過期鎖，可能真的有其他實例
                is_running = True
                print("DEBUG: Could not remove stale lock, assuming running.")
            
    # 3. Shared Memory Check (Final Fallback) - More lenient
    app._shared_mem = QSharedMemory("ProTranscoder_SharedMem_Key")
    if not is_running:
        if not app._shared_mem.create(1): # Try to create 1 byte
            # Creation failed, try to attach to verify it's real
            print("DEBUG: Shared memory exists, attempting to attach...")
            if app._shared_mem.attach():
                # Successfully attached, another instance is really running
                is_running = True
                app._shared_mem.detach()
                print("DEBUG: Successfully attached to shared memory, another instance confirmed.")
            else:
                # Can't attach, might be stale, ignore this check
                print("DEBUG: Could not attach to shared memory, assuming stale entry, ignoring.")
                pass
    
    if is_running:
        # Already exists!
        msg = QMessageBox()
        msg.setWindowTitle("ProTranscoder 2026")
        msg.setText("程式已在運行中 (Already Running)")
        msg.setInformativeText("請切換至已開啟的視窗。\n\nPlease switch to the existing window.")
        msg.setStandardButtons(QMessageBox.Ok)
        msg.setIcon(QMessageBox.Warning)
        
        # Force the message box to be top-most to ensure visibility
        msg.setWindowFlags(msg.windowFlags() | Qt.WindowStaysOnTopHint)
        
        # Dark mode text fix
        msg.setStyleSheet("QLabel { color: #000; }") 
        
        msg.exec()
        sys.exit(0)
    
    # Import required modules
    from PySide6.QtCore import QTimer
    
    import traceback
    
    # [v27.9.12] Logging is now handled by the global debug_log function at top of file

    try:
        debug_log("Starting Application Init...")
        app.setApplicationName("ProTranscoder 2026")
        
        # --- Two-Stage Hardware Protection Check ---
        from ui.startup_dialog import StartupCheckDialog
        from core.security import LicenseManager
        
        # Create and show startup dialog
        startup_dlg = StartupCheckDialog()
        startup_dlg.show()
        app.processEvents()
        
        # Stage 1: Driver Check
        startup_dlg.set_stage_1()
        app.processEvents()
        QTimer.singleShot(500, lambda: None)  # Brief pause for visual feedback
        app.processEvents()
        
        # Create License Manager
        lm = LicenseManager()
        
        # Stage 2: Dongle Detection with Auto-Detection
        startup_dlg.set_stage_2(lm)  # Pass LicenseManager for auto-detection
        # Initial check
        allowed, status_msg, ids = lm.check_protection()
        
        if allowed:
            # Dongle already present
            debug_log(f"License Verified: {status_msg}")
            startup_dlg.set_success(status_msg)
            QTimer.singleShot(800, startup_dlg.accept)
        
        # Show dialog (will auto-detect if dongle not yet present)
        result = startup_dlg.exec()
        
        if not startup_dlg.check_result:
            # User clicked Exit or dialog was rejected
            debug_log("User cancelled startup")
            sys.exit(0)
        
        debug_log(f"License Verified: Proceeding to main window")
        
        debug_log("Initializing MainWindow...")
        window = ModernTranscoderUI()
        
        debug_log("Showing Window...")
        window.show()
        
        # Debug: Check if mutex persists
        print(f"DEBUG: App Launched. Mutex: {_app_mutex}")
        debug_log("Entering Main Loop...")
        
        exit_code = app.exec()
        debug_log(f"Application Exiting with code: {exit_code}")
        sys.exit(exit_code)
        
    except Exception as e:
        err_msg = traceback.format_exc()
        print(f"CRITICAL ERROR: {err_msg}")
        debug_log(f"CRITICAL STARTUP ERROR:\n{err_msg}")
        
        # Try to show error box if QApplication is alive
        try:
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Critical)
            msg.setWindowTitle("Startup Error")
            msg.setText("Application Failed to Start")
            msg.setInformativeText(f"Error: {e}\n\nPlease check debug.log.")
            msg.exec()
        except:
            pass
        sys.exit(1)
