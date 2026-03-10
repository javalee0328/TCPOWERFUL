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
    file_detected = Signal(str, str, bool, bool) # file_path, folder_name, is_repeat, is_qc_mode
    snapshot_ready = Signal(dict) # [NEW] Signal for async dashboard data
    log_message = Signal(str)    # [v27.10.77] Real-time log for Dashboard

    def __init__(self, settings_manager, parent=None):
        super().__init__(parent)
        self.settings = settings_manager
        
        from core.settings import get_app_path
        self.processed_db_path = get_app_path("watch_folder_history.json")

        self.processed_files = self.load_history() # Now a dict {path: mtime}
        self._last_seen_mtimes = {}  # mtime from last scan
        self._seen_this_session = set()  # [v27.10.52] Files seen at least once this session
        self._sniff_cache = {} # [v27.10.89] Cache for extension-less files: {filepath: is_valid_media}
        self.is_running = False
        self._snapshot_requested = False # Flag for async request

    def _log(self, msg):
        """[v27.10.77] Emit to both console and UI log panel."""
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        full = f"[{ts}] {msg}"
        print(full)
        try: self.log_message.emit(full)
        except: pass

    def request_snapshot(self):
        """Thread-safe request for a snapshot."""
        self._snapshot_requested = True

    def force_scan(self):
        """[v27.10.78] Force an immediate, fresh scan of all folders."""
        self._last_seen_mtimes.clear()
        self._seen_this_session.clear()

    def _get_cleared_basenames(self):
        """[v27.11.0] Load cleared task basenames (no extension, no timestamp) from cleared_tasks.json.
        Returns a set of lowercase, extension-stripped, timestamp-stripped filenames.
        E.g.: {'真相對話錄第4集##phd', '星空下的仁醫#9'}
        """
        import re
        try:
            from core.settings import get_app_path
            cleared_path = get_app_path("cleared_tasks.json")
            if not os.path.exists(cleared_path):
                return set()
            with open(cleared_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            result = set()
            for item in raw:
                # New format: 'file::basename'
                if item.startswith("file::"):
                    result.add(item[6:].lower())
                elif "::" in item:
                    # Old format: path::basename.ext
                    parts = item.split("::", 1)
                    base_no_ext = os.path.splitext(parts[1].lower())[0]
                    base_no_ext = re.sub(r'_\d{6}$', '', base_no_ext)
                    result.add(base_no_ext)
            return result
        except:
            return set()

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
                 self._log(f"WatchFolderEngine: 角色 '{current_role}' 非主節點，暫停掃描。")
                 self._notified_wrong_role = True
             return
        self._notified_wrong_role = False

        watch_folders = self.settings.get("watch_folders", [])
        if not watch_folders:
            if not getattr(self, '_notified_empty', False):
                self._log("WatchFolderEngine: 未設定監控目錄。")
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
            
            # [v27.10.68] Pro-active folder creation (DONE, TEMP, ERROR)
            try:
                for sub in ["DONE", "TEMP", "ERROR"]:
                    p = os.path.join(path, sub)
                    if not os.path.exists(p):
                        os.makedirs(p, exist_ok=True)
            except: pass
            
            try:
                # print(f"WatchFolderEngine: Scanning {path}...")
                for filename in os.listdir(path):
                    if not self.is_running: return
                    
                    file_path = os.path.normpath(os.path.join(path, filename))
                    
                    if os.path.isdir(file_path):
                        continue # [v27.7] Strict: Ignore all subdirectories

                    # Extension filter
                    ext = os.path.splitext(filename)[1].lower()
                    allowed_exts = [".mxf", ".mp4", ".mov", ".mkv", ".ts", ".mpg", ".avi", ".wmv"]
                    
                    # [v27.10.89] FFprobe Fallback for Extension-less files
                    if ext not in allowed_exts:
                        # If no extension or unknown, try a quick ffprobe sniff if file > 1MB
                        # to avoid probing tiny text logs
                        if file_path.startswith(".") or filename.startswith("~$"):
                            continue
                            
                        # Use cache to prevent infinite ffprobe loop on valid media or confirmed non-media
                        if file_path in self._sniff_cache:
                            if not self._sniff_cache[file_path]:
                                continue
                        else:
                            is_valid_media = False
                            sniff_successful = False # Track if probe actually completed without error
                            try:
                                if os.path.getsize(file_path) > 1024 * 1024:
                                    import subprocess
                                    try:
                                        out = subprocess.check_output([
                                            "ffprobe", "-v", "quiet", "-show_format", "-print_format", "json", file_path
                                        ], timeout=2, stderr=subprocess.STDOUT)
                                        sniff_successful = True # Command executed successfully
                                        
                                        probe_data = json.loads(out)
                                        if "format" in probe_data and probe_data["format"].get("format_name"):
                                            fmt = probe_data["format"]["format_name"].lower()
                                            # Basic sanity check that it's a media container
                                            if any(x in fmt for x in ["mxf", "mp4", "mov", "matroska", "mpeg", "avi"]):
                                                is_valid_media = True
                                                print(f"WatchFolderEngine: [Sniffed] Extension-less file {filename} is valid media ({fmt}).")
                                    except subprocess.CalledProcessError as e:
                                        pass # FFprobe explicitly failed (e.g. file locked or corrupted)
                                    except subprocess.TimeoutExpired as e:
                                        pass # Took too long
                                    except Exception as e:
                                        pass # JSON error or other
                            except OSError:
                                pass # File locked during getsize()
                                
                            if is_valid_media:
                                self._sniff_cache[file_path] = True
                            elif sniff_successful:
                                # Only blacklist if FFprobe definitively proved it was not media
                                self._sniff_cache[file_path] = False
                                
                            if not is_valid_media:
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
                    
                    # [v27.10.61] Determine if this file is "new" to the current scan cycle
                    is_new_appearance = file_path not in self._last_seen_mtimes
                    mtime_changed = (file_path in self._last_seen_mtimes and self._last_seen_mtimes[file_path] != current_mtime)
                    
                    if is_new_appearance:
                        if file_path in self._seen_this_session:
                            # Already handled in this session - skip to avoid duplicate every poll
                            current_files_mtimes[file_path] = current_mtime
                            continue
                        
                        # [v27.10.61] KEY FIX: If the file is already in persistent history
                        # WITH THE SAME mtime, it was processed in a prior session.
                        # Skip it silently to prevent re-queuing on every app restart.
                        hist_mtime = self.processed_files.get(file_path)
                        
                        is_history_repeat = (hist_mtime is not None)
                        
                        if is_history_repeat and abs(hist_mtime - current_mtime) < 2.0:
                            # File is unchanged since last run. Mark as seen so we don't re-check.
                            self._seen_this_session.add(file_path)
                            current_files_mtimes[file_path] = current_mtime
                            continue

                        print(f"WatchFolderEngine: [EVENT] NEW FILE Detected: {filename} (Repeat: {is_history_repeat})")
                        if self.is_file_ready(file_path):
                            self._log(f"[\u65b0\u6a94] {filename}")
                            self._seen_this_session.add(file_path)
                            is_qc = wf.get("qc_mode", False)
                            self.file_detected.emit(file_path, wf.get("name", "WatchFolder"), is_history_repeat, is_qc)
                            # Update persistent history AFTER emit
                            self.processed_files[file_path] = current_mtime
                            self.save_history()

                    elif mtime_changed:
                        self._log(f"[更新] {filename} (mtime 變更)")
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
                    print(f"WatchFolderEngine: WARNING - '{os.path.basename(file_path)}' 開啟測試失敗: {e}")
                
                # [FIX v27.9.17] Return True if size is stable, regardless of open() result  
                return True
            else:
                if size1 != size2:
                    self._log(f"[等待] {os.path.basename(file_path)} 仍在寫入 ({size1}→{size2} bytes)")
                elif size1 == 0:
                    self._log(f"[跳過] {os.path.basename(file_path)} 文件大小為 0")
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
        
        # [v27.10.62] Gate: Only Master should scan NAS for snapshot to avoid redundancy/conflicts
        current_role = self.settings.get("cluster_role", "Worker")
        if current_role != "Master":
             return snapshot

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

            # [v27.10.68] Strict: Only scan Root (Pending/Active)
            # subdirectories (DONE, ERROR, _DONE, _ERROR) are now ignored.
            scan_dir(path, 'pending')
            
        return snapshot
