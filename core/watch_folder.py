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
        
        # [v27.10.6.3] Unify Path Logic with Main UI
        import sys
        if getattr(sys, 'frozen', False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.getcwd()
            
        self.processed_db_path = os.path.normpath(os.path.join(base, "watch_folder_history.json"))
        self.processed_files = self.load_history() # Now a dict {path: mtime}
        self.is_running = False
        self._snapshot_requested = False # Flag for async request
        self._last_seen_mtimes = {} # {file_path: mtime} from last scan cycle

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
    
    def clear_history_for_paths(self, paths_to_clear):
        """[v27.10.50] Allow UI to remove specific paths from processed history."""
        changed = False
        for path in paths_to_clear:
            norm = os.path.normpath(path)
            if norm in self.processed_files:
                del self.processed_files[norm]
                changed = True
                print(f"WatchFolderEngine: Cleared history for: {os.path.basename(norm)}")
        if changed:
            self.save_history()

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
            
            # [v27.10.48] Tighter Polling: 1s total for faster detection
            for _ in range(10): 
                if not self.is_running or self._snapshot_requested: break
                time.sleep(0.1)

    def scan(self):
        # [v27.10.20] Fail-safe: Only Master is allowed to scan
        current_role = self.settings.get("cluster_role", "Worker")
        if current_role != "Master":
             if not getattr(self, '_notified_wrong_role', False):
                 print(f"WatchFolderEngine: Scissor! Engine is running but role is '{current_role}'. Aborting scan.")
                 self._notified_wrong_role = True
             return
        self._notified_wrong_role = False

        watch_folders = self.settings.get("watch_folders", [])
        if not watch_folders:
            if not getattr(self, '_notified_empty', False):
                print("WatchFolderEngine: Zero folders configured. Nothing to monitor.")
                self._notified_empty = True
            return
        self._notified_empty = False
        
        # [v27.8.7] State for appearance detection across all folders
        current_files_mtimes = {}

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
                    
                    if os.path.isdir(file_path):
                        continue # [v27.7] Strict: Ignore all subdirectories

                    # Extension filter
                    ext = os.path.splitext(filename)[1].lower()
                    if ext not in [".mxf", ".mp4", ".mov", ".mkv", ".ts", ".mpg", ".avi", ".wmv"]:
                        continue

                    # Avoid temp files (hidden or starting with . or ~)
                    if filename.startswith(".") or filename.startswith("~$"):
                        continue
                        
                    # Rule 1 & 2 Detection (Scan-to-Scan Comparison)
                    try:
                        current_mtime = os.path.getmtime(file_path)
                    except:
                        continue # File might be locked or gone

                    current_files_mtimes[file_path] = current_mtime
                    
                    # [v27.8.7] "No Limits": Trigger if file is NEW to this scan, or mtime changed
                    is_new_appearance = file_path not in self._last_seen_mtimes
                    mtime_changed = (file_path in self._last_seen_mtimes and self._last_seen_mtimes[file_path] != current_mtime)
                    
                    # [v27.10.1] CRITICAL FIX: Only generate tasks on NEW files
                    # mtime_changed means file is being copied/modified - DON'T generate duplicate tasks!
                    if is_new_appearance:
                        # [v27.9.3] Persistence Check: Ignore if already processed with same mtime
                        last_processed_mtime = self.processed_files.get(file_path, 0)
                        if last_processed_mtime == current_mtime:
                            # [v27.10.50] RE-DETECTION GUARD:
                            # On fresh restart, _last_seen_mtimes is empty.
                            # A match in processed_files does NOT mean a live cluster task exists.
                            # We skip ONLY if we have already seen this file in THIS session.
                            # This prevents permanent skip of files like 11集.
                            if len(self._last_seen_mtimes) > 0:
                                # We've already scanned once this session - safe to skip.
                                current_files_mtimes[file_path] = current_mtime 
                                continue
                            else:
                                # First scan of session - DO NOT skip, re-emit to be safe.
                                print(f"WatchFolderEngine: [RE-DETECT] First scan: re-emitting known file: {filename}")
                                # Fall through to detection logic below

                        print(f"WatchFolderEngine: [EVENT] NEW FILE Detected: {filename}")
                        if self.is_file_ready(file_path):
                            print(f"WatchFolderEngine: [READY] Emit detected signal: {filename}")
                            # Update persistent history
                            self.processed_files[file_path] = current_mtime
                            self.save_history()
                            self.file_detected.emit(file_path, wf.get("name", "WatchFolder"))
                        else:
                            # If not ready, we DON'T add it to current_files_mtimes yet 
                            pass
                    elif mtime_changed:
                        print(f"WatchFolderEngine: [UPDATE] File {filename} updated (mtime changed)")
                        # [v27.10.49] Even if it's updated, it will be added to mtimes below 
                        # so it doesn't trigger 'is_new_appearance' yet.
                        current_files_mtimes[file_path] = current_mtime
                            
            except Exception as e:
                print(f"WatchFolderEngine Scan Error [{path}]: {e}")
        
        # Update _last_seen_mtimes for the next scan cycle
        self._last_seen_mtimes = current_files_mtimes

    def is_file_ready(self, file_path):
        """Checks if a file is ready for processing (no size growth and can be opened)."""
        try:
            if not os.path.exists(file_path): return False
            
            size1 = os.path.getsize(file_path)
            # [v27.10.46] Increased wait to 1.5s for slow NAS stability
            time.sleep(1.5)
            if not self.is_running: return False
            size2 = os.path.getsize(file_path)
            
            if size1 == size2 and size1 > 0:
                # [FIX v27.9.17] For network shares (SMB), open() can fail with PermissionError 
                # even when the file is not actually locked. Size stability is more reliable.
                # Try opening for reading but DON'T block if it fails - just warn
                try:
                    # Using "rb" is safer for read-only shares than "ab"
                    with open(file_path, "rb") as f:
                        # Can we read at least one byte?
                        f.read(1)
                except (IOError, PermissionError) as e:
                    # [FIX v27.9.17] Don't block on network permission errors
                    # If size is stable, the file is likely ready despite permission issues
                    print(f"WatchFolderEngine: WARNING - File '{os.path.basename(file_path)}' open test failed: {e}")
                    print(f"WatchFolderEngine: Proceeding anyway since size is stable ({size1} bytes)")
                
                # [FIX v27.9.17] Return True if size is stable, regardless of open() result  
                return True
            else:
                if size1 != size2:
                    print(f"WatchFolderEngine: File '{os.path.basename(file_path)}' still growing ({size1} -> {size2})")
                elif size1 == 0:
                    print(f"WatchFolderEngine: File '{os.path.basename(file_path)}' is empty (size 0)")
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

            # [v27.7] Strict: Only scan Root (Pending/Active)
            # Other subdirectories (_DONE, _ERROR) are now ignored as per user request.
            scan_dir(path, 'pending')
            
        return snapshot
