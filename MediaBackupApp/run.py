import subprocess
import os
import sys
import time

def run():
    # Base directory
    base_dir = os.path.dirname(os.path.abspath(__file__))
    backend_dir = os.path.join(base_dir, "backend")
    frontend_dir = os.path.join(base_dir, "frontend")
    
    print("[*] Starting Media Backup System...")
    
    # 1. Start Backend (FastAPI)
    print("[*] Launching Backend on http://localhost:8000")
    backend = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=backend_dir
    )
    
    # 2. Start Frontend (Vite)
    print("[*] Launching Frontend on http://localhost:5173")
    # Using npm.cmd for Windows compatibility
    npm_cmd = "npm.cmd" if os.name == "nt" else "npm"
    frontend = subprocess.Popen(
        [npm_cmd, "run", "dev"],
        cwd=frontend_dir
    )
    
    # 3. Start Watcher (Optional focus for desktop)
    print("[*] Launching Desktop Watcher (local_sync folder)")
    watcher = subprocess.Popen(
        [sys.executable, "watcher.py"],
        cwd=backend_dir
    )
    
    print("\n" + "="*50)
    print("  Media Backup System is running!")
    print(f"  - Web UI: http://localhost:5173")
    print(f"  - API: http://localhost:8000")
    print(f"  - Desktop Watcher Folder: {os.path.join(base_dir, 'local_sync')}")
    print("="*50 + "\n")
    
    try:
        while True:
            time.sleep(1)
            if backend.poll() is not None:
                print("[!] Backend stopped unexpectedly")
                break
            if frontend.poll() is not None:
                print("[!] Frontend stopped unexpectedly")
                break
    except KeyboardInterrupt:
        print("\n[*] Shutting down...")
        backend.terminate()
        frontend.terminate()
        watcher.terminate()

if __name__ == "__main__":
    run()
