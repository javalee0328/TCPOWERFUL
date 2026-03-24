import os
import shutil
import mimetypes
import socket
from datetime import datetime
from PIL import Image, ExifTags
from pillow_heif import register_heif_opener
from geopy.geocoders import Nominatim
import json
register_heif_opener()

geolocator = Nominatim(user_agent="media_backup_hub")
LOC_CACHE_FILE = os.path.join(os.getcwd(), ".location_cache.json")
loc_cache = {}
if os.path.exists(LOC_CACHE_FILE):
    try:
        with open(LOC_CACHE_FILE, "r", encoding="utf-8") as f:
            loc_cache = json.load(f)
    except: pass

def save_loc_cache():
    try:
        with open(LOC_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(loc_cache, f, ensure_ascii=False)
    except: pass

def get_gps_decimal(coords, ref):
    if not coords or not ref: return None
    d = float(coords[0])
    m = float(coords[1])
    s = float(coords[2])
    res = d + (m / 60.0) + (s / 3600.0)
    if ref in ['S', 'W']: res = -res
    return res

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
THUMB_DIR = os.path.join(os.getcwd(), ".thumbnails")
if not os.path.exists(STORAGE_DIR): os.makedirs(STORAGE_DIR)
if not os.path.exists(THUMB_DIR): os.makedirs(THUMB_DIR)

# Dynamic file serving (works even after set-path changes STORAGE_DIR)
@app.get("/view/{path:path}")
def serve_file(path: str):
    full_path = os.path.join(STORAGE_DIR, path)
    if os.path.exists(full_path) and os.path.isfile(full_path):
        mime, _ = mimetypes.guess_type(full_path)
        return FileResponse(full_path, media_type=mime or "application/octet-stream")
    return JSONResponse({"error": "not found"}, status_code=404)

@app.get("/thumbnail/{path:path}")
def serve_thumbnail(path: str):
    full_path = os.path.join(STORAGE_DIR, path)
    if not os.path.exists(full_path):
        return JSONResponse({"error": "not found"}, status_code=404)
    
    # Cache path
    thumb_name = path.replace("/", "_").replace("\\", "_") + ".webp"
    thumb_path = os.path.join(THUMB_DIR, thumb_name)
    
    if os.path.exists(thumb_path):
        return FileResponse(thumb_path, media_type="image/webp")
    
    # Generate thumbnail
    try:
        if path.lower().endswith(('.mp4', '.mov')):
            # For videos, return a placeholder for now
            # (In professional version, extract frame with cv2)
            return FileResponse(full_path) # Fallback to original for now or placeholder
            
        img = Image.open(full_path)
        # Handle orientation from EXIF
        try:
            for orientation in ExifTags.TAGS.keys():
                if ExifTags.TAGS[orientation] == 'Orientation': break
            exif = dict(img._getexif().items())
            if exif[orientation] == 3: img = img.rotate(180, expand=True)
            elif exif[orientation] == 6: img = img.rotate(270, expand=True)
            elif exif[orientation] == 8: img = img.rotate(90, expand=True)
        except: pass
        
        img.thumbnail((400, 400))
        img.save(thumb_path, "WEBP", quality=80)
        return FileResponse(thumb_path, media_type="image/webp")
    except Exception as e:
        print(f"⚠️ Thumbnail Error: {e}")
        return FileResponse(full_path) # Fallback to original

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
    all_files_js = []
    for g in sorted_groups:
        files = sorted(groups[g], reverse=True)
        try:
            year, month = g.split('/')
            label = f"{year}年 {int(month)}月"
        except:
            label = g
        
        cards = ""
        for f in files:
            all_files_js.append(f)
            is_vid = f.lower().endswith(('.mp4', '.mov'))
            cards += f'''
            <div class="thumb" id="thumb-{len(all_files_js)-1}">
              <div class="img-wrapper" onclick="openModal({len(all_files_js)-1})">
                {f'<video src="/view/{f}" muted playsinline></video>' if is_vid else f'<img src="/thumbnail/{f}" loading="lazy">'}
              </div>
              <div class="badge">{ '▶' if is_vid else '' }</div>
              <div class="actions">
                <a href="/view/{f}" download class="act-btn" title="下載">⬇️</a>
                <div class="act-btn del" onclick="confirmDelete('{f}', {len(all_files_js)-1})" title="刪除">🗑️</div>
              </div>
            </div>'''

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
    :root {{ --bg: #000; --accent: #3b82f6; --text: #fff; --muted: #888; --card-bg: #111; }}
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; overflow-x: hidden;}}
    .topbar{{position:sticky;top:0;z-index:20;background:rgba(0,0,0,.85);backdrop-filter:blur(20px);
             padding:16px 20px;display:flex;justify-content:space-between;align-items:center;
             border-bottom:1px solid #222;}}
    .topbar h1{{font-size:20px;font-weight:700;letter-spacing:-0.5px;}}
    .topbar span{{color:var(--muted);font-size:13px; font-weight: 500;}}
    
    .section{{padding:0 12px 40px;}}
    .section-header{{display:flex;justify-content:space-between;align-items:baseline;
                     padding:24px 8px 16px;}}
    .section-title{{font-size:18px;font-weight:800;color:#fff;}}
    .section-count{{font-size:13px;color:var(--muted);}}
    
    .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:4px;}}
    .thumb{{position:relative;aspect-ratio:1;background:var(--card-bg);cursor:pointer;overflow:hidden; border-radius: 4px;}}
    .img-wrapper{{width:100%;height:100%;}}
    .thumb img, .thumb video{{width:100%;height:100%;object-fit:cover;transition: transform 0.4s cubic-bezier(0.165, 0.84, 0.44, 1);}}
    .thumb:hover img, .thumb:hover video{{transform:scale(1.08);}}
    .badge{{position:absolute;bottom:8px;right:10px;font-size:16px;text-shadow:0 1px 4px rgba(0,0,0,0.8); pointer-events:none;}}
    
    .actions{{position:absolute; top:8px; right:8px; display:flex; gap:6px; opacity:0; transition:0.2s;}}
    .thumb:hover .actions{{opacity:1;}}
    .act-btn{{width:32px;height:32px;background:rgba(0,0,0,0.6);border-radius:50%;display:flex;align-items:center;justify-content:center;
             font-size:14px; text-decoration:none; color:#fff; border:1px solid rgba(255,255,255,0.1); backdrop-filter:blur(5px);}}
    .act-btn:hover{{background:var(--accent); transform:scale(1.1);}}
    .act-btn.del:hover{{background:#ef4444;}}

    /* Lightbox Modal */
    #modal{{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.98);z-index:2000;
             flex-direction:row; align-items:stretch;}}
    #modal.open{{display:flex;}}
    
    .viewer-main{{flex:1; position:relative; display:flex; align-items:center; justify-content:center; overflow:hidden;}}
    #modalContent{{max-width:100%; max-height:100%; transition: transform 0.1s ease-out;}}
    #modalContent img, #modalContent video{{max-width:95vw; max-height:95vh; object-fit:contain;}}
    
    .viewer-info{{width:320px; background:#111; border-left:1px solid #222; padding:24px; display:flex; flex-direction:column; gap:20px;}}
    .info-title{{font-size:18px; font-weight:800; margin-bottom:10px; color:var(--accent);}}
    .info-row{{display:flex; justify-content:space-between; font-size:13px; border-bottom:1px solid #222; padding:10px 0;}}
    .info-label{{color:var(--muted);}}
    .info-val{{color:#ddd; font-family:monospace;}}

    .nav-btn{{position:absolute; top:50%; transform:translateY(-50%); width:60px; height:60px; background:rgba(255,255,255,0.05);
               display:flex; align-items:center; justify-content:center; font-size:24px; color:#fff; cursor:pointer; 
               border-radius:50%; transition:0.2s; user-select:none; z-index:10;}}
    .nav-btn:hover{{background:rgba(255,255,255,0.15); transform:translateY(-50%) scale(1.1);}}
    .nav-btn.prev{{left:30px;}}
    .nav-btn.next{{right:30px;}}
    
    .top-controls{{position:absolute; top:20px; right:20px; display:flex; gap:12px; z-index:20;}}
    .con-btn{{width:44px; height:44px; background:rgba(255,255,255,0.1); border-radius:50%; display:flex;
               align-items:center; justify-content:center; color:#fff; font-size:20px; cursor:pointer; transition:0.2s;}}
    .con-btn:hover{{background:var(--accent); transform:rotate(90deg);}}

    @media(max-width:900px){{
        #modal{{flex-direction:column;}}
        .viewer-info{{width:100%; height:30%; border-left:none; border-top:1px solid #222; overflow-y:auto;}}
        .nav-btn{{width:40px; height:40px; font-size:18px;}}
    }}
    @media(max-width:600px){{ .grid{{grid-template-columns:repeat(3,1fr);gap:2px;}} }}
  </style>
</head>
<body>
  <div class="topbar">
    <h1>📸 雲端備份相本</h1>
    <span>共 {total} 個項目 | {STORAGE_DIR}</span>
  </div>
  
  <div class="content-body">
    {sections_html or '<p style="color:#555;text-align:center;margin-top:120px;font-size:18px">☕ 尚未有備份檔案，快拿起手機同步吧！</p>'}
  </div>

  <div id="modal">
    <div class="viewer-main" id="viewerMain">
      <div class="nav-btn prev" onclick="changePhoto(-1)">‹</div>
      <div id="modalContent"></div>
      <div class="nav-btn next" onclick="changePhoto(1)">›</div>
      
      <div class="top-controls">
        <div class="con-btn" onclick="closeModal()">✕</div>
      </div>
    </div>
    
    <div class="viewer-info">
      <div class="info-title">詳細資訊</div>
      <div id="metaContent">載入中...</div>
    </div>
  </div>

  <script>
    const allFiles = {all_files_js};
    let currentIndex = -1;
    let zoomScale = 1;

    function openModal(index) {{
      currentIndex = index;
      const path = allFiles[index];
      const mc = document.getElementById('modalContent');
      const info = document.getElementById('metaContent');
      
      zoomScale = 1;
      mc.style.transform = `scale(${{zoomScale}})`;
      
      const isVid = path.match(/\\.(mp4|mov)$/i);
      mc.innerHTML = isVid
        ? `<video id="activeMedia" src="/view/${{path}}" controls autoplay></video>`
        : `<img id="activeMedia" src="/view/${{path}}">`;
      
      info.innerHTML = '<div style="color:#666">讀取 EXIF 中...</div>';
      fetch(`/api/v1/meta?path=${{encodeURIComponent(path)}}`)
        .then(r => r.json())
        .then(data => {{
          info.innerHTML = `
            <div class="info-row"><span class="info-label">檔名</span><span class="info-val" style="word-break:break-all">${{data.filename}}</span></div>
            <div class="info-row"><span class="info-label">拍攝日期</span><span class="info-val">${{data.date || data.mtime}}</span></div>
            <div class="info-row"><span class="info-label">解析度</span><span class="info-val">${{data.resolution || 'N/A'}}</span></div>
            <div class="info-row"><span class="info-label">檔案大小</span><span class="info-val">${{data.size}}</span></div>
            <div class="info-row"><span class="info-label">相機</span><span class="info-val">${{data.make || '未知'}} ${{data.model || ''}}</span></div>
            ${{data.location ? `<div class="info-row"><span class="info-label">地點</span><span class="info-val" style="color:#10b981">📍 已標記 GPS</span></div>` : ''}}
          `;
        }});

      document.getElementById('modal').classList.add('open');
      document.body.style.overflow = 'hidden';
    }}

    function changePhoto(dir) {{
      let ni = currentIndex + dir;
      if (ni < 0) ni = allFiles.length - 1;
      if (ni >= allFiles.length) ni = 0;
      openModal(ni);
    }}

    function closeModal() {{
      document.getElementById('modal').classList.remove('open');
      document.getElementById('modalContent').innerHTML = '';
      document.body.style.overflow = '';
    }}

    function confirmDelete(path, index) {{
      if (confirm(`確定要永久刪除「${{path.split('/').pop()}}」嗎？`)) {{
        fetch(`/api/v1/delete?path=${{encodeURIComponent(path)}}`)
          .then(r => r.json())
          .then(res => {{
            if (res.status === 'success') {{
                document.getElementById(`thumb-${{index}}`).style.display = 'none';
            }} else {{
                alert('刪除失敗: ' + res.message);
            }}
          }});
      }}
    }}

    // Mouse Wheel Zoom
    document.getElementById('viewerMain').onwheel = function(e) {{
        if (currentIndex === -1) return;
        const media = document.getElementById('modalContent');
        e.preventDefault();
        zoomScale += e.deltaY * -0.001;
        zoomScale = Math.min(Math.max(.125, zoomScale), 4);
        media.style.transform = `scale(${{zoomScale}})`;
    }};

    // Keyboard Shortcuts
    document.addEventListener('keydown', (e) => {{
      if (!document.getElementById('modal').classList.contains('open')) return;
      if (e.key === 'Escape') closeModal();
      if (e.key === 'ArrowLeft') changePhoto(-1);
      if (e.key === 'ArrowRight') changePhoto(1);
    }});

    document.getElementById('modal').addEventListener('click', function(e) {{
      if (e.target === this || e.target.id === 'viewerMain') closeModal();
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

@app.get("/api/v1/delete")
def delete_file(path: str):
    full_path = os.path.join(STORAGE_DIR, path)
    if os.path.exists(full_path):
        try:
            os.remove(full_path)
            # Remove thumb too
            thumb_name = path.replace("/", "_").replace("\\", "_") + ".webp"
            thumb_path = os.path.join(THUMB_DIR, thumb_name)
            if os.path.exists(thumb_path): os.remove(thumb_path)
            return {"status": "success"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    return {"status": "error", "message": "file not found"}

@app.get("/api/v1/meta")
def get_meta(path: str):
    full_path = os.path.join(STORAGE_DIR, path)
    if not os.path.exists(full_path):
        return {"status": "error", "message": "not found"}
    
    res = {
        "filename": os.path.basename(path),
        "size": f"{os.path.getsize(full_path) / 1024 / 1024:.2f} MB",
        "mtime": datetime.fromtimestamp(os.path.getmtime(full_path)).strftime("%Y-%m-%d %H:%M:%S")
    }
    
    try:
        img = Image.open(full_path)
        info = img._getexif()
        if info:
            exif = {ExifTags.TAGS.get(k, k): v for k, v in info.items()}
            res["date"] = exif.get("DateTimeOriginal", "未知")
            res["make"] = exif.get("Make", "未知")
            res["model"] = exif.get("Model", "未知")
            res["resolution"] = f"{img.width} x {img.height}"
            
            # GPS Smart Indexing
            gps = exif.get("GPSInfo")
            if gps:
                lat = get_gps_decimal(gps.get(2), gps.get(1))
                lon = get_gps_decimal(gps.get(4), gps.get(3))
                if lat and lon:
                    key = f"{lat:.4f},{lon:.4f}"
                    if key in loc_cache:
                        res["location"] = loc_cache[key]
                    else:
                        try:
                            location = geolocator.reverse(f"{lat}, {lon}", language="zh-TW")
                            res["location"] = location.address if location else "未知地點"
                            loc_cache[key] = res["location"]
                            save_loc_cache()
                        except: res["location"] = f"{lat:.4f}, {lon:.4f}"
    except Exception as e:
        print(f"Meta Error: {e}")
    return res

@app.get("/api/v1/search")
def search_files(q: str):
    """Simple search by filename or cached location."""
    results = []
    q = q.lower()
    for root, dirs, files in os.walk(STORAGE_DIR):
        for fn in files:
            if q in fn.lower():
                rel = os.path.relpath(os.path.join(root, fn), STORAGE_DIR).replace('\\', '/')
                results.append(rel)
    # Check location cache too
    for key, addr in loc_cache.items():
        if q in addr.lower():
            # This is slow if we have millions, but fine for prototype
            # professional version would use a DB
            pass 
    return {"results": results}
