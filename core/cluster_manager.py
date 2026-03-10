import os
import json
import socket
import datetime
import time
from PySide6.QtCore import QObject, QTimer, Signal
import psutil
import random # [NEW] For backoff
import hashlib
from core.settings import CURRENT_VERSION, debug_log

class ClusterWorker(QObject):
    """
    Background worker for ClusterManager to prevent UI freezes due to slow Network IO.
    """
    task_synced = Signal(dict)
    node_updated = Signal(dict)
    watch_config_synced = Signal(list) # [NEW] Signal for synced watch folders
    role_changed = Signal(str) # [NEW] Signal for role change
    master_stale_detected = Signal() # [v27.10.20] Signal for failover
    task_removed = Signal(str) # [FIX v27.9.15] Signal for task deletion
    finished = Signal()

    def __init__(self, cluster_path, node_id, settings_dict):
        super().__init__()
        self._cluster_path = cluster_path
        # [v27.10.60] Identify if this is a placeholder path to avoid root clutter
        self._path_is_placeholder = settings_dict.get("_path_is_placeholder", False)
        self.node_id = node_id
        # We pass a dict copy of settings to avoid thread safety issues with SettingsManager
        self.settings = settings_dict 
        self.lock = None # Will be set for thread safety if needed, but dict.update is mostly atomic
        self.running = True
        self._known_tasks = {}
        self._known_nodes = {}
        self.current_activity = "Idle"
        self.active_task_count = 0
        self.total_ram_gb = round(psutil.virtual_memory().total / (1024**3), 1)
        self._first_sync = True
        self._node_failure_penalty = {} # [v27.10.76] {node_id: {count, expires_at}}
        self._rr_index = 0 # [v27.10.77] Round-Robin pointer for even task distribution


    def run_loop(self):
        """Main loop for the worker thread."""
        print(f"ClusterWorker: Thread Started. ID={self.node_id}")
        self.initialize_structure()
        
        while self.running:
            try:
                self.sync()
            except Exception as e:
                print(f"ClusterWorker: Cycle Error - {e}")
                import traceback
                traceback.print_exc()
            
            # Sleep 5s (but check running flag frequently)
            for _ in range(50): 
                if not self.running: break
                time.sleep(0.1)
                
        print("ClusterWorker: Thread Finished.")
        try:
            self.finished.emit()
        except RuntimeError:
            pass  # Object was deleted before thread had a chance to emit

    def stop(self):
        self.running = False
        print("ClusterWorker: Stop Requested.")

    def record_node_failure(self, node_id):
        """[v27.10.76] Record a task failure for a node; penalizes it in future scoring."""
        now = time.time()
        entry = self._node_failure_penalty.get(node_id, {'count': 0, 'expires_at': 0})
        if now > entry['expires_at']:
            entry = {'count': 0, 'expires_at': 0}
        entry['count'] += 1
        entry['expires_at'] = now + 300  # Penalty lasts 5 min
        self._node_failure_penalty[node_id] = entry
        debug_log(f"Cluster: Failure penalty recorded for {node_id} (total={entry['count']})")

    def _get_failure_penalty(self, node_id):
        """[v27.10.76] Returns score penalty for a failing node."""
        now = time.time()
        entry = self._node_failure_penalty.get(node_id)
        if not entry or now > entry.get('expires_at', 0):
            return 0
        return entry['count'] * 500  # Each failure adds 500 to score (heavily penalized)


    def set_activity(self, activity, count=0):
        self.current_activity = activity
        self.active_task_count = count

    def _get_local_ip(self):
        """[v27.10.24] Robust UDP probe to get correct LAN IP."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Doesn't even have to be reachable
            s.connect(('8.8.8.8', 1))
            IP = s.getsockname()[0]
        except Exception:
            try:
                IP = socket.gethostbyname(socket.gethostname())
            except:
                IP = "127.0.0.1"
        finally:
            s.close()
        return IP

    def update_settings(self, new_settings):
        """Allows main thread to update settings without restarting worker."""
        # Update our internal dict copy
        self.settings.update(new_settings)


    def initialize_structure(self):
        """Create necessary subdirectories only if a valid path is provided."""
        try:
            # [v27.10.67] Relax deferral: Allow if it's the internal CLUSTER_SYNC folder
            # This ensures default local setup works out of the box.
            if self._path_is_placeholder:
                if "CLUSTER_SYNC" not in self._cluster_path:
                    print(f"ClusterWorker: Deferring folder creation (Remote Placeholder Path)")
                    return
            
            if not os.path.exists(self._cluster_path):
                os.makedirs(self._cluster_path, exist_ok=True)
            
            for sub in ["nodes", "tasks", "logs"]:
                path = os.path.join(self._cluster_path, sub)
                if not os.path.exists(path):
                    os.makedirs(path, exist_ok=True)
            print(f"ClusterWorker: Initialized structure at {self._cluster_path}")
        except Exception as e:
            print(f"ClusterWorker: Init Error - {e}")

    def sync(self):
        # [v27.10.67] Ensure structure exists (Recover if wiped by Master Reset)
        # We only do this if we have a path.
        if self._cluster_path and os.path.isabs(self._cluster_path):
            if not os.path.exists(os.path.join(self._cluster_path, "nodes")):
                self.initialize_structure()

        # 3. [NEW] Leader Election & Role Enforcement
        self._perform_leader_election()
        
        # 1. Heartbeat
        self._update_my_heartbeat("Online")
        
        # 2. [FIX v27.10.24] Restore Node Discovery
        # Scans nodes/ folder for other participants
        self._discover_nodes()
        # 4. Task Logic (Split by Role)
        my_role = self.settings.get("cluster_role", "Worker")
        
        if my_role == "Master":
             # Master: Allocates Tasks
             self._allocate_pending_tasks()
             # [NEW] Master: Broadcast Watch Config
             self._sync_watch_config(role="Master")
             
             # [NEW v27.10.20] Master: Backup Global Settings for Failover
             self._backup_master_settings()
        else:
             # [NEW] Worker: Read Watch Config
             self._sync_watch_config(role="Worker")
        
        # All Nodes: Sync Assigned Tasks (Worker reads assignments, Master reads status updates)
        if self.settings.get("cluster_sync_tasks", True):
             self._sync_tasks()


    def _perform_leader_election(self):
        """
        Attempts to acquire the 'master.lock' file.
        """
        lock_file = os.path.join(self._cluster_path, "master.lock")
        now = time.time()
        
        current_lock = {}
        lock_stale = False
        
        if os.path.exists(lock_file):
            try:
                with open(lock_file, 'r', encoding='utf-8') as f:
                    current_lock = json.load(f)
                
                last_seen_lock = current_lock.get("timestamp", 0)
                if now - last_seen_lock > 15:  # [v27.10.77] 15s (was 30s) for faster failover
                    lock_stale = True
            except:
                lock_stale = True
        
        my_role = "Worker"
        
        if lock_stale:
            # [v27.10.49] AUTO-FAILOVER: If lock is stale, take over!
            # Using randomized jitter based on node ID to avoid multiple nodes fighting
            # This ensures only one node wins the race condition on the lock file.
            jitter = int(hashlib.md5(self.node_id.encode()).hexdigest(), 16) % 5  # [v27.10.77] max 1s (was 3s)
            time.sleep(jitter * 0.2)
            
            # Re-read lock after jitter to see if someone else claimed it
            if os.path.exists(lock_file):
                try:
                    with open(lock_file, 'r', encoding='utf-8') as f:
                        check_lock = json.load(f)
                    check_ts = check_lock.get("timestamp", 0)
                    if abs(now - check_ts) < 15: # [v27.10.77] Match new stale threshold
                        lock_stale = False
                except: pass
            
            if lock_stale:
                debug_log(f"Cluster: Stale Master Lock detected. Performing Autonomous Failover: {self.node_id}")
                self._write_master_lock(lock_file, now)
                my_role = "Master"
                self.master_stale_detected.emit() # Signal for UI update if needed
        
        if current_lock.get("node_id") == self.node_id:
            my_role = "Master"
            self._write_master_lock(lock_file, now)
        elif not os.path.exists(lock_file):
             self._write_master_lock(lock_file, now)
             my_role = "Master"
        else:
             # Lock exists, not stale, not ours -> We are a Worker
             my_role = "Worker"
            
        if my_role == "Master":
             # Tie-break: Higher ID wins if two nodes claim simultaneously
             # (though file system lock usually prevents this)
             for other_id, data in self._known_nodes.items():
                 if other_id == self.node_id: continue
                 if data.get("role") == "Master" and data.get("status") == "Online":
                     if self.node_id < other_id:
                         my_role = "Worker"
                         break
            
        current_stored = self.settings.get("cluster_role", "Worker")
        if current_stored != my_role or self._first_sync:
            self.settings["cluster_role"] = my_role
            self.role_changed.emit(my_role)
            self._first_sync = False

    def _write_master_lock(self, lock_file, timestamp):
        data = {
            "node_id": self.node_id,
            "timestamp": timestamp,
            "hostname": socket.gethostname()
        }
        try:
            with open(lock_file, 'w', encoding='utf-8') as f:
                json.dump(data, f)
                f.flush()
                try: os.fsync(f.fileno())
                except: pass
        except Exception as e:
             print(f"Cluster: Failed to write lock: {e}")

    def _update_my_heartbeat(self, status):
        node_file = os.path.join(self._cluster_path, "nodes", f"{self.node_id}.json")
        cpu = 0
        ram = 0
        try:
            cpu = psutil.cpu_percent()
            ram = psutil.virtual_memory().percent
        except: pass

        hb_data = {
            "node_id": self.node_id,
            "ip": self._get_local_ip(),
            "last_seen": time.time(),
            "status": status,
            "role": self.settings.get("cluster_role", "Master"),
            "version": CURRENT_VERSION,
            "cpu_usage": cpu,
            "ram_usage": ram,
            "total_ram": self.total_ram_gb,
            "current_activity": self.current_activity,
            "active_task_count": self.active_task_count,
            "alias": self.settings.get("worker_alias") or self.node_id
        }
        try:
            # [v27.10.67] Final fallback to ensure dir exists
            os.makedirs(os.path.dirname(node_file), exist_ok=True)
            with open(node_file, "w", encoding="utf-8") as f:
                json.dump(hb_data, f, indent=2)
                f.flush()
                try: os.fsync(f.fileno())
                except: pass 
            self._known_nodes[self.node_id] = hb_data
        except Exception as e:
            print(f"Cluster: Heartbeat Write Error - {e}")

    def _discover_nodes(self):
        node_dir = os.path.join(self._cluster_path, "nodes")
        if not os.path.exists(node_dir): return
        try:
            now = time.time()
            cluster_nodes = [f for f in os.listdir(node_dir) if f.endswith(".json")]
            for filename in cluster_nodes:
                filepath = os.path.join(node_dir, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        data["_filepath"] = filepath
                        data["_filename"] = filename
                        nid = data.get("node_id")
                        if nid:
                            ls = data.get("last_seen")
                            # [v27.10.41] Robust type handling for last_seen
                            ts = ls
                            if ls:
                                try:
                                    ts = ls if isinstance(ls, (int, float)) else datetime.datetime.fromisoformat(ls).timestamp()
                                    # [v27.10.49] Use abs() for diff to tolerate clock skew/future timestamps
                                    diff = abs(now - ts)
                                    IS_ONE_YEAR = abs(diff - 31536000) < 3600
                                    
                                    # [v27.10.64] Cleanup Logic: Remove files older than 1 hour
                                    if diff > 3600 and not IS_ONE_YEAR:
                                        try: 
                                            os.remove(filepath)
                                            debug_log(f"Cluster: Cleaned up stale node file: {filename}")
                                            continue 
                                        except: pass

                                    if diff < 60 or IS_ONE_YEAR: data["status"] = "Online"
                                    else: data["status"] = "Offline (Timeout)"
                                    data["last_seen"] = ts
                                except: pass
                            
                            self._known_nodes[nid] = data
                            self.node_updated.emit(data)
                except Exception as e:
                    print(f"Cluster: Discovery Error {filename} - {e}")
        except: pass


    def data_cpu_usage(self):
        try: return psutil.cpu_percent()
        except: return 0

    def _allocate_pending_tasks(self):
        task_dir = os.path.join(self._cluster_path, "tasks")
        if not os.path.exists(task_dir): return
        
        workers = []
        now = datetime.datetime.now()
        if not hasattr(self, '_transient_assignment_cache'):
             self._transient_assignment_cache = {}
             
        self._transient_assignment_cache = {k:v for k,v in self._transient_assignment_cache.items() 
                                          if (now - v['time']).total_seconds() < 10}

        for nid, data in self._known_nodes.items():
            pending_count = self._transient_assignment_cache.get(nid, {}).get('count', 0)
            real_tasks = data.get("active_task_count", 0)
            effective_tasks = real_tasks + pending_count
            score = (effective_tasks * 100) + data.get("cpu_usage", 0)
            is_remote_worker = (nid != self.node_id)
            
            # Robust Clock-Skew Tolerance
            # [v27.10.40] Robust timing with Clock Skew tolerance
            is_recent = False
            ls = data.get("last_seen")
            if ls:
                try:
                    ts = ls if isinstance(ls, (int, float)) else datetime.datetime.fromisoformat(ls).timestamp()
                    diff = time.time() - ts
                    IS_ONE_YEAR = abs(abs(diff) - 31536000) < 3600
                    # Standard 600s check, but using abs() to handle future timestamps (clock skew)
                    if abs(diff) < 60 or IS_ONE_YEAR: is_recent = True
                except: pass

            status = data.get("status", "Unknown")
            # If status is Online, we trust it or use is_recent for auto-recovery
            is_online = (status == "Online" or status == "Online (Local)" or is_recent)
            if not is_online: continue

            workers.append({
                "id": nid, "score": score, "data": data, "tasks": effective_tasks,
                "is_remote": is_remote_worker, "is_active": True
            })
            
        master_entry = next((w for w in workers if w["id"] == self.node_id), None)
        if master_entry:
            pending_count = self._transient_assignment_cache.get(self.node_id, {}).get('count', 0)
            eff_tasks = self.active_task_count + pending_count
            master_entry["data"]["active_task_count"] = self.active_task_count
            master_entry["tasks"] = eff_tasks
            # [v27.10.50] ZERO MASTER PENALTY: No base penalty (+0).
            master_entry["score"] = (eff_tasks * 100) + self.data_cpu_usage()
        else:
            pending_count = self._transient_assignment_cache.get(self.node_id, {}).get('count', 0)
            eff_tasks = self.active_task_count + pending_count
            has_remote = any(w["is_remote"] for w in workers)
            score = 999999 if has_remote else (eff_tasks * 100) + self.data_cpu_usage()
            workers.append({
                "id": self.node_id, "score": score,
                "data": { "node_id": self.node_id, "status": "Online", "active_task_count": self.active_task_count, "cpu_usage": self.data_cpu_usage() },
                "tasks": eff_tasks, "is_remote": False, "is_active": True
            })
             
        if not workers: return
        
        workers.sort(key=lambda x: x["score"])
        try:
            for filename in os.listdir(task_dir):
                if not filename.endswith(".json"): continue
                filepath = os.path.join(task_dir, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        task_data = json.load(f)
                    status = task_data.get("cluster_status", "Pending")
                    assigned_to = task_data.get("assigned_to")

                    # [v27.10.76] Auto-reassign Failed tasks to a different node
                    if status == "Failed" and assigned_to:
                        failed_node = assigned_to
                        self.record_node_failure(failed_node)
                        alt_workers = [w for w in workers if w["id"] != failed_node]
                        if not alt_workers:
                            alt_workers = workers
                        if alt_workers:
                            alt_workers.sort(key=lambda x: x["score"] + self._get_failure_penalty(x["id"]))
                            best_alt = alt_workers[0]
                            task_data["assigned_to"] = best_alt["id"]
                            task_data["cluster_status"] = "Pending"
                            task_data["assignment_time"] = datetime.datetime.now().isoformat()
                            task_data["previous_failure"] = failed_node
                            with open(filepath, "w", encoding="utf-8") as f:
                                json.dump(task_data, f, indent=2, ensure_ascii=False)
                            best_alt["tasks"] += 1
                            best_alt["score"] += 100
                            debug_log(f"Cluster: Reassigned failed task from {failed_node} -> {best_alt['id']}")
                        continue

                    if status == "Pending" and not assigned_to:
                        # [v27.10.77] Round-Robin: pick next node in rotation, skip penalized nodes
                        eligible = [w for w in workers if self._get_failure_penalty(w["id"]) == 0]
                        if not eligible:
                            eligible = workers  # fallback: use all if all penalized
                        self._rr_index = self._rr_index % len(eligible)
                        best_worker = eligible[self._rr_index]
                        self._rr_index = (self._rr_index + 1) % len(eligible)
                        assigned_nid = best_worker["id"]
                        task_data["assigned_to"] = assigned_nid
                        task_data["cluster_status"] = "Assigned"
                        task_data["assignment_time"] = datetime.datetime.now().isoformat()
                        with open(filepath, "w", encoding="utf-8") as f:
                            json.dump(task_data, f, indent=2, ensure_ascii=False)
                        best_worker["tasks"] += 1
                        best_worker["score"] += 100 
                        now_ts = datetime.datetime.now()
                        cache_entry = self._transient_assignment_cache.get(assigned_nid, {'count': 0})
                        self._transient_assignment_cache[assigned_nid] = { 'count': cache_entry['count'] + 1, 'time': now_ts }
                        workers.sort(key=lambda x: x["score"])
                except Exception as e: pass
        except: pass

    def _sync_tasks(self):
        task_dir = os.path.join(self._cluster_path, "tasks")
        if not os.path.exists(task_dir): return
        scanned_files = set()
        try:
            for filename in os.listdir(task_dir):
                if filename.endswith(".json"):
                    scanned_files.add(filename)
                    filepath = os.path.join(task_dir, filename)
                    try:
                        mtime = os.path.getmtime(filepath)
                        if filename not in self._known_tasks or mtime > self._known_tasks[filename]:
                            self._known_tasks[filename] = mtime
                            task_data = None
                            for _ in range(3):
                                    try:
                                        with open(filepath, "r", encoding="utf-8") as f:
                                            task_data = json.load(f)
                                        break
                                    except: time.sleep(0.1)
                            if not task_data: continue
                            task_data["cluster_filename"] = filename
                            self.task_synced.emit(task_data)
                    except: pass
            missing_files = [f for f in self._known_tasks.keys() if f not in scanned_files]
            for missing in missing_files:
                del self._known_tasks[missing]
                self.task_removed.emit(missing)
        except Exception as e:
            print(f"Cluster: Sync Loop Error: {e}")

    def _sync_watch_config(self, role):
        config_file = os.path.join(self._cluster_path, "watch_config.json")
        if role == "Master":
            try:
                watch_list = self.settings.get("watch_folders", [])
                write_needed = True
                if os.path.exists(config_file):
                    try:
                        with open(config_file, 'r', encoding='utf-8') as f:
                            existing = json.load(f)
                        if existing == watch_list: write_needed = False
                    except: pass
                if write_needed:
                    with open(config_file, 'w', encoding='utf-8') as f:
                        json.dump(watch_list, f, indent=2, ensure_ascii=False)
            except: pass
        else:
            if os.path.exists(config_file):
                try:
                    mtime = os.path.getmtime(config_file)
                    if not hasattr(self, '_last_watch_config_mtime') or mtime > self._last_watch_config_mtime:
                        self._last_watch_config_mtime = mtime
                        with open(config_file, 'r', encoding='utf-8') as f:
                            remote_config = json.load(f)
                        self.watch_config_synced.emit(remote_config)
                except: pass

    def _backup_master_settings(self):
        backup_file = os.path.join(self._cluster_path, "master_config_backup.json")
        try:
             keys = ["watch_folders", "max_parallel_tasks", "worker_output_path", "history_retention"]
             backup_data = {k: self.settings.get(k) for k in keys if k in self.settings}
             backup_data["backup_time"] = time.time()
             backup_data["master_node_id"] = self.node_id
             with open(backup_file, 'w', encoding='utf-8') as f:
                 json.dump(backup_data, f, indent=2, ensure_ascii=False)
        except: pass

    def promote_to_master(self):
        lock_file = os.path.join(self._cluster_path, "master.lock")
        self._write_master_lock(lock_file, time.time())


class ClusterManager(QObject):
    """
    Main thread interface for the ClusterWorker.
    """
    task_synced = Signal(dict)
    task_removed = Signal(str)
    node_updated = Signal(dict)
    watch_config_synced = Signal(list)
    role_changed = Signal(str)
    master_stale_detected = Signal()

    def __init__(self, settings_manager, parent=None):
        super().__init__(parent)
        self.settings = settings_manager
        
        # [v27.10.64] PERSISTENT NODE ID: Load from settings or generate once
        stored_id = self.settings.get("cluster_node_id")
        if stored_id:
            self.node_id = stored_id
        else:
            import uuid
            import socket
            try:
                # Use hostname + last 6 digits of MAC for uniqueness
                mac = str(uuid.getnode())[-6:]
                hostname = socket.gethostname()
                self.node_id = f"{hostname}-{mac}"
            except:
                # Last resort fallback to random uuid
                self.node_id = f"Node-{str(uuid.uuid4())[:8]}"
            
            self.settings.set("cluster_node_id", self.node_id)
            debug_log(f"[CLUSTER] Generated new persistent Node ID: {self.node_id}")

        from core.settings import BASE_DIR
        
        # [v27.10.60] Check if path is explicitly set in settings
        raw_path = self.settings.get("cluster_path")
        if not raw_path:
            # No path saved. Use relative placeholder but mark as placeholder.
            self._cluster_path = os.path.join(BASE_DIR, "CLUSTER_SYNC")
            self._path_is_placeholder = True
        elif not os.path.isabs(raw_path):
             # Relative path saved. Treat as placeholder/relative deferred.
             self._cluster_path = os.path.abspath(os.path.join(BASE_DIR, raw_path))
             self._path_is_placeholder = True
        else:
            self._cluster_path = os.path.normpath(raw_path)
            self._path_is_placeholder = False
        
        # Only create if it's a real, absolute, and non-placeholder path
        if not self._path_is_placeholder:
            try:
                if not os.path.exists(self._cluster_path):
                    os.makedirs(self._cluster_path, exist_ok=True)
            except:
                import tempfile
                self._cluster_path = os.path.join(tempfile.gettempdir(), "ProTranscoder_Cluster")
                if not os.path.exists(self._cluster_path):
                    os.makedirs(self._cluster_path, exist_ok=True)

        debug_log(f"[CLUSTER] Init Path: {self._cluster_path} (Placeholder: {self._path_is_placeholder})")

        self.worker = None
        self.thread = None
        self._known_nodes_cache = {} 
        self._transient_assignment_cache = {} # [v27.10.50] Track recent assignments to handle bursts

    @property
    def master_id(self):
        for nid, data in self._known_nodes_cache.items():
            if data.get("role") == "Master" and data.get("status") == "Online": return nid
        return None

    def start(self):
        if self.thread and self.thread.isRunning(): return
        from PySide6.QtCore import QThread
        self.thread = QThread()
        settings_snapshot = {
            "cluster_sync_tasks": self.settings.get("cluster_sync_tasks", True),
            "cluster_role": self.settings.get("cluster_role", "Master"),
            "watch_folders": self.settings.get("watch_folders", []),
            "worker_alias": self.settings.get("worker_alias"),
            "_path_is_placeholder": self._path_is_placeholder
        }
        self.worker = ClusterWorker(self._cluster_path, self.node_id, settings_snapshot)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run_loop)
        self.worker.task_synced.connect(self.task_synced)
        self.worker.node_updated.connect(self._on_node_updated)
        self.worker.watch_config_synced.connect(self.watch_config_synced)
        self.worker.role_changed.connect(self.role_changed)
        self.worker.master_stale_detected.connect(self.master_stale_detected)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def delete_task(self, cluster_filename):
        try:
            task_path = os.path.join(self._cluster_path, "tasks", cluster_filename)
            if os.path.exists(task_path): os.remove(task_path)
            if self.worker and hasattr(self.worker, '_known_tasks') and cluster_filename in self.worker._known_tasks:
                 del self.worker._known_tasks[cluster_filename]
        except: pass

    def stop(self):
        if self.worker: self.worker.stop()

    def update_worker_settings(self, new_settings):
        if self.worker: self.worker.update_settings(new_settings)

    def restart(self, new_path=None):
        self.stop()
        if self.thread:
            self.thread.quit()
            self.thread.wait(2000) 
            self.thread = None
            self.worker = None
        if new_path:
            self._cluster_path = new_path
            # [v27.10.61] If a real absolute path is provided, lift the placeholder lock
            # so heartbeats and directory creation work correctly.
            if os.path.isabs(new_path):
                self._path_is_placeholder = False
                debug_log(f"[CLUSTER] Restart: Placeholder mode LIFTED. Active path: {new_path}")
            else:
                self._path_is_placeholder = True
        self._known_nodes_cache = {}
        self.start()


    def promote_to_master(self):
        if self.worker: self.worker.promote_to_master()

    def load_master_backup_settings(self):
        backup_file = os.path.join(self._cluster_path, "master_config_backup.json")
        if os.path.exists(backup_file):
            try:
                with open(backup_file, 'r', encoding='utf-8') as f: data = json.load(f)
                for k in ["watch_folders", "max_parallel_tasks", "worker_output_path", "history_retention"]:
                    if k in data: self.settings.set(k, data[k])
                return True
            except: pass
        return False

    def _on_node_updated(self, data):
        nid = data.get("node_id")
        if nid:
            last_seen = data.get("last_seen")
            if last_seen:
                try:
                    ts = last_seen if isinstance(last_seen, (int, float)) else datetime.datetime.fromisoformat(last_seen).timestamp()
                    secs = time.time() - ts
                    is_online = (data.get("status") in ["Online", "Online (Local)"])
                    IS_ONE_YEAR = abs(abs(secs) - 31536000) < 3600
                    # [v27.10.43] Reduced timeout from 600s to 60s for faster detection
                    if abs(secs) > 60 and not IS_ONE_YEAR and is_online:
                        data["status"] = "Offline (Timeout)"
                    elif abs(secs) <= 60 or IS_ONE_YEAR:
                         if data.get("status") not in ["Online", "Online (Local)"]: data["status"] = "Online"
                except: pass
            self._known_nodes_cache[nid] = data
        self.node_updated.emit(data)

    def get_all_nodes(self):
        if self.node_id not in self._known_nodes_cache:
            self._known_nodes_cache[self.node_id] = {
                "node_id": self.node_id, "alias": self.settings.get("worker_alias") or self.node_id,
                "role": self.settings.get("cluster_role", "Master"), "status": "Online (Local)",
                "ip": "127.0.0.1", "last_seen": time.time()
            }
        return self._known_nodes_cache

    def set_local_activity(self, activity_str, active_count=0):
        if self.worker: self.worker.set_activity(activity_str, active_count)

    def broadcast_task(self, task):
        try:
            import hashlib
            source_path = task.get("source") or task.get("source_path") or task.get("base_name")
            if not source_path: return None
            
            base_name, size, mtime = task.get('base_name', 'Task'), task.get('size', 0), task.get('mtime', 0)
            hash_seed = f"{base_name}_{size}_{mtime}"
            path_hash = hashlib.md5(hash_seed.encode('utf-8')).hexdigest()[:12]
            sanitized_name = "".join([c if c.isalnum() or c in ".-_" else "_" for c in base_name])
            filename = f"{sanitized_name}_{path_hash}.json"
            
            tasks_dir = os.path.join(self._cluster_path, "tasks")
            if not self._path_is_placeholder:
                if not os.path.exists(tasks_dir): os.makedirs(tasks_dir, exist_ok=True)
            
            task_file = os.path.join(tasks_dir, filename)
            
            cluster_task = task.copy()
            if os.path.exists(task_file):
                 try:
                     with open(task_file, 'r', encoding='utf-8') as f: existing = json.load(f)
                     existing.update(task)
                     cluster_task = existing
                 except: pass
            
            cluster_task["node_origin"] = self.node_id
            cluster_task["broadcast_time"] = datetime.datetime.now().isoformat()
            if "widget" in cluster_task: del cluster_task["widget"]
            
            # Predictive Assignment (Master Only)
            if self.settings.get("cluster_role") == "Master" and not cluster_task.get("assigned_to"):
                try:
                    candidates = []
                    now = time.time()
                    for nid, n_data in self._known_nodes_cache.items():
                        ts = n_data.get("last_seen", 0)
                        diff = now - (ts if isinstance(ts, (int, float)) else datetime.datetime.fromisoformat(ts).timestamp())
                        if diff < 60 or abs(abs(diff)-31536000) < 3600:
                            # [v27.10.50] Atomic Scoring: Heartbeat Count + Transient Count (Unconfirmed assignments)
                            transient_count = self._transient_assignment_cache.get(nid, {"count": 0, "time": 0})
                            if now - transient_count["time"] > 10: # 10s transient expiry
                                actual_transient = 0
                            else:
                                actual_transient = transient_count["count"]
                                
                            score = ((n_data.get("active_task_count", 0) + actual_transient) * 100) + n_data.get("cpu_usage", 0)
                            # [v27.10.50] ZERO MASTER PENALTY: Treat Master as equal
                            candidates.append({"id": nid, "score": score})
                    if candidates:
                        candidates.sort(key=lambda x: x["score"])
                        best_node = candidates[0]["id"]
                        cluster_task["assigned_to"] = best_node
                        task["assigned_to"] = best_node
                        
                        # Update Transient Cache immediately
                        curr = self._transient_assignment_cache.get(best_node, {"count": 0, "time": 0})
                        if now - curr["time"] > 10: curr = {"count": 1, "time": now}
                        else: curr["count"] += 1; curr["time"] = now
                        self._transient_assignment_cache[best_node] = curr
                        debug_log(f"Cluster: Atomic assignment -> {best_node}")
                    else:
                        # [v27.10.50] FALLBACK: If node cache is empty (startup), assign to self
                        cluster_task["assigned_to"] = self.node_id
                        task["assigned_to"] = self.node_id
                        debug_log(f"Cluster: No candidates in cache. Fallback: self-assign -> {self.node_id}")
                except: pass

            with open(task_file, "w", encoding="utf-8") as f:
                json.dump(cluster_task, f, indent=2, ensure_ascii=False)
            return filename
        except: return None

    def claim_task(self, task_filename, node_id):
        task_dir, lock_file = os.path.join(self._cluster_path, "tasks"), os.path.join(self._cluster_path, "tasks", f"{task_filename}.lock")
        if os.path.exists(lock_file): return False
        try:
            with open(lock_file, "x") as f: f.write(json.dumps({"claimed_by": node_id, "time": datetime.datetime.now().isoformat()}))
            task_path = os.path.join(task_dir, task_filename)
            if os.path.exists(task_path):
                with open(task_path, "r+", encoding="utf-8") as f:
                    data = json.load(f); data["claimed_by"] = node_id; data["cluster_status"] = "Claimed";
                    f.seek(0); json.dump(data, f, indent=2, ensure_ascii=False); f.truncate()
            return True
        except: return False

    def delete_cluster_task(self, base_name, cluster_filename=None):
        task_dir = os.path.join(self._cluster_path, "tasks")
        if not os.path.exists(task_dir): return
        try:
            if cluster_filename:
                t = os.path.join(task_dir, cluster_filename)
                if os.path.exists(t): os.remove(t)
                l = t + ".lock"
                if os.path.exists(l): os.remove(l)
                return
            s = "".join([c if c.isalnum() or c in ".-_" else "_" for c in base_name])
            for f in os.listdir(task_dir):
                if f.startswith(s + "_") or f == (s + ".json"):
                    try:
                        os.remove(os.path.join(task_dir, f))
                        l = os.path.join(task_dir, f + ".lock")
                        if os.path.exists(l): os.remove(l)
                    except: pass
        except: pass
