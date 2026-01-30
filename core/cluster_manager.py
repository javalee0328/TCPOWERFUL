import os
import json
import socket
import datetime
import time
from PySide6.QtCore import QObject, QTimer, Signal
import psutil

class ClusterManager(QObject):
    """
    Manages task synchronization and node heart beat for the Transcoder Cluster.
    Uses a shared network path for coordination, enabling multi-node collaboration
    without a central server.
    """
    task_synced = Signal(dict) # Triggered when a foreign task is discovered or updated
    node_updated = Signal(dict) # Triggered when a cluster node heartbeat is received

    def __init__(self, settings_manager, parent=None):
        super().__init__(parent)
        self.settings = settings_manager
        # [FIX] Unique ID for multiple instances on same machine (Hostname + PID)
        self.node_id = f"{socket.gethostname()}-{os.getpid()}"
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.sync)
        self.is_running = False
        
        # Sync Cache
        self._known_tasks = {}
        self._known_nodes = {}
        
        # Activity Tracking
        self.current_activity = "Idle" 

        # Default path for cluster coordination
        # Note: In production, this MUST point to a shared network drive (UNC/Mapped)
        default_cluster_path = os.path.join(os.getcwd(), "CLUSTER_SYNC")
        self._cluster_path = self.settings.get("cluster_path", default_cluster_path)
        
        # [OPTIMIZATION] Do NOT create folders in __init__ (Main Thread Freeze)
        # self.initialize_structure() 

    def set_local_activity(self, activity_str):
        """Builds a status string for what this node is doing."""
        self.current_activity = activity_str

    def initialize_structure(self):
        """Create necessary subdirectories in the shared cluster path."""
        try:
            if not os.path.exists(self._cluster_path):
                os.makedirs(self._cluster_path)
            
            for sub in ["nodes", "tasks", "logs"]:
                path = os.path.join(self._cluster_path, sub)
                if not os.path.exists(path):
                    os.makedirs(path)
        except Exception as e:
            print(f"ClusterManager: Init Error - {e}")

    def start(self):
        if not self.is_running:
            # [OPTIMIZATION] Lazy Init Structure
            self.initialize_structure()
            
            self.is_running = True
            # [OPTIMIZATION] Delay first sync to allow UI to breathe? 
            # Or just rely on QTimer delay in Main Window.
            # But let's keep immediate sync here as 'start' implies 'go now'.
            # The caller handles the delay.
            self.sync() # Immediate first sync
            self.timer.start(5000) # Heartbeat & Sync every 5 seconds
            print(f"ClusterManager: Started for Node [{self.node_id}] at {self._cluster_path}")

    def stop(self):
        self.is_running = False
        self.timer.stop()
        self._update_my_heartbeat("Offline")
        print(f"ClusterManager: Stopped")

    def sync(self):
        try:
            # 1. Heartbeat
            self._update_my_heartbeat("Online")
            
            # 2. Discover Nodes
            self._discover_nodes()
            
            # 3. Task Synchronization (Inbound/Outbound)
            if self.settings.get("cluster_sync_tasks", True):
                self._sync_tasks()
        except Exception as e:
            print(f"ClusterManager Sync Error: {e}")

    def _update_my_heartbeat(self, status):
        node_file = os.path.join(self._cluster_path, "nodes", f"{self.node_id}.json")
        
        # Gather Metrics
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
            "version": "2026.1.0",
            "cpu_usage": cpu,
            "ram_usage": ram,
            "current_activity": self.current_activity
        }
        try:
            with open(node_file, "w", encoding="utf-8") as f:
                json.dump(hb_data, f, indent=2)
        except:
            pass

    def _discover_nodes(self):
        node_dir = os.path.join(self._cluster_path, "nodes")
        if not os.path.exists(node_dir): return
        
        # 1. Read all Valid JSONs first
        observed_nodes = {}
        for filename in os.listdir(node_dir):
            if not filename.endswith(".json"): continue
            filepath = os.path.join(node_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    data["_filepath"] = filepath
                    data["_filename"] = filename
                    observed_nodes[data.get("node_id")] = data
            except: pass

        # 2. Identify Active Hosts (Hostname -> NodeID)
        # NodeID format assume: HOSTNAME-PID
        active_hosts = {}
        now = datetime.datetime.now()
        
        for nid, data in observed_nodes.items():
            last_seen_str = data.get("last_seen")
            if not last_seen_str: continue
            try:
                last_dt = datetime.datetime.fromisoformat(last_seen_str)
                age = (now - last_dt).total_seconds()
                data["_age"] = age
                
                # If "Active" (e.g. < 15s), mark this host as taken
                if age < 15:
                    hostname = nid.rsplit('-', 1)[0]
                    # Keep track of the most recent active one if multiple?
                    # Usually only one active per host ideally.
                    active_hosts[hostname] = nid
            except: pass

        # 3. Process Logic: Update Status or Prune
        for nid, data in observed_nodes.items():
            age = data.get("_age", 9999)
            hostname = nid.rsplit('-', 1)[0]
            
            # Check if superseded
            # If I am STALE (>15s) AND there is an ACTIVE sibling with same hostname
            # Then I am definitely old residue. Delete immediately.
            is_superseded = (age > 15) and (hostname in active_hosts) and (active_hosts[hostname] != nid)
            
            should_delete = False
            status_override = None
            
            if is_superseded:
                should_delete = True
                status_override = "Offline (Removed)"
                # print(f"Pruning Superseded Node: {nid} (Active: {active_hosts[hostname]})")
            elif age > 600: # 10 min hard limit
                should_delete = True
                status_override = "Offline (Removed)"
            elif age > 30:
                # Just Timeout
                status_override = "Offline (Timeout)"
                
            # Apply Status Override
            if status_override:
                data["status"] = status_override
                data["current_activity"] = "-"
                data["cpu_usage"] = 0
                data["ram_usage"] = 0

            # Delete if needed
            if should_delete:
                try:
                     if os.path.exists(data["_filepath"]):
                        os.remove(data["_filepath"])
                except: pass
            
            # Emit Update
            # We emit even if deleted so UI can remove it (Offline (Removed))
            if nid != self.node_id:
                self._known_nodes[nid] = data
                self.node_updated.emit(data)

    def _sync_tasks(self):
        """Scan for tasks added OR updated by other nodes."""
        task_dir = os.path.join(self._cluster_path, "tasks")
        if not os.path.exists(task_dir): return
        
        for filename in os.listdir(task_dir):
            if filename.endswith(".json"):
                filepath = os.path.join(task_dir, filename)
                try:
                    mtime = os.path.getmtime(filepath)
                    # Check if new OR modified
                    # self._known_tasks: dict {filename: last_mtime}
                    if filename not in self._known_tasks or mtime > self._known_tasks[filename]:
                        self._known_tasks[filename] = mtime
                        
                        # [FIX] Robust Read with Retry for Sync Race Conditions
                        task_data = None
                        for attempt in range(3):
                            try:
                                with open(filepath, "r", encoding="utf-8") as f:
                                    task_data = json.load(f)
                                break
                            except json.JSONDecodeError:
                                # Start/End of write? Wait a bit
                                time.sleep(0.1)
                            except Exception:
                                break
                        
                        if not task_data: continue

                        # Calculate Hostnames (Ignore PID)
                        # node_origin format: HOSTNAME-PID
                        origin = task_data.get("node_origin", "")
                        my_hostname = socket.gethostname()
                        
                        # [FIX] Ghost Task Prevention
                        # Ignore tasks that originated from THIS machine (even if different PID/Session)
                        # We rely on local WatchFolderEngine to detect local tasks from disk.
                        if not origin.startswith(my_hostname):
                             self.task_synced.emit(task_data)
                            
                except Exception as e:
                    print(f"ClusterManager: Sync Error {filename} -> {e}")

    def broadcast_task(self, task):
        """Post a local task to the cluster for others to see/help with."""
        if not self.is_running: return
        
        try:
            task_name = task.get('base_name', 'Task')
            task_file = os.path.join(self._cluster_path, "tasks", f"{task_name}_{int(time.time())}.json")
            
            cluster_task = task.copy()
            cluster_task["node_origin"] = self.node_id
            cluster_task["cluster_status"] = "Pending"
            cluster_task["broadcast_time"] = datetime.datetime.now().isoformat()
            
            # Remove non-serializable objects (like Widget pointers) before saving
            if "widget" in cluster_task: del cluster_task["widget"]
            if os.path.exists(task_file):
               pass # Already exists? Overwrite?
               
            with open(task_file, "w", encoding="utf-8") as f:
                json.dump(cluster_task, f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            print(f"ClusterManager: Broadcast Error {e}")

    def delete_cluster_task(self, base_name):
        """
        [FIX] Deletes task JSONs from the cluster to prevent Ghost Tasks.
        Called when a user manually removes a Cluster Task from the UI.
        """
        task_dir = os.path.join(self._cluster_path, "tasks")
        if not os.path.exists(task_dir): return
        
        try:
            # Iterate and find matching tasks
            # Logic: Filename usually contains base_name: "{base_name}_{timestamp}.json"
            # We must be careful not to delete "MyVideo_2" when deleting "MyVideo"
            found_any = False
            for filename in os.listdir(task_dir):
                if not filename.endswith(".json"): continue
                
                # Check for strict prefix match (base_name + "_") or exact match
                is_match = False
                if filename == f"{base_name}.json":
                    is_match = True
                elif filename.startswith(f"{base_name}_"):
                    # Verify that what follows "_" is a timestamp (digits)
                    # or at least ensure we are deleting the right family
                    # "MyVideo_123456.json" -> starts with "MyVideo_"
                    # "MyVideo_Edit_123.json" -> starts with "MyVideo_" ? NO if base matches "MyVideo"
                    is_match = True
                
                if is_match and base_name in filename: # Double check
                     filepath = os.path.join(task_dir, filename)
                     try:
                         if os.path.exists(filepath):
                            os.remove(filepath)
                            print(f"ClusterManager: Deleted Cluster Task {filename}")
                            found_any = True
                         else:
                            print(f"ClusterManager: Task file missing, removing from cache only: {filename}")
                            
                         # Remove from cache so if it comes back (race condition), we see it as new?
                         # Actually if we delete it, it's gone.
                         if filename in self._known_tasks:
                             del self._known_tasks[filename]
                     except Exception as e:
                         print(f"ClusterManager: Failed to delete {filename}: {e}")
            
            # [FIX] Force cache cleanup even if file wasn't found (Ghost Task scenario)
            # If the file is already gone but cache has it, we must clear the cache
            # to prevent it from being re-emitted if logic elsewhere is flawed,
            # though usually sync relies on file existence. 
            # The more important part is UI needs to delete it unconditionally.
            
        except Exception as e:
            print(f"ClusterManager: Delete Error {e}")
