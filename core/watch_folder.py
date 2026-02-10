import os
import time
import json
from PySide6.QtCore import QObject, QThread, Signal

class WatchFolderEngine(QThread):
    """
    Background engine that polls multiple directories for new video files.
    Automatically triggers transcoding based on folder-specific presets.
    Runs in a separate thread to prevent UI blocking.
    """
    file_detected = Signal(str, str) # file_path, folder_name
    snapshot_ready = Signal(dict) # [NEW] Signal for async dashboard data

    def __init__(self, settings_manager, parent=None):
        super().__init__(parent)
        self.settings = settings_manager
        self.processed_db_path = os.path.join(os.getcwd(), "watch_folder_history.json")
        self.processed_files = self.load_history() # Now a dict {path: mtime}
        self.is_running = False
        self._snapshot_requested = False # Flag for async request

    def request_snapshot(self):
        """Thread-safe request for a snapshot."""
        self._snapshot_requested = True

    def load_history(self):
        if os.path.exists(self.processed_db_path):
            try:
                with open(self.processed_db_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    
                    # [MIGRATION] Convert old list format to dict
                    if isinstance(data, list):
                        new_db = {}
                        for path in data:
                            # If file exists, grab its current mtime to avoid immediate re-process
                            # If not, use 0
                            try:
                                if os.path.exists(path):
                                    new_db[path] = os.path.getmtime(path)
                                else:
                                    new_db[path] = 0
                            except:
                                new_db[path] = 0
                        return new_db
                    elif isinstance(data, dict):
                        return data
                    return {}
            except:
                return {}
        return {}

    def save_history(self):
        try:
            with open(self.processed_db_path, "w", encoding="utf-8") as f:
                json.dump(self.processed_files, f, ensure_ascii=False, indent=2)
        except:
            pass

    def stop(self):
        self.is_running = False
        print(f"WatchFolderEngine: Stopping...")

    def run(self):
        self.is_running = True
        print(f"WatchFolderEngine: Thread Started. Polling intervals: 5s")
        
        while self.is_running:
            try:
                self.scan()
                
                # [NEW] Check for One-Off Snapshot Request
                if self._snapshot_requested:
                    self._snapshot_requested = False
                    data = self.scan_status_snapshot()
                    self.snapshot_ready.emit(data)
                    
            except Exception as e:
                print(f"WatchFolderEngine Thread Error: {e}")
            
            # Sleep in intervals for responsiveness to stop signals
            for _ in range(50): # 5 seconds total
                if not self.is_running or self._snapshot_requested: break
                time.sleep(0.1)

    def scan(self):
        watch_folders = self.settings.get("watch_folders", [])
        if not watch_folders:
            if not getattr(self, '_notified_empty', False):
                print("WatchFolderEngine: Zero folders configured. Nothing to monitor.")
                self._notified_empty = True
            return
        self._notified_empty = False

        for wf in watch_folders:
            if not self.is_running: return
            
            path = wf.get("path")
            enabled = wf.get("enabled", True)
            
            if not enabled:
                continue
                
            if not path or not os.path.exists(path):
                continue
            
            try:
                # print(f"WatchFolderEngine: Scanning {path}...")
                for filename in os.listdir(path):
                    if not self.is_running: return
                    
                    file_path = os.path.normpath(os.path.join(path, filename))
                    
                    # [FIX] Skip special directories (_DONE, _ERROR, _TEMP)
                    if os.path.isdir(file_path):
                        dir_name = os.path.basename(file_path)
                        if dir_name in ["_DONE", "_ERROR", "_TEMP"]:
                            continue
                    
                    if not os.path.isfile(file_path):
                        continue
                    
                    # Extension filter
                    ext = os.path.splitext(filename)[1].lower()
                    if ext not in [".mxf", ".mp4", ".mov", ".mkv", ".ts", ".mpg", ".avi", ".wmv"]:
                        continue

                    # Avoid temp files (hidden or starting with . or ~)
                    if filename.startswith(".") or filename.startswith("~$"):
                        continue
                        
                    # [FIX] Check mtime for duplicates/updates
                    try:
                        current_mtime = os.path.getmtime(file_path)
                    except:
                        continue # File might be locked or gone

                    # If file not in DB OR mtime changed -> Process
                    last_mtime = self.processed_files.get(file_path, -1)
                    
                    # Fuzzy match for float precision issues (optional, but == usually check exact matches)
                    # Let's use exact inequality for now.
                    if last_mtime != current_mtime:
                        print(f"WatchFolderEngine: Checking potential file: {filename} (New/Modified)")
                        # Stability check: ensure file is not being copied
                        if self.is_file_ready(file_path):
                            print(f"WatchFolderEngine: NEW FILE READY: {filename}")
                            self.processed_files[file_path] = current_mtime
                            self.save_history()
                            self.file_detected.emit(file_path, wf.get("name", "WatchFolder"))
                        else:
                            # print(f"WatchFolderEngine: File not ready yet: {filename}")
                            pass
                            
            except Exception as e:
                print(f"WatchFolderEngine Scan Error [{path}]: {e}")

    def is_file_ready(self, file_path):
        """Checks if a file is ready for processing (no size growth and can be opened)."""
        try:
            if not os.path.exists(file_path): return False
            
            size1 = os.path.getsize(file_path)
            # [OPTIMIZED] Short wait to check for growth (0.5s for faster response)
            time.sleep(0.5)
            if not self.is_running: return False
            size2 = os.path.getsize(file_path)
            
            if size1 == size2 and size1 > 0:
                # Try opening for reading - if this fails on Windows, it's often because another process has it open for writing
                try:
                    # Using "rb" is safer for read-only shares than "ab"
                    with open(file_path, "rb") as f:
                        # Can we read at least one byte?
                        f.read(1)
                        return True
                except (IOError, PermissionError) as e:
                    # print(f"WatchFolderEngine: File '{os.path.basename(file_path)}' locked: {e}")
                    return False
        except Exception as e:
            print(f"WatchFolderEngine: Error checking file readiness: {e}")
        return False
    def scan_status_snapshot(self):
        """
        Returns a snapshot of the current file system state for Dashboard population.
        Result: {'pending': [], 'done': [], 'error': []}
        item structure: {'path', 'base_name', 'folder_name', 'timestamp', 'log_content'}
        """
        snapshot = {'pending': [], 'done': [], 'error': []}
        watch_folders = self.settings.get("watch_folders", [])
        
        valid_exts = [".mxf", ".mp4", ".mov", ".mkv", ".ts", ".mpg", ".avi", ".wmv"]
        
        for wf in watch_folders:
            if not wf.get("enabled", True): continue
            path = wf.get("path")
            if not path or not os.path.exists(path): continue
            
            folder_name = wf.get("name", "WatchFolder")
            
            # Helper to scan a dir
            def scan_dir(target_dir, category):
                if not os.path.exists(target_dir): return
                try:
                    for fname in os.listdir(target_dir):
                        fpath = os.path.join(target_dir, fname)
                        if not os.path.isfile(fpath): continue
                        
                        base, ext = os.path.splitext(fname)
                        # Skip logs for file list (logs are read separately for errors)
                        if ext.lower() == ".log": continue 
                        if ext.lower() not in valid_exts: continue
                        
                        item = {
                            "path": fpath,
                            "base_name": fname, # [FIX] Use full filename to match ClusterManager's convention (foo.ts vs foo)
                            "folder_name": folder_name,
                            "timestamp": os.path.getmtime(fpath)
                        }
                        
                        # Special handling for Error Logs
                        if category == 'error':
                            log_path = os.path.join(target_dir, f"{base}.log")
                            if os.path.exists(log_path):
                                try:
                                    with open(log_path, 'r', encoding='utf-8', errors='ignore') as logf:
                                        item["log_content"] = logf.read()
                                except:
                                    item["log_content"] = "Read Error Log Failed"
                        
                        snapshot[category].append(item)
                except Exception as e:
                    print(f"Snapshot Scan Error {target_dir}: {e}")

            # 1. Scan Root (Pending)
            scan_dir(path, 'pending')
            
            # 2. Scan _DONE
            scan_dir(os.path.join(path, "_DONE"), 'done')
            
            # 3. Scan _ERROR
            scan_dir(os.path.join(path, "_ERROR"), 'error')
            
        return snapshot
