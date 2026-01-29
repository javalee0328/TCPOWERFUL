import sys
import os
import time
import ctypes
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QPushButton, QLabel, QListWidget, QProgressBar, QFileDialog,
    QFrame, QSplitter, QCheckBox, QLineEdit, QSpinBox, QListWidgetItem,
    QAbstractItemView, QGridLayout, QStackedLayout, QComboBox, QDoubleSpinBox,
    QInputDialog, QMessageBox, QProgressDialog, QMenu, QWidgetAction,
    QToolButton, QStyle, QAbstractSpinBox, QDialog, QTextEdit
)
from PySide6.QtCore import Qt, QSize, QProcess, QTimer, QDir, QEvent, Signal, QRectF, QThread, QTime
from PySide6.QtGui import QIcon, QAction, QKeySequence, QShortcut, QPixmap, QPainter, QPainterPath, QPen, QColor, QKeyEvent, QBrush, QPalette
from core.settings import SettingsManager
from core.metadata import get_video_metadata
from core.preset_data import PRESETS
from core.watch_folder import WatchFolderEngine
from core.cluster_manager import ClusterManager
import subprocess
import logging
import traceback
import re

def debug_log(msg):
    try:
        log_path = os.path.join(os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.getcwd(), "debug.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}\n")
    except:
        pass

class TranscodeWorker(QThread):
    progress_signal = Signal(int, str) # percent, text_status
    finished_signal = Signal(bool, str) # success, msg

    def __init__(self, cmd, target_duration):
        super().__init__()
        self.cmd = cmd
        self.target_duration = target_duration
        self.target_duration = target_duration
        self.killed = False
        self.paused = False
        self._process = None

    def run(self):
        # Force Hide Window
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        
        try:
            debug_log(f"Starting Worker: {self.cmd}")
            self._process = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, # Merge stderr
                stdin=subprocess.PIPE,
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1
            )
            
            self.last_log_lines = []
            for line in self._process.stdout:
                if self.killed: break
                stripped = line.strip()
                debug_log(f"FFMPEG: {stripped}")
                self.parse_line(stripped)
                
                # Keep last 10 lines for error reporting
                # Filter out progress stats (key=value) to keep real errors visible
                if "=" not in stripped and stripped: 
                    self.last_log_lines.append(stripped)
                    if len(self.last_log_lines) > 15: # Increase buffer slightly
                        self.last_log_lines.pop(0)
                
            self._process.wait()
            
            if self.killed: return
            
            if self._process.returncode == 0:
                self.finished_signal.emit(True, "Done")
                debug_log("Worker Finished: Success")
            else:
                # Compile error info from last lines
                error_detail = "\n".join(self.last_log_lines)
                err_msg = f"Exit Code {self._process.returncode}\n\nLast Output:\n{error_detail}"
                self.finished_signal.emit(False, err_msg)
                debug_log(f"Worker Failed: {err_msg}")
                
        except Exception as e:
            debug_log(f"Worker Exception: {e}")
            self.finished_signal.emit(False, str(e))

    def kill(self):
        self.killed = True
        if self._process:
            try:
                self._process.kill()
            except: pass

    def stop(self):
        self.killed = True

    def pause(self):
        self.paused = True
        if self._process:
            # Send 'p' to ffmpeg to pause
            try:
                self._process.stdin.write('p')
                self._process.stdin.flush()
            except: pass

    def resume(self):
        self.paused = False
        if self._process:
            # Send 'p' to ffmpeg to resume
            try:
                self._process.stdin.write('p')
                self._process.stdin.flush()
            except: pass

    def parse_line(self, line):
        if "out_time_us=" in line:
            try:
                time_us = int(line.split('=')[1])
                time_sec = time_us / 1_000_000.0
                
                if self.target_duration > 0:
                    percent = int((time_sec / self.target_duration) * 100)
                    if percent > 100:
                        self.progress_signal.emit(-1, "PKG")
                    else:
                        self.progress_signal.emit(percent, f"{percent}%")
                else:
                    self.progress_signal.emit(-1, "Busy")
            except:
                pass

class DongleCheckThread(QThread):
    """Background hardware lock check to prevent UI stuttering."""
    result_ready = Signal(bool, str, list) # allowed, msg, ids

    def run(self):
        try:
            from core.security import LicenseManager
            lm = LicenseManager()
            allowed, msg, ids = lm.check_protection()
            self.result_ready.emit(allowed, msg, ids)
        except Exception as e:
            self.result_ready.emit(False, str(e), [])

class SmartFailureDialog(QDialog):
    """Professional dialog to translate technical errors into actionable solutions."""
    def __init__(self, technical_log, user_suggestion, fix_params=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("智能轉碼診斷 (Smart Diagnosis)")
        self.resize(550, 420)
        self.setStyleSheet("background-color: #1e1e1e; color: #e0e0e0;")
        self.fix_params = fix_params
        self.apply_fix = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        # Header
        header = QLabel("💡 智能分析建議 (Smart Suggestion)")
        header.setStyleSheet("font-size: 18px; font-weight: bold; color: #40C4FF;")
        layout.addWidget(header)

        # Suggestion Text
        self.lbl_suggestion = QLabel(user_suggestion)
        self.lbl_suggestion.setWordWrap(True)
        self.lbl_suggestion.setStyleSheet("font-size: 14px; line-height: 1.5; background: #2d2d2d; padding: 15px; border-radius: 6px;")
        layout.addWidget(self.lbl_suggestion)

        # Tech Details (Collapsed by default)
        self.details_btn = QPushButton("▶ 顯示技術詳情 (Technical Details)")
        self.details_btn.setCheckable(True)
        self.details_btn.setStyleSheet("text-align: left; background: transparent; border: none; color: #888; padding: 5px;")
        layout.addWidget(self.details_btn)

        self.txt_details = QTextEdit()
        self.txt_details.setPlainText(technical_log)
        self.txt_details.setReadOnly(True)
        self.txt_details.setFixedHeight(120)
        self.txt_details.setStyleSheet("background: #000; color: #aaa; font-family: 'Consolas'; font-size: 11px; border: 1px solid #333;")
        self.txt_details.hide()
        layout.addWidget(self.txt_details)

        self.details_btn.clicked.connect(lambda: self.txt_details.setVisible(self.details_btn.isChecked()))

        layout.addStretch()

        # Action Buttons
        btns = QHBoxLayout()
        self.btn_cancel = QPushButton("取消 (Cancel)")
        self.btn_cancel.setFixedSize(120, 36)
        self.btn_cancel.setStyleSheet("QPushButton { background: #444; border-radius: 4px; color: white; } QPushButton:hover { background: #555; }")
        self.btn_cancel.clicked.connect(self.reject)

        self.btn_retry = QPushButton("套用修復並重試 (Apply & Retry)")
        self.btn_retry.setFixedSize(180, 36)
        if fix_params:
            self.btn_retry.setStyleSheet("QPushButton { background: #1976d2; color: white; font-weight: bold; border-radius: 4px; } QPushButton:hover { background: #1e88e5; }")
        else:
            self.btn_retry.setEnabled(False)
            self.btn_retry.setStyleSheet("background: #333; color: #666; border-radius: 4px;")

        self.btn_retry.clicked.connect(self.on_apply)

        btns.addStretch()
        btns.addWidget(self.btn_cancel)
        btns.addWidget(self.btn_retry)
        layout.addLayout(btns)

    def on_apply(self):
        self.apply_fix = True
        self.accept()

class TaskProgressWidget(QWidget):
    removed = Signal(object)
    transcode_requested = Signal(object) 
    pause_requested = Signal(object)
    resume_requested = Signal(object)
    stop_requested = Signal(object)

    def __init__(self, filename, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 0, 5, 0) # Zero vertical margin for compactness
        layout.setSpacing(10)

        # 1. Name
        self.lbl_name = QLabel(filename)
        self.lbl_name.setFixedWidth(240)
        self.lbl_name.setStyleSheet("color: #ffffff; font-weight: bold; font-size: 16px;") # Pure White
        self.lbl_name.setToolTip(filename)
        layout.addWidget(self.lbl_name)

        # 2. Status
        self.lbl_status = QLabel("Pending")
        self.lbl_status.setFixedWidth(140)
        self.lbl_status.setStyleSheet("color: #ffffff; font-size: 14px;") # Changed to White
        layout.addWidget(self.lbl_status)

        # 3. Format Info
        self.lbl_fmt_info = QLabel("-")
        self.lbl_fmt_info.setFixedWidth(260)
        self.lbl_fmt_info.setStyleSheet("color: #ffffff; font-size: 14px;") # Changed to White
        layout.addWidget(self.lbl_fmt_info)

        # 4. Time Range (In/Out)
        # 4. Time Range (Start - End)
        self.lbl_time_range = QLabel("-")
        self.lbl_time_range.setFixedWidth(220) # Widened to prevent truncation
        self.lbl_time_range.setStyleSheet("color: #ffffff; font-size: 14px;")
        layout.addWidget(self.lbl_time_range)

        # 5. Performance
        self.lbl_perf = QLabel("")
        self.lbl_perf.setFixedWidth(140)
        self.lbl_perf.setStyleSheet("color: #ffffff; font-size: 14px;") # Changed to White
        layout.addWidget(self.lbl_perf)

        # [NEW] 6. Source & Worker
        self.lbl_source = QLabel("Manual")
        self.lbl_source.setFixedWidth(120)
        self.lbl_source.setStyleSheet("color: #90caf9; font-size: 13px;") 
        layout.addWidget(self.lbl_source)

        self.lbl_worker = QLabel("-")
        self.lbl_worker.setFixedWidth(60)
        self.lbl_worker.setStyleSheet("color: #ce93d8; font-size: 13px;")
        layout.addWidget(self.lbl_worker)

        # 7. Progress
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(8) # Slightly thicker
        self.progress.setTextVisible(False) # HIDE TEXT INSIDE BAR
        layout.addWidget(self.progress, 1) # Expand progress to fill space

        # 8. Percent Label (Next to Progress Bar)
        self.lbl_percent = QLabel("")
        self.lbl_percent.setFixedWidth(60)
        self.lbl_percent.setStyleSheet("color: #ffffff; font-weight: bold; font-size: 13px;") # Changed to White
        self.lbl_percent.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_percent)

        # 9. Finish Time
        self.lbl_finish = QLabel("-")
        self.lbl_finish.setFixedWidth(140)
        self.lbl_finish.setStyleSheet("color: #81c784; font-size: 13px;")
        layout.addWidget(self.lbl_finish)

        # 10. Target Path (New)
        self.lbl_target = QLabel("-")
        self.lbl_target.setFixedWidth(200)
        self.lbl_target.setStyleSheet("color: #ffa726; font-size: 12px;")
        self.lbl_target.setToolTip("-")
        layout.addWidget(self.lbl_target)

        # 9. Buttons
        self.btn_transcode = QToolButton()
        self.btn_transcode.setFixedSize(30, 30)
        self.btn_transcode.setIcon(self.create_geometric_icon("refresh", "#E0E0E0", size=24)) # Revert to Refresh (Original)
        self.btn_transcode.setToolTip("當前任務轉碼 (Start Transcode)") # Requested Tooltip
        self.btn_transcode.setStyleSheet(self.get_btn_style("transparent"))
        self.btn_transcode.clicked.connect(self.toggle_transcode)
        layout.addWidget(self.btn_transcode)
        
        self.btn_play_result = QToolButton()
        self.btn_play_result.setFixedSize(30, 30)
        self.btn_play_result.setIcon(self.create_geometric_icon("play", "#ffffff", size=20)) # White Arrow
        self.btn_play_result.setToolTip("播放/刷新結果 (Play/Refresh - F5)")
        self.btn_play_result.hide()
        # [NEW] Green Background Stylesheet
        self.play_style_active = """
            QToolButton { background-color: #2e7d32; border: 1px solid #388e3c; border-radius: 4px; }
            QToolButton:hover { background-color: #388e3c; }
            QToolButton:pressed { background-color: #1b5e20; }
        """
        self.btn_play_result.setStyleSheet(self.play_style_active)
        self.btn_play_result.clicked.connect(self.play_or_refresh)
        layout.addWidget(self.btn_play_result)

        self.btn_open_folder = QToolButton()
        self.btn_open_folder.setFixedSize(30, 30)
        self.btn_open_folder.setIconSize(QSize(26, 26))
        self.btn_open_folder.setIcon(self.create_geometric_icon("folder", size=30))
        self.btn_open_folder.setToolTip("開啟目錄 (Open Folder)")
        self.btn_open_folder.hide()
        self.btn_open_folder.setStyleSheet(self.get_btn_style("transparent"))
        self.btn_open_folder.clicked.connect(self.open_folder)
        layout.addWidget(self.btn_open_folder)

        # Cancel/Remove
        # Cancel/Stop
        self.btn_cancel = QToolButton()
        self.btn_cancel.setFixedSize(30, 30)
        self.btn_cancel.setIcon(self.create_geometric_icon("close", "#ffffff", size=24)) 
        self.btn_cancel.setToolTip("移除任務 (Remove)")
        self.btn_cancel.setStyleSheet(self.get_btn_style("filled_close")) 
        self.btn_cancel.clicked.connect(self.request_stop_or_remove)
        layout.addWidget(self.btn_cancel)

        self.output_path = ""
        self.workers = {} # Key: widget, Value: TranscodeWorker
        self.current_process = None 
        self.player_ref = None # [NEW] Placeholder for main player reference
        self.task_data = None
        self.start_time = None
        self.end_time = None
        self.last_seen_percent = 0 # Track play progress
        self.stopped = False # Flag to block updates after stop

    def set_done(self, out_path, player, speed_text):
        """Called upon successful transcode completion"""
        self.state = "done"
        self.player_ref = player
        self.output_path = out_path
        
        # UI Updates
        self.lbl_status.setText("Done")
        self.lbl_perf.setText(speed_text)
        self.lbl_percent.setText("100%")
        
        # Display full time range: Start - End
        end_time = QTime.currentTime().toString("HH:mm:ss")
        start_t_str = self.start_time.toString("HH:mm:ss") if hasattr(self, 'start_time') and self.start_time else "--:--:--"
        self.lbl_time_range.setText(f"{start_t_str} - {end_time}")
        
        # Icon/Buttons
        self.btn_transcode.setIcon(self.create_geometric_icon("refresh", "#E0E0E0", size=24))
        self.btn_transcode.setToolTip("重新轉碼 (Re-Transcode)")
        
        self.btn_play_result.show()
        self.btn_open_folder.show()
        
        # Reset Cancel Button to Close (Remove) style
        self.btn_cancel.setFixedSize(30, 30)
        self.btn_cancel.setIconSize(QSize(24, 24))
        self.btn_cancel.setIcon(self.create_geometric_icon("close", "#ffffff", size=24))
        self.btn_cancel.setToolTip("移除任務 (Remove)")
        self.btn_cancel.setStyleSheet(self.get_btn_style("filled_close"))

    def get_btn_style(self, variant="transparent"):
        if variant == "filled_close":
             return """
                QToolButton { 
                    background: #37474f; 
                    border: none; 
                    border-radius: 4px; 
                } 
                QToolButton:hover { background: #d32f2f; }
                QToolButton:pressed { background: #b71c1c; }
            """
        elif variant == "green_active":
             return """
                QToolButton { background-color: #2e7d32; border: 1px solid #388e3c; border-radius: 4px; }
                QToolButton:hover { background-color: #388e3c; }
                QToolButton:pressed { background-color: #1b5e20; }
            """
        return """
            QToolButton { 
                background: transparent; 
                border: 1px solid #555; 
                border-radius: 4px; 
            } 
            QToolButton:hover { background: #444; border: 1px solid #777; }
            QToolButton:pressed { background: #222; }
        """

    def set_task_data(self, task):
        self.task_data = task
        # 1. Format Info
        fmt_parts = []
        fmt_parts.append(task.get('container', '').upper())
        fmt_parts.append(task.get('vcodec', ''))
        
        bitrate = task.get('bitrate', '')
        if bitrate:
            try:
                b_val = int(bitrate)
                if b_val >= 1000:
                    fmt_parts.append(f"{b_val/1000:g}Mbps")
                else:
                    fmt_parts.append(f"{b_val}k")
            except:
                fmt_parts.append(f"{bitrate}k")
        
        res = task.get('resolution')
        if res: fmt_parts.append(res)
        
        self.lbl_fmt_info.setText(" / ".join(fmt_parts))
        
        # 2. Time Range (Start - End)
        in_p = task.get("in_point", 0)
        out_p = task.get("out_point", 0)
        
        def ms_to_fmt(ms):
             if not ms: return "00:00:00"
             s = int(ms / 1000)
             m, s = divmod(s, 60)
             h, m = divmod(m, 60)
             return f"{h:02d}:{m:02d}:{s:02d}"

        if in_p or out_p:
            self.lbl_time_range.setText(f"{ms_to_fmt(in_p)} - {ms_to_fmt(out_p)}")
        else:
            self.lbl_time_range.setText("Full")

        # [NEW] Source & Worker Info
        source_type = task.get("source_type", "Manual")
        self.lbl_source.setText(source_type)
        if source_type != "Manual":
            self.lbl_source.setStyleSheet("color: #ff9800; font-weight: bold; font-size: 13px;") # Highlight folder tasks
        else:
            self.lbl_source.setStyleSheet("color: #90caf9; font-size: 13px;")

        worker_id = task.get("worker_id", "-")
        self.lbl_worker.setText(worker_id)

        finish_time = task.get("finish_time", "-")
        self.lbl_finish.setText(finish_time)
        
        target_dir = task.get("output_dir", "-")
        self.lbl_target.setText(os.path.basename(target_dir))
        self.lbl_target.setToolTip(target_dir)

        # Highlight Play Button (Actually, User wants ONLY at 100%)
        # So we ensure it is hidden here
        self.btn_play_result.hide() 
        self.btn_transcode.setIcon(self.create_geometric_icon("refresh", "#E0E0E0", size=24)) # Reset to Refresh icon for re-run
        self.btn_transcode.setToolTip("當前任務轉碼 (Start Transcode)") # Reset to initial state
        self.state = "pending" # FIX: Set to pending, NOT done

    def toggle_transcode(self):
        state = getattr(self, 'state', 'pending') # Get current state, default to 'pending'
        if state == 'running':
            # Pause
            self.pause_requested.emit(self)
            self.state = 'paused'
            # Resume Icom = Refresh (Original Shape) but Green to show "Active/Resume"? Or just Original?
            # User said: "Recover original... don't become Play style".
            # Original was #E0E0E0 Refresh.
            self.btn_transcode.setIcon(self.create_geometric_icon("refresh", "#E0E0E0", size=24)) 
            self.btn_transcode.setToolTip("繼續轉碼 (Resume)")
            self.lbl_status.setText("Paused")
            self.lbl_status.setStyleSheet("color: #FFC107;")
            
        elif state == 'paused':
            # Resume
            self.resume_requested.emit(self)
            self.state = 'running'
            self.btn_transcode.setIcon(self.create_geometric_icon("pause", "#40C4FF", size=24))
            self.btn_transcode.setToolTip("暫停轉碼 (Pause)")
            self.lbl_status.setText("Transcoding...")
            self.lbl_status.setStyleSheet("color: #4CAF50;")
            
        else:
            # Start
            self.transcode_requested.emit(self)
            
    def set_started(self):
        self.state = 'running'
        self.stopped = False # Reset flag
        self.start_time = QTime.currentTime()
        t_str = self.start_time.toString("HH:mm:ss") + " - ..."
        self.lbl_time_range.setText(t_str)
        self.lbl_status.setText("Transcoding...")
        self.lbl_status.setStyleSheet("color: #4CAF50; font-weight: bold;")
        self.btn_transcode.setIcon(self.create_geometric_icon("pause", "#40C4FF", size=24)) 
        self.btn_transcode.setToolTip("暫停轉碼 (Pause)")
        
        # Change Cancel to Stop Style (Larger Icon)
        self.btn_cancel.setIconSize(QSize(28, 28)) # Slightly larger icon
        self.btn_cancel.setIcon(self.create_geometric_icon("stop", "#ffffff", size=28)) 
        self.btn_cancel.setToolTip("停止轉碼 (Stop)")
        self.btn_cancel.setStyleSheet(self.get_btn_style("filled_close")) 

    def request_stop_or_remove(self):
        state = getattr(self, 'state', 'pending')
        
        if state in ['running', 'paused']:
            # Confirm Stop
            reply = QMessageBox.question(self, "停止轉碼?", "確定要終止目前的轉碼任務嗎？\n(進度將歸零)", 
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                self.stop_requested.emit(self)
                # Reset UI
                self.state = 'stopped'
                self.stopped = True # Block future updates
                self.progress.setValue(0)
                self.lbl_percent.setText("") 
                self.lbl_time_range.setText("") 
                self.lbl_perf.setText("") 
                self.lbl_status.setText("Stopped")
                self.lbl_status.setStyleSheet("color: #aaa;")
                
                # Reset Size
                self.btn_cancel.setFixedSize(30, 30)
                self.btn_cancel.setIconSize(QSize(24, 24)) # Reset to normal
                self.btn_cancel.setIcon(self.create_geometric_icon("close", "#ffffff", size=24)) 
                self.btn_cancel.setToolTip("移除任務 (Remove)")
                self.btn_cancel.setStyleSheet(self.get_btn_style("filled_close"))
                
                # Reset Transcode Button to Original (Refresh)
                self.btn_transcode.setIcon(self.create_geometric_icon("refresh", "#E0E0E0", size=24))
                self.btn_transcode.setToolTip("重新轉碼 (Re-Transcode)") # Changed to Re-transcode AFTER stop
                
        else:
            # Just Remove
            self.removed.emit(self)

    def play_or_refresh(self):
        # Reset Style to "Seen" (Transparent)
        self.btn_play_result.setStyleSheet(self.get_btn_style("transparent"))
        # Update last seen
        self.last_seen_percent = self.progress.value()
        
        if self.player_ref:
            if self.player_ref.current_file and os.path.normpath(self.player_ref.current_file) == os.path.normpath(self.output_path):
                 self.player_ref.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_F5, Qt.NoModifier))
            else:
                 if os.path.exists(self.output_path):
                     self.player_ref.load_video(self.output_path, is_result=True, start_pos=0)
                     self.player_ref.setFocus()

    def play_this(self):
        try:
            if self.player_ref and os.path.exists(self.output_path):
                self.player_ref.load_video(self.output_path, is_result=True, start_pos=0)
                self.player_ref.setFocus()
        except Exception as e:
            print(f"Error playing result: {e}")

    def create_geometric_icon(self, shape, color="#E0E0E0", size=32):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        
        pen = QPen(QColor(color))
        pen.setWidth(2)
        painter.setPen(pen)
        
        # Margins to prevent clipping
        m = 2 
        s = size - 2*m
        
        if shape == "play":
            painter.setBrush(QColor(color))
            painter.setPen(Qt.NoPen)
            path = QPainterPath()
            # Triangle pointing right
            path.moveTo(m + 4, m)
            path.lineTo(m + s - 2, m + s/2)
            path.lineTo(m + 4, m + s)
            path.closeSubpath()
            painter.drawPath(path)
            
        elif shape == "folder":
            painter.setBrush(Qt.NoBrush) # Outline style
            painter.setPen(QPen(QColor(color), 2))
            # Tab
            painter.drawRoundedRect(m, m, s, s*0.8, 2, 2)
            painter.drawLine(m, m+6, m+s, m+6) # Folder flap line (simulated)
            
        elif shape == "refresh":
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(color), 2.5))
            rect = QRectF(m, m, s, s)
            painter.drawArc(rect, 30 * 16, 300 * 16) # 300 degree arc
            # Arrow head
            painter.drawLine(int(m+s-4), int(m+s/2), int(m+s), int(m+s/2+4))
            
        elif shape == "stop":
            painter.setBrush(QColor(color))
            painter.setPen(Qt.NoPen)
            pad = 6
            painter.drawRect(m+pad, m+pad, s-2*pad, s-2*pad)

        elif shape == "pause":
            painter.setBrush(QColor(color))
            painter.setPen(Qt.NoPen)
            w = (s - 6)/2
            painter.drawRect(m + 2, m + 4, w, s - 8)
            painter.drawRect(m + 2 + w + 2, m + 4, w, s - 8)

        elif shape == "close":
            painter.setPen(QPen(QColor(color), 3))
            painter.drawLine(m+4, m+4, m+s-4, m+s-4)
            painter.drawLine(m+s-4, m+4, m+4, m+s-4)
            
        painter.end()
        return QIcon(pixmap)

    def open_folder(self):
        if os.path.exists(self.output_path):
            try:
                folder = os.path.dirname(self.output_path)
                os.startfile(folder)
            except: pass





class HistoryItemWidget(QWidget):
    """Custom widget for History menu entries with a delete button."""
    triggered = Signal(str)
    removed = Signal(str)

    def __init__(self, path, display_text, parent=None):
        super().__init__(parent)
        self.path = path
        self.setMinimumWidth(300) # Slightly narrower for safe fit
        self.setFixedHeight(34)
        self.setMouseTracking(True)
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(5)
        
        # Folder Icon + Text
        self.lbl_path = QLabel(display_text)
        self.lbl_path.setStyleSheet("color: white; font-size: 13px; background: transparent; border: none; font-weight: bold;")
        # Prevent it from taking too much space and pushing the button out
        self.lbl_path.setMinimumWidth(100)
        layout.addWidget(self.lbl_path, 1)
        
        # Delete Button - Standard Red 'X' with Border for visibility
        self.btn_del = QPushButton("X")
        self.btn_del.setFixedSize(22, 22)
        self.btn_del.setCursor(Qt.PointingHandCursor)
        self.btn_del.setToolTip("移除 (Remove)")
        self.btn_del.setStyleSheet("""
            QPushButton { 
                background-color: transparent; color: #ff453a; 
                border: 2px solid #ff453a; border-radius: 4px;
                font-weight: bold; font-size: 13px;
            }
            QPushButton:hover { background-color: #ff453a; color: white; }
        """)
        self.btn_del.clicked.connect(lambda: self.removed.emit(self.path))
        layout.addWidget(self.btn_del)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            # Check if clicked outside delete button
            if not self.btn_del.geometry().contains(event.pos()):
                self.triggered.emit(self.path)
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        self.lbl_path.setStyleSheet("color: white; font-size: 13px; background: #0078d4; border: none; font-weight: bold;")
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.lbl_path.setStyleSheet("color: white; font-size: 13px; background: transparent; border: none; font-weight: bold;")
        super().leaveEvent(event)





class ModernTranscoderUI(QMainWindow):
    def __init__(self):
        super().__init__()
        debug_log("MainWindow: Init Start")
        self.loading = True
        
        # Default Settings
        self.default_settings = {
            "vu_offset": 150,
            "presets": {
                "MP4 (H.264 High)": {"container": "mp4", "vcodec": "h264", "bitrate": "5000", "gain": 0.0},
                "MXF (XDCAM 50)": {"container": "mxf", "vcodec": "mpeg2video", "bitrate": "50000", "gain": 0.0},
                "ProRes 422": {"container": "mov", "vcodec": "prores", "bitrate": "150000", "gain": 0.0}
            }
        }
        
        debug_log("MainWindow: Loading SettingsManager")
        self.settings = SettingsManager()
        
        # Determine if we need to merge system presets
        current_presets = self.settings.get("presets", {})
        # Update with new system presets (PRESETS)
        # This ensures new features appear even if file exists
        current_presets.update(PRESETS)
        self.settings.set("presets", current_presets)
        
        self.setWindowTitle("ProTranscoder 2026 - Windows 11 Edition")
        self.resize(1200, 800)
        self.current_source = ""
        self.pending_tasks = []
        self.pending_tasks = []
        self.workers = {} # Key: widget, Value: TranscodeWorker
        self.is_processing = False
        
        # [NEW] Initialize Watch Folder Engine
        self.watch_engine = WatchFolderEngine(self.settings, self)
        self.watch_engine.file_detected.connect(self.on_watch_folder_detected)
        self.watch_engine.start()
        
        # [NEW] Initialize Cluster Manager
        self.cluster_mgr = ClusterManager(self.settings, self)
        self.cluster_mgr.task_synced.connect(self.on_cluster_task_synced)
        self.cluster_mgr.node_updated.connect(self.on_cluster_node_updated)
        self.cluster_mgr.start()

        debug_log("MainWindow: Calling setup_ui")
        self.setup_ui()
        
        debug_log("MainWindow: Restoring Saved Settings")
        self.load_saved_settings()
        
        debug_log("MainWindow: Applying Styles")
        self.apply_styles()
        
        debug_log("MainWindow: Loading Pending Tasks")
        self.load_pending_tasks() # Restore tasks
        
        # Auto-save timer (every 30 seconds)
        self.auto_save_timer = QTimer(self)
        self.auto_save_timer.timeout.connect(self.auto_save)
        self.auto_save_timer.start(30000)  # 30 seconds
        
        # Dongle removal detection timer (every 10 seconds - Asynchronous)
        self.dongle_monitor_timer = QTimer(self)
        self.dongle_monitor_timer.timeout.connect(self.check_dongle_status)
        self.dongle_monitor_timer.start(10000)  # 10 seconds
        
        self.loading = False
        
        debug_log("MainWindow: Init Complete")
        

    def update_history_menus(self):
        # 1. Output History (Existing)
        try:
             if hasattr(self, 'menu_hist_out'):
                self.menu_hist_out.clear()
        except: pass
        
        out_history = self.settings.get("output_history", [])
        if hasattr(self, 'menu_hist_out'):
            for path in out_history:
                 action = self.menu_hist_out.addAction(path)
                 action.triggered.connect(lambda checked=False, p=path: self.set_output_dir_from_history(p))
             
        # 2. Source History (New)
        if hasattr(self, 'menu_hist_source'):
            self.menu_hist_source.clear()
            self.menu_hist_source.setStyleSheet("QMenu { background-color: #2b2b2b; }")
            
            src_history = self.settings.get("source_history", [])
            print(f"DEBUG: Updating History Menu. Count: {len(src_history)}")
            
            # [History Cleanup]
            clean_history = []
            seen = set()
            for path in src_history:
                if not path: continue
                p = os.path.normpath(path)
                if os.path.isfile(p): p = os.path.dirname(p)
                if p not in seen:
                    seen.add(p)
                    clean_history.append(p)
            src_history = clean_history

            if not src_history:
                act = self.menu_hist_source.addAction("(Empty History)")
                act.setEnabled(False)
            else:
                for path in src_history:
                    display = path
                    if len(display) > 40:
                        try:
                            head, tail = os.path.split(path)
                            display = f"📁 .../{os.path.basename(head)}/{tail}" if head else f"📁 {display[-35:]}"
                        except: display = f"📁 {display[-35:]}"
                    else:
                        display = f"📁 {display}"

                    # Custom Widget
                    item_w = HistoryItemWidget(path, display)
                    action = QWidgetAction(self.menu_hist_source)
                    action.setDefaultWidget(item_w)
                    
                    item_w.triggered.connect(lambda p, a=action: self.handle_history_menu_selection(p, a))
                    item_w.removed.connect(self.remove_source_history_item)
                    self.menu_hist_source.addAction(action)
            
            self.menu_hist_source.addSeparator()
            self.menu_hist_source.addAction("全部清空 (Clear All)").triggered.connect(self.clear_source_history)



    def set_output_dir_from_history(self, path):
         if os.path.isdir(path):
             self.output_dir = path
             self.lbl_output_path.setText(path)
             self.settings.add_output_history(path)
             self.update_history_menus() # Refresh order
             self.save_settings()

    def load_source_from_history(self, path):
        """Open File Dialog at the history folder."""
        if os.path.exists(path):
            # Instead of loading directly, we OPEN the dialog at this path
            self.last_source_dir = path
            self.add_files() # This opens QFileDialog using last_source_dir
            
            # Move to top of history
            self.settings.add_source_history(path)
            self.update_history_menus()
            self.save_settings()
        else:
             from PySide6.QtWidgets import QMessageBox
             QMessageBox.warning(self, "Error", "Folder not found: " + path)

    def check_smart_remux(self, path):
        """Probes file and returns fixed path if SLES/Unknown & _remux exists."""
        if not path or not os.path.exists(path): return path, None
        
        try:
            from core.metadata import get_video_metadata
            meta = get_video_metadata(path)
            is_sles = meta and ("SLES" in meta.get("codec_tag", "") or meta.get("codec") == "unknown")
            
            print(f"DEBUG: SmartCheck - File: {os.path.basename(path)}")
            print(f"DEBUG: SmartCheck - Codec: {meta.get('codec')}, Tag: {meta.get('codec_tag')}, is_sles: {is_sles}")
            
            if is_sles:
                fixed_path = os.path.splitext(path)[0] + "_remux.ts"
                print(f"DEBUG: SmartCheck - Looking for: {fixed_path} -> Exists: {os.path.exists(fixed_path)}")
                
                if os.path.exists(fixed_path):
                     print(f"DEBUG: Auto-switching to fixed version: {fixed_path}")
                     # RE-PROBE metadata for the fixed file so codec info is correct
                     from core.metadata import get_video_metadata
                     fixed_meta = get_video_metadata(fixed_path)
                     print(f"DEBUG: Fixed Meta Probe -> Codec: {fixed_meta.get('codec') if fixed_meta else 'FAILED'}")
                     
                     if hasattr(self, 'statusBar'):
                         self.statusBar().showMessage(f"已自動載入修復版本 (Codec: {fixed_meta.get('codec') if fixed_meta else '??'})", 3000)
                     return fixed_path, fixed_meta if fixed_meta else meta
            return path, meta
            
        except Exception as e:
            print(f"Error checking remux: {e}")
            return path, None

    def clear_source_history(self):
        self.settings.set("source_history", [])
        self.update_history_menus()
        self.save_settings()

    def open_history_manager(self):
        try:
            from ui.history_dialog import HistoryManagerDialog
            dlg = HistoryManagerDialog(self.settings, "source_history", "管理源檔歷史 (Manage Source History)", self)
            dlg.exec()
            # Refresh menu after dialog closes
            self.update_history_menus()
        except Exception as e:
            print(f"Error opening history manager: {e}")
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Error", f"Failed to open manager: {e}")

    def remove_source_history_item(self, path):
        """Remove a specific path and refresh menu."""
        history = self.settings.get("source_history", [])
        if path in history:
            history.remove(path)
            self.settings.set("source_history", history)
            # Use a longer delay (100ms) to ensure event processing is finished 
            # to avoid "Internal C++ object already deleted" crash
            QTimer.singleShot(100, self.update_history_menus)

    def handle_history_menu_selection(self, path, action):
        """Standard selection handler that also closes the menu."""
        self.menu_hist_source.close()
        self.load_source_from_history(path)

    def load_saved_settings(self):
        saved_out = self.settings.get("output_dir")
        print(f"DEBUG: Loading Settings... OutputDir: {saved_out}")
        if saved_out and os.path.exists(saved_out):
            self.output_dir = saved_out
            if hasattr(self, 'lbl_output_path'):
                self.lbl_output_path.setText(saved_out)
        
        # Init History Menus
        self.update_history_menus()
        
        self.chk_rename.setChecked(self.settings.get("chk_rename", False))
        self.edit_base_name.setText(self.settings.get("base_name", ""))
        self.spin_seq.setValue(self.settings.get("next_seq", 1))
        # Strip 'k' from loaded bitrate to prevent '5000kk'
        self.edit_bitrate.setText(str(self.settings.get("bitrate", "5000")).rstrip('k'))
        if hasattr(self, 'combo_fps'):
            self.combo_fps.setCurrentText(str(self.settings.get("fps", "")))
        if hasattr(self, 'combo_sys'):
            self.combo_sys.setCurrentText(str(self.settings.get("tv_system", "Auto")))
            
        self.output_dir = self.settings.get("output_dir", "")
        if self.output_dir:
            self.lbl_output_path.setText(self.output_dir)
        
        # Load Last File - defer ALL operations to avoid blocking
        last_file = self.settings.get("last_source_file", "")
        print(f"DEBUG: Loaded Settings - Output: {self.output_dir}, Source: {last_file}")
        if last_file:
            # Apply Smart Check on Auto-Load
            final_path, meta = self.check_smart_remux(last_file)
            self.current_source = final_path
            
            # Use history position
            saved_pos = self.settings.get_history_position(final_path)
            
            # Defer ALL file operations
            def load_deferred():
                # Use preloaded meta if available
                self.update_metadata_panel(final_path, preloaded_meta=meta)
                self.player.load_video(final_path, start_pos=saved_pos)
            
            QTimer.singleShot(100, load_deferred)
                
        # Load VU Offset
        vu_offset = self.settings.get("vu_offset", 150)
        self.player.set_vu_offset(int(vu_offset))

        # Load Target Settings
        self.combo_container.setCurrentText(self.settings.get("tgt_container", "mp4"))
        self.combo_vcodec.setCurrentText(self.settings.get("tgt_vcodec", "h264"))
        self.spin_gain.setValue(float(self.settings.get("tgt_gain", 0.0)))


        # Load Presets
        self.combo_presets.blockSignals(True)
        self.combo_presets.clear()
        self.combo_presets.addItem("Custom / Unsaved")
        presets = self.settings.get("presets", self.default_settings["presets"])
        self.combo_presets.addItems(sorted(presets.keys()))
        self.combo_presets.setCurrentText(self.settings.get("last_preset", "Custom / Unsaved"))
        self.combo_presets.blockSignals(False)

        # Trigger load if a valid preset was selected
        if self.combo_presets.currentText() != "Custom / Unsaved":
            self.load_preset(self.combo_presets.currentText())

    def save_settings(self):
        if getattr(self, 'loading', False):
            return
        last_file = getattr(self, 'current_source', "")
        self.settings.set("output_dir", getattr(self, 'output_dir', ""))
        self.settings.set("chk_rename", self.chk_rename.isChecked())
        self.settings.set("base_name", self.edit_base_name.text())
        self.settings.set("next_seq", self.spin_seq.value())
        self.settings.set("bitrate", self.edit_bitrate.text())
        self.settings.set("last_source_dir", getattr(self, 'last_source_dir', ""))
        self.settings.set("last_source_file", last_file)
        
        # Save Target Settings
        if hasattr(self, 'combo_fps') and self.combo_fps.currentText():
            self.settings.set("fps", self.combo_fps.currentText())
        if hasattr(self, 'combo_sys') and self.combo_sys.currentText():
            self.settings.set("tv_system", self.combo_sys.currentText())
            
        self.settings.set("tgt_container", self.combo_container.currentText())
        self.settings.set("tgt_vcodec", self.combo_vcodec.currentText())
        self.settings.set("tgt_gain", self.spin_gain.value())
        self.settings.set("last_preset", self.combo_presets.currentText())
        
        # Save VU Offset
        if hasattr(self, 'player'):
            self.settings.set("vu_offset", self.player.get_vu_offset())
        
        # Save specific history for this file
        if last_file and hasattr(self, 'player'):
            current_pos = self.player.media_player.position()
            self.settings.update_history(last_file, current_pos)

    def update_audio_defaults(self, container):
        """Auto-configure Audio Codec based on Container."""
        container = container.lower()
        if container == "mxf":
             self.combo_acode.setCurrentText("pcm_s16le")
        elif container == "mp4" and self.combo_acode.currentText() == "pcm_s16le":
             self.combo_acode.setCurrentText("aac") # Revert to AAC for MP4
        self.save_settings()

    def on_video_loaded(self, file_path, is_result):
        print(f"DEBUG: on_video_loaded - Path: {file_path}, IsResult: {is_result}")
        if not is_result:
            self.current_source = file_path
            self.last_source_dir = os.path.dirname(file_path)
            # Add DIRECTORY to Source History (Deduped by folder)
            print(f"DEBUG: Adding to Source History: {self.last_source_dir}")
            self.settings.add_source_history(self.last_source_dir)
            self.update_history_menus()
            self.save_settings()
        else:
            pass

    def closeEvent(self, event):
        print("DEBUG: closeEvent triggered")
        # Save position FIRST
        if hasattr(self, 'player') and hasattr(self, 'current_source') and self.current_source:
            pos = self.player.media_player.position()
            self.settings.update_history(self.current_source, pos)
            print(f"DEBUG: Saved position {pos}")
        
        if hasattr(self, 'player'):
            self.player.shutdown()
                
        self.save_pending_tasks()
        self.save_settings()
        print("DEBUG: closeEvent complete")
        event.accept()

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # Shortcut: Enter to Add Task
        self.sc_add = QShortcut(QKeySequence(Qt.Key_Return), self)
        self.sc_add.activated.connect(self.add_task_to_queue)
        self.sc_add2 = QShortcut(QKeySequence(Qt.Key_Enter), self)
        self.sc_add2.activated.connect(self.add_task_to_queue)

        # 1. Sidebar (Navigation)
        self.sidebar = QFrame()
        self.sidebar.setFixedWidth(200)
        self.sidebar.setObjectName("sidebar")
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(0, 10, 0, 10)
        sidebar_layout.setSpacing(5)
        
        self.btn_home = QPushButton("金碼湛 Transcoder")
        script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base_path = sys._MEIPASS if hasattr(sys, '_MEIPASS') else script_dir
        logo_path = os.path.join(base_path, "assets", "logo.png")
        if os.path.exists(logo_path):
            self.btn_home.setIcon(QIcon(logo_path))
            self.btn_home.setIconSize(QSize(24, 24))
        self.btn_dash = QPushButton("📊  Dashboard")
        self.btn_watch = QPushButton("👀  Watch Folders")
        self.btn_cluster = QPushButton("🖥  Cluster Status")
        self.btn_settings = QPushButton("⚙  Settings")
        
        for btn in [self.btn_home, self.btn_dash, self.btn_watch, self.btn_cluster, self.btn_settings]:
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked=True, b=btn: self.on_nav_clicked(b))
            sidebar_layout.addWidget(btn)
        
        self.btn_home.setChecked(True)
            
        sidebar_layout.addStretch()
        main_layout.addWidget(self.sidebar)

        # 2. Main Content Area (Stacked)
        stack_container = QWidget()
        self.stack = QStackedLayout(stack_container)
        main_layout.addWidget(stack_container, 1)

        # --- Page 0: Transcoder (Home) ---
        self.transcoder_page = QWidget()
        trans_layout = QVBoxLayout(self.transcoder_page)
        trans_layout.setContentsMargins(10, 10, 10, 10)
        self.stack.addWidget(self.transcoder_page)

        # --- Page 1: Watch Folders ---
        self.watch_page = QWidget()
        w_layout = QVBoxLayout(self.watch_page)
        w_title = QLabel("📂 監控資料夾設定 (Watch Folder Settings)")
        w_title.setStyleSheet("font-size: 20px; font-weight: bold; color: #4CAF50;")
        w_layout.addWidget(w_title)
        self.watch_list = QListWidget()
        w_layout.addWidget(self.watch_list)
        w_btn_add = QPushButton("添加監控路徑 (Add Path)")
        w_btn_add.clicked.connect(self.add_watch_folder_ui)
        w_layout.addWidget(w_btn_add)
        self.stack.addWidget(self.watch_page)

        # --- Page 2: Cluster Status ---
        self.cluster_page = QWidget()
        cl_layout = QVBoxLayout(self.cluster_page)
        cl_title = QLabel("🖥 集群節點狀態 (Cluster Nodes)")
        cl_title.setStyleSheet("font-size: 20px; font-weight: bold; color: #BB86FC;")
        cl_layout.addWidget(cl_title)
        self.node_list = QListWidget()
        cl_layout.addWidget(self.node_list)
        self.lbl_node_info = QLabel("本機辨識碼 (Local Node): -")
        cl_layout.addWidget(self.lbl_node_info)
        self.stack.addWidget(self.cluster_page)
        
        content_splitter = QSplitter(Qt.Vertical)
        
        # Upper: Player and Controls
        upper_widget = QFrame()
        upper_layout = QHBoxLayout(upper_widget)
        
        # Player Column (Vertical: Player + Button Row)
        player_col_widget = QWidget()
        player_col_layout = QVBoxLayout(player_col_widget)
        player_col_layout.setContentsMargins(0,0,0,0)
        player_col_layout.setSpacing(10) # Visual separation

        from ui.player_widget import VideoPlayerWidget
        from core.transcoder import Transcoder
        
        debug_log("setup_ui: Init Player Helper")
        self.player = VideoPlayerWidget()
        
        debug_log("setup_ui: Injecting ffplay path")
        # Inject ffplay path for external preview
        self.player.ffplay_path = Transcoder().ffplay_path
        
        self.player.videoLoaded.connect(self.on_video_loaded)
        player_col_layout.addWidget(self.player, 1) # Expand Player
        
        # --- Page 3: Settings ---
        self.settings_page = QWidget()
        s_layout = QVBoxLayout(self.settings_page)
        s_title = QLabel("⚙ 全局設定 (Global Settings)")
        s_title.setStyleSheet("font-size: 20px; font-weight: bold; color: #757575;")
        s_layout.addWidget(s_title)
        
        s_form = QGridLayout()
        s_form.addWidget(QLabel("集群同步路徑 (Cluster Sync Path):"), 0, 0)
        self.edit_cluster_path = QLineEdit(self.settings.get("cluster_path", ""))
        s_form.addWidget(self.edit_cluster_path, 0, 1)
        
        btn_save_s = QPushButton("儲存設定 (Save Settings)")
        btn_save_s.clicked.connect(self.save_global_settings_ui)
        s_layout.addLayout(s_form)
        s_layout.addWidget(btn_save_s)
        s_layout.addStretch()
        self.stack.addWidget(self.settings_page)

        debug_log("setup_ui: Creating Buttons")
        # Action Bar (Add Task + Start All)
        action_bar = QHBoxLayout()
        action_bar.setSpacing(10)
        
        self.btn_large_add = QPushButton(" + 添加任務 (Add to Queue)")
        self.btn_large_add.setCursor(Qt.PointingHandCursor)
        self.btn_large_add.setToolTip("將目前設定加入排程 (快速鍵: Enter)")
        self.btn_large_add.setFixedHeight(50) # Big Click Target
        self.btn_large_add.setStyleSheet("""
            QPushButton { 
                background-color: #0060a0; 
                color: white; 
                font-size: 16px; 
                font-weight: bold; 
                border-radius: 6px; 
                border: 1px solid #0078d4;
            }
            QPushButton:hover { background-color: #0078d4; border: 1px solid #40a9ff; }
            QPushButton:pressed { background-color: #004a7c; }
        """)
        self.btn_large_add.clicked.connect(self.add_task_to_queue)
        
        # Global Start Button
        self.btn_start_all = QPushButton("⚡ 開始所有轉碼 (Start All)")
        self.btn_start_all.setFixedHeight(50) # Match height
        self.btn_start_all.setCursor(Qt.PointingHandCursor)
        self.btn_start_all.setStyleSheet("""
            QPushButton { 
                background-color: #27663e; 
                color: #e6fcf5; 
                font-size: 16px; 
                font-weight: 900; 
                border-radius: 6px; 
                border: 1px solid #388e3c;
            }
            QPushButton:hover { background-color: #2e7d32; color: white; border: 1px solid #66bb6a; }
            QPushButton:pressed { background-color: #1b5e20; }
        """)
        self.btn_start_all.clicked.connect(self.start_transcoding_queue)
        
        action_bar.addWidget(self.btn_large_add, 1)   # Blue Button
        action_bar.addWidget(self.btn_start_all, 1)   # Green Button
        
        player_col_layout.addLayout(action_bar)
        
        upper_layout.addWidget(player_col_widget, 2)
        
        # Right Control Panel (Playlist + Metadata)
        params_panel = QFrame()
        params_panel.setFixedWidth(600) # Widened for long presets
        params_layout = QVBoxLayout(params_panel)
        params_layout.setSpacing(5)
        params_layout.setContentsMargins(5, 5, 5, 5)
        
        # Metadata
        # Metadata
        meta_group = QFrame()
        meta_group.setStyleSheet("background-color: #222; border-radius: 5px;")
        meta_layout = QGridLayout(meta_group)
        meta_layout.setContentsMargins(8, 8, 8, 8)
        meta_layout.setContentsMargins(8, 8, 8, 8)
        meta_layout.setVerticalSpacing(2)
        
        # [NEW] Source Path Label (High Visibility) [Button] [Label]
        source_container = QWidget()
        source_layout = QHBoxLayout(source_container)
        source_layout.setContentsMargins(0,0,0,0)
        source_layout.setSpacing(5) # Exactly matching Output Layout spacing
        
        # 1. Main Load Source Button
        self.btn_source = QToolButton()
        self.btn_source.setIcon(self.style().standardIcon(QStyle.SP_DirHomeIcon))
        self.btn_source.setFixedSize(45, 45)
        self.btn_source.setIconSize(QSize(32, 32))
        self.btn_source.setToolTip("載入源檔 (Load Source)")
        self.btn_source.setStyleSheet("""
            QToolButton { background-color: transparent; border: 1px solid #555; border-radius: 4px; }
            QToolButton:hover { background-color: #444; border-color: #777; }
            QToolButton:pressed { background-color: #222; }
        """)
        self.btn_source.clicked.connect(self.add_files) 
        source_layout.addWidget(self.btn_source)
        
        # 2. History Dropdown Button (Matching btn_hist_out style)
        self.btn_hist_source = QToolButton()
        self.btn_hist_source.setText("▼")
        self.btn_hist_source.setPopupMode(QToolButton.InstantPopup)
        self.btn_hist_source.setFixedSize(25, 45)
        self.btn_hist_source.setStyleSheet("""
             QToolButton { background: transparent; border: 1px solid #555; border-top-right-radius: 4px; border-bottom-right-radius: 4px; color: #aaa; }
             QToolButton:hover { background: #444; color: white; }
             QToolButton::menu-indicator { image: none; }
        """)
        
        # Init Menu
        self.menu_hist_source = QMenu(self.btn_hist_source)
        self.menu_hist_source.setStyleSheet("QMenu { background-color: #333; color: white; border: 1px solid #555; } QMenu::item:selected { background-color: #0078d4; }")
        self.btn_hist_source.setMenu(self.menu_hist_source)
        source_layout.addWidget(self.btn_hist_source)
        
        self.lbl_source_path = QLabel("請載入源檔 (Please Load Source)")
        self.lbl_source_path.setWordWrap(True)
        self.lbl_source_path.setStyleSheet("""
            QLabel {
                background-color: #004a7c; 
                color: #ffffff; 
                font-size: 14px; 
                font-weight: bold; 
                padding: 8px; 
                border-radius: 4px; 
                border: 1px solid #005a9e;
            }
        """)
        source_layout.addWidget(self.lbl_source_path, 1)
        
        HEADER_STYLE = """
            QLabel {
                color: white;
                font-weight: 900;
                font-size: 16px;
                padding: 4px 8px;
                border-left: 5px solid #F0E68C;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(240, 230, 140, 40), stop:1 rgba(240, 230, 140, 0));
                margin-top: 10px;
                margin-bottom: 5px;
            }
        """
        
        lbl_source_header = QLabel("源檔路徑 (Source Path)")
        lbl_source_header.setStyleSheet(HEADER_STYLE)
        params_layout.addWidget(lbl_source_header)
        params_layout.addWidget(source_container)
        
        self.lbl_format = QLabel("Fmt: --")
        self.lbl_vcodec = QLabel("V.Codec: --")
        self.lbl_res = QLabel("Res: --")
        self.lbl_fps = QLabel("FPS: --")
        self.lbl_bitrate = QLabel("Bitrate: --")
        self.lbl_dur = QLabel("Dur: --")
        self.lbl_acodec = QLabel("A.Codec: --")
        self.lbl_ach = QLabel("A.Ch: --")
        
        for lbl in [self.lbl_format, self.lbl_vcodec, self.lbl_res, self.lbl_fps, 
                   self.lbl_bitrate, self.lbl_dur, self.lbl_acodec, self.lbl_ach]:
            lbl.setStyleSheet("color: #aaa; font-size: 11px;")
        
        # Grid Layout (Row, Col)
        meta_layout.addWidget(self.lbl_format, 0, 0)
        
        # VCodec with Fix Button
        vcodec_container = QWidget()
        vcodec_layout = QHBoxLayout(vcodec_container)
        vcodec_layout.setContentsMargins(0,0,0,0)
        vcodec_layout.setSpacing(5)
        vcodec_layout.addWidget(self.lbl_vcodec)
        
        self.btn_fix_codec = QPushButton("重新解碼 (Re-Decode)")
        self.btn_fix_codec.setCursor(Qt.PointingHandCursor)
        self.btn_fix_codec.setStyleSheet("background-color: #d32f2f; color: white; border-radius: 4px; padding: 2px 8px; font-weight: bold;")
        self.btn_fix_codec.hide()
        self.btn_fix_codec.clicked.connect(self.fix_video_codec)
        vcodec_layout.addWidget(self.btn_fix_codec)
        
        meta_layout.addWidget(vcodec_container, 0, 1)
        
        meta_layout.addWidget(self.lbl_res, 1, 0)
        meta_layout.addWidget(self.lbl_fps, 1, 1)
        meta_layout.addWidget(self.lbl_bitrate, 2, 0)
        meta_layout.addWidget(self.lbl_dur, 2, 1)
        meta_layout.addWidget(self.lbl_acodec, 3, 0)
        meta_layout.addWidget(self.lbl_ach, 3, 1)
        
        params_layout.addWidget(QLabel("影片詳細資訊 (Detail Info)"))
        params_layout.addWidget(meta_group)
        
        # Global Start Button - REMOVED from here, moved to below player
        # self.btn_start_all = QPushButton("⚡ 開始所有轉碼 (Start All)")
        # ...
        
        # Playlist Controls
        pl_ctrl = QHBoxLayout()
        lbl_pl_header = QLabel("源檔列表 (Source Files)")
        lbl_pl_header.setStyleSheet(HEADER_STYLE)
        pl_ctrl.addWidget(lbl_pl_header)
        pl_ctrl.addStretch()
        
        # Add All Button (Final Polish: Narrower, Maximized Icon, Tight Spacing)
        self.btn_add_all = QPushButton()
        self.btn_add_all.setToolTip("添加選取項，若未選取則添加全部 (Add Selected or All)")
        self.btn_add_all.setFixedSize(105, 50) # Narrowed by ~1/4 (was 140)
        self.btn_add_all.setCursor(Qt.PointingHandCursor)
        self.btn_add_all.setStyleSheet("""
            QPushButton { 
                background-color: #2e7d32; 
                border-radius: 25px; 
                border: 2px solid #388e3c;
            }
            QPushButton:hover { background-color: #388e3c; border-color: #4caf50; }
            QPushButton:pressed { background-color: #1b5e20; }
        """)
        
        btn_layout = QHBoxLayout(self.btn_add_all)
        btn_layout.setContentsMargins(10, 0, 5, 0) # Tight margins
        btn_layout.setSpacing(2) # Extremely tight spacing
        
        lbl_all = QLabel("ALL")
        lbl_all.setAttribute(Qt.WA_TransparentForMouseEvents)
        lbl_all.setStyleSheet("color: white; font-weight: 900; font-size: 22px; font-family: 'Arial Black', 'Segoe UI Black', sans-serif;")
        
        lbl_icon = QLabel()
        # Maximized icon to 48px to fill nearly the entire 50px height (leaving 1px border room)
        icon_pix = self.create_geometric_icon_plus(color="white", size=48).pixmap(48, 48)
        lbl_icon.setPixmap(icon_pix)
        lbl_icon.setAttribute(Qt.WA_TransparentForMouseEvents)
        
        btn_layout.addStretch()
        btn_layout.addWidget(lbl_all)
        btn_layout.addWidget(lbl_icon)
        btn_layout.addStretch()
        
        self.btn_add_all.clicked.connect(self.add_all_to_queue)
        pl_ctrl.addWidget(self.btn_add_all)
        
        # btn_add removed as per user request (Duplicate of Source Button)
        
        btn_clear = QToolButton()
        btn_clear.setToolTip("清除所有條目 (Clear All Entries)")
        btn_clear.setIcon(self.style().standardIcon(QStyle.SP_TrashIcon))
        btn_clear.setIconSize(QSize(24, 24))
        btn_clear.setMinimumWidth(80) # Text style maybe? Or keep icon. User said "All icons enlarge".
        # Actually user pointed to button 2 being redundant. 
        # But for 'Clear', let's keep it but maybe match style?
        # User screenshot didn't highlight Clear as 'change size', but 'icons enlarge'.
        # I'll keep Clear slightly regular or match others?
        # Let's just resize it to match the standard 45x45 if it's an icon button, or keep as is if it's special.
        # But wait, the user marked '2' (Add) as duplicate. 'Clear' is next to it.
        # I will keep btn_clear but remove btn_add.
        
        btn_clear.setFixedSize(45, 45) 
        btn_clear.setIconSize(QSize(32, 32))
        btn_clear.setStyleSheet("""
            QToolButton { background-color: transparent; border: 1px solid #555; border-radius: 4px; }
            QToolButton:hover { background-color: #d32f2f; border-color: #ff5252; }
            QToolButton:pressed { background-color: #b71c1c; }
        """)
        btn_clear.clicked.connect(self.clear_playlist)
        
        # pl_ctrl.addWidget(btn_add) # Removed
        pl_ctrl.addWidget(btn_clear)
        params_layout.addLayout(pl_ctrl)
        
        # Playlist Widget
        self.playlist = QListWidget()
        self.playlist.setSelectionMode(QAbstractItemView.MultiSelection) # [NEW] Toggle selection behavior
        self.playlist.setAlternatingRowColors(True) # [NEW] Alternate background colors
        self.playlist.setStyleSheet("""
            QListWidget { 
                background-color: #1a1a1a; 
                alternate-background-color: #2a2a2a; /* [NEW] Light grey separation */
                border: 1px solid #333; 
                border-radius: 3px; 
                color: #ddd; 
                font-size: 13px; 
            }
            QListWidget::item { 
                padding: 8px; 
                border-bottom: 1px solid #222; 
            }
            QListWidget::item:selected { 
                background-color: #2d5f9e; 
                color: white; 
                border-left: 3px solid #4A9EFF;
            }
            QListWidget::item:hover {
                background-color: rgba(255, 255, 255, 0.05);
            }
        """)
        self.playlist.itemPressed.connect(self.on_playlist_item_clicked)
        params_layout.addWidget(self.playlist, 1)
        
        # --- Target Settings ---
        target_header_layout = QHBoxLayout()
        lbl_target = QLabel("目標格式設置 (Target Settings)")
        lbl_target.setStyleSheet(HEADER_STYLE)
        target_header_layout.addWidget(lbl_target)
        
        btn_copy_source = QPushButton("複製源檔參數 (Copy Source)")
        btn_copy_source.setMinimumWidth(200) # Flexible width
        btn_copy_source.setCursor(Qt.PointingHandCursor)
        btn_copy_source.setStyleSheet("""
            QPushButton { background-color: #333; color: #888; border: 1px solid #555; border-radius: 3px; font-size: 11px; }
            QPushButton:hover { background-color: #444; color: #F0E68C; border: 1px solid #777; }
        """)
        btn_copy_source.clicked.connect(self.apply_source_settings)
        target_header_layout.addWidget(btn_copy_source)
        target_header_layout.addStretch()
        
        params_layout.addLayout(target_header_layout)
        
        target_group = QFrame()
        target_group.setStyleSheet("""
            QFrame { background-color: #262626; border-radius: 3px; border: 1px solid #333; }
            QComboBox, QLineEdit, QDoubleSpinBox, QSpinBox {
                background-color: #333;
                color: #F0E68C;
                border: 1px solid #555;
                font-weight: bold;
                padding: 2px;
                font-size: 14px;
                min-height: 25px;
            }
            /* Clean Large Buttons style */
            QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
                width: 30px; /* Reliable width */
                background-color: #444;
                border: 1px solid #555;
            }
            QSpinBox::up-button:hover, QSpinBox::down-button:hover, QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
                background-color: #555;
            }
            QSpinBox::up-button:pressed, QSpinBox::down-button:pressed, QDoubleSpinBox::up-button:pressed, QDoubleSpinBox::down-button:pressed {
                background-color: #0078d4;
            }
            QComboBox QAbstractItemView {
                background-color: #333;
                color: #F0E68C;
                selection-background-color: #555;
            }
        """)
        target_layout = QVBoxLayout(target_group)
        target_layout.setContentsMargins(5, 5, 5, 5)
        
        # Row 0: Presets
        preset_layout = QHBoxLayout()
        preset_layout.addWidget(QLabel("預設:"))
        self.combo_presets = QComboBox()
        self.combo_presets.addItem("Custom / Unsaved")
        self.combo_presets.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.combo_presets.currentTextChanged.connect(self.load_preset)
        self.combo_presets.setMinimumWidth(350)
        preset_layout.addWidget(self.combo_presets, 1)
        
        
        
        btn_save_preset = QToolButton()
        btn_save_preset.setToolTip("儲存 (Save)")
        btn_save_preset.setIcon(self.style().standardIcon(QStyle.SP_DialogSaveButton))
        btn_save_preset.setFixedSize(45, 45)
        btn_save_preset.setIconSize(QSize(32, 32))
        btn_save_preset.setStyleSheet("""
            QToolButton { background-color: transparent; border: 1px solid #555; border-radius: 4px; }
            QToolButton:hover { background-color: #444; border-color: #F0E68C; }
            QToolButton:pressed { background-color: #222; }
        """)
        btn_save_preset.clicked.connect(self.save_current_as_preset)
        preset_layout.addWidget(btn_save_preset)
        
        btn_del_preset = QToolButton()
        btn_del_preset.setToolTip("刪除 (Delete)")
        btn_del_preset.setIcon(self.style().standardIcon(QStyle.SP_TrashIcon))
        btn_del_preset.setFixedSize(45, 45)
        btn_del_preset.setIconSize(QSize(32, 32))
        btn_del_preset.setStyleSheet("""
            QToolButton { background-color: transparent; border: 1px solid #555; border-radius: 4px; }
            QToolButton:hover { background-color: #d32f2f; border-color: #ff5252; }
            QToolButton:pressed { background-color: #b71c1c; }
        """)
        btn_del_preset.clicked.connect(self.delete_current_preset)
        preset_layout.addWidget(btn_del_preset)
        
        target_layout.addLayout(preset_layout)
        
        # Row 1: Container & Codec
        t_row1 = QHBoxLayout()
        t_row1.addWidget(QLabel("封裝:"))
        self.combo_container = QComboBox()
        self.combo_container.addItems(["mp4", "mov", "mkv", "ts", "mxf"])
        self.combo_container.currentTextChanged.connect(self.save_settings)
        t_row1.addWidget(self.combo_container, 1)
        
        t_row1.addWidget(QLabel(" 編碼:"))
        self.combo_vcodec = QComboBox()
        # "hqx" usually refers to Grass Valley HQX. ffmpeg encoder might be 'hqx' or 'canopus_hqx'. 
        # Checking: ffmpeg -h encoder=hqx usually works if built-in. 
        self.combo_vcodec.addItems(["h264", "hevc", "prores", "hqx"]) 
        self.combo_vcodec.currentTextChanged.connect(self.save_settings)
        t_row1.addWidget(self.combo_vcodec, 1) # Fixed duplicate
        
        t_row1.addWidget(QLabel(" 音訊:"))
        self.combo_acode = QComboBox()
        self.combo_acode.addItems(["aac", "pcm_s16le", "mp3", "copy"]) 
        self.combo_acode.currentTextChanged.connect(self.save_settings)
        t_row1.addWidget(self.combo_acode, 1)
        
        t_row1.addLayout(t_row1)
        
        # Row 1.5: FPS & TV System
        t_row15 = QHBoxLayout()
        t_row15.addWidget(QLabel("幀率:"))
        self.combo_fps = QComboBox()
        self.combo_fps.addItems(["", "23.976", "24", "25", "29.97", "30", "50", "59.94", "60"])
        self.combo_fps.setEditable(True)
        self.combo_fps.setFixedWidth(80)
        self.combo_fps.currentTextChanged.connect(self.save_settings)
        t_row15.addWidget(self.combo_fps)
        
        t_row15.addWidget(QLabel(" 制式:"))
        self.combo_sys = QComboBox()
        self.combo_sys.addItems(["Auto", "NTSC", "PAL"])
        self.combo_sys.setFixedWidth(70)
        t_row15.addWidget(self.combo_sys)
        t_row15.addStretch()
        
        target_layout.addLayout(t_row15)

        target_layout.addLayout(t_row1)
        
        self.combo_container.currentTextChanged.connect(self.update_audio_defaults)
        
        # Row 2: Bitrate & Audio Gain
        
        # Row 2: Bitrate & Audio Gain
        t_row2 = QHBoxLayout()
        t_row2.addWidget(QLabel("碼率:"))
        self.edit_bitrate = QLineEdit("5000") # Moved here
        self.edit_bitrate.setFixedWidth(60)
        self.edit_bitrate.textChanged.connect(self.save_settings)
        t_row2.addWidget(self.edit_bitrate)
        t_row2.addWidget(QLabel("k"))
        
        t_row2.addWidget(QLabel(" 增益:"))
        
        # Auto Gain Button
        self.btn_auto_gain = QToolButton()
        self.btn_auto_gain.setText("Auto")
        self.btn_auto_gain.setCheckable(True)
        self.btn_auto_gain.setFixedSize(50, 30)
        self.btn_auto_gain.setStyleSheet("""
            QToolButton { background-color: #444; border: 1px solid #555; border-radius: 2px; font-weight: bold; color: #ccc; }
            QToolButton:checked { background-color: #0078d4; color: white; border: 1px solid #005a9e; }
            QToolButton:hover { border: 1px solid #F0E68C; }
        """)
        self.btn_auto_gain.toggled.connect(self.on_auto_gain_toggled)
        t_row2.addWidget(self.btn_auto_gain)
        
        # Manual SpinBox Implementation [ - ][ 0.0 ][ + ]
        self.btn_gain_less = QToolButton()
        self.btn_gain_less.setText("−")
        self.btn_gain_less.setFixedSize(30, 30) 
        self.btn_gain_less.setStyleSheet("QToolButton { font-size: 20px; font-weight: bold; background-color: #444; color: #F0E68C; border: 1px solid #555; border-radius: 2px; } QToolButton:hover { background-color: #555; }")
        
        self.spin_gain = QDoubleSpinBox()
        self.spin_gain.setRange(-6.0, 6.0) # Professional Scale: -6 to +6
        self.spin_gain.setSingleStep(0.5)  # Professional Step: 0.5dB
        self.spin_gain.setSuffix(" dB")
        self.spin_gain.setValue(0.0)
        self.spin_gain.setButtonSymbols(QAbstractSpinBox.NoButtons) # Hide default arrows
        self.spin_gain.setAlignment(Qt.AlignCenter)
        self.spin_gain.setMinimumWidth(70)
        self.spin_gain.setFixedHeight(30)
        self.spin_gain.valueChanged.connect(self.save_settings)
        
        self.btn_gain_more = QToolButton()
        self.btn_gain_more.setText("+")
        self.btn_gain_more.setFixedSize(30, 30)
        self.btn_gain_more.setStyleSheet("QToolButton { font-size: 20px; font-weight: bold; background-color: #444; color: #F0E68C; border: 1px solid #555; border-radius: 2px; } QToolButton:hover { background-color: #555; }")
        
        # Connect manual buttons
        self.btn_gain_less.clicked.connect(lambda: self.spin_gain.stepBy(-1))
        self.btn_gain_more.clicked.connect(lambda: self.spin_gain.stepBy(1))
        
        t_row2.addWidget(self.btn_gain_less)
        t_row2.addWidget(self.spin_gain)
        t_row2.addWidget(self.btn_gain_more)
        target_layout.addLayout(t_row2)
        
        params_layout.addWidget(target_group)

        # Output Settings
        lbl_out_header = QLabel("輸出設定 (Output)")
        lbl_out_header.setStyleSheet(HEADER_STYLE)
        params_layout.addWidget(lbl_out_header)
        
        out_layout = QHBoxLayout()
        out_layout.setSpacing(5)
        
        btn_out = QToolButton()
        btn_out.setIcon(self.style().standardIcon(QStyle.SP_DirIcon))
        btn_out.setFixedSize(45, 45) 
        btn_out.setIconSize(QSize(32, 32))
        btn_out.setToolTip("選擇輸出資料夾 (Select Output Folder)")
        btn_out.setStyleSheet("""
            QToolButton { background-color: transparent; border: 1px solid #555; border-radius: 4px; }
            QToolButton:hover { background-color: #444; border-color: #777; }
            QToolButton:pressed { background-color: #222; }
        """)
        btn_out.clicked.connect(self.select_output_dir)
        out_layout.addWidget(btn_out)
        
        # History Menu (Output)
        self.btn_hist_out = QToolButton()
        self.btn_hist_out.setText("▼")
        self.btn_hist_out.setPopupMode(QToolButton.InstantPopup)
        self.btn_hist_out.setFixedSize(25, 45)
        self.btn_hist_out.setStyleSheet("""
             QToolButton { background: transparent; border: 1px solid #555; border-top-right-radius: 4px; border-bottom-right-radius: 4px; color: #aaa; }
             QToolButton:hover { background: #444; color: white; }
             QToolButton::menu-indicator { image: none; }
        """)
        self.menu_hist_out = QMenu(self.btn_hist_out)
        self.menu_hist_out.setStyleSheet("QMenu { background-color: #333; color: white; border: 1px solid #555; } QMenu::item:selected { background-color: #0078d4; }")
        self.btn_hist_out.setMenu(self.menu_hist_out)
        out_layout.addWidget(self.btn_hist_out)
        
        self.lbl_output_path = QLabel("預設 (Default)")
        self.lbl_output_path.setWordWrap(True)
        # Highlight Style
        self.lbl_output_path.setStyleSheet("""
            QLabel {
                background-color: #1b5e20; 
                border: 1px solid #2e7d32; 
                border-radius: 4px; 
                padding: 6px; 
                color: #e8f5e9; 
                font-weight: bold; 
                font-size: 14px; 
                font-family: 'Segoe UI', sans-serif;
            }
        """)
        out_layout.addWidget(self.lbl_output_path, 1) # Add to Layout with stretch
        
        params_layout.addLayout(out_layout)
        
        # Naming & Seq
        name_layout = QHBoxLayout()
        self.chk_rename = QCheckBox("改名:")
        self.chk_rename.setStyleSheet("color: #ccc;") 
        self.chk_rename.toggled.connect(self.save_settings)
        name_layout.addWidget(self.chk_rename)
        
        self.edit_base_name = QLineEdit()
        self.edit_base_name.setPlaceholderText("主檔名...")
        self.edit_base_name.textChanged.connect(self.save_settings)
        name_layout.addWidget(self.edit_base_name, 1)
        
        name_layout.addWidget(QLabel("序:"))
        self.spin_seq = QSpinBox()
        self.spin_seq.setRange(1, 999)
        self.spin_seq.setValue(1)
        self.spin_seq.valueChanged.connect(self.save_settings)
        name_layout.addWidget(self.spin_seq)
        
        params_layout.addLayout(name_layout)
        
        # Actions - REMOVED per user request
        # self.btn_add_task = QPushButton("加入任務 (Queue)")
        # ...
        # self.btn_execute = QPushButton("開始轉碼 (START)")
        # ...
        
        # Settings Path Info
        settings_path_lbl = QLabel(f"設定存於: {os.path.abspath('settings.json')}")
        settings_path_lbl.setStyleSheet("color: #555; font-size: 10px; margin-top: 20px;")
        params_layout.addWidget(settings_path_lbl)
        
        params_layout.addStretch()
        
        upper_layout.addWidget(params_panel)
        content_splitter.addWidget(upper_widget)
        
        # Lower: Task Monitor
        monitor_widget = QFrame()
        mon_layout = QVBoxLayout(monitor_widget)
        mon_layout.setContentsMargins(5,5,5,5)
        
        # Header Row
        mon_header = QHBoxLayout()
        mon_header.addWidget(QLabel("任務隊列"))
        mon_header.addStretch()
        
        # Clear Button
        self.btn_clear_list = QToolButton()
        self.btn_clear_list.setText(" 清空列表 (Clear)")
        self.btn_clear_list.setIcon(self.style().standardIcon(QStyle.SP_TrashIcon))
        self.btn_clear_list.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        # self.btn_clear_list.setFixedSize(120, 24) # Removed fixed size to allow expansion
        self.btn_clear_list.setMinimumHeight(24)
        self.btn_clear_list.setCursor(Qt.PointingHandCursor)
        self.btn_clear_list.setStyleSheet("""
            QToolButton { background-color: transparent; color: #aaa; border: 1px solid #555; border-radius: 4px; font-size: 11px; padding-left: 5px; padding-right: 5px; }
            QToolButton:hover { background-color: #d32f2f; color: white; border-color: #ff5252; }
            QToolButton:pressed { background-color: #b71c1c; }
        """)
        self.btn_clear_list.clicked.connect(self.clear_task_list)
        mon_header.addWidget(self.btn_clear_list)
        
        mon_layout.addLayout(mon_header)

        # [NEW] Dashboard Header
        db_header = QFrame()
        db_header.setFixedHeight(30)
        db_header.setStyleSheet("background-color: #333; border-bottom: 1px solid #444;")
        dbh_layout = QHBoxLayout(db_header)
        dbh_layout.setContentsMargins(15, 0, 5, 0)
        dbh_layout.setSpacing(10)
        
        col_lbls = [
            ("任務名稱", 240), ("狀態", 140), ("格式資訊", 260), 
            ("時間範圍", 220), ("效能", 140), ("來源", 120), 
            ("節點", 60), ("進度", 100), ("完成時間", 140),
            ("目標路徑", 200)
        ]
        for txt, w in col_lbls:
            l = QLabel(txt)
            l.setFixedWidth(w)
            l.setStyleSheet("color: #888; font-size: 11px; font-weight: bold;")
            dbh_layout.addWidget(l)
        dbh_layout.addStretch()
        mon_layout.addWidget(db_header)
        self.task_list.setAlternatingRowColors(True)
        self.task_list.setStyleSheet("""
            QListWidget {
                background-color: #2b2b2b;
                border: 1px solid #333;
                border-radius: 4px;
            }
            QListWidget::item {
                border-bottom: 1px solid #333;
            }
            QListWidget::item:selected {
                background-color: #444; 
            }
        """)
        self.task_list.itemClicked.connect(self.on_task_item_clicked)
        self.task_list.itemDoubleClicked.connect(self.on_task_item_double_clicked)
        mon_layout.addWidget(self.task_list)
        content_splitter.addWidget(monitor_widget)
        
        trans_layout.addWidget(content_splitter)

    def add_files(self):
        start_dir = getattr(self, 'last_source_dir', "")
        files, _ = QFileDialog.getOpenFileNames(self, "選擇影音檔案", start_dir)
        
        if not files: return
        
        self.last_source_dir = os.path.normpath(os.path.dirname(files[0]))
        # [FAITHFUL RECORDING] Add directory to history immediately
        print(f"DEBUG: add_files -> Recording history: {self.last_source_dir}")
        self.settings.add_source_history(self.last_source_dir)
        self.update_history_menus()
        self.save_settings()

        added_first = False
        
        for f in files:
            path = os.path.normpath(f)
            # Check duplicates
            exists = False
            for i in range(self.playlist.count()):
                if self.playlist.item(i).data(Qt.UserRole) == path:
                    exists = True
                    break
            
            if not exists:
                base = os.path.basename(path)
                item = QListWidgetItem(base)
                item.setData(Qt.UserRole, path)
                item.setToolTip(path)
                self.playlist.addItem(item)
                
                if not added_first:
                    self.playlist.setCurrentItem(item)
                    self.on_playlist_item_clicked(item)
                    added_first = True
        
        self.save_settings()

    def select_output_dir(self):
        start_dir = getattr(self, 'output_dir', "")
        d = QFileDialog.getExistingDirectory(self, "選擇輸出資料夾 (Select Output Folder)", start_dir)
        if d:
            self.output_dir = os.path.normpath(d)
            self.lbl_output_path.setText(self.output_dir)
            # Add to Output History
            self.settings.add_output_history(self.output_dir)
            self.update_history_menus()
            self.save_settings()

    def clear_playlist(self):
        self.playlist.clear()
        self.lbl_res.setText("Res: --")
        self.lbl_vcodec.setText("V.Codec: --") # Corrected from lbl_codec
        self.lbl_fps.setText("FPS: --")
        self.lbl_dur.setText("Dur: --")
        self.current_source = ""
        

    def on_playlist_item_clicked(self, item):
        path = item.data(Qt.UserRole)
        if not path or not os.path.exists(path): return
        
        # [Refactored] Use Shared Smart Check
        path, meta = self.check_smart_remux(path)
        print(f"DEBUG: Playlist Clicked -> {path} (Remuxed? {'yes' if '_remux.ts' in path.lower() else 'no'})")

        self.current_source = path
        self.last_source_dir = os.path.dirname(path)
        # FORCE add to history on click to ensure "faithful recording"
        self.settings.add_source_history(self.last_source_dir)
        self.update_history_menus()

        # History position check
        start_pos = self.settings.get_history_position(path)
        
        # Load Video
        self.player.load_video(path, start_pos=start_pos if start_pos else 0)
        
        # Update Metadata
        if 'meta' in locals() and meta:
             self.update_metadata_panel(path, preloaded_meta=meta)
        else:
             self.update_metadata_panel(path)
        
        # Auto-set output if needed
        if not getattr(self, 'output_dir', None):
             src_dir = os.path.dirname(path)
             if os.access(src_dir, os.W_OK):
                 self.output_dir = src_dir
                 self.lbl_output_path.setText(src_dir)

    def save_current_as_preset(self):
        name, ok = QInputDialog.getText(self, "儲存預設 (Save Preset)", "請輸入預設名稱:")
        if ok and name:
            presets = self.settings.get("presets", self.default_settings["presets"])
            presets[name] = {
                "container": self.combo_container.currentText(),
                "vcodec": self.combo_vcodec.currentText(),
                "bitrate": self.edit_bitrate.text(),
                "gain": self.spin_gain.value(),
                "fps": self.combo_fps.currentText(),
                "tv_system": self.combo_sys.currentText()
            }
            self.settings.set("presets", presets)
            
            # Update Combo
            curr = self.combo_presets.currentText()
            self.combo_presets.blockSignals(True)
            self.combo_presets.clear()
            self.combo_presets.addItem("Custom / Unsaved")
            self.combo_presets.addItems(sorted(presets.keys()))
            self.combo_presets.setCurrentText(name)
            self.combo_presets.blockSignals(False)
            
    def delete_current_preset(self):
        curr = self.combo_presets.currentText()
        if curr == "Custom / Unsaved": return
        
        curr = self.combo_presets.currentText()
        if curr == "Custom / Unsaved": return
        
        msg = QMessageBox()
        msg.setWindowTitle("刪除預設")
        msg.setText("已運行")
        msg.setInformativeText("要重新開啟程式？")
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg.setDefaultButton(QMessageBox.No)
        msg.setIcon(QMessageBox.Question)
        msg.setStyleSheet("QLabel { color: black; } QPushButton { color: black; }") # Force visible text
        
        if msg.exec() == QMessageBox.Yes:
            presets = self.settings.get("presets", self.default_settings["presets"])
            if curr in presets:
                del presets[curr]
                self.settings.set("presets", presets)
                
                self.combo_presets.blockSignals(True)
                self.combo_presets.clear()
                self.combo_presets.addItem("Custom / Unsaved")
                self.combo_presets.addItems(sorted(presets.keys()))
                self.combo_presets.setCurrentIndex(0)
                self.combo_presets.blockSignals(False)

    def load_preset(self, name):
        if name == "Custom / Unsaved": return
        
        presets = self.settings.get("presets", self.default_settings["presets"])
        data = presets.get(name)
        if data:
            self.loading = True # Block save_settings loops
            self.combo_container.setCurrentText(data.get("container", "mp4"))
            self.combo_vcodec.setCurrentText(data.get("vcodec", "h264"))
            
            # Strip 'k' since UI has a label for it
            raw_bitrate = str(data.get("bitrate", "5000")).lower().replace("k", "")
            self.edit_bitrate.setText(raw_bitrate)
            
            self.spin_gain.setValue(float(data.get("gain", 0.0)))
            
            # Store hidden params
            self.current_preset_extra = {
                "resolution": data.get("resolution"),
                "fps": data.get("fps"),
                "audio_ch": data.get("audio_ch"),
                "tv_system": data.get("tv_system")
            }
            if hasattr(self, 'combo_fps') and data.get("fps"):
                self.combo_fps.setCurrentText(data.get("fps"))
            if hasattr(self, 'combo_sys') and data.get("tv_system"):
                self.combo_sys.setCurrentText(data.get("tv_system"))
            
            self.loading = False
            self.save_settings() # Save explicit state

        # Install Event Filter for Global Keys (ESC)
        self.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                # Global ESC Handler
                print("DEBUG: Global ESC Caught!")
                if hasattr(self, 'player'):
                    # Call player's ESC logic specifically
                    print("DEBUG: Forwarding to Player...")
                    self.player.keyPressEvent(event)
                    return True # Consumed
        return super().eventFilter(obj, event)

    def update_metadata_panel(self, path, preloaded_meta=None):
         # Update Source Label
         self.lbl_source_path.setText(f"{path}")
         
         if preloaded_meta:
             meta = preloaded_meta
         else:
             from core.metadata import get_video_metadata
             meta = get_video_metadata(path)
         if meta:
             self.lbl_format.setText(f"Fmt: {meta.get('format', '--')}")
             self.lbl_vcodec.setText(f"V.Codec: {meta.get('codec', '--')}")
             self.lbl_res.setText(f"Res: {meta['width']}x{meta['height']}")
             self.lbl_fps.setText(f"FPS: {meta['fps']}")
             self.lbl_bitrate.setText(f"Bitrate: {meta.get('bitrate', '--')}")
             
             # Restore Audio Labels with nice formatting
             a_codec = meta.get('audio_codec', '--').lower()
             if a_codec == 'mp2': a_codec = 'MPEG Audio'
             elif a_codec == 'aac': a_codec = 'AAC'
             self.lbl_acodec.setText(f"A.Codec: {a_codec}")
             
             a_ch = meta.get('audio_channels', '--')
             self.lbl_ach.setText(f"A.Ch: {a_ch} Ch" if a_ch != '--' else "A.Ch: --")
             
             dur_s = meta['duration']
             m, s = divmod(int(dur_s), 60)
             h, m = divmod(m, 60)
             self.lbl_dur.setText(f"Dur: {h:02d}:{m:02d}:{s:02d}")
             
             # [Enhanced Status Detection]
             codec = meta.get('codec', '').lower()
             codec_tag = meta.get('codec_tag', '')
             path_lower = path.lower()
             
             # Final Display Fix: If codec name is unknown, try tag
             display_codec = meta.get('codec', '--')
             if display_codec.lower() == "unknown" and codec_tag:
                 display_codec = f"tag:{codec_tag}"
             
             if "_remux.ts" in path_lower:
                 # Blue "Already Re-decoded" State
                 self.lbl_vcodec.setText(f"V.Codec: {display_codec}")
                 self.lbl_vcodec.setStyleSheet("color: #0078d4; font-weight: bold; font-size: 11px;")
                 self.btn_fix_codec.setText(f"已重新解碼 ({display_codec.upper()})")
                 self.btn_fix_codec.setStyleSheet("background-color: #0078d4; color: white; border-radius: 4px; padding: 2px 8px; font-weight: bold;")
                 self.btn_fix_codec.show()
             elif 'unknown' in codec or codec == '':
                 # Red "Re-Decode" State
                 self.lbl_vcodec.setStyleSheet("color: red; font-weight: bold; font-size: 11px;")
                 self.btn_fix_codec.setText("重新解碼 (Re-Decode)")
                 self.btn_fix_codec.setStyleSheet("background-color: #d32f2f; color: white; border-radius: 4px; padding: 2px 8px; font-weight: bold;")
                 self.btn_fix_codec.show()
             else:
                 # Normal State
                 self.lbl_vcodec.setText(f"V.Codec: {display_codec}")
                 self.lbl_vcodec.setStyleSheet("color: #aaa; font-size: 11px;")
                 self.btn_fix_codec.hide()
                 
         else:
             self.lbl_res.setText("Res: ??")
             self.btn_fix_codec.hide()

    def select_output_dir(self):
        dir_path = QFileDialog.getExistingDirectory(self, "選擇輸出目錄")
        if dir_path:
            self.output_dir = dir_path
            self.lbl_output_path.setText(dir_path)
            self.settings.set("output_dir", dir_path)

    def format_ms(self, ms):
        if ms is None: return None
        total_seconds = ms / 1000.0
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        seconds = total_seconds % 60
        return "%02d:%02d:%06.3f" % (hours, minutes, seconds)
        
    def on_auto_gain_toggled(self, checked):
        # Enable/Disable Manual Controls
        self.spin_gain.setEnabled(not checked)
        self.btn_gain_less.setEnabled(not checked)
        self.btn_gain_more.setEnabled(not checked)
        
        # Update stylesheet to reflect disabled state if needed (Qt usually handles this, but custom style might need tweaks)
        if checked:
            self.spin_gain.setStyleSheet("background-color: #222; color: #555;")
        else:
            self.spin_gain.setStyleSheet("") # Revert to default stylesheet defined in parent
            
        self.save_settings()
        self.load_pending_tasks() # Restore tasks

    def apply_source_settings(self):
        if not hasattr(self, 'current_source') or not self.current_source:
             QMessageBox.warning(self, "提示", "請先載入源檔 (Please load a source file first)")
             return
            
        from core.metadata import get_video_metadata
        meta = get_video_metadata(self.current_source)
        print(f"DEBUG: Metadata: {meta}") # Audit Log
        
        if not meta: 
            self.statusBar().showMessage("無法讀取源檔資訊 (Failed to read metadata)", 3000)
            return
        
        # 1. Container
        # Get detected format(s) from FFmpeg
        # Format string can be multiple like "mov,mp4,m4a"
        fmt_str = meta.get('format', '').lower()
        detected_formats = fmt_str.split(',')
        
        # Get file extension
        file_ext = os.path.splitext(self.current_source)[1].lower().replace('.', '')
        
        # Normalization Helper
        def normalize_fmt(f):
            if f == "qt": return "mov"
            if f == "mpeg": return "mpg"
            if f in ["mpegts", "m2t", "mts"]: return "ts"
            if f == "matroska": return "mkv"
            return f
            
        final_ext = "mp4" # Default fallback
        
        if not detected_formats or detected_formats == ['']:
             final_ext = normalize_fmt(file_ext)
        else:
             # Check if file extension is valid for this format
             # (matches one of the detected aliases)
             is_match = False
             for f in detected_formats:
                 if normalize_fmt(f) == normalize_fmt(file_ext):
                     is_match = True
                     break
             
             if is_match:
                 final_ext = normalize_fmt(file_ext)
             else:
                 # Mismatch (e.g. mpegts in .mp4) -> Trust FFmpeg
                 final_ext = normalize_fmt(detected_formats[0])

        ext = final_ext
        
        # Add if missing
        found_ext = False
        for i in range(self.combo_container.count()):
            if self.combo_container.itemText(i) == ext:
                self.combo_container.setCurrentIndex(i)
                found_ext = True
                break
        
        if not found_ext:
            self.combo_container.addItem(ext)
            self.combo_container.setCurrentText(ext)

        # 2. Codec
        codec = meta.get('codec', '').lower()
        
        has_codec = False 
        # Search Existing
        for i in range(self.combo_vcodec.count()):
            item_text = self.combo_vcodec.itemText(i)
            # Fuzzy Logic
            if item_text == codec:
                self.combo_vcodec.setCurrentIndex(i)
                has_codec = True
                break
            elif codec == 'h264' and 'h264' in item_text: # h264 matches h264_nvenc
                self.combo_vcodec.setCurrentIndex(i)
                has_codec = True
                break
                
        if not has_codec:
            # Add dynamic codec
            self.combo_vcodec.addItem(codec)
            self.combo_vcodec.setCurrentText(codec)

        # 3. Bitrate (Now getting raw Int bps)
        br_bps = meta.get('bitrate', 0)
        try:
            br_bps = int(br_bps)
        except:
            br_bps = 0
            
        final_kbps = 0
        if br_bps > 0:
            final_kbps = int(br_bps / 1000)
            self.edit_bitrate.setText(str(final_kbps))
        else:
            print("DEBUG: Bitrate is 0 or invalid.")
            
        # 4. FPS & TV System (New)
        fps = str(meta.get('fps', ''))
        if fps:
             self.combo_fps.setCurrentText(fps)
             # Heuristic for TV System
             try:
                 fps_val = float(fps)
                 if 23.9 <= fps_val <= 30.0:
                     self.combo_sys.setCurrentText("NTSC")
                 elif fps_val == 50.0 or fps_val == 25.0:
                      self.combo_sys.setCurrentText("PAL")
                 elif fps_val >= 59.9:
                      self.combo_sys.setCurrentText("NTSC")
                 else:
                      self.combo_sys.setCurrentText("Auto")
             except:
                 pass

        # 4. Reset Audio Gain
        self.btn_auto_gain.setChecked(False)
        self.spin_gain.setValue(0.0)
        
        # Status Feedback
        info = f"Container: {self.combo_container.currentText()} | Codec: {self.combo_vcodec.currentText()} | Bitrate: {final_kbps}k"
        if hasattr(self, 'statusBar'):
            self.statusBar().showMessage(f"已套用: {info}", 5000)

    def fix_video_codec(self):
        if not self.current_source: return
        
        meta = get_video_metadata(self.current_source)
        is_sles = meta and ("SLES" in meta.get("codec_tag", "") or meta.get("codec") == "unknown")
        
        msg = "檢測到未知編碼。是否嘗試轉換為標準 H.264 格式？"
        if is_sles:
            msg = "偵測到 SLES (Sony LX) 或未知編碼。\n將使用 [科學偵測 + 快速封裝] (Smart Remux) 方式處理。\n(Video Pass-through, Audio AAC, Container TS)"
            
        reply = QMessageBox.question(self, "重新解碼 (Re-Decode)", 
                                   msg,
                                   QMessageBox.Yes | QMessageBox.No)
                                   
        if reply == QMessageBox.Yes:
            from core.transcoder import Transcoder
            tx = Transcoder()
            ffmpeg_exe = tx.ffmpeg_path
            
            src = self.current_source
            # Use TS container for max compatibility with MPEG2 streams on Windows
            fixed_path = os.path.splitext(src)[0] + "_remux.ts"
            
            # Show progress dialog with readable styling
            progress = QProgressDialog("解析封裝中 (Remuxing)... 不損畫質", "取消 (Cancel)", 0, 0, self)
            progress.setWindowModality(Qt.WindowModal)
            progress.setStyleSheet("""
                QProgressDialog { background-color: white; color: black; }
                QLabel { color: black; font-weight: bold; font-size: 14px; }
                QPushButton { color: black; background-color: #ddd; border: 1px solid #aaa; padding: 5px; }
            """)
            with open("transcode_fix.log", "w") as log:
                 log.write(f"Remuxing: {src} -> {fixed_path}\nUsing: {ffmpeg_exe}\n")
            progress.show()
            
            # Scientific Method: Increase Probe Size & Analyze Duration
            # And use Stream Copy (No Re-encode)
            cmd = [
                ffmpeg_exe, "-y", 
                "-probesize", "100M", "-analyzeduration", "100M", # Deep Scan
                "-i", src,
                "-c:v", "copy", # Pass-through video
                "-c:a", "aac", "-b:a", "256k", # Ensure audio is playable
                "-f", "mpegts", # Robust container
                fixed_path
            ]
            
            self.fix_process = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            
            while self.fix_process.poll() is None:
                QApplication.processEvents()
                if progress.wasCanceled():
                    self.fix_process.terminate()
                    return
                time.sleep(0.1)
                
            progress.close()
            
            if self.fix_process.returncode == 0 and os.path.exists(fixed_path):
                # Auto-Load and Play
                # Treat as new source (is_result=False) to fully replace the broken one
                self.current_source = fixed_path
                self.update_metadata_panel(fixed_path) # Refresh UI with new file info
                self.player.load_video(fixed_path) 
                
                # Force Play
                if hasattr(self.player, 'media_player'):
                     self.player.media_player.play()
                     
                QMessageBox.information(self, "完成", "重新解碼完成！已直接播放。\n(Re-decode Complete! Playing now.)")
            else:
                QMessageBox.critical(self, "錯誤", "重新解碼失敗。請檢查日誌。\n(Failed to re-decode.)")

    def clean_up_after_task(self):
        self.current_running_task = None
        # self.current_worker = None # Removed
        self.process_next_task()

    def remove_task_by_widget(self, widget_to_remove):
        print("DEBUG: Removing Task...")
        
        # 1. Check if it's a running task
        if self.is_processing:
             # Check if this widget has an active worker
             if widget_to_remove in self.workers:
                  print("DEBUG: Killing running task.")
                  worker = self.workers[widget_to_remove]
                  # CRITICAL: Disconnect all signals to prevent callback to deleted widget
                  try:
                      worker.progress_signal.disconnect()
                      worker.finished_signal.disconnect()
                      worker.finished.disconnect()
                  except:
                      pass
                  widget_to_remove.lbl_status.setText("Cancelled")
                  # Fix for "Destroyed while thread is still running"
                  if worker.isRunning():
                        worker.terminate()
                        worker.wait(100) # Give it time to die
                  worker.deleteLater()
                  del self.workers[widget_to_remove]
             elif getattr(self, 'current_running_task', None) and self.current_running_task.get("widget") == widget_to_remove:
                   # Fallback if somehow not in workers dict but marked as active
                   self.clean_up_after_task()
                 
        # 2. Check Pending Queue
        for i, task in enumerate(self.pending_tasks):
            if task.get("widget") == widget_to_remove:
                print(f"DEBUG: Removed pending task index {i}")
                self.pending_tasks.pop(i)
                break
                
        # 3. Remove from UI List
        for i in range(self.task_list.count()):
            item = self.task_list.item(i)
            if self.task_list.itemWidget(item) == widget_to_remove:
                self.task_list.takeItem(i)
                print(f"DEBUG: Removed UI item at row {i}")
                break
                
        # 4. Check Queue State
        if not self.is_processing and not self.pending_tasks:
             self.btn_start_all.setEnabled(True) # Corrected from btn_execute

    def clear_task_list(self):
        # Only clear "Done" tasks as per user request
        # Do NOT clear pending_tasks automatically
        
        count = self.task_list.count()
        for i in range(count - 1, -1, -1):
            item = self.task_list.item(i)
            widget = self.task_list.itemWidget(item)
            
            if not widget:
                continue
                
            # Only remove if status is "Done"
            if widget.lbl_status.text().strip() == "Done":
                self.task_list.takeItem(i)
        
        # If nothing running, ensure button state
        if not self.is_processing:
            if self.task_list.count() == 0:
                 self.btn_start_all.setEnabled(True) # Ready
            else:
                 # Check if we have pending tasks to re-enable? 
                 # Actually simply check queue state
                 self.btn_start_all.setEnabled(bool(self.pending_tasks))

    def add_task_to_queue(self, source_path=None, full_duration=False, source_type="Manual", worker_id="-", preset_name=None):
        print(f"DEBUG: add_task_to_queue triggered. Source: {source_type}")
        target_source = source_path if source_path else self.current_source
        
        if not target_source:
             if source_type == "Manual":
                QMessageBox.warning(self, "無來源 (No Source)", "請先載入或播放一個影片檔案 (Please load a video first)")
             return
            
        # Capture current naming
        if self.chk_rename.isChecked() and self.edit_base_name.text().strip() and source_type == "Manual":
            base_name = self.edit_base_name.text().strip()
        else:
            base_name = os.path.splitext(os.path.basename(target_source))[0]
            
        final_base = f"{base_name}_{self.spin_seq.value():03d}"
        
        # Determine in/out
        in_p = self.player.in_point if (not source_path or source_path == self.current_source) else 0
        out_p = self.player.out_point if (not source_path or source_path == self.current_source) else 0
        
        # New: Use Global Worker Registry to prevent GC
        if not hasattr(self, 'workers'): self.workers = {}
        
        if full_duration:
            in_p = 0
            out_p = 0

        # Ensure integers
        in_p = int(in_p) if in_p is not None else 0
        out_p = int(out_p) if out_p is not None else 0

        # Prepare params
        task = {
            "source": target_source,
            "in_point": in_p,
            "out_point": out_p,
            "base_name": final_base,
            "output_dir": getattr(self, 'output_dir', os.path.dirname(target_source)),
            "sequence": self.spin_seq.value(),
            "bitrate": self.edit_bitrate.text(),
            "container": self.combo_container.currentText(),
            "vcodec": self.combo_vcodec.currentText(),
            "audio_gain": 'auto' if self.btn_auto_gain.isChecked() else self.spin_gain.value(),
            "resolution": getattr(self, 'current_preset_extra', {}).get("resolution"),
            "fps": getattr(self, 'current_preset_extra', {}).get("fps"),
            "fps_text": self.combo_fps.currentText() if hasattr(self, 'combo_fps') else None,
            "audio_ch": getattr(self, 'current_preset_extra', {}).get("audio_ch"),
            "acodec": self.combo_acode.currentText(),
            "source_type": source_type,
            "worker_id": worker_id
        }

        # Override with preset if provided (for Watch Folder)
        if preset_name and preset_name in PRESETS:
             p = PRESETS[preset_name]
             task.update({
                 "container": p.get("container", task["container"]),
                 "vcodec": p.get("vcodec", task["vcodec"]),
                 "bitrate": str(p.get("bitrate", task["bitrate"])),
                 "resolution": p.get("resolution", task["resolution"]),
                 "fps_text": p.get("fps", task["fps_text"]),
                 "acodec": p.get("acodec", task["acodec"]),
                 "audio_ch": p.get("audio_ch", task["audio_ch"])
             })
        
        # Add a visual entry in the list as "Pending"
        item = QListWidgetItem(self.task_list)
        item.setSizeHint(QSize(0, 36)) # Reduced height (was 50)
        widget = TaskProgressWidget(final_base)
        widget.set_task_data(task) # Store Data & Set Tooltip
        widget.lbl_status.setText("Pending")
        widget.removed.connect(self.remove_task_by_widget) # CONNECT SIGNAL
        widget.transcode_requested.connect(self.transcode_single_item) 
        widget.pause_requested.connect(self.pause_task)
        widget.resume_requested.connect(self.resume_task)
        widget.stop_requested.connect(self.stop_current_task)
        self.task_list.addItem(item)
        self.task_list.setItemWidget(item, widget)
        task["widget"] = widget # Store widget ref for later
        
        # Atomic Add: Only append to queue after successful setup
        self.pending_tasks.append(task)
        self.btn_start_all.setEnabled(True)
        
        # [NEW] Broadcast to Cluster (if not already from cluster)
        if source_type == "Manual" or source_type == "WatchFolder":
             self.cluster_mgr.broadcast_task(task)
        
        # Increment seq for next manually added task
        self.spin_seq.setValue(self.spin_seq.value() + 1)
        
        # [NEW] Clear dirty status after task is queued
        if hasattr(self, 'player'):
            self.player._is_dirty = False
            self.player.update_trim_labels()
            
        self.save_settings()

    def add_all_to_queue(self):
        """Add selected items in the playlist, or all if none selected."""
        items = self.playlist.selectedItems()
        if not items:
            items = [self.playlist.item(i) for i in range(self.playlist.count())]
            
        if not items:
            return
            
        original_source = getattr(self, 'current_source', None)
        
        for item in items:
            file_path = item.data(Qt.UserRole)
            if file_path and os.path.exists(file_path):
                # Add to queue with full duration by default
                self.add_task_to_queue(source_path=file_path, full_duration=True)
        
        self.current_source = original_source

    def create_geometric_icon_plus(self, color="#E0E0E0", size=32):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Draw Circle
        painter.setBrush(QColor(color))
        painter.setPen(Qt.NoPen)
        m = 4
        s = size - 2*m
        painter.drawEllipse(m, m, s, s)
        
        # Draw Plus in different color (e.g. green or dark)
        painter.setPen(QPen(QColor(46, 125, 50), 3)) # Green plus on white circle
        pm = m + s//4
        ps = s//2
        painter.drawLine(pm, m + s//2, pm + ps, m + s//2) # Horiz
        painter.drawLine(m + s//2, pm, m + s//2, pm + ps) # Vert
        
        painter.end()
        return QIcon(pixmap)

    def on_task_item_clicked(self, item):
        # We might keep this for selection or simple highlighting
        pass

    def on_task_item_double_clicked(self, item):
        widget = self.task_list.itemWidget(item)
        if widget and widget.task_data:
            task = widget.task_data
            src = task.get("source")
            if src and os.path.exists(src):
                # Load Source
                # This will restore markers if original_source matches norm_path
                self.player.load_video(src, is_result=False)
                # Explicitly set In/Out for the selected task segment
                self.player.set_in_out(task.get("in_point"), task.get("out_point"))
                # Seek to IN point immediately
                target_in = task.get("in_point", 0)
                if target_in is None: target_in = 0
                QTimer.singleShot(200, lambda: self.player.media_player.setPosition(target_in))
                
                # Feedback
                if hasattr(self, 'statusBar'):
                    self.statusBar().showMessage(f"已載入任務來源: {os.path.basename(src)}", 3000)

    def start_transcoding_queue(self):
        if not self.pending_tasks or self.is_processing:
            return
        self.is_processing = True
        self.btn_start_all.setEnabled(False)
        self.process_next_task()

    def transcode_single_item(self, widget):
        # Allow start if:
        # 1. No other task is RUNNING (active)
        # 2. Or if other tasks are PAUSED.
        
        active_running = [w for w in self.workers.values() if not w.paused]
        
        if active_running:
            QMessageBox.warning(self, "忙碌中 (Busy)", "有其他任務正在進行中。請先暫停該任務，再啟動此任務。\n(Another task is running. Please pause it first.)")
            return
            
        task = widget.task_data
        
        # Ensure task is removed from pending list if it was there
        if task in self.pending_tasks:
            self.pending_tasks.remove(task)
            
        self.is_processing = True
        self.btn_start_all.setEnabled(False)
        self.run_transcode(task, single_run=True)

    def process_next_task(self):
        if not self.pending_tasks:
            self.is_processing = False
            # Check if we should re-enable (if user added more while processing?)
            self.btn_start_all.setEnabled(False)
            return
            
        task = self.pending_tasks.pop(0)
        # Safety Check: Skip broken tasks (e.g. from previous crashes)
        if "widget" not in task:
            print("Skipping broken task (no widget)")
            self.process_next_task()
            return
            
        self.run_transcode(task)

    def run_transcode(self, task, single_run=False):
        self.current_running_task = task # Store ref for cancellation
        from core.transcoder import Transcoder
        from core.gpu_detector import get_gpu_encoders, get_best_h264_encoder
        
        gpu_info = get_gpu_encoders()
        tx = Transcoder()
        duration = tx.get_duration(task["source"])
        
        # Codec Selection
        selected_codec = task.get("vcodec", "h264")
        if selected_codec == "h264":
            vcodec = get_best_h264_encoder(gpu_info)
        elif selected_codec == "hevc":
            # Simple fallback for now
            vcodec = "hevc_nvenc" if "nvidia" in str(gpu_info).lower() else "libx265"
        elif selected_codec == "prores":
            vcodec = "prores_ks"
        elif selected_codec == "hqx":
            vcodec = "hqx" # Grass Valley HQX
        else:
            # Use the codec name directly (e.g. mpeg2video, dnxhd)
            # Fallback to libx264 only if unknown/empty
            vcodec = selected_codec if selected_codec else "libx264"
            
        transcode_params = {
            "vcodec": vcodec,
            "in_point": self.format_ms(task["in_point"]),
            "out_point": self.format_ms(task["out_point"]),
            "bitrate": str(task.get("bitrate", "5000")).rstrip('k') + "k",
            "audio_gain": task.get("audio_gain", 0.0),
            "resolution": task.get("resolution"),
            "fps": task.get("fps"),
            "audio_ch": task.get("audio_ch"),
            "acodec": task.get("acodec", "aac") # Pass Audio Codec
        }
        
        target_duration = duration
        if task["out_point"] and task["in_point"]:
            target_duration = (task["out_point"] - task["in_point"]) / 1000.0
            transcode_params["duration"] = str(target_duration) # Use duration for safer trimming
        elif task["in_point"]:
            target_duration = duration - (task["in_point"] / 1000.0)

        # Determine Extension
        ext = task.get("container", "mp4").lower()
        if not ext.startswith("."): ext = "." + ext
        
        # [FIX] Dynamic Output Directory Resolution
        # Force use of current global output setting if available,
        # to ensure "Open Folder" works as expected even if setting changed after task add.
        # But we must respect "Same as Source" if that was the intent?
        # The current UI model is: Global Setting dictates output.
        # So we should enforce current self.output_dir.
        
        current_global_out = getattr(self, 'output_dir', None)
        if current_global_out and os.path.isdir(current_global_out):
            task["output_dir"] = current_global_out
        else:
            # Fallback to source directory if no global output set
            task["output_dir"] = os.path.dirname(task["source"])
            
        output_path = os.path.join(task["output_dir"], task["base_name"] + ext)

        task["output_path_ref"] = output_path
        task["target_duration"] = target_duration
        task["start_time"] = time.time()
        
        # Prepare command with safe paths (Use native separators for UNC support)
        src_norm = os.path.normpath(task["source"])
        out_norm = os.path.normpath(output_path)
        
        cmd = tx.construct_command(src_norm, out_norm, transcode_params)
        
        # Use TranscodeWorker to ensure NO CONSOLE WINDOW on Windows
        # Prevent double creation
        if task["widget"] in self.workers:
            old_worker = self.workers.pop(task["widget"])
            old_worker.stop()
            old_worker.wait()
            old_worker.deleteLater()

        worker = TranscodeWorker(cmd, target_duration)
        worker.progress_signal.connect(lambda p, t: self.update_task_progress(task["widget"], p, t))
        worker.finished_signal.connect(lambda s, m: self.on_transcode_finished_worker(task, s, m, single_run))
        # [FIX] Do NOT connect finished->deleteLater automatically. 
        # We must manage lifecycle explicitly in on_transcode_complete to prevent race conditions (Crash at 99%)
        # worker.finished.connect(worker.deleteLater) 
        worker.start()
        
        self.workers[task["widget"]] = worker # Track worker
        task["widget"].lbl_status.setText("Transcoding...")
        task["widget"].set_started() # Ensure start time is recorded for UI

    def on_nav_clicked(self, clicked_btn):
        # Uncheck others
        for btn in [self.btn_home, self.btn_dash, self.btn_watch, self.btn_cluster, self.btn_settings]:
            btn.setChecked(btn == clicked_btn)
        
        # Switch Page
        if clicked_btn in [self.btn_home, self.btn_dash]:
             self.stack.setCurrentIndex(0) 
        elif clicked_btn == self.btn_watch:
             self.refresh_watch_list_ui()
             self.stack.setCurrentIndex(1)
        elif clicked_btn == self.btn_cluster:
             self.refresh_cluster_ui()
             self.stack.setCurrentIndex(2)
        elif clicked_btn == self.btn_settings:
             self.stack.setCurrentIndex(3)

    def save_global_settings_ui(self):
        self.settings.set("cluster_path", self.edit_cluster_path.text())
        self.save_settings()
        QMessageBox.information(self, "Success", "設定已儲存 (Settings Saved)")

    def refresh_watch_list_ui(self):
        self.watch_list.clear()
        for wf in self.settings.get("watch_folders", []):
            self.watch_list.addItem(f"{wf.get('name')} -> {wf.get('path')} [{wf.get('preset')}]")

    def refresh_cluster_ui(self):
        self.lbl_node_info.setText(f"本機辨識碼 (Local Node): {self.cluster_mgr.node_id}")
        self.node_list.clear()
        for nid, data in self.cluster_mgr._known_nodes.items():
            self.node_list.addItem(f"{nid} - {data.get('status')} (IP: {data.get('ip')})")

    def show_watch_folder_page(self): # Compatibility or removed
        self.on_nav_clicked(self.btn_watch)

    def show_cluster_page(self): # Compatibility or removed
        self.on_nav_clicked(self.btn_cluster)

    def add_watch_folder_ui(self):
        path = QFileDialog.getExistingDirectory(self, "選擇監控資料夾")
        if path:
            name, ok = QInputDialog.getText(self, "資料夾名稱", "請輸入識別名稱:")
            if ok and name:
                 presets = list(PRESETS.keys())
                 preset, ok2 = QInputDialog.getItem(self, "選擇轉碼預設", "請選擇該資料夾對應的格式:", presets, 0, False)
                 if ok2:
                     current = self.settings.get("watch_folders", [])
                     current.append({"name": name, "path": path, "preset": preset})
                     self.settings.set("watch_folders", current)
                     self.save_settings()
                     self.show_watch_folder_page()

    def show_cluster_page(self):
        """Lazy create Cluster Info page."""
        if not hasattr(self, 'node_list'):
            self.cluster_page = QWidget()
            layout = QVBoxLayout(self.cluster_page)
            
            title = QLabel("🖥 集群節點狀態 (Cluster Nodes)")
            title.setStyleSheet("font-size: 20px; font-weight: bold; color: #BB86FC;")
            layout.addWidget(title)
            
            self.node_list = QListWidget()
            layout.addWidget(self.node_list)
            
            lbl_info = QLabel(f"本機辨識碼 (Local Node): {self.cluster_mgr.node_id}")
            layout.addWidget(lbl_info)
            
            self.stack.addWidget(self.cluster_page)
            
        self.node_list.clear()
        for nid, data in self.cluster_mgr._known_nodes.items():
            self.node_list.addItem(f"{nid} - {data.get('status')} (IP: {data.get('ip')})")
            
        self.stack.setCurrentWidget(self.cluster_page)

    def update_task_progress(self, widget, percent, text):
        try:
            if not widget or getattr(widget, 'stopped', False): return # Block updates if stopped

            if percent == -1: # Indeterminate
                widget.progress.setRange(0, 0)
                widget.progress.setStyleSheet("QProgressBar { text-align: center; color: white; background-color: #222; border: 1px solid #333; border-radius: 6px; }") 
            else:
                widget.progress.setRange(0, 100)
                widget.progress.setValue(percent)
            if text:
                widget.lbl_percent.setText(text)
        except RuntimeError:
            pass # Widget likely deleted
        except Exception as e:
            debug_log(f"update_task_progress Error: {e}\n{traceback.format_exc()}")
        
        # [REMOVED] Enable Play Result at 5% - User now wants ONLY at 100% DONE


    def on_transcode_finished_worker(self, task, success, msg, single_run):
        try:
            widget = task["widget"]
            # Optimization: Worker is already finished, but we keep ref until complete logic is done
            worker = self.workers.get(widget)
            
            if success:
               self.on_transcode_complete(0, QProcess.NormalExit, task, single_run)
            else:
               if getattr(widget, 'lbl_status', None) and widget.lbl_status.text() == "Stopped":
                   pass
               else:
                   if widget:
                       widget.lbl_status.setText(f"Failed")
                       widget.progress.setValue(0)
                       widget.lbl_status.setStyleSheet("color: #ff5252;")
                   print(f"Transcode Failed: {msg}")
                   
                   # [NEW] Intelligent Auto-Retry for Automated Tasks (WatchFolder/Node)
                   source_type = task.get("source_type", "Manual")
                   if source_type != "Manual" and "Cancelled" not in msg:
                       retries = task.get("retry_count", 0)
                       if retries < 2:
                           task["retry_count"] = retries + 1
                           debug_log(f"Auto-Retry Task: {task.get('base_name')} (Attempt {retries+1}) triggered by {source_type}")
                           if widget:
                               widget.lbl_status.setText(f"Retrying ({retries+1})")
                               widget.lbl_status.setStyleSheet("color: #ffa726;")
                           QTimer.singleShot(3000, lambda: self.start_transcode_task(task, single_run))
                           return

                   suggestion, fix_params = self.analyze_error_suggestion(msg)
                    
                   if "Task Cancelled" not in msg:
                       dlg = SmartFailureDialog(msg, suggestion, fix_params, self)
                       if dlg.exec() and dlg.apply_fix:
                           # Apply fix to task params
                           for k, v in fix_params.items():
                               task[k] = v
                           # Retry
                           QTimer.singleShot(500, lambda: self.start_transcode_task(task, single_run))
            
               # CLEANUP WORKER IN FAILURE PATH
               if widget in self.workers:
                   worker = self.workers.pop(widget)
                   try:
                       worker.progress_signal.disconnect()
                       worker.finished_signal.disconnect()
                   except: pass
                   worker.deleteLater()

               if not single_run:
                   self.process_next_task()
               else:
                   self.is_processing = False
        except Exception as e:
            debug_log(f"on_transcode_finished_worker Critical Error: {e}\n{traceback.format_exc()}")
            # Critical protection: always cleanup worker on error
            if widget in self.workers:
                worker = self.workers.pop(widget)
                worker.deleteLater()
            self.is_processing = False
            self.process_next_task()
           
    def analyze_error_suggestion(self, log_output):
        """Analyzes FFmpeg log to provide actionable fixes."""
        log_lower = log_output.lower()
        
        # 1. FPS / Standard Mismatch (The user's specific request)
        if "drop frame" in log_lower and "multiples of 30000/1001" in log_lower:
            return ("偵測到 [影格率 (Frame Rate)] 與選定的制式不匹配。\n您的來源檔案可能是 NTSC (29.97fps)，但目前設定可能為 25fps 或其他不相容數值。\n\n💡 建議：自動將目標影格率修正為 29.97 以符合電視制式。", 
                    {"fps": "29.97"})

        # 2. Container/Codec Incompatibility (e.g. MXF doesn't support AAC)
        if "mxf" in log_lower and "aac" in log_lower:
            return ("MXF 封裝格式不支援 AAC 音訊編碼。\n\n💡 建議：自動將音訊改為穩定且高品質的 PCM (s16le) 編碼。", 
                    {"acodec": "pcm_s16le"})

        # 3. Write Header failures (often bitrate or codec params)
        if "could not write header" in log_lower or "incorrect codec parameters" in log_lower:
             return ("封裝參數錯誤。這通常是因為編碼組合或位元率不被該容器支援。\n\n💡 建議：切換為通用的 H.264 + MP4 組合重試。", 
                     {"container": "mp4", "vcodec": "libx264", "acodec": "aac"})

        if "permission denied" in log_lower:
             return ("輸出目錄無寫入權限，或磁碟空間不足。\n\n💡 請檢查目標資料夾權限。", None)

        if "unknown codec" in log_lower:
             return ("來源檔編碼無法識別，這常見於損壞的素材。\n\n💡 建議：使用主介面的 [重新解碼 (Re-Decode)] 按鈕嘗試修復。", None)
            
        return ("未知錯誤。建議檢查輸出路徑是否正確，或更換輸出容器 (如 MP4) 再試一次。", None)

    def on_transcode_complete(self, exit_code, exit_status, task, single_run):
        try:
            widget = task["widget"]
            start_time = task.get("start_time", 0)
            target_duration = task.get("target_duration", 0)
            output_path = task.get("output_path_ref", "")
            
            speed_text = ""
            if exit_code != 0:
                 if widget:
                     widget.lbl_status.setText(f"Failed ({exit_code})")
                     widget.progress.setStyleSheet("QProgressBar::chunk { background-color: #d32f2f; }")
            else:
                duration_sec = time.time() - start_time
                speed_text = f"{int(duration_sec)}s"
                
                if target_duration > 0:
                    speed = target_duration / duration_sec if duration_sec > 0 else 0
                    speed_text += f", {speed:.1f}x"
                
                if widget:
                    widget.progress.setRange(0, 100)
                    widget.progress.setValue(100)
                    widget.progress.setStyleSheet("""
                        QProgressBar {
                            background-color: #222; border: 1px solid #333; border-radius: 6px;
                            text-align: center; color: white; font-size: 9px;
                        }
                        QProgressBar::chunk { background-color: #00c853; border-radius: 5px; }
                    """)
                    widget.set_done(output_path, self.player, speed_text)
            
            # SAFE CLEANUP: Remove worker from dict AFTER UI updates are finished
            if widget in self.workers:
                worker = self.workers.pop(widget)
                try:
                    # Explicitly wait for thread to be truly finished if it's still running
                    # This prevents "Destroyed while thread is still running"
                    if worker.isRunning():
                        worker.wait(100) 
                    
                    # Disconnect signals to be safe (though deleteLater usually handles this)
                    try:
                        worker.progress_signal.disconnect()
                        worker.finished_signal.disconnect()
                    except: pass
                    
                    worker.deleteLater() # Safely schedule deletion
                except Exception as cleanup_err:
                    print(f"Cleanup Warning: {cleanup_err}")
            
            # Reset current task ref
            if getattr(self, 'current_running_task', None) == task:
                self.current_running_task = None

            if not single_run:
                self.process_next_task()
            else:
                self.is_processing = False
                if self.pending_tasks:
                    self.btn_start_all.setEnabled(True)
        except Exception as e:
            debug_log(f"on_transcode_complete Error: {e}\n{traceback.format_exc()}")
            if not single_run: self.process_next_task()
            else: self.is_processing = False

    def apply_styles(self):
        # Professional Dark Mode Stylesheet (Windows 11 inspired)
        self.setStyleSheet("""
            QMainWindow {
                background-color: #0f0f0f;
                font-family: 'Segoe UI', sans-serif;
                font-size: 13px; /* Standard Pixel Size */
            }
            * {
                 /* Global reset to avoid point-size issues */
            }
            #sidebar {
                background-color: #1a1a1a;
                border-right: 1px solid #333;
            }
            QPushButton {
                background-color: transparent;
                color: #ddd;
                border: none;
                padding: 10px;
                text-align: left;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #2a2a2a;
            }
            QPushButton:checked {
                background-color: #0078d4;
                color: white;
            }
            QLabel {
                color: #eee;
                font-weight: bold;
            }
            QFrame {
                border: none;
            }
            QListWidget {
                background-color: #151515;
                color: #ccc;
                border: 1px solid #333;
            }
            QMessageBox {
                background-color: #2b2b2b;
                color: #eee;
            }
            QMessageBox QLabel {
                color: #eee;
            }
            QMessageBox QPushButton {
                background-color: #444;
                color: #eee;
                border: 1px solid #555;
                padding: 4px 12px;
                border-radius: 3px;
                min-width: 60px;
            }
            QMessageBox QPushButton:hover {
                background-color: #555;
                border-color: #777;
            }
            QMessageBox QPushButton:pressed {
                background-color: #333;
            }
            QToolTip {
                background-color: #000000 !important;
                color: #ffffff !important;
                border: 1px solid #ffffff;
                padding: 4px;
                font-size: 13px;
            }
        """)

    def save_pending_tasks(self):
        to_save = []
        count = self.task_list.count()
        count = self.task_list.count()
        keys_to_save = ["source", "in_point", "out_point", "base_name", "output_dir", "sequence", "bitrate", "container", "vcodec", "acodec", "audio_gain", "resolution", "fps", "audio_ch"]
        
        for i in range(count):
             item = self.task_list.item(i)
             widget = self.task_list.itemWidget(item)
             if widget and widget.task_data:
                 status = widget.lbl_status.text()
                 if status != "Done":
                      # Safe Whitelist Copy to prevent JSON Error
                      safe_data = {}
                      for k in keys_to_save:
                          if k in widget.task_data:
                              safe_data[k] = widget.task_data[k]
                      to_save.append(safe_data)
        
        try:
            self.settings.set("saved_queue", to_save)
            self.settings.save()  # Force immediate save
            print(f"DEBUG: Saved {len(to_save)} pending tasks to settings.")
        except Exception as e:
            print(f"Error saving queue: {e}")
    
    def auto_save(self):
        """Periodic auto-save to prevent data loss"""
        try:
            self.save_pending_tasks()
            print("DEBUG: Auto-save completed")
        except Exception as e:
            print(f"Auto-save error: {e}")

    def load_pending_tasks(self):
        saved = self.settings.get("saved_queue", [])
        if not saved: return
        
        print(f"DEBUG: Restoring {len(saved)} tasks...")
        for task_data in saved:
             # Sanity check
             if not task_data.get("base_name"): continue
             
             final_base = task_data["base_name"]
             
             item = QListWidgetItem(self.task_list)
             item.setSizeHint(QSize(0, 36))
             widget = TaskProgressWidget(final_base)
             # Restore source and worker for saved tasks
             task_data["source_type"] = task_data.get("source_type", "Manual")
             task_data["worker_id"] = task_data.get("worker_id", "-")
             widget.set_task_data(task_data) 
             widget.lbl_status.setText("Pending")
             widget.removed.connect(self.remove_task_by_widget)
             widget.transcode_requested.connect(self.transcode_single_item)
             widget.pause_requested.connect(self.pause_task)
             widget.resume_requested.connect(self.resume_task)
             widget.stop_requested.connect(self.stop_current_task)
             
             self.task_list.addItem(item)
             self.task_list.setItemWidget(item, widget)
             task_data["widget"] = widget
             self.pending_tasks.append(task_data)
             
        if self.pending_tasks:
             self.btn_start_all.setEnabled(True)

    def check_dongle_status(self):
        """Asynchronously check lock status to prevent stuttering."""
        if hasattr(self, '_dongle_checking') and self._dongle_checking:
            return
            
        self._dongle_checking = True
        self.dongle_thread = DongleCheckThread()
        self.dongle_thread.result_ready.connect(self.on_dongle_check_result)
        self.dongle_thread.finished.connect(lambda: setattr(self, '_dongle_checking', False))
        self.dongle_thread.start()

    def on_dongle_check_result(self, allowed, msg, ids):
        """Handle async dongle check result."""
        if not allowed:
            # Stop main monitor timer
            self.dongle_monitor_timer.stop()
            
            # Show special alert dialog with countdown
            from ui.startup_dialog import StartupCheckDialog
            alert_dlg = StartupCheckDialog(self)
            
            # Countdown timer for the dialog
            self._countdown = 60
            
            from core.security import LicenseManager
            lm = LicenseManager()

            def update_countdown():
                self._countdown -= 1
                alert_dlg.set_dongle_removed(self._countdown)
                
                # Re-check if dongle is inserted
                is_back, _, _ = lm.check_protection()
                if is_back:
                    timer.stop()
                    alert_dlg.accept()
                    self.dongle_monitor_timer.start(10000) # Resume monitoring
                
                if self._countdown <= 0:
                    timer.stop()
                    QApplication.quit()

            timer = QTimer(alert_dlg)
            timer.timeout.connect(update_countdown)
            timer.start(1000)
            
            alert_dlg.set_dongle_removed(self._countdown)
            alert_dlg.exec()
            
            # If we exit the dialog (via Exit button), quit app
            if not lm.check_protection()[0]:
                QApplication.quit()

    def on_watch_folder_detected(self, file_path, folder_name):
        """Handler for automated folder monitoring detections."""
        print(f"WatchFolder Trigger: {file_path} from {folder_name}")
        
        # Find preset for this folder
        watch_folders = self.settings.get("watch_folders", [])
        preset_name = None
        for wf in watch_folders:
            if wf.get("name") == folder_name:
                preset_name = wf.get("preset")
                break
        
        # Add to queue automatically
        self.add_task_to_queue(
            source_path=file_path, 
            full_duration=True, 
            source_type=folder_name, 
            worker_id="AUTO", 
            preset_name=preset_name
        )
        
        # Start immediately if no other task is running? 
        # For now, let user press "Start All" or just pend it.
        # Requirement says "啟動轉碼" (Launch Transcode)
        QTimer.singleShot(500, self.start_transcode_all)

    def on_cluster_task_synced(self, task_data):
        """Handler for tasks broadcasted by other nodes."""
        # Prevent duplicates
        for t in self.pending_tasks:
            if t.get("base_name") == task_data.get("base_name") and t.get("node_origin") == task_data.get("node_origin"):
                return
        
        print(f"Cluster: New Task from {task_data.get('node_origin')}")
        self.add_task_to_queue(
            source_path=task_data.get("source"),
            full_duration=True,
            source_type=f"Node:{task_data.get('node_origin')}",
            worker_id="Cluster"
        )

    def on_cluster_node_updated(self, node_data):
        """Update cluster node status in the Dashboard (e.g. status bar)."""
        node_id = node_data.get("node_id")
        status = node_data.get("status")
        # For now, just a debug log, could be a list in a side panel later
        print(f"Cluster Node Update: {node_id} is {status}")
        if hasattr(self, 'statusBar'):
            self.statusBar().showMessage(f"Cluster: node {node_id} is {status}", 3000)

    def closeEvent(self, event):
        """Standardized clean shutdown to prevent QThread crashes on exit."""
        try:
            # 1. Stop all timers and save state
            if hasattr(self, 'auto_save_timer'): self.auto_save_timer.stop()
            if hasattr(self, 'dongle_monitor_timer'): self.dongle_monitor_timer.stop()
            self.save_settings()
            
            # 2. Force player and its sub-threads to shut down
            if hasattr(self, 'player') and self.player:
                self.player.shutdown()
                
            # 3. Stop all active transcode worker threads
            if hasattr(self, 'workers'):
                for widget, worker in list(self.workers.items()):
                    try:
                        worker.blockSignals(True)
                        worker.terminate() # Force kill if necessary on exit
                        worker.wait(50)
                        worker.deleteLater()
                    except: pass
                self.workers.clear()
            
        except Exception as e:
            print(f"Error during shutdown: {e}")
        finally:
            event.accept()

    def pause_task(self, widget):
        if widget in self.workers:
            print("Requesting Pause...")
            self.workers[widget].pause()

    def resume_task(self, widget):
        # Resource limit check
        active_running = [w for w in self.workers.values() if not w.paused]
        if len(active_running) > 0:
             QMessageBox.warning(self, "資源限制", "只能同時進行一個轉碼任務。請先暫停其他任務。")
             return
        
        if widget in self.workers:
             print("Requesting Resume...")
             self.workers[widget].resume()

    def stop_current_task(self, widget):
        if widget in self.workers:
            print("Requesting Stop...")
            self.workers[widget].stop()

