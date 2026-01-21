import sys
import os

# Ensure project root is in path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from ui.main_window import ModernTranscoderUI
import logging

# Global Mutex Reference to prevent Garbage Collection
_app_mutex = None

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    
    # --- Single Instance Check (Triple-Lock: Mutex + LockFile + SharedMem) ---
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

    # 2. LockFile Check (Backup)
    # Bind to app to ensure lifetime
    app._lock_file = QLockFile(QDir.temp().filePath("ProTranscoder.lock"))
    if not is_running: 
        if not app._lock_file.tryLock(100):
            is_running = True
            
    # 3. Shared Memory Check (Final Fallback)
    app._shared_mem = QSharedMemory("ProTranscoder_SharedMem_Key")
    if not is_running:
        if not app._shared_mem.create(1): # Try to create 1 byte
            # Creation failed, means it exists
            is_running = True
    
    if is_running:
        # Already exists!
        msg = QMessageBox()
        msg.setWindowTitle("ProTranscoder 2026")
        msg.setText("已運行")
        msg.setInformativeText("要重新開啟程式？")
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg.setDefaultButton(QMessageBox.No)
        msg.setIcon(QMessageBox.Question)
        
        # Force the message box to be top-most to ensure visibility
        msg.setWindowFlags(msg.windowFlags() | Qt.WindowStaysOnTopHint)
        
        # Dark mode text fix
        msg.setStyleSheet("QLabel { color: #000; }") 
        
        ret = msg.exec()
        if ret == QMessageBox.No:
            sys.exit(0)
    
    import traceback
    
    # Debug logging helper (Duplicated from main_window temporarily for startup safety)
    def main_debug_log(msg):
        try:
            log_path = os.path.join(os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.getcwd(), "dist", "debug.log")
            # Ensure dir exists if we are making it (though dist usually exists in dev, in frozen it's different)
            # In frozen, dirname(sys.executable) is the folder.
            if getattr(sys, 'frozen', False):
                 log_path = os.path.join(os.path.dirname(sys.executable), "debug.log")
            
            with open(log_path, "a", encoding="utf-8") as f:
                import time
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - [MAIN] {msg}\n")
        except:
            pass

    try:
        main_debug_log("Starting Application Init...")
        app.setApplicationName("ProTranscoder 2026")
        
        main_debug_log("Initializing MainWindow...")
        window = ModernTranscoderUI()
        
        main_debug_log("Showing Window...")
        window.show()
        
        # Debug: Check if mutex persists
        print(f"DEBUG: App Launched. Mutex: {_app_mutex}")
        main_debug_log("Entering Main Loop...")
        
        exit_code = app.exec()
        main_debug_log(f"Application Exiting with code: {exit_code}")
        sys.exit(exit_code)
        
    except Exception as e:
        err_msg = traceback.format_exc()
        print(f"CRITICAL ERROR: {err_msg}")
        main_debug_log(f"CRITICAL STARTUP ERROR:\n{err_msg}")
        
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
