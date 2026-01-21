from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
    QPushButton, QFrame, QApplication
)
from PySide6.QtCore import Qt, QSize, Signal, QTimer
from PySide6.QtGui import QIcon, QFont, QColor, QPixmap

class LockScreen(QDialog):
    verify_requested = Signal()

    def __init__(self, hardware_ids, status_msg="No Valid Dongle Found", parent=None):
        super().__init__(parent)
        self.hardware_ids = hardware_ids
        self.status_msg = status_msg
        self.setWindowTitle("ProTranscoder 2026 - Security Lock")
        self.setFixedSize(500, 450)
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
        self.setStyleSheet("background-color: #121212; color: #efefef;")
        self.init_ui()
        
        # Auto-Detect Timer (Poll every 1.5s)
        self.poller = QTimer(self)
        self.poller.timeout.connect(self.auto_scan)
        self.poller.start(1500)

    def auto_scan(self):
        # Silent Scan
        self.verify_requested.emit()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(20)

        # Icon / Header
        header_layout = QHBoxLayout()
        self.icon_label = QLabel()
        # Create a simple red lock or warning icon if no pixmap
        self.icon_label.setText("🔒")
        self.icon_label.setStyleSheet("font-size: 64px; color: #e53935;")
        header_layout.addWidget(self.icon_label)
        
        title_label = QLabel("應用程式已鎖定\n(Application Locked)\n請插入加密鎖！")
        title_label.setStyleSheet("font-size: 20px; font-weight: bold; color: #e53935;")
        header_layout.addWidget(title_label, 1)
        layout.addLayout(header_layout)

        # Status Message
        self.lbl_status = QLabel(self.status_msg)
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet("color: #ff8a80; font-size: 14px; background-color: #2c1a1a; padding: 10px; border-radius: 4px;")
        layout.addWidget(self.lbl_status)

        # Hardware Info Section
        info_frame = QFrame()
        info_frame.setStyleSheet("background-color: #1e1e1e; border: 1px solid #333; border-radius: 8px;")
        info_layout = QVBoxLayout(info_frame)
        
        info_title = QLabel("偵測到的硬體 ID (Hardware IDs):")
        info_title.setStyleSheet("font-weight: bold; color: #90caf9; border: none;")
        info_layout.addWidget(info_title)

        if not self.hardware_ids:
            id_text = "未偵測到抽取式磁碟\n(No removable drives detected)"
        else:
            # Hide specific IDs as per user request (Privacy/Cleanliness)
            count = len(self.hardware_ids)
            id_text = f"已偵測到 {count} 個移動裝置 (Storage Devices Detected)\n(詳細資訊隱藏 / Details Hidden)"
            
        self.id_label = QLabel(id_text)
        self.id_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.id_label.setStyleSheet("font-family: 'Consolas', 'Monaco', monospace; font-size: 12px; color: #aaa; border: none;")
        info_layout.addWidget(self.id_label)
        
        layout.addWidget(info_frame)

        # Suggestion / Contact
        contact_info = QLabel("請將上述 ID 提供給管理員以獲取授權檔 (license.dat)。\n並將該檔案存放於 USB 磁碟的根目錄下。\n(插入後自動偵測 / Auto-Detecting...)")
        contact_info.setStyleSheet("font-size: 12px; color: #888;")
        contact_info.setAlignment(Qt.AlignCenter)
        layout.addWidget(contact_info)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(15)

        self.btn_retry = QPushButton("重新驗證 (Retry)")
        self.btn_retry.setFixedHeight(45)
        self.btn_retry.setCursor(Qt.PointingHandCursor)
        self.btn_retry.setStyleSheet("""
            QPushButton {
                background-color: #1976d2;
                color: white;
                font-weight: bold;
                border-radius: 4px;
                border: none;
            }
            QPushButton:hover { background-color: #1e88e5; }
            QPushButton:pressed { background-color: #1565c0; }
        """)
        self.btn_retry.clicked.connect(self.on_retry)

        self.btn_exit = QPushButton("結束 (Exit)")
        self.btn_exit.setFixedHeight(45)
        self.btn_exit.setCursor(Qt.PointingHandCursor)
        self.btn_exit.setStyleSheet("""
            QPushButton {
                background-color: #333;
                color: #ccc;
                border-radius: 4px;
                border: 1px solid #444;
            }
            QPushButton:hover { background-color: #444; color: white; }
        """)
        self.btn_exit.clicked.connect(self.reject)

        btn_layout.addWidget(self.btn_exit, 1)
        btn_layout.addWidget(self.btn_retry, 2)
        layout.addLayout(btn_layout)

    def on_retry(self):
        self.lbl_status.setText("正在掃描 USB 並驗證中...")
        self.lbl_status.setStyleSheet("color: #90caf9; font-size: 14px; background-color: #1a2733; padding: 10px; border-radius: 4px;")
        QApplication.processEvents()
        self.verify_requested.emit()

    def update_status(self, success, message, hardware_ids=None):
        self.status_msg = message
        self.lbl_status.setText(message)
        if success:
            self.lbl_status.setStyleSheet("color: #81c784; font-size: 14px; background-color: #1b2e1b; padding: 10px; border-radius: 4px;")
            self.poller.stop() # Stop polling on success
            QTimer.singleShot(1000, self.accept) # Close dialog after success
        else:
            self.lbl_status.setStyleSheet("color: #ff8a80; font-size: 14px; background-color: #2c1a1a; padding: 10px; border-radius: 4px;")
            if hardware_ids is not None:
                self.hardware_ids = hardware_ids # Update internal state too
                id_text = "\n".join(hardware_ids) if hardware_ids else "未偵測到抽取式磁碟"
                self.id_label.setText(id_text)

if __name__ == "__main__":
    import sys
    # app/QTimer already imported above? No, checking imports
    # Need to ensure QTimer is imported at top level
    app = QApplication(sys.argv)
    ui = LockScreen(["E:\\ (A1B2C3D4)", "F:\\ (E5F6G7H8)"])
    ui.show()
    sys.exit(app.exec())
