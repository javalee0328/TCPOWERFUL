from fastapi import FastAPI, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import traceback
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import os
import shutil
import mimetypes
import socket
from datetime import datetime

app = FastAPI()

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print(f"🔥 Global Error: {exc}\n{traceback.format_exc()}")
    return JSONResponse(
        status_code=500,
        content={"message": str(exc), "trace": traceback.format_exc()}
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STORAGE_DIR = os.path.join(os.getcwd(), "storage")
if not os.path.exists(STORAGE_DIR):
    os.makedirs(STORAGE_DIR)

# Dynamic file serving (works even after set-path changes STORAGE_DIR)
@app.get("/view/{path:path}")
def serve_file(path: str):
    full_path = os.path.join(STORAGE_DIR, path)
    if os.path.exists(full_path) and os.path.isfile(full_path):
        mime, _ = mimetypes.guess_type(full_path)
        return FileResponse(full_path, media_type=mime or "application/octet-stream")
    return JSONResponse({"error": "not found"}, status_code=404)

@app.get("/set-path")
def set_storage_path(path: str):
    global STORAGE_DIR
    if os.path.exists(path):
        STORAGE_DIR = path
        print(f"📂 [Sync] Storage Directory Updated -> {path}")
        return {"status": "success", "path": path}
    return {"status": "error", "message": "Path does not exist"}

# Global store for the latest Expo tunnel URL
current_expo_url = "exp://localhost:8081"

@app.get("/set-expo")
def set_expo_url(url: str):
    global current_expo_url
    current_expo_url = url
    print(f"📡 [Sync] Expo URL Updated -> {url}")
    return {"status": "updated", "url": url}

@app.get("/gallery", response_class=HTMLResponse)
def view_gallery():
    """iOS Photos style gallery — date grouped grid."""
    # Collect files grouped by YYYY/MM
    from collections import defaultdict
    groups = defaultdict(list)
    for root, dirs, files in os.walk(STORAGE_DIR):
        for fn in sorted(files):
            if fn.lower().endswith(('.png', '.jpg', '.jpeg', '.heic', '.mp4', '.mov', '.dng')):
                rel = os.path.relpath(os.path.join(root, fn), STORAGE_DIR).replace('\\', '/')
                # Extract YYYY/MM from path (e.g. "2026/03/IMG.jpg")
                parts = rel.split('/')
                group = '/'.join(parts[:2]) if len(parts) >= 3 else '其他'
                groups[group].append(rel)

    # Sort groups newest first
    sorted_groups = sorted(groups.keys(), reverse=True)
    total = sum(len(v) for v in groups.values())

    sections_html = ""
    for g in sorted_groups:
        files = sorted(groups[g], reverse=True)
        try:
            year, month = g.split('/')
            label = f"{year}年 {int(month)}月"
        except:
            label = g
        cards = "".join([
            f'''<div class="thumb" onclick="openModal('{f}')">
                  {'<video src="/view/'+f+'" muted playsinline></video>' if f.lower().endswith(('.mp4','.mov')) else '<img src="/view/'+f+'" loading="lazy">'}
                  <div class="badge">{'▶' if f.lower().endswith(('.mp4','.mov')) else ''}</div>
                </div>'''
            for f in files
        ])
        sections_html += f'''
        <div class="section">
          <div class="section-header">
            <span class="section-title">{label}</span>
            <span class="section-count">{len(files)} 個項目</span>
          </div>
          <div class="grid">{cards}</div>
        </div>'''

    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <title>備份相本 — {total} 個項目</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#000;color:#fff;font-family:-apple-system,sans-serif;}}
    .topbar{{position:sticky;top:0;z-index:10;background:rgba(0,0,0,.85);backdrop-filter:blur(20px);
             padding:16px 20px;display:flex;justify-content:space-between;align-items:center;
             border-bottom:1px solid #222;}}
    .topbar h1{{font-size:20px;font-weight:700;}}
    .topbar span{{color:#888;font-size:13px;}}
    .section{{padding:0 12px 28px;}}
    .section-header{{display:flex;justify-content:space-between;align-items:baseline;
                     padding:20px 4px 12px;}}
    .section-title{{font-size:17px;font-weight:700;color:#fff;}}
    .section-count{{font-size:13px;color:#888;}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:3px;}}
    .thumb{{position:relative;aspect-ratio:1;background:#111;cursor:pointer;overflow:hidden;}}
    .thumb img,.thumb video{{width:100%;height:100%;object-fit:cover;transition:.2s;}}
    .thumb:hover img,.thumb:hover video{{transform:scale(1.05);opacity:.9;}}
    .badge{{position:absolute;bottom:6px;right:8px;font-size:14px;text-shadow:0 1px 4px #000;}}
    /* Modal */
    #modal{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.95);z-index:100;
            align-items:center;justify-content:center;}}
    #modal.open{{display:flex;}}
    #modal img,#modal video{{max-width:92vw;max-height:92vh;border-radius:8px;object-fit:contain;}}
    #modal .close{{position:absolute;top:20px;right:24px;font-size:32px;cursor:pointer;color:#fff;
                   line-height:1;background:rgba(0,0,0,.5);border-radius:50%;width:40px;height:40px;
                   display:flex;align-items:center;justify-content:center;}}
    #modal .fname{{position:absolute;bottom:24px;left:50%;transform:translateX(-50%);
                   color:#aaa;font-size:12px;}}
    @media(max-width:600px){{.grid{{grid-template-columns:repeat(3,1fr);gap:2px;}}}}
  </style>
</head>
<body>
  <div class="topbar">
    <h1>📸 備份相本</h1>
    <span>共 {total} 個項目</span>
  </div>
  {sections_html or '<p style="color:#555;text-align:center;margin-top:80px">尚無備份檔案</p>'}

  <div id="modal">
    <div class="close" onclick="closeModal()">✕</div>
    <div id="modalContent"></div>
    <div class="fname" id="modalName"></div>
  </div>

  <script>
    function openModal(path) {{
      const mc = document.getElementById('modalContent');
      const isVid = path.match(/\\.(mp4|mov)$/i);
      mc.innerHTML = isVid
        ? `<video src="/view/${{path}}" controls autoplay style="max-width:92vw;max-height:92vh;"></video>`
        : `<img src="/view/${{path}}" style="max-width:92vw;max-height:92vh;">`;
      document.getElementById('modalName').textContent = path.split('/').pop();
      document.getElementById('modal').classList.add('open');
    }}
    function closeModal() {{
      document.getElementById('modal').classList.remove('open');
      document.getElementById('modalContent').innerHTML = '';
    }}
    document.getElementById('modal').addEventListener('click', function(e) {{
      if (e.target === this) closeModal();
    }});
  </script>
</body>
</html>""")

def connect_bridge():
    """Universal Connect Bridge - SSL Safe & Production Ready."""
    hostname = socket.gethostname()
    local_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 1))
        local_ip = s.getsockname()[0]
        s.close()
    except: pass

    local_expo_url = f"exp://{local_ip}:8081"
    
    return HTMLResponse(content=f"""
    <!DOCTYPE html>
    <html lang="zh-TW">
    <head>
        <title>Media Hub 連線中心</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=0">
        <style>
            :root {{
                --primary: #3b82f6; --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --muted: #94a3b8;
            }}
            body {{ margin: 0; background: var(--bg); color: var(--text); font-family: -apple-system, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; }}
            .card {{ background: var(--card); border-radius: 32px; padding: 40px 24px; width: 90%; max-width: 450px; text-align: center; border: 1px solid #334155; box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5); }}
            .btn {{ display: block; background: var(--primary); color: white; padding: 22px; border-radius: 20px; text-decoration: none; font-weight: 800; font-size: 20px; margin-bottom: 20px; transition: 0.2s; box-shadow: 0 10px 15px -3px rgba(37,99,235,0.4); }}
            .btn:active {{ transform: scale(0.96); }}
            .btn.secondary {{ background: #1e293b; border: 2px solid #3b82f6; box-shadow: none; color: #3b82f6; }}
            .alert {{ background: rgba(239, 68, 68, 0.1); border: 1px solid #ef4444; color: #f87171; padding: 15px; border-radius: 16px; font-size: 13px; margin-top: 25px; text-align: left; }}
            .success-tip {{ background: rgba(16, 185, 129, 0.1); border: 1px solid #10b981; color: #34d399; padding: 12px; border-radius: 12px; font-size: 12px; margin-top: 15px; }}
        </style>
    </head>
    <body>
        <div class="card">
            <div style="font-size: 56px; margin-bottom: 15px;">🚀</div>
            <h1 style="font-size: 26px; margin-bottom: 5px;">專業備份對接中心</h1>
            <p style="color: var(--muted); font-size: 14px; margin-bottom: 35px;">系統已就緒，請選擇您的目前連線環境</p>
            
            <a href="{current_expo_url}" class="btn">🌐 出外模式 (4G/5G 雲端穩定版)</a>
            <a href="{local_expo_url}" class="btn secondary">🏠 在家模式 (Wi-Fi 直連)</a>

            <div class="alert">
                ⚠️ <b>Safari 讀取失敗？</b> <br>
                如果點擊「在家模式」顯示無法連接 (HTTPS 限制)，請改用上方 **「🌐 出外模式」**。雲端通道已通過 SSL 安全認證，可 100% 成功對接。
            </div>
            
            <div class="success-tip">
                ✅ 已與電腦對接成功 | {hostname}
            </div>
        </div>
    </body>
    </html>
    """, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/hub-discover")
def discover_hub():
    """Returns local IP and metadata for smart discovery."""
    hostname = socket.gethostname()
    local_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 1))
        local_ip = s.getsockname()[0]
        s.close()
    except: pass
    
    return {
        "local_ip": local_ip,
        "port": 8000,
        "hostname": hostname,
        "status": "online",
        "timestamp": datetime.now().isoformat()
    }

@app.get("/api/v1/health")
def health_check():
    return {"status": "ok"}

@app.get("/api/v1/list")
def list_files():
    """Returns all backed-up files as relative paths."""
    files = []
    for root, dirs, filenames in os.walk(STORAGE_DIR):
        for fn in sorted(filenames):
            rel = os.path.relpath(os.path.join(root, fn), STORAGE_DIR).replace('\\', '/')
            files.append(rel)
    files.sort(reverse=True)
    return {"count": len(files), "files": files}


@app.get("/api/v1/check")
def check_file_exists(filename: str, size: int = 0):
    """Checks if a file already exists. Requires size match if size>0, and file must not be empty."""
    for root, dirs, files in os.walk(STORAGE_DIR):
        if filename in files:
            file_path = os.path.join(root, filename)
            stored_size = os.path.getsize(file_path)
            if stored_size == 0:
                # Empty/corrupt file — treat as not backed up
                try: os.remove(file_path)
                except: pass
                return {"exists": False}
            if size > 0 and stored_size != size:
                # Size mismatch — re-upload
                return {"exists": False}
            return {"exists": True, "path": os.path.relpath(file_path, STORAGE_DIR)}
    return {"exists": False}

@app.post("/api/v1/upload")
async def upload_media(file: UploadFile = File(...), timestamp: str = None, filename: str = None):
    try:
        # Determine filename — use query param if file.filename is missing
        fname = filename or file.filename
        if not fname:
            fname = f"file_{datetime.now().strftime('%Y%m%d_%H%M%S%f')}.jpg"
        # Sanitize
        fname = os.path.basename(fname)

        # Parse date for folder organization
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00')) if timestamp else datetime.now()
        except:
            dt = datetime.now()

        date_path = os.path.join(dt.strftime("%Y"), dt.strftime("%m"))
        target_dir = os.path.join(STORAGE_DIR, date_path)
        os.makedirs(target_dir, exist_ok=True)

        file_path = os.path.join(target_dir, fname)

        # Save file
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        saved_size = os.path.getsize(file_path)
        if saved_size == 0:
            os.remove(file_path)
            return JSONResponse({"status": "error", "message": "Empty file received"}, status_code=500)

        print(f"✅ Saved: {fname} ({saved_size} bytes) → {date_path}", flush=True)
        return JSONResponse({"filename": fname, "status": "success", "saved_to": date_path, "size": saved_size})

    except Exception as e:
        print(f"❌ Upload error: {e}", flush=True)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
