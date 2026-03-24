import sys
import os
import threading
import subprocess
import socket
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QVBoxLayout, 
    QWidget, QLabel, QSystemTrayIcon, QMenu, QMessageBox
)
from PySide6.QtGui import QIcon, QAction
from PySide6.QtCore import Qt, QTimer

# Import backend components
sys.path.append(os.path.join(os.path.dirname(__file__), 'backend'))
import main as backend_main
import watcher as backend_watcher
import config

class MediaBackupApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Media Backup Hub (Desktop Native)")
        self.setFixedSize(400, 300)
        
        self.server_thread = None
        self.watcher_thread = None
        self.is_running = False

        self.setup_ui()
        self.setup_tray()
        
        # Auto-start on launch
        self.toggle_service()

    def setup_ui(self):
        layout = QVBoxLayout()
        
        self.status_label = QLabel("狀態: 已停止")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #ef4444;")
        layout.addWidget(self.status_label)

        self.ip_label = QLabel(f"電腦 IP: {self.get_local_ip()}")
        self.ip_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.ip_label)

        self.toggle_btn = QPushButton("啟動伺服器")
        self.toggle_btn.clicked.connect(self.toggle_service)
        self.toggle_btn.setMinimumHeight(50)
        self.toggle_btn.setStyleSheet("background-color: #3b82f6; color: white; border-radius: 10px; font-weight: bold;")
        layout.addWidget(self.toggle_btn)

        self.firewall_btn = QPushButton("修復防火牆 (以管理員執行)")
        self.firewall_btn.clicked.connect(self.fix_firewall)
        layout.addWidget(self.firewall_btn)

        self.open_ui_btn = QPushButton("開啟管理網頁")
        self.open_ui_btn.clicked.connect(lambda: os.startfile("http://localhost:5173"))
        layout.addWidget(self.open_ui_btn)

        central_widget = QWidget()
        central_widget.setLayout(layout)
        self.setCentralWidget(central_widget)

    def setup_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        # Use a standard icon if custom is not found
        self.tray_icon.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_DriveHDIcon))
        
        show_action = QAction("開啟視窗", self)
        show_action.triggered.connect(self.showNormal)
        
        quit_action = QAction("結束程式", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        
        tray_menu = QMenu()
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)
        
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()

    def get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"

    def toggle_service(self):
        if not self.is_running:
            # Start Backend
            import uvicorn
            self.server_thread = threading.Thread(
                target=uvicorn.run, 
                args=(backend_main.app,), 
                kwargs={"host": "0.0.0.0", "port": 8000, "log_level": "info"},
                daemon=True
            )
            self.server_thread.start()

            # Start Watcher
            watch_dir = os.path.join(os.path.dirname(__file__), "local_sync")
            self.watcher_thread = threading.Thread(
                target=backend_watcher.start_watcher,
                args=(watch_dir, True),
                daemon=True
            )
            self.watcher_thread.start()

            self.is_running = True
            self.status_label.setText("狀態: 運作中 (已廣播 mDNS)")
            self.status_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #10b981;")
            self.toggle_btn.setText("停止伺服器")
            self.toggle_btn.setStyleSheet("background-color: #ef4444; color: white; border-radius: 10px; font-weight: bold;")
            self.tray_icon.showMessage("Media Backup", "伺服器已啟動，iPhone 可自動連線", QSystemTrayIcon.Information)
        else:
            # Shutdown logic would go here (FastAPI shutdown is tricky with uvicorn.run in a thread)
            QMessageBox.information(self, "提示", "請直接關閉程式以停止服務。")

    def fix_firewall(self):
        try:
            # command to open port 8000
            cmd = 'netsh advfirewall firewall add rule name="MediaBackupAPI" dir=in action=allow protocol=TCP localport=8000'
            # Run as admin
            subprocess.run(["powershell", "Start-Process", "cmd.exe", f'/c {cmd}', "-Verb", "RunAs"])
            QMessageBox.information(self, "成功", "防火牆規則已發送申請。")
        except Exception as e:
            QMessageBox.warning(self, "錯誤", f"無法執行：{e}")

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        self.tray_icon.showMessage("Media Backup", "程式仍在背景執行", QSystemTrayIcon.Information)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    
    window = MediaBackupApp()
    window.show()
    
    sys.exit(app.exec())
