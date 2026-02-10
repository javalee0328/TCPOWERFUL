import os
import json
import socket
import datetime
import time
from PySide6.QtCore import QObject, QTimer, Signal
import psutil
import random # [NEW] For backoff
from core.settings import CURRENT_VERSION

class ClusterWorker(QObject):
    """
    Background worker for ClusterManager to prevent UI freezes due to slow Network IO.
    """
    task_synced = Signal(dict)
    node_updated = Signal(dict)
    watch_config_synced = Signal(list) # [NEW] Signal for synced watch folders
    role_changed = Signal(str) # [NEW] Signal for role change
    finished = Signal()

    def __init__(self, cluster_path, node_id, settings_dict):
        super().__init__()
        self._cluster_path = cluster_path
        self.node_id = node_id
        # We pass a dict copy of settings to avoid thread safety issues with SettingsManager
        self.settings = settings_dict 
        self.lock = None # Will be set for thread safety if needed, but dict.update is mostly atomic
        self.running = True
        self._known_tasks = {}
        self._known_nodes = {}
        self.current_activity = "Idle"
        self.active_task_count = 0 # [NEW] Track count for load balancing
        self.total_ram_gb = round(psutil.virtual_memory().total / (1024**3), 1)
        self._first_sync = True # [FIX] Ensure we emit role on first run


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
        self.finished.emit()

    def stop(self):
        self.running = False
        print("ClusterWorker: Stop Requested.")

    def set_activity(self, activity, count=0):
        self.current_activity = activity
        self.active_task_count = count

    def update_settings(self, new_settings):
        """Allows main thread to update settings without restarting worker."""
        # Update our internal dict copy
        self.settings.update(new_settings)


    def initialize_structure(self):
        """Create necessary subdirectories."""
        try:
            if not os.path.exists(self._cluster_path):
                os.makedirs(self._cluster_path)
            
            for sub in ["nodes", "tasks", "logs"]:
                path = os.path.join(self._cluster_path, sub)
                if not os.path.exists(path):
                    os.makedirs(path)
            print(f"ClusterWorker: Initialized structure at {self._cluster_path}")
        except Exception as e:
            print(f"ClusterWorker: Init Error - {e}")

    def sync(self):
        # 1. Heartbeat
        self._update_my_heartbeat("Online")
        
        # 2. Discover Nodes
        self._discover_nodes()
        
        # 3. [NEW] Leader Election & Role Enforcement
        self._perform_leader_election()
        
        # 4. Task Logic (Split by Role)
        my_role = self.settings.get("cluster_role", "Worker")
        
        if my_role == "Master":
             # Master: Allocates Tasks
             self._allocate_pending_tasks()
             # [NEW] Master: Broadcast Watch Config
             self._sync_watch_config(role="Master")
        else:
             # [NEW] Worker: Read Watch Config
             self._sync_watch_config(role="Worker")
        
        # All Nodes: Sync Assigned Tasks (Worker reads assignments, Master reads status updates)
        if self.settings.get("cluster_sync_tasks", True):
             self._sync_tasks()


    def _perform_leader_election(self):
        """
        Attempts to acquire the 'master.lock' file.
        - If available or stale: Become Master.
        - If held by another valid node: Become Worker.
        """
        lock_file = os.path.join(self._cluster_path, "master.lock")
        now = time.time()
        
        # 1. Read existing lock
        current_lock = {}
        lock_stale = False
        
        if os.path.exists(lock_file):
            try:
                with open(lock_file, 'r', encoding='utf-8') as f:
                    current_lock = json.load(f)
                
                # Check stale (30s timeout)
                last_seen = current_lock.get("timestamp", 0)
                if now - last_seen > 30:
                    lock_stale = True
                    # debug_log("Cluster: Master lock is stale.")
            except:
                lock_stale = True # Corrupt file -> Stale
        
        # 2. Decide Role
        my_role = "Worker"
        
        # Condition A: I am already the owner -> Renew
        if current_lock.get("node_id") == self.node_id:
            my_role = "Master"
            self._write_master_lock(lock_file, now)
            
        # Condition B: No lock OR Stale lock -> Claim it
        elif not os.path.exists(lock_file) or lock_stale:
            # Try to claim (first come first served)
            # Random backoff to reduce collision on simultaneous start
            time.sleep(random.uniform(0.1, 0.5))
            
            self._write_master_lock(lock_file, now)
            # Re-read to confirm we won the race
            try:
                with open(lock_file, 'r', encoding='utf-8') as f:
                    check = json.load(f)
                if check.get("node_id") == self.node_id:
                    my_role = "Master"
            except:
                pass # Failed to verification, fallback to Worker
            
        # [FIX] Global Split-Brain Resolution (Always Run)
        # Even if we hold the lock, we must check if someone else is also acting as Master (e.g. forced by User or clock skew)
        if my_role == "Master":
             for other_id, data in self._known_nodes.items():
                 if other_id == self.node_id: continue
                 if data.get("role") == "Master" and data.get("status") == "Online":
                     # CONFLICT DETECTED: Two Masters!
                     print(f"Cluster: Split-Brain Detected! Me({self.node_id}) vs Other({other_id})")
                     
                     # Tie-Breaker: Lower Node ID Yields (Backs off)
                     if self.node_id < other_id:
                         print("Cluster: I am yielding Master role (Tie-Breaker).")
                         my_role = "Worker" # Downgrade immediately
                         break
                     else:
                         print("Cluster: I am keeping Master role (Tie-Breaker).")
            
            # 3. Apply Role
                
        # 3. Apply Role
        # Update settings dict in memory (not disk, to avoid settings thrashing) only if changed
        current_stored = self.settings.get("cluster_role", "Worker")
        
        if current_stored != my_role or self._first_sync:
            print(f"Cluster: Role Synced to {my_role} (Changed or First Run)")
            self.settings["cluster_role"] = my_role # Update local thread copy
            
            # Persist to real settings via Main Thread Signal? 
            # Actually, better to just emit a signal and let Main Window handle the state change.
            # But for heartbeat, we need to know NOW.
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
                os.fsync(f.fileno()) # [FIX] Force write to disk/network
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
            "ip": socket.gethostbyname(socket.gethostname()),
            "last_seen": datetime.datetime.now().isoformat(),
            "status": status,
            "role": self.settings.get("cluster_role", "Master"),
            "version": CURRENT_VERSION,
            "cpu_usage": cpu,
            "ram_usage": ram,
            "total_ram": self.total_ram_gb,
            "current_activity": self.current_activity,
            "active_task_count": self.active_task_count, # [NEW] Crucial for Load Balancing
            "alias": self.settings.get("worker_alias") or (f"{self.settings.get('cluster_role', 'Worker')}-{str(self.node_id)[-2:]}" if self.settings.get('cluster_role') != 'Master' else "Master")
        }
        try:
            # Atomic Write (Temp file -> Rename) could be better, but 'w' is okay for now
            # On network shares, sometimes direct write is safer than rename due to perms
            # Atomic Write (Temp file -> Rename) could be better, but 'w' is okay for now
            # On network shares, sometimes direct write is safer than rename due to perms
            with open(node_file, "w", encoding="utf-8") as f:
                json.dump(hb_data, f, indent=2)
            # print(f"Cluster: Heartbeat updated for {self.node_id}") 
        except Exception as e:
            print(f"Cluster: Heartbeat Error - {e}")

    def _discover_nodes(self):
        node_dir = os.path.join(self._cluster_path, "nodes")
        if not os.path.exists(node_dir):
             print(f"Cluster: Node dir missing: {node_dir}")
             return
        
        # Read all Valid JSONs
        cluster_nodes = [f for f in os.listdir(node_dir) if f.endswith(".json")]
        # print(f"Cluster: Discovery found {len(cluster_nodes)} node files.")

        for filename in cluster_nodes:
            filepath = os.path.join(node_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    data["_filepath"] = filepath
                    data["_filename"] = filename
                    
                    nid = data.get("node_id")
                    if nid:
                        self._known_nodes[nid] = data
                        self.node_updated.emit(data)
            except Exception as e:
                print(f"Cluster: Discovery Error {filename} - {e}")


    def _allocate_pending_tasks(self):
        """
        MASTER ONLY: Scans for 'Pending' tasks (unassigned) and assigns them 
        to the best available node (lowest Active Task Count, then lowest CPU).
        """
        task_dir = os.path.join(self._cluster_path, "tasks")
        if not os.path.exists(task_dir): return
        
        # 1. Gather Online Nodes (Candidates)
        workers = []
        # print(f"DEBUG_ALLOC: checking {len(self._known_nodes)} known nodes...")
        for nid, data in self._known_nodes.items():
            # Must be Online
            if "Offline" in data.get("status", ""): 
                 # print(f"DEBUG_ALLOC: Node {nid} is Offline. Skip.")
                 continue
            
            # 2. Calculate Score (Tasks ASC, CPU ASC)
            score = (data.get("active_task_count", 0) * 100) + data.get("cpu_usage", 0)
            workers.append({"id": nid, "score": score, "data": data})
            
        # [FIX] Guarantee Master is a candidate (Self-Injection)
        # Even if file system sync is slow, Master knows it exists.
        if self.node_id not in [w["id"] for w in workers]:
            print(f"DEBUG_ALLOC: Self-injecting Master {self.node_id} as candidate.")
            # Construct self-data
            self_data = {
                "node_id": self.node_id,
                "status": "Online",
                "active_task_count": self.active_task_count,
                "cpu_usage": 0 # Assume low priority if not polled yet
            }
            score = (self.active_task_count * 100)
            workers.append({"id": self.node_id, "score": score, "data": self_data})
            
        if not workers: 
            print("DEBUG_ALLOC: No eligible workers found.")
            return # No workers to assign to
        
        # Sort by Score (Best First)
        workers.sort(key=lambda x: x["score"])
        
        # 3. Scan Tasks
        try:
            for filename in os.listdir(task_dir):
                if not filename.endswith(".json"): continue
                
                filepath = os.path.join(task_dir, filename)
                try:
                    # Check if modified recently? Or just read.
                    # Optimization: Use _known_tasks cache to check if we processed this strict state?
                    # No, we need fresh "assigned_to" status.
                    
                    with open(filepath, "r", encoding="utf-8") as f:
                        task_data = json.load(f)
                        
                    # Target: Status=Pending AND No Assigned Node
                    status = task_data.get("cluster_status", "Pending")
                    assigned_to = task_data.get("assigned_to")
                    
                    if status == "Pending" and not assigned_to:
                        # FOUND UNASSIGNED TASK -> ASSIGN TO BEST WORKER
                        best_worker = workers[0]
                        assigned_nid = best_worker["id"]
                        
                        # Atomic Update
                        task_data["assigned_to"] = assigned_nid
                        task_data["cluster_status"] = "Assigned"
                        task_data["assignment_time"] = datetime.datetime.now().isoformat()
                        
                        with open(filepath, "w", encoding="utf-8") as f:
                            json.dump(task_data, f, indent=2, ensure_ascii=False)
                            
                        print(f"Cluster[Master]: Assigned {task_data.get('base_name')} -> {assigned_nid} (Score: {best_worker['score']})")
                        
                        # Update Local Mock State for next iteration in this same loop?
                        # Yes, heavily penalize this worker so we round-robin
                        best_worker["score"] += 100 
                        best_worker["data"]["active_task_count"] += 1
                        workers.sort(key=lambda x: x["score"]) # Re-sort
                        
                except Exception as e:
                    # print(f"Cluster: Alloc loop error {filename}: {e}")
                    pass
        except: pass

    def _sync_tasks(self):
        """
        Reads tasks.
        - Worker: Only emits if assigned_to == ME.
        - Master: Emits ALL updates (to update Dashboard).
        """
        task_dir = os.path.join(self._cluster_path, "tasks")
        if not os.path.exists(task_dir): return
        
        my_role = self.settings.get("cluster_role", "Worker")
        
        for filename in os.listdir(task_dir):
            if filename.endswith(".json"):
                filepath = os.path.join(task_dir, filename)
                try:
                    mtime = os.path.getmtime(filepath)
                    if filename not in self._known_tasks or mtime > self._known_tasks[filename]:
                        self._known_tasks[filename] = mtime
                        
                        # Read Task
                        task_data = None
                        for _ in range(3): # Retry for network stability
                                try:
                                    with open(filepath, "r", encoding="utf-8") as f:
                                        task_data = json.load(f)
                                    break
                                except: time.sleep(0.1)
                                
                        if not task_data: continue
                        
                        task_data["cluster_filename"] = filename
                        assigned_to = task_data.get("assigned_to")
                        
                        # Worker logic: Only emit if assigned to ME or if I sent it (Master sees all)
                        if my_role == "Master" or assigned_to == self.node_id or task_data.get("node_origin") == self.node_id:
                             self.task_synced.emit(task_data)
                except Exception as e:
                    # print(f"Cluster: Sync Task Error {filename}: {e}")
                    pass

    def _sync_watch_config(self, role):
        """Syncs watch folder configuration via watch_config.json."""
        config_file = os.path.join(self._cluster_path, "watch_config.json")
        
        if role == "Master":
            # Master: Write current local config to shared area
            try:
                watch_list = self.settings.get("watch_folders", [])
                
                # Check if file exists and has different content to avoid redundant writes
                write_needed = True
                if os.path.exists(config_file):
                    try:
                        with open(config_file, 'r', encoding='utf-8') as f:
                            existing = json.load(f)
                        if existing == watch_list:
                             write_needed = False
                    except: pass
                
                if write_needed:
                    with open(config_file, 'w', encoding='utf-8') as f:
                        json.dump(watch_list, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"Cluster: Master Sync Watch Config Error - {e}")
                
        else: # Worker
            # Worker: Read from shared area and update if changed
            if os.path.exists(config_file):
                try:
                    mtime = os.path.getmtime(config_file)
                    if not hasattr(self, '_last_watch_config_mtime') or mtime > self._last_watch_config_mtime:
                        self._last_watch_config_mtime = mtime
                        with open(config_file, 'r', encoding='utf-8') as f:
                            remote_config = json.load(f)
                        
                        # Emit signal for Main Window to update its UI
                        self.watch_config_synced.emit(remote_config)
                except Exception as e:
                    print(f"Cluster: Worker Sync Watch Config Error - {e}")


class ClusterManager(QObject):
    """
    Main thread interface for the ClusterWorker.
    """
    task_synced = Signal(dict)
    node_updated = Signal(dict)
    watch_config_synced = Signal(list) # [NEW] Signal for synced watch folders
    role_changed = Signal(str)

    def __init__(self, settings_manager, parent=None):
        super().__init__(parent)
        self.settings = settings_manager
        self.node_id = f"{socket.gethostname()}"
        
        # Path setup
        default_cluster_path = os.path.join(os.getcwd(), "CLUSTER_SYNC")
        self._cluster_path = self.settings.get("cluster_path", default_cluster_path)
        
        # [FIX] Ensure Robustness
        try:
            if not os.path.exists(self._cluster_path):
                os.makedirs(self._cluster_path, exist_ok=True)
            # Test Write
            test_file = os.path.join(self._cluster_path, "test_write")
            with open(test_file, 'w') as f: f.write("ok")
            os.remove(test_file)
        except Exception as e:
            print(f"Cluster: Failed to write to {self._cluster_path}. Fallback to TMP.")
            import tempfile
            self._cluster_path = os.path.join(tempfile.gettempdir(), "ProTranscoder_Cluster")
            if not os.path.exists(self._cluster_path):
                os.makedirs(self._cluster_path, exist_ok=True)

        
        self.worker = None
        self.thread = None
        self._known_nodes_cache = {} # Local copy for immediate access

    def start(self):
        if self.thread and self.thread.isRunning():
            return

        from PySide6.QtCore import QThread
        self.thread = QThread()
        
        # Pass raw dict of settings needed
        settings_snapshot = {
            "cluster_sync_tasks": self.settings.get("cluster_sync_tasks", True),
            "cluster_role": self.settings.get("cluster_role", "Master"),
            "watch_folders": self.settings.get("watch_folders", []) # [FIX] Include watch_folders for Master Sync
        }
        
        self.worker = ClusterWorker(self._cluster_path, self.node_id, settings_snapshot)
        self.worker.moveToThread(self.thread)
        
        # Connect Signals
        self.thread.started.connect(self.worker.run_loop)
        self.worker.task_synced.connect(self.task_synced)
        self.worker.node_updated.connect(self._on_node_updated) # Cache locally
        self.worker.watch_config_synced.connect(self.watch_config_synced) # [NEW]
        self.worker.role_changed.connect(self.role_changed) # [NEW] Re-emit
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        
        self.thread.start()
        print(f"ClusterManager: Background Thread Started. NodeID: {self.node_id}")

    def stop(self):
        if self.worker:
            self.worker.stop()
        print("ClusterManager: Stopping...")

    def update_worker_settings(self, new_settings):
        """Passes new settings to the active background worker."""
        if self.worker:
            self.worker.update_settings(new_settings)

    def restart(self, new_path=None):
        """Stops current worker and restarts with new settings."""
        print("ClusterManager: Restarting...")
        self.stop()
        
        # Wait for thread to cleanup (simple blocking wait)
        if self.thread:
            self.thread.quit()
            self.thread.wait(2000) 
            self.thread = None
            self.worker = None
            
        # Update internal path if provided
        if new_path:
            self._cluster_path = new_path
            
        # Clear cache
        self._known_nodes_cache = {}
        
        # Start fresh
        self.start()

    def _on_node_updated(self, data):
        """Update local cache and re-emit."""
        nid = data.get("node_id")
        if nid:
            # Re-implement timeout check logic:
            last_seen_str = data.get("last_seen")
            if last_seen_str:
                try:
                    last = datetime.datetime.fromisoformat(last_seen_str)
                    secs = (datetime.datetime.now() - last).total_seconds()
                    if secs > 30 and data.get("status") == "Online":
                        data["status"] = "Offline (Timeout)"
                except: pass
            
            self._known_nodes_cache[nid] = data
            
        self.node_updated.emit(data)

    def get_all_nodes(self):
        return self._known_nodes_cache

    def set_local_activity(self, activity_str, active_count=0):
        if self.worker:
            self.worker.set_activity(activity_str, active_count)

    # [DIRECT IO METHODS]
    # These are occasional and quick enough to keep on main thread, 
    # OR we can move them too if they prove slow. For now, writing small JSONs is usually fast.
    # Reading/Listing dirs (in sync) was the main blocker.
    
    def broadcast_task(self, task):
        """Post a local task to the cluster."""
        # Main thread write is okay for singular events
        try:
            import hashlib
            source_path = task.get("source") or task.get("source_path") or task.get("base_name")
            if not source_path: return None
            
            # [FIX] Path-Agnostic Deduplication (Handle Mapped Drives D: vs Z:)
            # Use BaseName + Size to identify "Same File" across different mount points
            base_name = task.get('base_name', 'Task')
            size = task.get('size', 0)
            
            # Robustness: If size is 0 (unlikely for ready file), fallback to path basename
            hash_seed = f"{base_name}_{size}"
            
            # [FIX] Deterministic Hash
            path_hash = hashlib.md5(hash_seed.encode('utf-8')).hexdigest()[:12]
            
            # keeping base_name in filename for readability
            sanitized_name = "".join([c if c.isalnum() or c in ".-_" else "_" for c in base_name])
            filename = f"{sanitized_name}_{path_hash}.json"
            
            # Ensure dir exists (Worker might not have created it yet if called immediately)
            tasks_dir = os.path.join(self._cluster_path, "tasks")
            if not os.path.exists(tasks_dir): os.makedirs(tasks_dir)
            
            task_file = os.path.join(tasks_dir, filename)
            
            # [FIX] Simplified Broadcast Logic
            # Trust the Hash ID. If file exists, we assume it's the correct task.
            if os.path.exists(task_file):
                 try:
                     with open(task_file, 'r', encoding='utf-8') as f:
                         existing = json.load(f)
                     
                     # [FIX] Merge instead of Overwrite (Prevent Metadata Loss)
                     # Only update fields provided in the new task
                     # But preserve "claimed_by" and "status" if they are advanced?
                     
                     # If existing is Running/Done, we generally don't want to reset it 
                     # UNLESS this is a forced re-broadcast from Master?
                     if existing.get("cluster_status") not in ["Pending", "Failed"]:
                         # If it's running, don't mess with it unless we are trying to stop it?
                         return filename

                     # Merge
                     existing.update(task)
                     # Ensure essential fields
                     existing["cluster_status"] = "Pending" 
                     existing["broadcast_time"] = datetime.datetime.now().isoformat()
                     
                     cluster_task = existing
                 except: 
                     # Corrupt file? Overwrite
                     cluster_task = task.copy()
                     cluster_task["cluster_status"] = "Pending"
            else:
                 cluster_task = task.copy()
                 cluster_task["node_origin"] = self.node_id 
                 cluster_task["cluster_status"] = "Pending"
            
            cluster_task["node_origin"] = self.node_id # Ensure origin is accurate
            cluster_task["broadcast_time"] = datetime.datetime.now().isoformat()
            if "widget" in cluster_task: del cluster_task["widget"]
            
            # [FIX] Immediate Assignment (User request: "Task generation -> Assign WORKER")
            # Don't wait for Load Balancer cycle. Assign immediately if possible.
            if not cluster_task.get("assigned_to") and not cluster_task.get("claimed_by"):
                # Find active workers
                try:
                    candidates = []
                    now = datetime.datetime.now()
                    for nid, n_data in self._known_nodes_cache.items():
                        # Basic liveness check (30s)
                        last_seen = datetime.datetime.fromisoformat(n_data.get("last_seen", now.isoformat()))
                        if (now - last_seen).total_seconds() < 30:
                            role = n_data.get("role", "Master")
                            # Prefer Workers, but include Master if no workers available
                            if role == "Worker":
                                candidates.insert(0, nid)  # Workers at front
                            elif role == "Master":
                                candidates.append(nid)  # Master as fallback
                    
                    if candidates:
                        # Simple Random Distribution for statelessness
                        import random
                        chosen = random.choice(candidates)
                        cluster_task["assigned_to"] = chosen
                        print(f"ClusterManager: Immediately assigned task to {chosen}")
                except Exception as e:
                    print(f"ClusterManager: Assignment Error - {e}")

            # Write to Shared Storage
            with open(task_file, "w", encoding="utf-8") as f:
                json.dump(cluster_task, f, indent=2, ensure_ascii=False)
            
            return filename
        except Exception as e:
            print(f"ClusterManager: Broadcast Error {e}")
            return None

    def claim_task(self, task_filename, node_id):
        """Attempts to claim a task atomic-style."""
        # Only claiming needs to be synchronous so we know result immediately
        task_dir = os.path.join(self._cluster_path, "tasks")
        lock_file = os.path.join(task_dir, f"{task_filename}.lock")
        
        if os.path.exists(lock_file): return False
            
        try:
            # Atomic 'x' creation
            with open(lock_file, "x") as f:
                f.write(json.dumps({"claimed_by": node_id, "time": datetime.datetime.now().isoformat()}))
            
            # Update content
            task_path = os.path.join(task_dir, task_filename)
            if os.path.exists(task_path):
                try:
                    with open(task_path, "r+", encoding="utf-8") as f:
                        data = json.load(f)
                        data["claimed_by"] = node_id
                        data["cluster_status"] = "Claimed"
                        f.seek(0)
                        json.dump(data, f, indent=2, ensure_ascii=False)
                        f.truncate()
                except: pass
            return True
        except:
            return False

    def delete_cluster_task(self, base_name):
        """Remove task file from cluster."""
        task_dir = os.path.join(self._cluster_path, "tasks")
        if not os.path.exists(task_dir): return
        
        try:
            for filename in os.listdir(task_dir):
                if not filename.endswith(".json"): continue
                if filename.startswith(base_name + "_") or filename == base_name:
                     try:
                         # Delete Task
                         os.remove(os.path.join(task_dir, filename))
                         # Delete Lock if exists
                         lock = os.path.join(task_dir, filename + ".lock")
                         if os.path.exists(lock): os.remove(lock)
                     except: pass
        except: pass
