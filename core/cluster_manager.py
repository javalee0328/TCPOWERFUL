import os
import json
import socket
import datetime
import time
from PySide6.QtCore import QObject, QTimer, Signal

class ClusterManager(QObject):
    """
    Manages task synchronization and node heartbeat for the Transcoder Cluster.
    Uses a shared network path for coordination, enabling multi-node collaboration
    without a central server.
    """
    task_synced = Signal(dict) # Triggered when a foreign task is discovered or updated
    node_updated = Signal(dict) # Triggered when a cluster node heartbeat is received

    def __init__(self, settings_manager, parent=None):
        super().__init__(parent)
        self.settings = settings_manager
        self.node_id = socket.gethostname()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.sync)
        self.is_running = False
        
        # Sync Cache
        self._known_tasks = set()
        self._known_nodes = {}

        # Default path for cluster coordination
        # Note: In production, this MUST point to a shared network drive (UNC/Mapped)
        default_cluster_path = os.path.join(os.getcwd(), "CLUSTER_SYNC")
        self._cluster_path = self.settings.get("cluster_path", default_cluster_path)
        
        self.initialize_structure()

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
            self.is_running = True
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
        hb_data = {
            "node_id": self.node_id,
            "ip": socket.gethostbyname(socket.gethostname()),
            "last_seen": datetime.datetime.now().isoformat(),
            "status": status,
            "version": "2026.1.0"
        }
        try:
            with open(node_file, "w", encoding="utf-8") as f:
                json.dump(hb_data, f, indent=2)
        except:
            pass

    def _discover_nodes(self):
        node_dir = os.path.join(self._cluster_path, "nodes")
        if not os.path.exists(node_dir): return
        
        for filename in os.listdir(node_dir):
            if filename.endswith(".json"):
                 try:
                    with open(os.path.join(node_dir, filename), "r", encoding="utf-8") as f:
                        data = json.load(f)
                        node_id = data.get("node_id")
                        if node_id and node_id != self.node_id:
                            self._known_nodes[node_id] = data
                            self.node_updated.emit(data)
                 except:
                    pass

    def _sync_tasks(self):
        """Scan for tasks added by other nodes."""
        task_dir = os.path.join(self._cluster_path, "tasks")
        if not os.path.exists(task_dir): return
        
        for filename in os.listdir(task_dir):
            if filename.endswith(".json"):
                # Simple file timestamp or filename based idempotency
                if filename not in self._known_tasks:
                   self._known_tasks.add(filename)
                   try:
                       with open(os.path.join(task_dir, filename), "r", encoding="utf-8") as f:
                           task_data = json.load(f)
                           if task_data.get("node_origin") != self.node_id:
                               # This is a task from another node
                               self.task_synced.emit(task_data)
                   except:
                       pass

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
            
            with open(task_file, "w", encoding="utf-8") as f:
                json.dump(cluster_task, f, indent=2)
            
            print(f"ClusterManager: Task Broadcasted -> {task_name}")
        except Exception as e:
            print(f"ClusterManager: Broadcast Fail -> {e}")
