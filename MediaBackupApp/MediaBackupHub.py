import re
import json
import sys
import os
import threading
import subprocess
import queue
import time
import qrcode
from datetime import datetime
from io import BytesIO

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
    QWidget, QLabel, QSystemTrayIcon, QMessageBox,
    QPlainTextEdit, QFrame, QPushButton, QFileDialog
)
from PySide6.QtGui import QPixmap, QImage
from PySide6.QtCore import Qt, QTimer

# ── PID 管理：確保每次啟動只有一個 Hub 實例 ──────────────────────────
PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".hub.pid")

def kill_previous():
    if os.path.exists(PID_FILE):
        try:
            old_pid = int(open(PID_FILE).read().strip())
            subprocess.call(f"taskkill /F /PID {old_pid} /T", shell=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except: pass
        try: os.remove(PID_FILE)
        except: pass
    
    # ── Non-blocking cleanup ──
    # Kill any lingering node.exe or processes holding port 8000 without blocking GUI
    cleanup_cmd = (
        "taskkill /F /IM node.exe /T & "
        "FOR /F \"tokens=5\" %a in ('netstat -aon ^| findstr :8000') do taskkill /F /PID %a /T"
    )
    subprocess.Popen(cleanup_cmd, shell=True, creationflags=0x08000000, 
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    # Write new PID
    try: open(PID_FILE, "w").write(str(os.getpid()))
    except: pass


class MediaBackupHub(QMainWindow):

    def __init__(self):
        super().__init__()
        kill_previous()
        self.setWindowTitle("專業照片備份中心")
        self.resize(500, 750)

        self.log_queue = queue.Queue()
        self.config_path = os.path.join(os.getcwd(), "hub_config.json")
        self.storage_path = os.path.join(os.getcwd(), "storage")
        self.expo_proc = None
        self.backend_proc = None

        self.setup_ui()
        self.load_config()

        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_ComputerIcon))
        self.tray.show()

        self.log_timer = QTimer()
        self.log_timer.timeout.connect(self._flush_logs)
        self.log_timer.start(100)

        QTimer.singleShot(600, self.auto_start)

    # ── UI ───────────────────────────────────────────────────────────────
    def setup_ui(self):
        lay = QVBoxLayout()
        lay.setContentsMargins(28, 28, 28, 28)
        lay.addWidget(self._lbl("🌟 專業照片對接中心",
                                "font-size:24px;font-weight:900;color:#1e293b;"))
        self.status_lbl = self._lbl("正在啟動…", "color:#64748b;margin-bottom:14px;")
        lay.addWidget(self.status_lbl)

        self.qr_frame = QFrame()
        self.qr_frame.setFixedSize(300, 300)
        self.qr_frame.setStyleSheet(
            "background:white;border-radius:18px;border:1px solid #e2e8f0;")
        fl = QVBoxLayout(self.qr_frame)
        self.qr_lbl = QLabel("準備中…")
        self.qr_lbl.setAlignment(Qt.AlignCenter)
        self.qr_lbl.setWordWrap(True)
        fl.addWidget(self.qr_lbl)
        lay.addWidget(self.qr_frame, 0, Qt.AlignCenter)

        row = QHBoxLayout()
        b1 = QPushButton("📁 設定存檔路徑")
        b1.setStyleSheet("background:#f1f5f9;padding:11px;border-radius:10px;font-weight:bold;")
        b1.clicked.connect(self.pick_path)
        row.addWidget(b1)
        b2 = QPushButton("🖼️ 開啟相本")
        b2.setStyleSheet("background:#3b82f6;color:white;padding:11px;border-radius:10px;font-weight:bold;")
        b2.clicked.connect(self.open_gallery)
        row.addWidget(b2)
        lay.addLayout(row)

        rb = QPushButton("🔄 重啟 Expo 對接")
        rb.setStyleSheet(
            "background:#ef4444;color:white;padding:13px;border-radius:10px;"
            "font-weight:800;margin-top:8px;")
        rb.clicked.connect(self.restart_expo)
        lay.addWidget(rb)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setStyleSheet(
            "background:#020617;color:#10b981;font-family:'Consolas';"
            "font-size:11px;margin-top:12px;border-radius:10px;")
        lay.addWidget(self.log_box)

        ctr = QWidget()
        ctr.setLayout(lay)
        self.setCentralWidget(ctr)

    def _lbl(self, text, style=""):
        l = QLabel(text); l.setStyleSheet(style); return l

    def _flush_logs(self):
        added = False
        while not self.log_queue.empty():
            self.log_box.appendPlainText(self.log_queue.get().rstrip())
            added = True
        if added:
            sb = self.log_box.verticalScrollBar()
            sb.setValue(sb.maximum())

    def log(self, msg):
        self.log_queue.put(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    # ── Config ───────────────────────────────────────────────────────────
    def load_config(self):
        if os.path.exists(self.config_path):
            try:
                cfg = json.load(open(self.config_path, encoding="utf-8"))
                self.storage_path = cfg.get("storage_path", self.storage_path)
                self.log(f"💾 已載入路徑: {self.storage_path}")
            except: pass

    def save_config(self):
        try:
            json.dump({"storage_path": self.storage_path},
                      open(self.config_path, "w", encoding="utf-8"), ensure_ascii=False)
        except: pass

    def pick_path(self):
        p = QFileDialog.getExistingDirectory(self, "選擇備份資料夾", self.storage_path)
        if p:
            self.storage_path = p; self.save_config()
            self._sync_path()
            QMessageBox.information(self, "完成", f"路徑:\n{p}")

    def _sync_path(self):
        try:
            import urllib.request
            urllib.request.urlopen(
                f"http://127.0.0.1:8000/set-path?path={self.storage_path.replace(chr(92),'/')}",
                timeout=3)
        except: pass

    def open_gallery(self):
        try: os.startfile("http://127.0.0.1:8000/gallery")
        except: os.startfile(self.storage_path)

    # ── Auto Start ───────────────────────────────────────────────────────
    def auto_start(self):
        self.start_backend()
        self.show_qr_from_settings()
        QTimer.singleShot(1000, self.start_expo)
        QTimer.singleShot(8000, self._sync_path)

    # ── Backend (subprocess) ──────────────────────────────────────────────
    def start_backend(self):
        backend_dir = os.path.join(os.getcwd(), "backend")
        try:
            self.backend_proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn",
                 "main:app", "--host", "0.0.0.0", "--port", "8000",
                 "--log-level", "warning"],
                cwd=backend_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000
            )
            self.log("✅ 後端啟動 (port 8000)")
            QTimer.singleShot(7000, self._verify_backend)
        except Exception as e:
            self.log(f"❌ 後端啟動失敗: {e}")

    def _verify_backend(self):
        import urllib.request
        try:
            urllib.request.urlopen("http://127.0.0.1:8000/api/v1/health", timeout=3)
            self.log("✅ 後端確認: port 8000 正常，可開始備份！")
        except Exception as e:
            self.log(f"❌ 後端 port 8000 無回應: {e}")

    # ── QR Code ──────────────────────────────────────────────────────────
    def show_qr_from_settings(self):
        """從 .expo/settings.json 讀取固定 urlRandomness，立即顯示 QR。"""
        try:
            settings = os.path.join(os.getcwd(), "mobile", ".expo", "settings.json")
            rand = json.load(open(settings)).get("urlRandomness", "").lower()
            if rand:
                url = f"exp://{rand}-anonymous-8081.exp.direct:80"
                self.log(f"📱 QR URL: {url}")
                self._show_qr(url)
                self.status_lbl.setText("✅ 用 Expo Go 掃描 QR Code")
                return
        except Exception as e:
            self.log(f"⚠️ 無法讀取 settings.json: {e}")
        self.status_lbl.setText("正在啟動 Expo 隧道…")
        self.qr_lbl.setText("等待隧道網址…")

    def _show_qr(self, url):
        try:
            qr = qrcode.QRCode(version=1, box_size=9, border=3)
            qr.add_data(url); qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO(); img.save(buf, format="PNG")
            pix = QPixmap.fromImage(QImage.fromData(buf.getvalue()))
            self.qr_lbl.setPixmap(pix.scaled(285, 285, Qt.KeepAspectRatio))
        except Exception as e:
            self.log(f"QR 錯誤: {e}")

    # ── Expo ─────────────────────────────────────────────────────────────
    def start_expo(self):
        mobile = os.path.join(os.getcwd(), "mobile")
        self.log("🚀 npx expo start --tunnel")
        try:
            self.expo_proc = subprocess.Popen(
                "npx expo start --tunnel",
                shell=True, cwd=mobile,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, creationflags=0x08000000
            )
            threading.Thread(target=self._log_expo, daemon=True).start()
        except Exception as e:
            self.log(f"❌ Expo 啟動失敗: {e}")

    def _log_expo(self):
        ansi = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')
        for raw in self.expo_proc.stdout:
            self.log(f"[Expo] {ansi.sub('', raw).rstrip()}")

    def restart_expo(self):
        self.log("🔄 重啟 Expo…")
        if self.expo_proc:
            try: self.expo_proc.kill()
            except: pass
        subprocess.call("taskkill /F /IM node.exe /T", shell=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.show_qr_from_settings()
        QTimer.singleShot(1500, self.start_expo)

    def closeEvent(self, event):
        """當視窗關閉時，確實殺死所有背景子程序（特別是 uvicorn）"""
        self.log("🛑 正在關閉系統...")
        try:
            if self.backend_proc:
                subprocess.call(f"taskkill /F /PID {self.backend_proc.pid} /T", shell=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except: pass
        
        try:
            if self.expo_proc:
                subprocess.call(f"taskkill /F /PID {self.expo_proc.pid} /T", shell=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except: pass
        
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MediaBackupHub()
    w.show()
    sys.exit(app.exec())
