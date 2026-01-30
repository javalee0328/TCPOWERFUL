from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QProgressBar, QPushButton, QWidget
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap, QIcon
import os
import sys

class StartupCheckDialog(QDialog):
    """Two-stage startup check dialog for driver and dongle verification"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("金碼湛 ProTranscoder 2026")
        self.setFixedSize(450, 320)  # Narrower width: 600 -> 450, Height: 320
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        
        # Enable transparency for glassmorphism
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        # Set window icon
        script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base_path = sys._MEIPASS if hasattr(sys, '_MEIPASS') else script_dir
        
        icon_path = os.path.join(base_path, "assets", "icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        
        # Main container with glassmorphism effect
        container = QWidget(self)
        container.setGeometry(0, 0, 450, 320)  # Adjusted width to 450

        container.setStyleSheet("""
            QWidget {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(30, 30, 30, 0.95),
                    stop:0.5 rgba(25, 25, 25, 0.92),
                    stop:1 rgba(20, 20, 20, 0.95));
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.1);
            }
        """)
        
        # Add subtle shadow effect
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        from PySide6.QtGui import QColor
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(30)
        shadow.setColor(QColor(0, 0, 0, 180))
        shadow.setOffset(0, 10)
        container.setGraphicsEffect(shadow)
        
        # Setup UI
        layout = QVBoxLayout(container)
        layout.setContentsMargins(30, 25, 30, 25)  # Reduced vertical margins
        layout.setSpacing(8)  # Reduced spacing: 18 → 8
        
        # Logo
        self.lbl_logo = QLabel()
        logo_path = os.path.join(base_path, "assets", "logo.png")
        if os.path.exists(logo_path):
            pixmap = QPixmap(logo_path)
            scaled_pixmap = pixmap.scaled(60, 60, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.lbl_logo.setPixmap(scaled_pixmap)
        self.lbl_logo.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_logo)
        
        # Title - Bolder font with text shadow
        self.lbl_title = QLabel("金碼湛 ProTranscoder 2026")
        self.lbl_title.setAlignment(Qt.AlignCenter)
        self.lbl_title.setStyleSheet("""
            font-size: 22px; 
            font-weight: 700;
            color: #FFFFFF;
            font-family: 'Microsoft YaHei UI', 'Segoe UI', 'Arial', sans-serif;
            letter-spacing: 1.2px;
            padding: 6px 0;
            text-shadow: 0 2px 8px rgba(0, 0, 0, 0.5);
        """)
        layout.addWidget(self.lbl_title)
        
        # Stage label - Bolder with glow effect
        self.lbl_stage = QLabel()
        self.lbl_stage.setAlignment(Qt.AlignCenter)
        self.lbl_stage.setStyleSheet("""
            font-size: 14px; 
            color: #4A9EFF;
            font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif;
            font-weight: 600;
            letter-spacing: 0.6px;
            text-shadow: 0 0 10px rgba(74, 158, 255, 0.5);
        """)
        layout.addWidget(self.lbl_stage)
        
        # Status message - Bolder with Golden Background
        self.lbl_status = QLabel()
        self.lbl_status.setAlignment(Qt.AlignCenter)
        self.lbl_status.setStyleSheet("""
            font-size: 16px; 
            color: #000000;
            background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FFD700, stop:1 #FFA500);
            border: 2px solid #B8860B;
            border-radius: 8px;
            font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif;
            font-weight: 700;
            padding: 12px;
            margin: 5px;
            line-height: 1.5;
            letter-spacing: 0.4px;
        """)
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)
        
        # Alert message (for dongle removal)
        self.lbl_alert = QLabel()
        self.lbl_alert.setAlignment(Qt.AlignCenter)
        self.lbl_alert.setStyleSheet("""
            font-size: 16px; 
            color: #FF5252;
            font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif;
            font-weight: 700;
            margin-top: 5px;
        """)
        self.lbl_alert.setWordWrap(True)
        self.lbl_alert.hide()
        layout.addWidget(self.lbl_alert)
        
        # Progress bar with smooth animation
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setStyleSheet("""
            QProgressBar {
                border: 1px solid rgba(255, 255, 255, 0.15);
                border-radius: 5px;
                background: rgba(26, 26, 26, 0.6);
                height: 12px;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(42, 95, 255, 0.9), 
                    stop:0.5 rgba(74, 158, 255, 1.0), 
                    stop:1 rgba(42, 95, 255, 0.9));
                border-radius: 4px;
                box-shadow: 0 0 10px rgba(74, 158, 255, 0.5);
            }
        """)
        layout.addWidget(self.progress)
        
        # Smooth animation for progress bar
        from PySide6.QtCore import QPropertyAnimation, QEasingCurve
        self.progress_animation = QPropertyAnimation(self.progress, b"value")
        self.progress_animation.setDuration(2000)
        self.progress_animation.setStartValue(0)
        self.progress_animation.setEndValue(100)
        self.progress_animation.setEasingCurve(QEasingCurve.Linear)
        self.progress_animation.setLoopCount(-1)
        
        layout.addStretch()
        
        # Exit button - Glassmorphism style
        self.btn_exit = QPushButton("退出 (Exit)")
        self.btn_exit.setFixedHeight(42)
        self.btn_exit.setCursor(Qt.PointingHandCursor)
        self.btn_exit.setStyleSheet("""
            QPushButton {
                background: rgba(42, 42, 42, 0.7);
                color: #FFFFFF;
                border: 1px solid rgba(255, 255, 255, 0.15);
                border-radius: 6px;
                font-size: 14px;
                font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif;
                font-weight: 600;
                letter-spacing: 0.8px;
                padding: 10px 20px;
            }
            QPushButton:hover {
                background: rgba(58, 58, 58, 0.8);
                border-color: rgba(255, 255, 255, 0.25);
            }
            QPushButton:pressed {
                background: rgba(26, 26, 26, 0.9);
            }
        """)
        self.btn_exit.clicked.connect(self.reject)
        layout.addWidget(self.btn_exit)
        
        self.current_stage = 0
        self.check_result = False
        self.license_manager = None
        
        # Auto-detection timer
        self.detection_timer = QTimer(self)
        self.detection_timer.timeout.connect(self.check_dongle)
        
    def set_stage_1(self):
        """Stage 1: Driver check"""
        self.current_stage = 1
        self.lbl_stage.setText("第一步：檢查驅動 (Step 1)")
        self.lbl_status.setText("正在檢查 SafeNet 驅動程式...\n(Checking SafeNet Driver...)")
        # Start progress animation
        self.progress_animation.start()
        
    def set_stage_2(self, license_manager=None):
        """Stage 2: Dongle detection with auto-detection"""
        self.current_stage = 2
        self.license_manager = license_manager
        self.lbl_stage.setText("第二步：請插入加密鎖")
        self.lbl_status.setText("驅動完成，請插入加密鎖\n(Environment OK, Please Insert Dongle)")
        # Continue progress animation
        if not self.progress_animation.state():
            self.progress_animation.start()
        
        # Start auto-detection (check every 500ms)
        if license_manager:
            self.detection_timer.start(500)
    
    def check_dongle(self):
        """Auto-detect dongle insertion"""
        if not self.license_manager or self.current_stage != 2:
            return
        
        allowed, status_msg, ids = self.license_manager.check_protection()
        if allowed:
            # Dongle detected!
            self.detection_timer.stop()
            self.set_success(status_msg)
            QTimer.singleShot(800, self.accept)
        
    def set_success(self, message=""):
        """Show success state"""
        self.detection_timer.stop()
        self.progress_animation.stop()
        self.lbl_stage.setText("✓ 驗證成功 (Verified)")
        self.lbl_stage.setStyleSheet("font-size: 14px; color: #4CAF50;")
        self.lbl_status.setText(message or "加密鎖已驗證，正在啟動...\n(Dongle Verified, Starting...)")
        self.progress.setValue(100)
        self.check_result = True
        
    def set_error(self, message=""):
        """Show error state"""
        self.detection_timer.stop()
        self.progress_animation.stop()
        self.lbl_stage.setText("✗ 驗證失敗 (Failed)")
        self.lbl_stage.setStyleSheet("font-size: 14px; color: #F44336;")
        self.lbl_status.setText(message or "未偵測到加密鎖\n(Dongle Not Found)")
        self.progress.setValue(0)
        self.check_result = False

    def set_dongle_removed(self, countdown=60):
        """Show dongle removal alert state (Stage 3)"""
        self.detection_timer.stop()
        self.progress_animation.stop()
        self.progress.hide()
        
        self.lbl_stage.setText("第二步：請插入加密鎖 (Step 2)")
        self.lbl_stage.setStyleSheet("font-size: 14px; color: #4A9EFF; font-weight: 600;")
        
        self.lbl_status.setText("請儘速插回加密鎖以恢復使用")
        self.lbl_status.setStyleSheet("font-size: 13px; color: #FFFFFF; font-weight: 500;")
        
        self.lbl_alert.show()
        self.lbl_alert.setText(f"加密鎖已拔出 程式將退出！\n(倒數 {countdown} 秒)")
        
        self.btn_exit.setText("退出程式 (Exit)")
        self.btn_exit.setStyleSheet("""
            QPushButton {
                background: rgba(42, 42, 42, 0.7);
                color: #FFFFFF;
                border: 1px solid rgba(255, 255, 255, 0.15);
                border-radius: 6px;
                font-size: 14px;
                font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif;
                font-weight: 600;
                padding: 10px 20px;
            }
            QPushButton:hover { background: rgba(229, 57, 53, 0.8); }
        """)
        self.check_result = False
