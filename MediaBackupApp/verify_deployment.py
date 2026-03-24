import socket
import requests
import time
import subprocess

def check_port(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0

def verify():
    print("="*40)
    print("      SYSTEM DIAGNOSTICS")
    print("="*40)
    
    # 1. Check Backend (Port 8000)
    print(f"[*] Checking Backend (Port 8000)... ", end="")
    if check_port(8000):
        print("ONLINE ✅")
        try:
            r = requests.get("http://127.0.0.1:8000/", timeout=2)
            print(f"    - API Response: {r.status_code} OK")
        except:
            print("    - API did not respond to GET")
    else:
        print("OFFLINE ❌")

    # 2. Check Expo (Port 8081)
    print(f"[*] Checking Expo Server (Port 8081)... ", end="")
    if check_port(8081):
        print("ONLINE ✅")
    else:
        print("OFFLINE ❌ (Note: Requires clicking the button)")

    # 3. Check IP and mDNS
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(('8.8.8.8', 1))
    local_ip = s.getsockname()[0]
    s.close()
    print(f"[*] Detected Local IP: {local_ip}")
    
    print("="*40)
    print("DIAGNOSTICS COMPLETE")

if __name__ == "__main__":
    verify()
