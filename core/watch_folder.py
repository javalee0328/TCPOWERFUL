import os
import time
import json
from PySide6.QtCore import QObject, QTimer, Signal

class WatchFolderEngine(QObject):
    """
    Background engine that polls multiple directories for new video files.
    Automatically triggers transcoding based on folder-specific presets.
    """
    file_detected = Signal(str, str) # file_path, folder_name

    def __init__(self, settings_manager, parent=None):
        super().__init__(parent)
        self.settings = settings_manager
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.scan)
        
        # Determine history path (frozen vs dev)
        base_dir = os.path.dirname(os.path.abspath(__file__)) # This is core/
        if getattr(os, 'frozen', False):
            base_dir = os.path.dirname(os.path.abspath(__file__))
        
        self.processed_db_path = os.path.join(os.getcwd(), "watch_folder_history.json")
        self.processed_files = self.load_history()
        self.is_running = False

    def load_history(self):
        if os.path.exists(self.processed_db_path):
            try:
                with open(self.processed_db_path, "r", encoding="utf-8") as f:
                    return set(json.load(f))
            except:
                return set()
        return set()

    def save_history(self):
        try:
            with open(self.processed_db_path, "w", encoding="utf-8") as f:
                json.dump(list(self.processed_files), f, ensure_ascii=False, indent=2)
        except:
            pass

    def start(self):
        if not self.is_running:
            self.is_running = True
            self.timer.start(5000) # Scan every 5 seconds
            print(f"WatchFolderEngine: Started polling at 5s intervals")

    def stop(self):
        self.is_running = False
        self.timer.stop()
        print(f"WatchFolderEngine: Stopped")

    def scan(self):
        # Format of "watch_folders" in settings: 
        # [{"name": "Ingest_CH02", "path": "D:/Watch", "preset": "XDCAM_HD422"}, ...]
        watch_folders = self.settings.get("watch_folders", [])
        if not watch_folders:
            return

        for wf in watch_folders:
            path = wf.get("path")
            if not path or not os.path.exists(path):
                continue
            
            try:
                for filename in os.listdir(path):
                    file_path = os.path.join(path, filename)
                    if not os.path.isfile(file_path):
                        continue
                    
                    # Extension filter
                    ext = os.path.splitext(filename)[1].lower()
                    if ext not in [".mxf", ".mp4", ".mov", ".mkv", ".ts", ".mpg", ".avi", ".wmv"]:
                        continue

                    # Avoid temp files (hidden or starting with . or ~)
                    if filename.startswith(".") or filename.startswith("~$"):
                        continue

                    if file_path not in self.processed_files:
                        # Stability check: ensure file is not being copied
                        if self.is_file_ready(file_path):
                            print(f"WatchFolderEngine: New File Detected: {filename}")
                            self.processed_files.add(file_path)
                            self.save_history()
                            self.file_detected.emit(file_path, wf.get("name", "WatchFolder"))
            except Exception as e:
                print(f"WatchFolderEngine Scan Error [{path}]: {e}")

    def is_file_ready(self, file_path):
        """Checks if a file is ready for processing (no size growth and can be opened exclusively)."""
        try:
            if not os.path.exists(file_path): return False
            
            size1 = os.path.getsize(file_path)
            # Short wait to check for growth
            time.sleep(1.0)
            size2 = os.path.getsize(file_path)
            
            if size1 == size2 and size1 > 0:
                # Try exclusive access - if this fails, the file is likely still being written
                try:
                    # On Windows, opening with 'ab' checks if we can append, which requires write access
                    with open(file_path, "ab"):
                        return True
                except (IOError, PermissionError):
                    return False
        except:
            pass
        return False
