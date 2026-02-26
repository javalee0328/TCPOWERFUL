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
    QToolButton, QStyle, QAbstractSpinBox, QDialog, QTextEdit, QSlider,
    QTableWidget, QTableWidgetItem, QHeaderView, QSizePolicy, QStyleFactory,
    QPlainTextEdit
)
from PySide6.QtCore import Qt, QSize, QProcess, QTimer, QDir, QEvent, Signal, QRectF, QThread, QTime
from PySide6.QtGui import QIcon, QAction, QKeySequence, QShortcut, QPixmap, QPainter, QPainterPath, QPen, QColor, QKeyEvent, QBrush, QPalette
from core.settings import (
    SettingsManager, DATA_DIR, get_app_path, debug_log, CURRENT_VERSION
)
from core.metadata import get_video_metadata
from core.preset_data import PRESETS
from core.watch_folder import WatchFolderEngine
from core.cluster_manager import ClusterManager

import subprocess
import logging
import traceback
import re
import shutil

# helpers removed - using core.settings version


class TranscodeWorker(QThread):
    progress_signal = Signal(int, str) # percent, text_status
    finished_signal = Signal(bool, str) # success, msg

    def __init__(self, cmd, target_duration):
        super().__init__()
        self.cmd = cmd
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
            err_msg = str(e)
            if "WinError 2" in err_msg:
                 err_msg += "\n\n[系統找不到指定的檔案]\n這表示 Worker 電腦上缺少 FFmpeg 執行檔。\n請嘗試將 ffmpeg.exe 複製到程式同一目錄下的 core 資料夾中。"
            self.finished_signal.emit(False, err_msg)

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
                val = line.split('=')[1]
                if not val.strip(): return
                time_us = int(val)
                time_sec = time_us / 1_000_000.0
                
                if self.target_duration > 0:
                    percent = int((time_sec / self.target_duration) * 100)
                    
                    # [v27.10.15] Force "Finalizing..." at 100%
                    if percent >= 100:
                        self.progress_signal.emit(100, "Finalizing...")
                    elif percent < 0:
                        self.progress_signal.emit(0, "0%")
                    else:
                        self.progress_signal.emit(percent, f"{percent}%")
                else:
                    self.progress_signal.emit(0, "Busy")
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

# [NEW] Background Thread for Watch Folder Task Creation
class WatchTaskCreationThread(QThread):
    task_ready = Signal(dict)
    task_failed = Signal(str) # [v27.10.74] Signal placeholder_id to unblock stuck UI
    
    def __init__(self, file_path, folder_name, parent_ui, is_repeat=False):
        super().__init__()
        self.file_path = file_path
        self.folder_name = folder_name
        self.parent_ui = parent_ui
        self.is_repeat = is_repeat
        
    def run(self):
        try:
            from core.metadata import get_video_metadata
            import os, time
            
            # [v27.3] Reinforce Role Check inside the thread
            my_role = self.parent_ui.settings.get("cluster_role", "Worker")
            if my_role != "Master":
                 print("DEBUG_THREAD: Aborting WatchTaskCreationThread - Local node is no longer Master.")
                 self.task_failed.emit(getattr(self, 'placeholder_id', ''))
                 return

            # 1. Metadata Probe
            meta_path = self.file_path
            # For UNC paths, sometimes ffprobe likes normalized slashes
            meta = get_video_metadata(meta_path)
            if meta is None: meta = {} # [FIX v27.9.8] Prevent NoneType crash
            
            # [v27.10.40] Unified Naming: Use full filename (with ext) as base_name 
            # to match physical file scans and prevent Dashboard duplication.
            f_base, f_ext = os.path.splitext(os.path.basename(self.file_path))
            full_name = os.path.basename(self.file_path)
            base_name = full_name 

            # [v27.10.52] Duplicate Check: Use flag from engine
            if self.is_repeat:
                # [v27.10.52] Add HHMMSS timestamp so user knows it's a re-process
                import datetime
                ts = datetime.datetime.now().strftime("%H%M%S")
                base_name = f"{f_base}_{ts}{f_ext}"
                print(f"DEBUG_THREAD: Repeat file (processed before). Adding timestamp: {base_name}")
            else:
                print(f"DEBUG_THREAD: Fresh file detected. Using original name: {base_name}")
            
            # 3. Construct Data (With Preset Injection)
            watch_folders = self.parent_ui.settings.get("watch_folders", [])
            target_wf = next((wf for wf in watch_folders if wf.get("name") == self.folder_name), None)
            
            if not target_wf:
                norm_file = os.path.normpath(self.file_path).lower()
                for wf in watch_folders:
                    wf_path = wf.get("path", "")
                    if wf_path and os.path.normpath(wf_path).lower() in norm_file:
                        target_wf = wf
                        break
            
            preset_data = {}
            if target_wf:
                preset_name = target_wf.get("preset_name", "MP4 (H.264 High)")
                preset_data = self.parent_ui.settings.get("presets", {}).get(preset_name, {})

            task_data = {
                "source": self.file_path,
                "source_path": self.file_path,
                "base_name": base_name,
                "size": os.path.getsize(self.file_path) if os.path.exists(self.file_path) else 0,
                "duration": meta.get("duration", 0),
                "audio_layout": meta.get("audio_layout", "Stereo"),
                "vcodec": preset_data.get("vcodec", "h264"),
                "container": preset_data.get("container", "mp4"),
                "bitrate": preset_data.get("bitrate", "5000"),
                "acodec": preset_data.get("acodec", "aac"),
                "resolution": preset_data.get("resolution"),
                "fps": preset_data.get("fps"),
                "audio_ch": preset_data.get("audio_ch"),
                "in_point": 0,
                "out_point": int(meta.get("duration", 0) * 1000),
                "source_type": f"Watch:{self.folder_name}",
                "status": "Pending",
                "cluster_status": "Pending",
                "placeholder_id": getattr(self, 'placeholder_id', self.file_path)
            }
            self.task_ready.emit(task_data)
        except Exception as e:
            print(f"DEBUG_THREAD: WatchTaskCreationThread Error: {e}")
            self.task_failed.emit(getattr(self, 'placeholder_id', '')) # [v27.10.74] Unblock placeholder

class SmartFailureDialog(QDialog):
    """Professional dialog to translate technical errors into actionable solutions."""
    def __init__(self, technical_log, user_suggestion, fix_params=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("智能轉碼診斷 (Smart Diagnosis)")
        self.setMinimumSize(600, 450) # Resizable and larger minimum
        self.setSizeGripEnabled(True)
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
        self.txt_details.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.txt_details.setMinimumHeight(150)
        self.txt_details.setStyleSheet("background: #000; color: #aaa; font-family: 'Consolas'; font-size: 11px; border: 1px solid #333;")
        self.txt_details.hide()
        layout.addWidget(self.txt_details, 1) # Give it stretch factor

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


# [NEW] Custom Widget for Watch Folder List

class PresetSelectorDialog(QDialog):
    def __init__(self, parent=None, initial_selection=None):
        super().__init__(parent)
        self.setWindowTitle("選擇監控轉碼目標格式")
        self.setMinimumSize(450, 600)
        self.selected_preset = None
        
        layout = QVBoxLayout(self)
        
        title = QLabel("請選擇該資料夾對應的格式:")
        title.setStyleSheet("font-weight: bold; font-size: 14px; margin-bottom: 5px;")
        layout.addWidget(title)
        
        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet("""
            QListWidget { background-color: #f5f5f5; color: #333; font-size: 13px; border: 1px solid #ccc; }
            QListWidget::item { padding: 10px; border-bottom: 1px solid #eee; }
            QListWidget::item:selected { background-color: #0078d4; color: white; }
        """)
        
        # Load Presets
        from core.preset_data import PRESETS
        preset_names = sorted(PRESETS.keys())
        for name in preset_names:
            item = QListWidgetItem(name)
            self.list_widget.addItem(item)
            if initial_selection == name:
                self.list_widget.setCurrentItem(item)
                
        layout.addWidget(self.list_widget)
        
        # Search Box
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("快速搜尋格式...")
        self.search_edit.textChanged.connect(self.filter_list)
        layout.addWidget(self.search_edit)
        
        btn_layout = QHBoxLayout()
        btn_ok = QPushButton("確認選擇 (OK)")
        btn_ok.setMinimumHeight(40)
        btn_ok.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold;")
        btn_ok.clicked.connect(self.accept)
        
        btn_cancel = QPushButton("取消 (Cancel)")
        btn_cancel.setMinimumHeight(40)
        btn_cancel.clicked.connect(self.reject)
        
        btn_layout.addWidget(btn_ok)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout)
        
        self.list_widget.itemDoubleClicked.connect(lambda: self.accept())

    def filter_list(self, text):
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            item.setHidden(text.lower() not in item.text().lower())

    def get_selection(self):
        curr = self.list_widget.currentItem()
        return curr.text() if curr else None

class WatchFolderRowWidget(QWidget):
    def __init__(self, folder_data, index, parent_controller):
        super().__init__()
        self.folder_data = folder_data
        self.index = index
        self.controller = parent_controller
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(15, 10, 15, 10)
        self.setFixedHeight(80) # Explicit height to prevent clipping
        
        # Info Column
        info_layout = QVBoxLayout()
        name_lbl = QLabel(f"📁 {folder_data.get('name')}")
        name_lbl.setStyleSheet("font-weight: bold; color: #E0E0E0; font-size: 14px;")
        path_lbl = QLabel(f"{folder_data.get('path')}")
        path_lbl.setStyleSheet("color: #888; font-size: 11px;")
        preset_lbl = QLabel(f"Preset: {folder_data.get('preset')}")
        preset_lbl.setStyleSheet("color: #4CAF50; font-size: 11px;")
        
        info_layout.addWidget(name_lbl)
        info_layout.addWidget(path_lbl)
        info_layout.addWidget(preset_lbl)
        layout.addLayout(info_layout, stretch=1)
        
        # Status & Control
        self.is_enabled = folder_data.get("enabled", True)
        
        status_text = "監控中 (Active)" if self.is_enabled else "已停止 (Stopped)"
        status_color = "#4CAF50" if self.is_enabled else "#757575"
        self.lbl_status = QLabel(status_text)
        self.lbl_status.setStyleSheet(f"color: {status_color}; font-weight: bold; font-size: 12px; margin-right: 15px;")
        layout.addWidget(self.lbl_status)
        
        self.btn_toggle = QToolButton()
        self.btn_toggle.setFixedSize(42, 42) # Slightly larger button
        self.btn_toggle.setCursor(Qt.PointingHandCursor)
        
        if self.is_enabled:
            self.btn_toggle.setIcon(self.controller.create_geometric_icon("stop", "#ffffff", size=32))
            self.btn_toggle.setToolTip("停止監控 (Stop)")
            self.btn_toggle.setStyleSheet("QToolButton { background-color: #c62828; border: none; border-radius: 8px; } QToolButton:hover { background-color: #d32f2f; }")
        else:
            self.btn_toggle.setIcon(self.controller.create_geometric_icon("play", "#ffffff", size=32))
            self.btn_toggle.setToolTip("啟動監控 (Start)")
            self.btn_toggle.setStyleSheet("QToolButton { background-color: #2e7d32; border: none; border-radius: 8px; } QToolButton:hover { background-color: #388e3c; }")
        
        self.btn_toggle.setIconSize(QSize(28, 28)) # Calibrated icon size
        self.btn_toggle.clicked.connect(self.on_toggle_clicked)
        layout.addWidget(self.btn_toggle)
        
        # [REMOVED v27.5] Browse DONE/TEMP Buttons moved per User request
        
        # [NEW] Edit Button
        self.btn_edit = QToolButton()
        self.btn_edit.setFixedSize(42, 42)
        self.btn_edit.setCursor(Qt.PointingHandCursor)
        self.btn_edit.setIcon(self.controller.create_geometric_icon("edit", "#ffffff", size=32))
        self.btn_edit.setIconSize(QSize(28, 28))
        self.btn_edit.setToolTip("編輯設定 (Edit)")
        self.btn_edit.setStyleSheet("QToolButton { background-color: #1976D2; border: none; border-radius: 8px; } QToolButton:hover { background-color: #2196F3; }")
        self.btn_edit.clicked.connect(lambda idx=self.index: self.controller.edit_watch_folder(idx))
        layout.addWidget(self.btn_edit)
        
        # [NEW] Delete Button
        self.btn_del = QToolButton()
        self.btn_del.setFixedSize(42, 42)
        self.btn_del.setCursor(Qt.PointingHandCursor)
        self.btn_del.setIcon(self.controller.create_geometric_icon("delete", "#ffffff", size=32))
        self.btn_del.setIconSize(QSize(28, 28))
        self.btn_del.setToolTip("刪除監控 (Delete)")
        self.btn_del.setStyleSheet("QToolButton { background-color: #c62828; border: none; border-radius: 8px; } QToolButton:hover { background-color: #d32f2f; }")
        self.btn_del.clicked.connect(lambda idx=self.index: self.controller.delete_watch_folder(idx))
        layout.addWidget(self.btn_del)

    def on_toggle_clicked(self):
        new_state = not self.is_enabled
        self.controller.toggle_watch_folder(self.index, new_state)

class ClusterNodeRowWidget(QWidget):
    def __init__(self, node_id, data, is_local=False):
        super().__init__()
        self.node_id = node_id # Store for updates
        self.setFixedHeight(85)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(15)
        
        # Background Style
        bg_color = "#353535" if is_local else "#2b2b2b"
        border_color = "#BB86FC" if is_local else "#444"
        self.setObjectName("NodeRow")
        self.setStyleSheet(f"""
            QWidget#NodeRow {{
                background-color: {bg_color};
                border: 1px solid {border_color};
                border-radius: 6px;
            }}
            QLabel {{ color: #e0e0e0; font-family: 'Segoe UI', sans-serif; }}
        """)
        
        # Icon
        lbl_icon = QLabel()
        lbl_icon.setFixedSize(48, 48)
        lbl_icon.setAlignment(Qt.AlignCenter)
        lbl_icon.setText("🖥️")
        lbl_icon.setStyleSheet("font-size: 24px; background-color: #444; border-radius: 6px;")
        layout.addWidget(lbl_icon)
        
        # Info Column
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)
        
        # ID & Role & Alias
        role_map = {"Master": "主監控節點", "Worker": "工作節點"}
        role_str = role_map.get(data.get("role"), data.get("role", "Node"))
        alias = data.get("alias") or node_id
        lbl_title = QLabel(f"{alias} ({role_str})")
        lbl_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #ffffff;")
        self.lbl_title = lbl_title # Keep ref for updates
        info_layout.addWidget(lbl_title)
        
        # IP & Status
        status = data.get("status", "Unknown")
        status_color = "#4CAF50" if status == "Online" or status == "Online (Local)" else "#F44336"
        self.lbl_meta = QLabel(f"<span style='color:{status_color}'>● {status}</span>  |  IP: {data.get('ip', '-')}")
        self.lbl_meta.setStyleSheet("font-size: 11px; color: #aaa;")
        info_layout.addWidget(self.lbl_meta)
        
        # Activity
        activity = data.get("current_activity", "Idle")
        activity_color = "#BB86FC" if activity != "Idle" else "#777"
        self.lbl_activity = QLabel(f"Activity: {activity}")
        self.lbl_activity.setStyleSheet(f"font-size: 11px; color: {activity_color}; font-style: italic;")
        info_layout.addWidget(self.lbl_activity)

        
        # [NEW] Active Task Count
        task_count = data.get("active_task_count", 0)
        self.lbl_tasks = QLabel(f"🔥 Active Tasks: {task_count}")
        self.lbl_tasks.setStyleSheet("color: #FF9800; font-weight: bold; font-size: 11px;")
        info_layout.addWidget(self.lbl_tasks)

        
        layout.addLayout(info_layout, 1) # Expand Info
        
        # Resources Column
        res_layout = QVBoxLayout()
        res_layout.setSpacing(4)
        
        # CPU Bar
        cpu_layout = QHBoxLayout()
        lbl_cpu = QLabel("CPU")
        lbl_cpu.setFixedWidth(30)
        self.bar_cpu = QProgressBar()
        self.bar_cpu.setRange(0, 100)
        self.bar_cpu.setValue(int(data.get("cpu_usage", 0)))
        self.bar_cpu.setFixedHeight(8)
        self.bar_cpu.setTextVisible(False)
        self.bar_cpu.setStyleSheet(self._get_bar_style("#03A9F4"))
        cpu_layout.addWidget(lbl_cpu)
        cpu_layout.addWidget(self.bar_cpu)
        res_layout.addLayout(cpu_layout)
        
        # RAM Bar
        ram_layout = QHBoxLayout()
        lbl_ram = QLabel("RAM")
        lbl_ram.setFixedWidth(30)
        self.bar_ram = QProgressBar()
        self.bar_ram.setRange(0, 100)
        self.bar_ram.setValue(int(data.get("ram_usage", 0)))
        self.bar_ram.setFixedHeight(8)
        self.bar_ram.setTextVisible(False)
        self.bar_ram.setStyleSheet(self._get_bar_style("#E91E63"))
        ram_layout.addWidget(lbl_ram)
        ram_layout.addWidget(self.bar_ram)
        res_layout.addLayout(ram_layout)
        
        layout.addLayout(res_layout)
        layout.addSpacing(10)
        
    def _get_bar_style(self, color):
        return f"""
            QProgressBar {{
                background-color: #222;
                border: 1px solid #444;
                border-radius: 4px;
            }}
            QProgressBar::chunk {{
                background-color: {color};
                border-radius: 3px;
            }}
        """

    def update_state(self, data):
        # Update Alias/Title
        alias = data.get("alias") or self.node_id
        role_map = {"Master": "主監控節點", "Worker": "工作節點"}
        role_str = role_map.get(data.get("role"), data.get("role", "Node"))
        if hasattr(self, 'lbl_title'):
             self.lbl_title.setText(f"{alias} ({role_str})")
        
        # Update Status/IP
        status = data.get("status", "Unknown")
        status_color = "#4CAF50" if status == "Online" or status == "Online (Local)" else "#F44336"
        self.lbl_meta.setText(f"<span style='color:{status_color}'>● {status}</span>  |  IP: {data.get('ip', '-')}")
        
        # Update Activity
        activity = data.get("current_activity", "Idle")
        activity_color = "#BB86FC" if activity != "Idle" else "#777"
        self.lbl_activity.setText(f"Activity: {activity}")
        self.lbl_activity.setStyleSheet(f"font-size: 11px; color: {activity_color}; font-style: italic;")
        
        # Update Task Count
        task_count = data.get("active_task_count", 0)
        self.lbl_tasks.setText(f"🔥 Active Tasks: {task_count}")

        
        # Update Resources
        self.bar_cpu.setValue(int(data.get("cpu_usage", 0)))
        self.bar_ram.setValue(int(data.get("ram_usage", 0)))

class TaskProgressWidget(QWidget):
    removed = Signal(object)
    transcode_requested = Signal(object) 
    pause_requested = Signal(object)
    resume_requested = Signal(object)
    stop_requested = Signal(object)
    switch_page_requested = Signal() # [NEW] Switch to Player Page

    # [v27.10.51] Widened name column; slightly trimmed status/perf/node to compensate
    WIDTHS = {
        "name": 250, "status": 82, "fmt": 120, 
        "start": 100, "perf": 62, "src": 80, 
        "node": 90, "prog": 140, "fin": 110, "act": 130
    }

    def __init__(self, filename, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        # [v27.10.42] Match Dashboard Header margins/spacing for perfect alignment
        layout.setContentsMargins(5, 0, 5, 0)
        layout.setSpacing(10)

        # 1. Task Name
        self.lbl_name = QLabel(filename)
        self.lbl_name.setFixedWidth(self.WIDTHS["name"])
        self.lbl_name.setStyleSheet("font-weight: bold; color: white; font-size: 13px;")
        self.lbl_name.setToolTip(filename)
        self.lbl_name.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(self.lbl_name)
        
        # 2. Status
        self.lbl_status = QLabel("Pending")
        self.lbl_status.setFixedWidth(self.WIDTHS["status"])
        self.lbl_status.setStyleSheet("color: #ffa726; font-weight: bold;")
        self.lbl_status.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(self.lbl_status)

        # 3. Format Info
        self.lbl_info = QLabel("-")
        self.lbl_info.setFixedWidth(self.WIDTHS["fmt"])
        self.lbl_info.setStyleSheet("color: #ccc; font-size: 11px;")
        self.lbl_info.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(self.lbl_info)

        # 4. Transcode Start
        self.lbl_time_range = QLabel("-") 
        self.lbl_time_range.setFixedWidth(self.WIDTHS["start"])
        self.lbl_time_range.setStyleSheet("color: #ccc; font-size: 11px;")
        self.lbl_time_range.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(self.lbl_time_range)

        # 5. Performance
        self.lbl_perf = QLabel("-")
        self.lbl_perf.setFixedWidth(self.WIDTHS["perf"])
        self.lbl_perf.setStyleSheet("color: #ffd54f; font-weight: bold;")
        self.lbl_perf.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(self.lbl_perf)

        # 6. Source Type
        self.lbl_source_tag = QLabel("MANUAL")
        self.lbl_source_tag.setFixedWidth(self.WIDTHS["src"])
        self.lbl_source_tag.setStyleSheet("color: #b0bec5; font-size: 10px; border: 1px solid #546e7a; border-radius: 3px; padding: 2px;")
        self.lbl_source_tag.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(self.lbl_source_tag)
        
        # 7. Node Alias
        self.lbl_node = QLabel("-")
        self.lbl_node.setFixedWidth(self.WIDTHS["node"])
        self.lbl_node.setStyleSheet("color: #81d4fa; font-size: 11px;")
        self.lbl_node.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(self.lbl_node)

        # 8. Progress - stacked container for in-progress bar + done label
        prog_w = self.WIDTHS["prog"]
        prog_container = QWidget()
        prog_container.setFixedWidth(prog_w)
        prog_container.setFixedHeight(26)
        prog_stack = QHBoxLayout(prog_container)
        prog_stack.setContentsMargins(0, 0, 0, 0)
        prog_stack.setSpacing(0)

        self.progress = QProgressBar()
        self.progress.setFixedWidth(prog_w)
        self.progress.setFixedHeight(26)
        self.progress.setTextVisible(True)
        self.progress.setStyle(QStyleFactory.create("Fusion"))
        self.progress.setStyleSheet("""
            QProgressBar {
                background-color: #333; border: none; border-radius: 0px;
                height: 26px; max-height: 26px;
                text-align: center; color: white; font-size: 11px; font-weight: bold;
            }
            QProgressBar::chunk { background-color: #2e7d32; border-radius: 0px; margin: 0px; }
        """)
        prog_stack.addWidget(self.progress)

        # [v27.10.54] DONE BAR - solid QLabel, 100% pixel-perfect fill, no Qt rendering quirks
        self.lbl_done_bar = QLabel("100%")
        self.lbl_done_bar.setFixedWidth(prog_w)
        self.lbl_done_bar.setFixedHeight(26)
        self.lbl_done_bar.setAlignment(Qt.AlignCenter)
        self.lbl_done_bar.setStyleSheet(
            "background-color: #2e7d32; color: white; font-size: 11px; font-weight: bold; border: none;"
        )
        self.lbl_done_bar.hide()  # hidden until task is done
        prog_stack.addWidget(self.lbl_done_bar)

        layout.addWidget(prog_container)


        # 9. Finished Time
        self.lbl_fin_time = QLabel("-")
        self.lbl_fin_time.setFixedWidth(self.WIDTHS["fin"])
        self.lbl_fin_time.setStyleSheet("color: #4CAF50; font-size: 11px; font-weight: bold;")
        self.lbl_fin_time.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(self.lbl_fin_time)

        container_9 = QWidget()
        container_9.setFixedWidth(self.WIDTHS["act"])
        l9 = QHBoxLayout(container_9)
        l9.setContentsMargins(0,0,10,0) # Increase right padding to avoid 'X' clipping
        l9.setSpacing(5)

        self.lbl_out_path = QLabel("-")
        self.lbl_out_path.setStyleSheet("color: #888; font-size: 10px;")
        self.lbl_out_path.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.lbl_out_path.setFixedWidth(20) # Strictly minimal
        l9.addWidget(self.lbl_out_path)

        btns_container = QWidget()
        btns_layout = QHBoxLayout(btns_container)
        btns_layout.setContentsMargins(0, 0, 0, 0)
        btns_layout.setSpacing(2)

        self.btn_transcode = QToolButton()
        self.btn_transcode.setFixedSize(26, 26)
        self.btn_transcode.setIcon(self.create_geometric_icon("refresh", "#E0E0E0", size=20))
        self.btn_transcode.setStyleSheet(self.get_btn_style("transparent"))
        self.btn_transcode.clicked.connect(self.toggle_transcode)
        btns_layout.addWidget(self.btn_transcode)
        
        self.btn_play_result = QToolButton()
        self.btn_play_result.setFixedSize(26, 26)
        self.btn_play_result.setIcon(self.create_geometric_icon("play", "#ffffff", size=18))
        self.btn_play_result.hide()
        self.btn_play_result.setStyleSheet("QToolButton { background-color: #2e7d32; border-radius: 4px; }")
        self.btn_play_result.clicked.connect(self.play_or_refresh)
        btns_layout.addWidget(self.btn_play_result)

        self.btn_open_folder = QToolButton()
        self.btn_open_folder.setFixedSize(26, 26)
        self.btn_open_folder.setIcon(self.create_geometric_icon("folder", size=24))
        self.btn_open_folder.hide()
        self.btn_open_folder.setStyleSheet(self.get_btn_style("transparent"))
        self.btn_open_folder.clicked.connect(self.open_folder)
        btns_layout.addWidget(self.btn_open_folder)

        self.btn_cancel = QToolButton()
        self.btn_cancel.setFixedSize(26, 26)
        self.btn_cancel.setIcon(self.create_geometric_icon("close", "#ffffff", size=20)) 
        self.btn_cancel.setStyleSheet(self.get_btn_style("filled_close")) 
        self.btn_cancel.clicked.connect(self.request_stop_or_remove)
        btns_layout.addWidget(self.btn_cancel)
        
        l9.addWidget(btns_container)
        layout.addWidget(container_9)

        self.state = "pending"
        self.output_path = ""
        self.workers = {}
        self.current_process = None 
        self.player_ref = None
        self.task_data = None
        self.start_time = None
        self.end_time = None
        self.last_seen_percent = 0
        self.stopped = False
        self.last_error_log = "" 

    def set_failed(self, error_msg="Error"):
        self.state = "error"
        self.lbl_status.setText(f"Failed: {error_msg}")
        self.lbl_status.setStyleSheet("color: #ff5252; font-weight: bold;")
        self.progress.setStyleSheet("""
            QProgressBar {
                background-color: #333; border: none; border-radius: 0px; height: 26px;
                text-align: center; color: white; font-size: 11px; font-weight: bold;
            }
            QProgressBar::chunk { background-color: #d32f2f; border-radius: 0px; }
        """)
        self.btn_transcode.show()
        self.btn_play_result.hide()
        self.btn_open_folder.hide()
        self.btn_cancel.show()

    def set_done(self, out_path, player, speed_text):
        """Called upon successful transcode completion"""
        self.state = "done"
        self.player_ref = player
        self.output_path = out_path
        
        # UI Updates
        self.lbl_status.setText("完成 (Done)")
        # [v27.10.51] Always green for Done - unify red/green confusion
        self.lbl_status.setStyleSheet("color: #4CAF50; font-weight: bold; border: none;")
        self.lbl_perf.setText(speed_text)
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        # [v27.10.54] Switch to solid done bar - bypass ALL Qt progress bar rendering issues
        self.progress.hide()
        if hasattr(self, 'lbl_done_bar'):
            self.lbl_done_bar.show()

        
        # [FIX] Use real file modification time if available
        try:
            mtime = os.path.getmtime(out_path)
            end_time = time.strftime("%H:%M:%S", time.localtime(mtime))
        except:
            end_time = QTime.currentTime().toString("HH:mm:ss")
            
        start_t_str = self.start_time.toString("HH:mm:ss") if hasattr(self, 'start_time') and self.start_time else "--:--:--"
        self.lbl_time_range.setText(start_t_str) # [v27.6] Show only start time
        self.lbl_fin_time.setText(end_time)
        self.lbl_out_path.setText(os.path.basename(out_path))
        self.lbl_out_path.setToolTip(out_path)
        
        # Icon/Buttons - SWAPPED per User Request
        # 1. Main Button (Left) -> REFRESH / RE-TRANSCODE
        self.btn_transcode.setIcon(self.create_geometric_icon("refresh", "#E0E0E0", size=24))
        self.btn_transcode.setToolTip("重新轉碼 (Re-Transcode)")
        # Disconnect old signal and connect toggle_transcode (which restarts)
        try: self.btn_transcode.clicked.disconnect() 
        except: pass
        self.btn_transcode.clicked.connect(self.toggle_transcode)
        
        # 2. Secondary Button (Right) -> PLAY (Green Triangle)
        self.btn_play_result.show()
        self.btn_play_result.setIcon(self.create_geometric_icon("play", "#4caf50", size=24)) # Green Play 
        self.btn_play_result.setToolTip("播放結果 (Play Result)")
        # Allow it to look like a play button
        self.btn_play_result.setStyleSheet("QToolButton { background-color: transparent; border: 1px solid #4caf50; border-radius: 4px; } QToolButton:hover { background-color: #1b5e20; }")
        
        try: self.btn_play_result.clicked.disconnect()
        except: pass
        self.btn_play_result.clicked.connect(self.play_or_refresh) 
        
        self.btn_open_folder.show()
        
        # Reset Cancel Button to Close style
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
        
        self.lbl_info.setText(" / ".join(fmt_parts))
        
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
            self.lbl_time_range.setText(ms_to_fmt(in_p))
        else:
            self.lbl_time_range.setText("Full")

        # [NEW] Source & Worker Info
        source_type = task.get("source_type", "Manual")
        self.lbl_source_tag.setText(source_type)
        if source_type != "Manual":
            self.lbl_source_tag.setStyleSheet("color: #ff9800; font-weight: bold; font-size: 10px; border: 1px solid #ff9800; border-radius: 3px; padding: 2px;") # Highlight folder tasks
        else:
            self.lbl_source_tag.setStyleSheet("color: #b0bec5; font-size: 10px; border: 1px solid #546e7a; border-radius: 3px; padding: 2px;")

        worker_id = task.get("worker_id", "-")
        worker_uuid = task.get("worker_uuid", worker_id)

        # [v27.10.56] Show alias via QApplication top-level window lookup
        # parent() traversal fails for QListWidget item widgets - use app-level search
        display_node = worker_id
        alias = task.get("worker_alias", "") or ""
        if not alias:
            try:
                from PySide6.QtWidgets import QApplication
                for w in QApplication.topLevelWidgets():
                    if hasattr(w, 'cluster_mgr'):
                        node_data = w.cluster_mgr._known_nodes.get(worker_id, {})
                        alias = node_data.get("alias", "") or ""
                        break
            except Exception:
                alias = ""

        if alias and alias != worker_id:
            display_node = alias
        elif len(worker_id) > 15:
            display_node = worker_id[:12] + "..."

        self.lbl_node.setText(display_node)
        tooltip = f"節點: {worker_id}"
        if alias and alias != worker_id:
            tooltip = f"{alias}\n({worker_id})"
        if worker_uuid != worker_id:
            tooltip += f"\n(ID: {worker_uuid})"
        self.lbl_node.setToolTip(tooltip)


        # [NEW/RESTORED] Populate Finish Time and Target Path
        fin_time = task.get("finish_time", "-")
        t_path = task.get("target_path", task.get("output_path", "-"))
        
        self.lbl_fin_time.setText(fin_time)
        if t_path and t_path != "-":
            self.lbl_out_path.setText(os.path.basename(t_path))
            self.lbl_out_path.setToolTip(t_path)
        else:
            self.lbl_out_path.setText("-")
            self.lbl_out_path.setToolTip("")

        # Highlight Play Button (Actually, User wants ONLY at 100%)
        # So we ensure it is hidden here
        self.btn_play_result.hide() 
        self.btn_transcode.setIcon(self.create_geometric_icon("refresh", "#E0E0E0", size=20)) # Reset to Refresh icon for re-run
        self.btn_transcode.setToolTip("當前任務轉碼 (Start Transcode)") # Reset to initial state
        
        # [v27.10.73] Dynamic Status Display
        s = task.get("status", "Pending")
        self.lbl_status.setText(s)
        # Apply style based on status
        if "探測中" in s or "Probing" in s:
            self.lbl_status.setStyleSheet("color: #BB86FC; font-weight: bold; font-style: italic;")
        elif s == "Pending":
            self.lbl_status.setStyleSheet("color: #ffa726; font-weight: bold;")
        elif s == "Claimed":
            self.lbl_status.setStyleSheet("color: #BB86FC; font-weight: bold;")
        
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
        t_str = self.start_time.toString("HH:mm:ss")
        self.lbl_time_range.setText(t_str)
        self.lbl_status.setText("Transcoding...")
        self.lbl_status.setStyleSheet("color: #4CAF50; font-weight: bold;")
        self.btn_transcode.setIcon(self.create_geometric_icon("pause", "#40C4FF", size=24)) 
        self.btn_transcode.setToolTip("暫停轉碼 (Pause)")
        
        # Change Cancel to Stop Style (Larger Icon)
        self.btn_cancel.setIconSize(QSize(28, 28)) 
        self.btn_cancel.setIcon(self.create_geometric_icon("stop", "#ffffff", size=28)) 
        self.btn_cancel.setToolTip("停止轉碼 (Stop)")
        self.btn_cancel.setStyleSheet(self.get_btn_style("filled_close")) 

    def request_stop_or_remove(self):
        state = getattr(self, 'state', 'pending')
        
        if state in ['running', 'paused']:
            # Confirm Stop
            box = QMessageBox(self)
            box.setWindowTitle("停止轉碼?")
            box.setText("確定要終止目前的轉碼任務嗎？\n(進度將歸零)")
            btn_stop = box.addButton("停止 (Stop)", QMessageBox.YesRole)
            btn_force = box.addButton("強制移除 (Force Remove)", QMessageBox.DestructiveRole)
            btn_cancel = box.addButton("取消 (Cancel)", QMessageBox.RejectRole)
            
            box.exec()
            
            if box.clickedButton() == btn_stop:
                self.stop_requested.emit(self)
                # Reset UI to Cleanable state
                self.set_stopped_ui()
            elif box.clickedButton() == btn_force:
                # Direct Remove Strategy
                self.removed.emit(self)
            else:
                # User said No - Resume if it was active
                if state == 'paused' or state == 'running':
                    self.resume_requested.emit(self)
                    self.state = 'running'
                    self.lbl_status.setText("Transcoding...")
                    self.lbl_status.setStyleSheet("color: #4CAF50;")
                    self.btn_transcode.setIcon(self.create_geometric_icon("pause", "#40C4FF", size=24))
        elif state in ['stopped', 'done', 'failed']:
            # Just Remove
            self.removed.emit(self)
        else:
            # Pending or others
            self.removed.emit(self)

    def set_stopped_ui(self):

        """Transition to 'Stopped' UI state where button becomes X"""
        self.state = 'stopped'
        self.stopped = True # Block future progress updates
        self.progress.setValue(0)
        self.lbl_status.setText("Stopped")
        self.lbl_status.setStyleSheet("color: #aaa;")
        
        # TRANSITION: Button becomes X (Clear)
        self.btn_cancel.setFixedSize(30, 30)
        self.btn_cancel.setIconSize(QSize(24, 24))
        self.btn_cancel.setIcon(self.create_geometric_icon("close", "#ffffff", size=24)) 
        self.btn_cancel.setToolTip("移除任務 (Remove)")
        self.btn_cancel.setStyleSheet(self.get_btn_style("filled_close"))
        
        # Reset Transcode Button to Refresh (Original)
        self.btn_transcode.setIcon(self.create_geometric_icon("refresh", "#E0E0E0", size=24))
        self.btn_transcode.setToolTip("重新轉碼 (Re-Transcode)")

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
                     self.switch_page_requested.emit() # [NEW] Switch to Player Page

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

        elif shape == "save":
             # Diskette Icon
             painter.setBrush(Qt.NoBrush)
             painter.setPen(QPen(QColor(color), 2))
             painter.drawRect(m, m, s, s) # Outer
             painter.drawRect(m+s/4, m, s/2, s/3) # Top Hub
             painter.drawRect(m+s/6, m+s/2, 2*s/3, s/2) # Bottom Hub
             painter.setBrush(QColor(color))
             painter.drawRect(int(m+s/2+2), int(m+2), 4, 6) # Slider
            
        painter.end()
        return QIcon(pixmap)

    def open_folder(self):
        # [MODIFIED] Logic to open _DONE folder for Watch Folder Tasks
        # [MODIFIED v27.5] Open Target File Folder
        target_path = self.output_path or (self.task_data.get("output_path") if self.task_data else None)
        if not target_path or target_path == "-":
             # Fallback to source dir if output not yet known
             if self.task_data and self.task_data.get("source"):
                 target_path = self.task_data.get("source")
             else:
                 return

        folder_path = os.path.dirname(target_path)
        if os.path.exists(folder_path):
            import subprocess
            try:
                os.startfile(os.path.normpath(folder_path))
            except:
                subprocess.Popen(f'explorer "{os.path.normpath(folder_path)}"')
        else:
            QMessageBox.warning(None, "錯誤", f"無法開啟目錄: {folder_path}")





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
        self._reset_in_progress = False # [NEW] Flag to block saves during factory reset
        
        # Default Settings
        self.default_settings = {
            "vu_offset": 150,
            "presets": {
                "MP4 (H.264 High)": {"container": "mp4", "vcodec": "h264", "bitrate": "5000", "gain": 0.0},
                "MXF (XDCAM 50)": {"container": "mxf", "vcodec": "mpeg2video", "bitrate": "50000", "gain": 0.0},
                "ProRes 422": {"container": "mov", "vcodec": "prores", "bitrate": "150000", "gain": 0.0}
            }
        }
        
        self.settings = SettingsManager()
        
        # [REMOVED v27.10.19] Role is now purely dynamic based on ClusterManager leader election.
        # w_folders = self.settings.get("watch_folders", [])
        # c_role = self.settings.get("cluster_role", "Worker")
        # if w_folders and c_role == "Worker":
        #      print("DEBUG: Auto-Correcting Cluster Role to 'Master' due to active Watch Folders.")
        #      self.settings.set("cluster_role", "Master")
        #      self.settings.save()
        
        # [NEW] Check Version Upgrade BEFORE loading UI or history
        # This allows for a clean "Factory Reset" before variables are populated.
        # [REMOVED] Duplicate version check - already scheduled with QTimer below
        # self.check_version_upgrade()
        
        # Determine if we need to merge system presets
        current_presets = self.settings.get("presets", {})
        # Update with new system presets (PRESETS)
        # This ensures new features appear even if file exists
        current_presets.update(PRESETS)
        self.settings.set("presets", current_presets) # Keep this line, as it updates the settings object.
        
        # Assuming self.settings_mgr is meant to be self.settings
        # If self.settings_mgr is a separate object, it needs to be initialized.
        # Based on the context, self.settings is the SettingsManager instance.
        # The instruction's line `self.settings = self.settings_mgr.load_settings()`
        # seems to be a misunderstanding or a future change not fully reflected.
        # I will interpret it as adding the new attribute.
        
        # [FIX] Session-based Deleted Filter for Ghost Tasks
        # Load persisted ignore list
        deleted_list = self.settings.get("deleted_tasks", [])
        self.deleted_cluster_tasks = set(deleted_list)
        debug_log(f"Loaded {len(self.deleted_cluster_tasks)} deleted tasks from settings.")
        
        # UI Setup
        from core.settings import CURRENT_VERSION
        self.setWindowTitle(f"ProTranscoder 2026 - Windows 11 Edition ({CURRENT_VERSION})")
        self.resize(1200, 800)
        self.current_source = ""
        self.pending_tasks = []
        # [v27.10.0] Persistent cleared tasks tracking
        self.cleared_tasks_file = get_app_path("cleared_tasks.json")
        self.cleared_tasks = self.load_cleared_tasks()
        self.node_aliases = {} # [NEW] Storage for Cluster Aliases
        self.current_running_task = None # [FIX] Initialize missing attribute
        self.workers = {} # Key: widget, Value: TranscodeWorker
        self.is_processing = False
        self._pumping_queue = False
        self.session_created_tasks = set() # [v27.10.6.1] Track locally created tasks for ghost-pruning safety

        
        # [NEW] Initialize Watch Folder Engine
        self.watch_engine = WatchFolderEngine(self.settings, self)
        self.watch_engine.file_detected.connect(self.on_watch_folder_detected)
        self.watch_engine.snapshot_ready.connect(self.populate_dashboard_ui)
        self.watch_engine.log_message.connect(self.append_watch_log)  # [v27.10.77] Live log
        
        # [NEW] Initialize Cluster Manager
        self.cluster_mgr = ClusterManager(self.settings, self)
        self.cluster_mgr.task_synced.connect(self.on_cluster_task_synced)
        self.cluster_mgr.task_removed.connect(self.on_cluster_task_removed)
        self.cluster_mgr.node_updated.connect(self.refresh_cluster_ui)
        self.cluster_mgr.watch_config_synced.connect(self.on_cluster_watch_config_synced)
        self.cluster_mgr.role_changed.connect(self.on_role_changed)
        self.cluster_mgr.master_stale_detected.connect(self.on_master_stale_detected)

        debug_log("MainWindow: Calling setup_ui")
        self.setup_ui()
        
        debug_log("MainWindow: Restoring Saved Settings")
        self.load_saved_settings()

        # [NEW] Stamp version if it's a new install/reset
        if self.settings.get("app_version") == "NEW_INSTALL":
            self.settings.stamp_version()
            debug_log("MainWindow: New Install detected. Version stamped.")
        
        debug_log("MainWindow: Applying Styles")
        self.apply_styles()
        
        debug_log("MainWindow: Loading Pending Tasks")
        # [DISABLED] User Request: Don't restore tasks on restart
        # self.load_pending_tasks() # Restore tasks
        debug_log("MainWindow: Skipping task restoration (clean start)")
        
        # Start Background Services AFTER UI Init
        # [OPTIMIZATION] Delay Start to allow UI to render first (Fixes "Slow Startup" feeling)
        # REMOVED: if self.settings.get("cluster_role", "Master") == "Master":
        # REMOVED:    QTimer.singleShot(2000, self.watch_engine.start)
        # Reason: Role is now dynamic. ClusterManager will trigger on_role_changed -> Start Engine
            
        # Cluster Manager often hits network drives (slow), call it later
        # Start Cluster Manager slightly earlier to determine role quickly
        # [REMOVED] Duplicate connection
        QTimer.singleShot(1500, self.cluster_mgr.start)
        
        # Auto-save timer (every 30 seconds)
        self.auto_save_timer = QTimer(self)
        self.auto_save_timer.timeout.connect(self.auto_save)
        self.auto_save_timer.start(30000)  # 30 seconds
        
        # Dongle removal detection timer (every 10 seconds - Asynchronous)
        self.dongle_monitor_timer = QTimer(self)
        self.dongle_monitor_timer.timeout.connect(self.check_dongle_status)
        self.dongle_monitor_timer.start(10000)  # 10 seconds
        
        # [NEW] Cluster Status Auto-Refresh (Every 3 seconds)
        self.cluster_refresh_timer = QTimer(self)
        self.cluster_refresh_timer.timeout.connect(self.refresh_cluster_ui_timer)
        self.cluster_refresh_timer.start(5000) # Match sync loop
        
        # [NEW] Schedule Version Check (instead of calling immediately)
        QTimer.singleShot(500, self.check_version_upgrade)
        
        # [FIX] Initialize Watch Folder UI and Start Engine
        self.refresh_watch_list_ui()
        
        self.loading = False
        
        debug_log("MainWindow: Init Complete")

    def closeEvent(self, event):
        """[v27.10.75] Graceful shutdown: Kill all active FFmpeg workers before exit.
        This releases all file handles so PyInstaller can clean its _MEI temp directory."""
        debug_log("MainWindow: closeEvent - Killing active workers...")
        
        # 1. Stop Cluster Sync Timer
        if hasattr(self, 'cluster_timer'):
            try: self.cluster_timer.stop()
            except: pass
        
        # 2. Stop Watch Folder Engine
        if hasattr(self, 'watch_engine'):
            try: self.watch_engine.stop()
            except: pass
        
        # 3. Kill all active TranscodeWorkers (releases ffmpeg subprocess handles)
        if hasattr(self, 'workers'):
            for widget, worker in list(self.workers.items()):
                try:
                    worker.kill()   # Terminate FFmpeg process
                    worker.quit()   # Quit the QThread
                    worker.wait(1000) # Wait max 1s
                except: pass
        
        # 4. Terminate any watch task creation threads
        for thread in getattr(self, '_watch_task_threads', []):
            try:
                thread.quit()
                thread.wait(500)
            except: pass
        
        # 5. Stop ClusterManager worker thread
        if hasattr(self, 'cluster_mgr') and hasattr(self.cluster_mgr, '_worker_thread'):
            try:
                self.cluster_mgr._worker_thread.quit()
                self.cluster_mgr._worker_thread.wait(2000)
            except: pass
        
        debug_log("MainWindow: closeEvent - Shutdown complete.")
        event.accept()

    def _safe_move(self, src, dst, retries=8, delay=2.0):
        """[v27.10.50] Robust file move with copy+delete for NAS/locked files (WinError 32)."""
        # Try shutil.move first (fast path)
        try:
            if not os.path.exists(src):
                return False, f"Source not found: {src}"
            dst_dir = os.path.dirname(dst)
            if dst_dir and not os.path.exists(dst_dir):
                try: os.makedirs(dst_dir, exist_ok=True)
                except: pass
            shutil.move(src, dst)
            return True, "Success"
        except Exception as e:
            first_err = str(e)
            
        # Slow path: copy + delete (Handles NAS locking)
        last_err = first_err
        for i in range(retries):
            try:
                if not os.path.exists(src):
                    return False, f"Source not found after wait: {src}"
                dst_dir = os.path.dirname(dst)
                if dst_dir and not os.path.exists(dst_dir):
                    try: os.makedirs(dst_dir, exist_ok=True)
                    except: pass
                # [KEY FIX] shutil.copy2 then os.remove = NAS safe
                shutil.copy2(src, dst)
                try: os.remove(src)
                except: pass  # Source delete failure is OK; file is already at destination
                debug_log(f"Move success via copy+delete (attempt {i+1}): {os.path.basename(src)}")
                return True, "Success"
            except Exception as e:
                last_err = str(e)
                debug_log(f"Move locked, retrying {i+1}/{retries}: {os.path.basename(src)}")
                time.sleep(delay)
        return False, f"Failed after {retries} retries: {last_err}"
        

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
            
        if hasattr(self, 'edit_worker_out'):
            self.edit_worker_out.setText(self.settings.get("worker_output_path", ""))

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
            
        # [NEW] Refresh Watch List & Cluster on Start
        self.refresh_watch_list_ui()
        self.refresh_cluster_ui()
    
    def check_version_upgrade(self):
        """Checks if app version changed and prompts for reset."""
        if self.settings.is_new_version():
            from PySide6.QtWidgets import QMessageBox
            msg = QMessageBox(self)
            msg.setWindowTitle("版本更新確認 (Version Update)")
            old_v = self.settings.get('app_version', '0.0.0')
            new_v = CURRENT_VERSION
            msg.setText(f"偵測到新版本！系統已從 {old_v} 更新至 {new_v}。")

            msg.setInformativeText("您希望[重新設置 (Factory Reset)] 獲取乾淨環境，還是 [載入其餘舊設定]？")
            btn_load = msg.addButton("載入舊設定 (Keep Settings)", QMessageBox.AcceptRole)
            btn_reset = msg.addButton("重新設置 (Factory Reset)", QMessageBox.DestructiveRole) 
            msg.setDefaultButton(btn_load) # [FIX] Default to Keep Settings to avoid accidental wipes
            msg.setIcon(QMessageBox.Question)
            msg.setStyleSheet("QMessageBox { background-color: #2b2b2b; } QLabel { color: white; } QPushButton { min-width: 140px; padding: 5px; }")
            
            msg.exec()
            
            # [FIX] Always stamp version before proceeding to prevent repeated prompts
            # This ensures that even if we do a Factory Reset later, the reboot knows it's current.
            self.settings.stamp_version()
            
            if msg.clickedButton() == btn_reset:
                debug_log("User chose Factory Reset on version upgrade.")
                self.do_factory_reset(silent=True)
                return 
            else:
                debug_log("User chose to keep settings on version upgrade.")
                # Already stamped above

    # Removed duplicated do_factory_reset to use consolidated version below.


    def update_dashboard_badge(self):
        """Adds a visual indicator to Dashboard button if background tasks exist."""
        try:
            # Count background tasks (exclude manual if not mirrored, but mirrored is default now)
            bg_tasks = 0
            # A simple way is to check the number of active workers
            active_count = len(self.workers)
            
            # Or check auto_task_list
            if hasattr(self, 'auto_task_list'):
                import shiboken6
                for i in range(self.auto_task_list.count()):
                    item = self.auto_task_list.item(i)
                    widget = self.auto_task_list.itemWidget(item)
                    if widget and shiboken6.isValid(widget) and widget.state == "running":
                        bg_tasks += 1
            
            if bg_tasks > 0:
                self.btn_dash.setText(f"📊  Dashboard ({bg_tasks})")
                self.btn_dash.setStyleSheet("color: #ff5252; font-weight: bold;")
            else:
                self.btn_dash.setText("📊  Dashboard")
                self.btn_dash.setStyleSheet("")
        except: pass

    # [REMOVED] Redundant definition of save_global_settings_ui (the primary one is at line 4551)

    def save_settings(self):
        if getattr(self, 'loading', False) or getattr(self, '_reset_in_progress', False):
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
        
        if hasattr(self, 'player'):
            self.settings.set("vu_offset", self.player.get_vu_offset())
            
        self.settings.save()
        
        # [NEW] Sync latest settings (especially watch folders) to background cluster worker
        if hasattr(self, 'cluster_mgr'):
            snapshot = {
                "watch_folders": self.settings.get("watch_folders", []),
                "cluster_sync_tasks": self.settings.get("cluster_sync_tasks", True),
                "cluster_role": self.settings.get("cluster_role", "Worker")
            }
            self.cluster_mgr.update_worker_settings(snapshot)
            
        # [NEW] Save Deleted Tasks History
        if hasattr(self, 'deleted_cluster_tasks'):
             self.settings.set("deleted_tasks", list(self.deleted_cluster_tasks))
        
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

    # [DELETED] Consolidated duplicate closeEvent into the one at the end of the file

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
            path.moveTo(m + 1, m)
            path.lineTo(m + s, m + s/2)
            path.lineTo(m + 1, m + s)
            path.closeSubpath()
            painter.drawPath(path)
            
        elif shape == "folder":
            painter.setBrush(Qt.NoBrush) 
            painter.setPen(QPen(QColor(color), 1.8))
            # Clean Outline Folder (Reference Style)
            path = QPainterPath()
            path.moveTo(m, m + 4)
            path.lineTo(m + s*0.4, m + 4)
            path.lineTo(m + s*0.5, m + 8)
            path.lineTo(m + s, m + 8)
            path.lineTo(m + s, m + s)
            path.lineTo(m, m + s)
            path.closeSubpath()
            painter.drawPath(path)
        elif shape == "refresh":
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(color), 2.5))
            rect = QRectF(m, m, s, s)
            painter.drawArc(rect, 30 * 16, 300 * 16) 
            painter.drawLine(int(m+s-4), int(m+s/2), int(m+s), int(m+s/2+4))
            
        elif shape == "stop":
            painter.setBrush(QColor(color))
            painter.setPen(Qt.NoPen)
            pad = 2 # Reduced Padding
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

        elif shape == "save":
             painter.setBrush(Qt.NoBrush)
             painter.setPen(QPen(QColor(color), 1.8))
             # Very Clean Outline Diskette
             painter.drawRoundedRect(m, m, s, s, 1, 1)
             painter.drawRect(int(m + s*0.3), m, int(s*0.4), int(s*0.3))
             painter.drawRoundedRect(int(m + s*0.2), int(m + s*0.6), int(s*0.6), int(s*0.4), 1, 1)
             
        elif shape == "edit":
            # Concise Line-Art Pencil (Outline) - Larger & Bolder
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(color), 2.2))
            path = QPainterPath()
            # Body outline
            path.moveTo(m + 1, m + s - 4)
            path.lineTo(m + s - 4, m + 1)
            path.lineTo(m + s - 1, m + 4)
            path.lineTo(m + 4, m + s - 1)
            path.closeSubpath()
            # Tip (connected)
            path.moveTo(m + 1, m + s - 4)
            path.lineTo(m, m + s)
            path.lineTo(m + 4, m + s - 1)
            painter.drawPath(path)
        elif shape == "delete":
            # Large X
            painter.setPen(QPen(QColor(color), 2.5))
            painter.drawLine(m+4, m+4, m+s-4, m+s-4)
            painter.drawLine(m+s-4, m+4, m+4, m+s-4)
            
        painter.end()
        return QIcon(pixmap)

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
            self.btn_home.setIconSize(QSize(36, 36))
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
        self.trans_layout = QVBoxLayout(self.transcoder_page)
        self.trans_layout.setContentsMargins(10, 10, 10, 10)
        self.stack.addWidget(self.transcoder_page)

        # --- Page 1: Watch Folders ---
        self.watch_page = QWidget()
        w_layout = QVBoxLayout(self.watch_page)
        w_title = QLabel("📂 監控資料夾設定 (Watch Folder Settings)")
        w_title.setStyleSheet("font-size: 20px; font-weight: bold; color: #4CAF50;")
        w_layout.addWidget(w_title)

        # Global Engine Status
        self.lbl_watch_GlobalStatus = QLabel("核心引擎狀態: 運行中 (Running)")
        self.lbl_watch_GlobalStatus.setStyleSheet("color: #666; font-size: 12px; margin-bottom: 10px;")
        w_layout.addWidget(self.lbl_watch_GlobalStatus)
        
        self.watch_list = QListWidget()
        self.watch_list.setStyleSheet("""
            QListWidget { background-color: #2b2b2b; color: #e0e0e0; border: 1px solid #444; }
            QListWidget::item { border-bottom: 1px solid #333; }
            QListWidget::item:selected { background-color: transparent; }
        """)
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

        # [NEW] Role Selection
        role_frame = QFrame()
        role_layout = QHBoxLayout(role_frame)
        role_layout.addWidget(QLabel("集群運行角色 (Cluster Role):"))
        self.combo_role = QComboBox()
        self.combo_role.addItems(["主監控節點 (Master - Scans Folder)", "工作計算節點 (Worker - Just Transcode)"])
        role_val = self.settings.get("cluster_role", "Master")
        self.combo_role.setCurrentIndex(0 if role_val == "Master" else 1)
        role_layout.addWidget(self.combo_role)
        
        # [NEW] Path Display
        self.lbl_cluster_path = QLabel(f"Path: {self.cluster_mgr._cluster_path}")
        self.lbl_cluster_path.setStyleSheet("color: #777; font-size: 10px; margin-left: 10px;")
        role_layout.addWidget(self.lbl_cluster_path)
        
        role_layout.addStretch()
        cl_layout.addWidget(role_frame)

        self.node_list = QListWidget()
        cl_layout.addWidget(self.node_list, 1) # Expand
        self.lbl_node_info = QLabel("本機辨識碼 (Local Node): -")
        self.lbl_node_info.setStyleSheet("color: #888; font-family: monospace;")
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
        
        # 1. Cluster Path
        s_form.addWidget(QLabel("集群同步路徑 (Cluster Sync Path):"), 0, 0)
        cl_path_layout = QHBoxLayout()
        self.edit_cluster_path = QLineEdit()
        self.edit_cluster_path.setText(self.settings.get("cluster_path", ""))
        cl_path_layout.addWidget(self.edit_cluster_path)
        btn_browse_cl = QPushButton("瀏覽 (Browse)")
        btn_browse_cl.clicked.connect(self.browse_cluster_path)
        cl_path_layout.addWidget(btn_browse_cl)
        s_form.addLayout(cl_path_layout, 0, 1)
        
        # [NEW] Parallel Tasks Control
        s_form.addWidget(QLabel("最大同時轉碼數量 (Max Parallel):"), 1, 0)
        parallel_layout = QHBoxLayout()
        self.spin_parallel = QSpinBox()
        self.spin_parallel.setRange(1, 16)
        suggested = self.settings.get("max_parallel_tasks", 1)
        self.spin_parallel.setValue(suggested)
        parallel_layout.addWidget(self.spin_parallel)
        
        btn_suggest = QPushButton("硬體建議 (Suggest)")
        btn_suggest.clicked.connect(self.apply_recommended_concurrency)
        parallel_layout.addWidget(btn_suggest)
        s_form.addLayout(parallel_layout, 1, 1)
        
        # [NEW] Node Alias
        s_form.addWidget(QLabel("節點名稱 (Node Alias):"), 2, 0)
        self.edit_node_alias = QLineEdit()
        self.edit_node_alias.setPlaceholderText("例如: MASTER-PC, WORKER-01")
        self.edit_node_alias.setText(self.settings.get("worker_alias", ""))
        s_form.addWidget(self.edit_node_alias, 2, 1)

        # [NEW] Worker Specific Output Path
        s_form.addWidget(QLabel("監控任務專用輸出路徑 (Worker Output):"), 3, 0)
        worker_out_layout = QHBoxLayout()
        self.edit_worker_out = QLineEdit()
        self.edit_worker_out.setPlaceholderText("留空則預設存於源檔目錄 (Default: Same as source)")
        self.edit_worker_out.setText(self.settings.get("worker_output_path", ""))
        worker_out_layout.addWidget(self.edit_worker_out)
        btn_browse_worker_out = QPushButton("瀏覽 (Browse)")
        btn_browse_worker_out.clicked.connect(self.browse_worker_output_path)
        worker_out_layout.addWidget(btn_browse_worker_out)
        s_form.addLayout(worker_out_layout, 3, 1)
        
        btn_save_s = QPushButton("儲存設定 (Save)")
        btn_save_s.setFixedHeight(40)
        btn_save_s.setIcon(self.create_geometric_icon("save", "#ffffff", size=32))
        btn_save_s.clicked.connect(self.save_global_settings_ui)
        s_layout.addLayout(s_form)
        
        maint_layout = QHBoxLayout()
        btn_reset = QPushButton("⚠️ 原廠預設 (Reset to Default)")
        btn_reset.setStyleSheet("background-color: #631212; color: #ffcccc; border: 1px solid #821414;")
        btn_reset.clicked.connect(self.do_factory_reset)
        maint_layout.addWidget(btn_reset)

        maint_layout.addStretch()
        
        s_layout.addLayout(maint_layout)
        s_layout.addWidget(btn_save_s)
        s_layout.addStretch()
        self.stack.addWidget(self.settings_page)
        
        # --- Page 4: Dashboard (Automated Tasks) ---
        self.dashboard_page = QWidget()
        self.dashboard_layout = QVBoxLayout(self.dashboard_page) 
        self.dashboard_layout.setContentsMargins(10, 10, 10, 10)
        
        # Title Row
        db_title_layout = QHBoxLayout()
        db_title = QLabel("📊 自動化監控任務隊列 (Automated Task Queue)")
        db_title.setStyleSheet("font-size: 20px; font-weight: bold; color: #4CAF50;")
        db_title_layout.addWidget(db_title)
        db_title_layout.addStretch()
        
        self.btn_clear_dash = QPushButton("🗑️ 清除完成 (Clear)")
        self.btn_clear_dash.setFixedWidth(110) # [v27.10.46] Compact
        self.btn_clear_dash.clicked.connect(self.clear_dashboard_finished)
        db_title_layout.addWidget(self.btn_clear_dash)
        self.dashboard_layout.addLayout(db_title_layout)
        
        # Active Queue Section
        self.dashboard_layout.addWidget(QLabel("運行中任務 (Active Queue)"))
        
        # Header Row (Copied from manual list for consistency)
        db_header_frame = QFrame()
        db_header_frame.setFixedHeight(30)
        # [v27.10.46] Force NoFrame and flat background to match row exactly
        db_header_frame.setFrameShape(QFrame.NoFrame)
        db_header_frame.setLineWidth(0)
        db_header_frame.setStyleSheet("background-color: #333; border: none; border-bottom: 1px solid #444;")
        dbh_layout = QHBoxLayout(db_header_frame)
        dbh_layout.setContentsMargins(5, 0, 5, 0)
        dbh_layout.setSpacing(10)

        col_lbls = [
            ("任務名稱", TaskProgressWidget.WIDTHS["name"]), 
            ("狀態", TaskProgressWidget.WIDTHS["status"]), 
            ("格式資訊", TaskProgressWidget.WIDTHS["fmt"]), 
            ("轉碼起始", TaskProgressWidget.WIDTHS["start"]), 
            ("效能", TaskProgressWidget.WIDTHS["perf"]), 
            ("來源", TaskProgressWidget.WIDTHS["src"]), 
            ("節點別名", TaskProgressWidget.WIDTHS["node"]), 
            ("進度", TaskProgressWidget.WIDTHS["prog"]), 
            ("完成時間", TaskProgressWidget.WIDTHS["fin"]), 
            ("操作", TaskProgressWidget.WIDTHS["act"])
        ]
        for txt, w in col_lbls:
            l = QLabel(txt)
            l.setFixedWidth(w)
            l.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            l.setStyleSheet("color: #888; font-size: 11px; font-weight: bold;")
            dbh_layout.addWidget(l)
        self.dashboard_layout.addWidget(db_header_frame)
        
        self.auto_task_list = QListWidget()
        self.auto_task_list.setSpacing(0) # [v27.10.43] Ensure no gaps between rows
        self.auto_task_list.setAlternatingRowColors(True)
        self.auto_task_list.setStyleSheet("""
            QListWidget { background-color: #2b2b2b; border: 1px solid #333; border-radius: 4px; }
            QListWidget::item { border-bottom: 1px solid #333; padding: 0px; }
        """)
        self.dashboard_layout.addWidget(self.auto_task_list, 2)
        
        # History Section — Live Watch Log
        log_header = QHBoxLayout()
        lbl_log_title = QLabel("📋 監控日誌 (Watch Log)")
        lbl_log_title.setStyleSheet("font-size: 13px; font-weight: bold; color: #4CAF50;")
        log_header.addWidget(lbl_log_title)
        log_header.addStretch()
        btn_clear_log = QPushButton("清除 (Clear)")
        btn_clear_log.setFixedWidth(80)
        btn_clear_log.setStyleSheet("QPushButton { background:#333; color:#888; border:1px solid #555; border-radius:3px; font-size:11px; } QPushButton:hover { color:#fff; }")
        log_header.addWidget(btn_clear_log)
        self.dashboard_layout.addLayout(log_header)

        self.watch_log = QPlainTextEdit()
        self.watch_log.setReadOnly(True)
        self.watch_log.setMaximumBlockCount(500)  # Keep last 500 lines
        self.watch_log.setStyleSheet("""
            QPlainTextEdit {
                background-color: #1a1a1a;
                color: #a8d8a8;
                border: 1px solid #333;
                border-radius: 4px;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 12px;
                padding: 4px;
            }
        """)
        btn_clear_log.clicked.connect(self.watch_log.clear)
        self.dashboard_layout.addWidget(self.watch_log, 1)

        
        self.stack.addWidget(self.dashboard_page)

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
        self.params_panel = QFrame()
        self.params_panel.setFixedWidth(460) # Reduced from 600 to prevent overflow
        self.params_layout = QVBoxLayout(self.params_panel) # Made self.params_layout
        self.params_layout.setSpacing(5)
        self.params_layout.setContentsMargins(5, 5, 5, 5)
        
        upper_layout.addWidget(self.params_panel, 0)
        
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
        self.params_layout.addWidget(lbl_source_header)
        self.params_layout.addWidget(source_container)
        
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
        
        self.params_layout.addWidget(QLabel("影片詳細資訊 (Detail Info)"))
        self.params_layout.addWidget(meta_group)
        
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
        self.params_layout.addLayout(pl_ctrl)
        
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
        self.params_layout.addWidget(self.playlist, 1)
        
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
        
        self.params_layout.addLayout(target_header_layout)
        
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
        
        self.params_layout.addWidget(target_group)

        # Output Settings
        lbl_out_header = QLabel("輸出設定 (Output)")
        lbl_out_header.setStyleSheet(HEADER_STYLE)
        self.params_layout.addWidget(lbl_out_header)
        
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
        
        self.params_layout.addLayout(out_layout)
        
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
        
        self.params_layout.addLayout(name_layout)
        
        # Actions - REMOVED per user request
        # self.btn_add_task = QPushButton("加入任務 (Queue)")
        # ...
        # self.btn_execute = QPushButton("開始轉碼 (START)")
        # ...
        
        # Settings Path Info
        settings_path_lbl = QLabel(f"設定存於: {os.path.abspath('settings.json')}")
        settings_path_lbl.setStyleSheet("color: #555; font-size: 10px; margin-top: 20px;")
        self.params_layout.addWidget(settings_path_lbl)
        
        self.params_layout.addStretch()
        
        upper_layout.addWidget(self.params_panel)
        content_splitter.addWidget(upper_widget)
        
        # Lower: Task Monitor
        monitor_widget = QFrame()
        mon_layout = QVBoxLayout(monitor_widget)
        mon_layout.setContentsMargins(5,5,5,5)
        
        # Header Row
        mon_header = QHBoxLayout()
        lbl_q_title = QLabel("手動任務隊列 (Manual Task Queue)")
        lbl_q_title.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lbl_q_title.setStyleSheet("font-weight: bold; padding-left: 10px;")
        mon_header.addWidget(lbl_q_title)
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
        
        # Header Row for Task List (Header Frame)
        db_header = QFrame()
        db_header.setFixedHeight(30)
        db_header.setStyleSheet("background-color: #333; border-bottom: 1px solid #444;")
        dbh_layout = QHBoxLayout(db_header)
        dbh_layout.setContentsMargins(5, 0, 5, 0)
        dbh_layout.setSpacing(10)

        col_lbls = [
            ("任務名稱", TaskProgressWidget.WIDTHS["name"]), 
            ("狀態", TaskProgressWidget.WIDTHS["status"]), 
            ("格式資訊", TaskProgressWidget.WIDTHS["fmt"]), 
            ("轉碼起始", TaskProgressWidget.WIDTHS["start"]), 
            ("效能", TaskProgressWidget.WIDTHS["perf"]), 
            ("來源", TaskProgressWidget.WIDTHS["src"]), 
            ("節點別名", TaskProgressWidget.WIDTHS["node"]), 
            ("進度", TaskProgressWidget.WIDTHS["prog"]), 
            ("完成時間", TaskProgressWidget.WIDTHS["fin"]), 
            ("操作", TaskProgressWidget.WIDTHS["act"])
        ]
        for txt, w in col_lbls:
            l = QLabel(txt)
            l.setFixedWidth(w)
            l.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            l.setStyleSheet("color: #888; font-size: 11px; font-weight: bold;")
            dbh_layout.addWidget(l)

        mon_layout.addWidget(db_header)

        # UNIFIED TASK LIST (Replaces manual_task_list and auto_task_list)
        self.task_list = QListWidget()
        mon_layout.addWidget(self.task_list)
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
        
        # [v27.10.38] Reverted pointer unification to fix Dashboard visibility
        # manual_task_list is the widget at the bottom of Home page.
        # auto_task_list is the widget on the Dashboard page (initialized at line 2189).
        self.manual_task_list = self.task_list
        # self.auto_task_list = self.task_list # [REMOVED] This caused the Dashboard widget to go orphan

        # [REMOVED] History & Logs Section as per user request (Space optimization)
        
        content_splitter.addWidget(monitor_widget)
        
        self.trans_layout.addWidget(content_splitter)

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
                        worker.wait(100) # Give it 200ms to die
                  worker.deleteLater()
                  del self.workers[widget_to_remove]
                  
                  # [NEW] Check if we need to fill the slot
                  QTimer.singleShot(500, self.process_next_task)
                  
             elif getattr(self, 'current_running_task', None) and self.current_running_task.get("widget") == widget_to_remove:
                   # Fallback if somehow not in workers dict but marked as active
                   self.clean_up_after_task()
                   
        # [FIX] Cluster Ghost Task: Cancel on Cluster
        # If this is a Cluster Task (from another node), removing it locally should stop it re-appearing.
        # We delete the JSON file from the shared folder.
        task_data = getattr(widget_to_remove, 'task_data', {})
        
        # [v27.10.2] PERSIST DELETION: Mark as cleared to prevent reappearance on restart
        if task_data:
            task_id = self.get_task_identifier(task_data)
            self.mark_task_as_cleared(task_id)
            
        worker_id = task_data.get("worker_id", "-")
        source_type = task_data.get("source_type", "")
        
        if worker_id == "Cluster" or "Node:" in source_type:
            base_name = task_data.get("base_name")
            if base_name:
                # [FIX] In-Memory Filter: Ensure it doesn't come back this session
                # Use composite key to allow re-adding same-named tasks (different timestamp)
                b_time = task_data.get("broadcast_time", "0")
                unique_key = f"{base_name}|{b_time}"
                
                self.deleted_cluster_tasks.add(unique_key)
                
                if hasattr(self, 'cluster_mgr'):
                    self.cluster_mgr.delete_cluster_task(base_name)
                    debug_log(f"Removed Cluster Task and deleted backend file: {base_name} (Key: {unique_key})")
                
                # [FIX] Force Save immediately to persist the ban list
                self.save_settings()
        
        # [v27.10.6.2] SYNC DISMISSAL: Also add to dashboard dismissal list
        source = task_data.get("source") or task_data.get("source_path")
        if source:
             norm_s = os.path.normpath(source).lower()
             dismissed = self.settings.get("dismissed_dashboard_items", [])
             if not any(os.path.normpath(d).lower() == norm_s for d in dismissed):
                 dismissed.append(source)
                 self.settings.set("dismissed_dashboard_items", dismissed)
                 self.settings.save()
                 
        # 2. Check Pending Queue (Legacy List - Keep for safety but Queue Pump uses UI)
        for i, task in enumerate(self.pending_tasks):
            if task.get("widget") == widget_to_remove:
                print(f"DEBUG: Removed pending task index {i}")
                self.pending_tasks.pop(i)
                break
                
        # 3. Remove from UI List (Check BOTH)
        def remove_from_list(list_widget):
            for i in range(list_widget.count()):
                item = list_widget.item(i)
                if list_widget.itemWidget(item) == widget_to_remove:
                    list_widget.takeItem(i)
                    print(f"DEBUG: Removed UI item at row {i}")
                    return True
            return False

        if not remove_from_list(self.manual_task_list):
            remove_from_list(self.auto_task_list)
            
        # [NEW] Remove Mirror Widget (if exists)
        task_data = getattr(widget_to_remove, 'task_data', {})
        mirror_item = task_data.get("mirror_item")
        if mirror_item:
             # It's in auto_task_list
             row = self.auto_task_list.row(mirror_item)
             if row >= 0:
                 self.auto_task_list.takeItem(row)

        # 4. Check Queue State
        if not self.workers and not self.pending_tasks: # Simple check
             self.btn_start_all.setEnabled(True) 

    def clear_dashboard_finished(self):
        # [v27.10.22] Full Revert logic: Point to main clearer
        self.clear_task_list(sender=self.btn_clear_dash)

        # [v27.10.55] NUCLEAR CLEAR: Also wipe physical cluster task files
        # to prevent ghost tasks from reloading on next restart.
        try:
            if hasattr(self, 'cluster_mgr'):
                task_dir = os.path.join(self.cluster_mgr._cluster_path, "tasks")
                if os.path.exists(task_dir):
                    cleared_count = 0
                    for f in os.listdir(task_dir):
                        if f.endswith(".json") or f.endswith(".lock"):
                            try:
                                os.remove(os.path.join(task_dir, f))
                                cleared_count += 1
                            except Exception as e:
                                debug_log(f"Clear: Failed to remove cluster task {f}: {e}")
                    debug_log(f"[v27.10.55] Nuclear clear: removed {cleared_count} cluster files from {task_dir}")
        except Exception as e:
            debug_log(f"[v27.10.55] Nuclear clear error: {e}")

        # [v27.10.55] Reset watch_folder history so files can be detected fresh
        try:
            if hasattr(self, 'watch_engine') and self.watch_engine:
                self.watch_engine.processed_files = {}
                self.watch_engine._seen_this_session = set()
                self.watch_engine.save_history()
                debug_log("[v27.10.55] Watch folder history cleared after dashboard clear.")
        except Exception as e:
            debug_log(f"[v27.10.55] Watch history reset error: {e}")

    def clear_task_list(self, sender=None):
        # [v27.7] Dynamic Clearing: Stage 1 (Success) -> Stage 2 (All)
        # Use provided sender or default to the manual clear button
        btn = sender if sender else self.btn_clear_list
        if not btn: return
        
        current_text = btn.text()
        clear_all_mode = "清除所有" in current_text
        
        dismissed = self.settings.get("dismissed_dashboard_items", [])
        if not isinstance(dismissed, list): dismissed = []
        
        # Track if anything was actually removed
        removed_count = 0
        
        def do_purge(list_widget, force_all=False):
            nonlocal removed_count
            if not list_widget: return
            
            # Success statuses only for Stage 1
            success_statuses = ["Done", "完成 (Done)", "Completed"]
            
            for i in range(list_widget.count() - 1, -1, -1):
                item = list_widget.item(i)
                widget = list_widget.itemWidget(item)
                if not widget: continue
                
                status_text = widget.lbl_status.text().strip()
                is_success = any(s in status_text for s in success_statuses)
                
                # Eligibility: It's success OR we are in "Clear All" mode
                if is_success or force_all:
                    # Collect for dismissal before removing
                    if hasattr(widget, 'task_data') and widget.task_data:
                        path = widget.task_data.get("source") or widget.task_data.get("source_path")
                        if path:
                            norm_p = os.path.normpath(path).lower()
                            if not any(os.path.normpath(d).lower() == norm_p for d in dismissed):
                                dismissed.append(path)
                                # [v27.7] Also dismiss potential _DONE/_ERROR variants
                                base = os.path.basename(path)
                                root = os.path.dirname(path)
                                dismissed.append(os.path.join(root, "_DONE", base))
                                dismissed.append(os.path.join(root, "_ERROR", base))

                        # Clean up from internal lists
                        if widget.task_data in self.pending_tasks:
                            self.pending_tasks.remove(widget.task_data)
                        
                        base_name = widget.task_data.get("base_name")
                        cluster_fn = widget.task_data.get("cluster_filename")
                        if base_name and hasattr(self, 'cluster_mgr'):
                            self.cluster_mgr.delete_cluster_task(base_name, cluster_filename=cluster_fn)
                            
                        # [v27.10.2] PERSIST DELETION for Bulk Clear
                        task_id = self.get_task_identifier(widget.task_data)
                        self.mark_task_as_cleared(task_id)

                    list_widget.takeItem(i)
                    removed_count += 1

        # Execution
        do_purge(self.manual_task_list, force_all=clear_all_mode)
        do_purge(self.auto_task_list, force_all=clear_all_mode)
        if hasattr(self, 'dashboard_history_list'):
            do_purge(self.dashboard_history_list, force_all=clear_all_mode)

        # [NEW v27.10.6.8] Aggressive Physical Cleanup for residues
        # If we cleared everything in Stage 2, ensure the CLUSTER_SYNC/tasks is empty
        if clear_all_mode and hasattr(self, 'cluster_mgr'):
            try:
                task_dir = os.path.join(self.cluster_mgr._cluster_path, "tasks")
                if os.path.exists(task_dir):
                    import shutil
                    for f in os.listdir(task_dir):
                        if f.endswith(".json") or f.endswith(".lock"):
                             try: os.remove(os.path.join(task_dir, f))
                             except: pass
                    debug_log("Stage 2 Clear: Wiped all residue task/lock files from cluster storage.")
            except Exception as e:
                debug_log(f"Stage 2 Clear residue error: {e}")

        # Update Settings
        self.settings.set("dismissed_dashboard_items", dismissed[-800:])
        self.settings.set("source_history", [])
        self.settings.set("output_history", [])
        self.save_pending_tasks()
        
        # Button State Transition
        total_remaining = self.manual_task_list.count() + self.auto_task_list.count()
        if hasattr(self, 'dashboard_history_list'):
            total_remaining += self.dashboard_history_list.count()
            
        if total_remaining > 0 and not clear_all_mode:
            # Transition to Stage 2
            btn.setText(" 清除所有 (Clear All)")
            if isinstance(btn, QToolButton):
                btn.setStyleSheet("""
                    QToolButton { background-color: #d32f2f; color: white; border: 1px solid #ff5252; border-radius: 4px; font-size: 11px; padding: 0 10px; }
                    QToolButton:hover { background-color: #f44336; }
                """)
            else:
                btn.setStyleSheet("background-color: #d32f2f; color: white; border-radius: 4px;")
        else:
            # Reset to Stage 1
            btn.setText(" 清除已完成 (Clear Finished)")
            if isinstance(btn, QToolButton):
                btn.setStyleSheet("""
                    QToolButton { background-color: transparent; color: #aaa; border: 1px solid #555; border-radius: 4px; font-size: 11px; padding: 0 10px; }
                    QToolButton:hover { background-color: #d32f2f; color: white; border-color: #ff5252; }
                    QToolButton:pressed { background-color: #b71c1c; }
                """)
            else:
                btn.setStyleSheet("") # Default PushButton style

        # If nothing running, ensure button state
        if not self.is_processing:
            if self.manual_task_list.count() == 0 and self.auto_task_list.count() == 0:
                 self.btn_start_all.setEnabled(True) 
            else:
                 self.btn_start_all.setEnabled(bool(self.pending_tasks))
        self.update_history_menus() # Sync UI menus
        debug_log("Cleared source_history and output_history from settings.")

    def reset_clear_button(self):
        """Helper to reset all clear buttons to Stage 1 (Clear Finished) state."""
        for btn in [getattr(self, 'btn_clear_list', None), getattr(self, 'btn_clear_dash', None)]:
            if not btn: continue
            btn.setText(" 清除已完成 (Clear Finished)")
            if isinstance(btn, QToolButton):
                btn.setStyleSheet("""
                    QToolButton { background-color: transparent; color: #aaa; border: 1px solid #555; border-radius: 4px; font-size: 11px; padding: 0 10px; }
                    QToolButton:hover { background-color: #d32f2f; color: white; border-color: #ff5252; }
                    QToolButton:pressed { background-color: #b71c1c; }
                """)
            else:
                btn.setStyleSheet("")

    def add_task_to_queue(self, source_path=None, full_duration=False, source_type="Manual", worker_id="Auto", preset_name=None, base_name_override=None, extra_data=None, skip_queue=False):
        """
        Main entry point to add a task (Manual or Auto).
        """
        print(f"DEBUG: add_task_to_queue triggered. Source: {source_type}")

        # 1. Determine Source
        if source_path:
            source = source_path
            # [NEW v27.4] If we are adding a file back MANUALLY, un-dismiss it from Dashboard
            if source_type == "Manual":
                dismissed = self.settings.get("dismissed_dashboard_items", [])
                norm_s = os.path.normpath(source).lower()
                if any(os.path.normpath(d).lower() == norm_s for d in dismissed):
                    new_dismissed = [d for d in dismissed if os.path.normpath(d).lower() != norm_s]
                    self.settings.set("dismissed_dashboard_items", new_dismissed)
        else:
            if not self.current_source:
                if source_type == "Manual":
                    QMessageBox.warning(self, "無來源 (No Source)", "請先載入或播放一個影片檔案 (Please load a video first)")
                return
            source = self.current_source
            
        # [v27.7.2] Strict Rejection Guard for Automated Sources
        # [v27.10.36] Prefix check now matches 'Watch' or 'WatchFolder' or 'Watch:'
        if source_type != "Manual" and not str(source_type).startswith("Watch"):
            dismissed = self.settings.get("dismissed_dashboard_items", [])
            norm_s = os.path.normpath(source).lower()
            if any(os.path.normpath(d).lower() == norm_s for d in dismissed):
                return
            
        # [FIX] Removed premature file existence check
        # os.path.exists() fails for some UNC paths even when files exist
        # Let probe in run_transcode handle file validation instead
            
        # 2. Base Name
        # Capture current naming
        if base_name_override:
            base_name = base_name_override
        elif self.chk_rename.isChecked() and self.edit_base_name.text().strip() and source_type == "Manual":
            base_name = self.edit_base_name.text().strip()
        else:
            base_name = os.path.splitext(os.path.basename(source))[0]
            
        # 3. Gather Settings (Target)
        task_data = {
            "source": source,
            "base_name": base_name,
            "source_type": source_type,
            "worker_id": worker_id,
            "status": "Pending",
            "progress": 0,
            "timestamp": time.time()
        }
        
        # Copy global settings
        # [NEW] Visual Separation for Automated Tasks
        display_name_prefix = ""
        if source_type != "Manual":
             # Auto-Naming distinction
             # Don't change base_name (filename), just the reference? 
             # Actually, let's keep filename clean, but we can set a visual tag in the Widget.
             pass
             
        if source_type == "Manual":
             final_base = f"{base_name}_{self.spin_seq.value():03d}"
        else:
             final_base = base_name # [FIX v27.10.32] Correct Variable Name to avoid NameError
        
        if source_type != "Manual":
             # For WatchFolder, maybe keep original name? 
             # User Request: "Sequencing" might confuse automation. 
             # Let's keep sequence for safety to avoid overwrite, but maybe append [AUTO] to display?
             # Let's stick to sequence for safety to avoid overwrite, but maybe append [AUTO] to display?
             pass
        
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

        try:
            task_size = os.path.getsize(source) if source and os.path.exists(source) else 0
            task_mtime = os.path.getmtime(source) if source and os.path.exists(source) else 0
        except:
            task_size = 0
            task_mtime = 0

        # Prepare params
        task = {
            "source": source,
            "in_point": in_p,
            "out_point": out_p,
            "base_name": final_base,
            "output_dir": getattr(self, 'output_dir', os.path.dirname(source)),
            "sequence": self.spin_seq.value(),
            "bitrate": self.edit_bitrate.text(),
            "container": self.combo_container.currentText(),
            "vcodec": self.combo_vcodec.currentText(),
            "audio_gain": 'auto' if self.btn_auto_gain.isChecked() else self.spin_gain.value(),
            "resolution": getattr(self, 'current_preset_extra', {}).get("resolution") or "-", # Fallback
            "fps": getattr(self, 'current_preset_extra', {}).get("fps"),
            "fps_text": self.combo_fps.currentText() if hasattr(self, 'combo_fps') else None,
            "audio_ch": getattr(self, 'current_preset_extra', {}).get("audio_ch"),
            "acodec": self.combo_acode.currentText(),
            "source_type": source_type,
            "worker_id": worker_id,
            "size": task_size,
            "mtime": task_mtime
        }
        
        # [FIX] Merge Extra Data (e.g. broadcast_time from Cluster)
        if extra_data:
            task.update(extra_data)

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
        # [RESTORED] Select List Widget based on Source Type (these widgets DO exist)
        target_list = self.manual_task_list
        if source_type != "Manual":
            target_list = self.auto_task_list
            
        # [FIX] Deduplication Check
        # User Complaint: "Duplicate tasks" (one from Watch, one from Cluster)
        # Check if SAME FILE (Path + Name) already exists in the target list
        duplicate_found = False
        
        # Normalize current source for comparison
        current_source_norm = os.path.normpath(source).lower() if source else ""
        
        for i in range(target_list.count()):
             it = target_list.item(i)
             w = target_list.itemWidget(it)
             if w:
                 w_data = getattr(w, 'task_data', {})
                 existing_source = w_data.get("source", "")
                 existing_source_norm = os.path.normpath(existing_source).lower() if existing_source else ""
                 
                 # [v27.10.63] User Requirement: "No coexistence of timestamped and original for same source"
                 if existing_source_norm == current_source_norm:
                     print(f"DEBUG_UI: add_task_to_queue Blocked coexistence for {final_base} on existing source.")
                     duplicate_found = True
                     break
                 
                 # Fallback: Base Name check
                 if w_data.get("base_name") == final_base:
                     pass
        
        if duplicate_found:
             debug_log(f"add_task_to_queue: Ignored duplicate task: {final_base}")
             return

        item = QListWidgetItem(target_list)
        item.setSizeHint(QSize(0, 36)) # Reduced height        
        # [NEW] Visual Distinction
        display_label = final_base
        # [FIX] Removed [監控] prefix to match cluster naming and prevent confusion
        # if source_type != "Manual":
        #    display_label = f"[監控] {final_base}"
            
        widget = TaskProgressWidget(display_label)
        
        # [NEW] Pre-populate Claiming Node Visibility
        claimed_by = extra_data.get("claimed_by") if extra_data else None
        if claimed_by:
            # [v27.10.56] Look up alias from node_aliases map (populated by cluster heartbeat)
            display_node = self.node_aliases.get(claimed_by, claimed_by.split('-')[0])
            widget.lbl_node.setText(display_node)
            widget.lbl_node.setStyleSheet("color: #BB86FC; font-weight: bold;")
            widget.lbl_status.setText("Claimed")
            widget.lbl_status.setStyleSheet("color: #BB86FC; font-weight: bold;")
        
        # Apply different color for Auto Tasks
        if source_type != "Manual":
             widget.setStyleSheet("QFrame#TaskRow { background-color: #203020; } QLabel { color: #cfd8dc; }")
             
        widget.set_task_data(task) # Store Data & Set Tooltip
        # [v27.10.73] Dynamic Initial Status (Respect probing/claimed state)
        # set_task_data now handles lbl_status.setText(s)
        widget.removed.connect(self.remove_task_by_widget) # CONNECT SIGNAL
        widget.transcode_requested.connect(self.transcode_single_item) 
        widget.pause_requested.connect(self.pause_task)
        widget.resume_requested.connect(self.resume_task)
        widget.stop_requested.connect(self.stop_current_task)
        widget.switch_page_requested.connect(self.show_transcoder_page) # [NEW] Switch to Player Page
        
        target_list.addItem(item)
        target_list.setItemWidget(item, widget)
        task["widget"] = widget # Store widget ref for later
        
        # [RESTORED] Dashboard Mirror for Manual Tasks
        # User request: "Manual add task also appear in DASHBOARD"
        if source_type == "Manual" and hasattr(self, 'auto_task_list'):
            # Add to Auto Task List (Dashboard) as well
            item_mirror = QListWidgetItem(self.auto_task_list)
            item_mirror.setSizeHint(QSize(0, 36))
            
            widget_mirror = TaskProgressWidget(f"[手動鏡像] {final_base}")
            widget_mirror.setStyleSheet("QFrame#TaskRow { background-color: #2b2b2b; } QLabel { color: #e0e0e0; font-style: italic; }")
            widget_mirror.set_task_data(task)
            widget_mirror.lbl_status.setText("Pending")
            
            widget_mirror.removed.connect(lambda w: self.remove_task_by_widget(widget)) # Removing mirror removes real task
            
            self.auto_task_list.addItem(item_mirror)
            self.auto_task_list.setItemWidget(item_mirror, widget_mirror)
            
            # Store mirror ref in task for updates
            task["mirror_widget"] = widget_mirror
            task["mirror_item"] = item_mirror # For removal
        
        # Atomic Add: Only append to queue if successful setup and NOT skipped (e.g. assigned to someone else)
        if not skip_queue:
            self.pending_tasks.append(task)
            self.btn_start_all.setEnabled(True)
        else:
            debug_log(f"add_task_to_queue: Added {final_base} to UI ONLY (assigned to remote node).")
        
        # [NEW] Broadcast to Cluster (Shared Workload)
        # Fix Recursive Loop: ONLY broadcast if it's a fresh LOCAL task.
        # Tasks from 'Remote' or 'Node:xxx' should NOT be re-broadcasted.
        # [FIX v27.9.14] Watch tasks have source_type="Watch:{folder_name}", not "WatchFolder"
        is_watch_task = source_type.startswith("Watch:")
        if source_type == "Manual" or is_watch_task:
             # [FIX v27.10.3] Load Balancing: 
             # If I am Master and this is a Watch Task, DO NOT assign to self immediately.
             # Leave assigned_to=None so ClusterManager can distribute it to best node.
             my_role = self.settings.get("cluster_role", "Worker")
             
             # [v27.10.50] ATOMIC ASSIGNMENT: DO NOT clear assigned_to here!
             # Let broadcast_task() pick the best node immediately and return the assignment.
             # This is the "one-step" solution.
             debug_log(f"Cluster[{my_role}]: Broadcasting {source_type} Task {final_base} for atomic assignment.")
             
             cf = self.cluster_mgr.broadcast_task(task)
             if cf:
                  task["cluster_filename"] = cf
                  # [v27.10.6.1] Mark as locally created to prevent aggressive ghost pruning
                  self.session_created_tasks.add(cf)
        # Increment seq for next manually added task
        self.spin_seq.setValue(self.spin_seq.value() + 1)
        
        # [NEW] Clear dirty status after task is queued
        if hasattr(self, 'player'):
            self.player._is_dirty = False
            self.player.update_trim_labels()
            
        self.save_settings()
        
        # [NEW] Trigger Queue Pump
        # Auto-start if slots available
        self.process_next_task()

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
        widget = self.manual_task_list.itemWidget(item)
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
        if not self.pending_tasks:
            return
        
        self.is_processing = True
        self.btn_start_all.setEnabled(False)
        self.process_next_task()

    def transcode_single_item(self, widget):
        # Even for single run, we check if we have room
        max_parallel = self.settings.get("max_parallel_tasks", 1)
        active_running = [w for w in self.workers.values() if not w.paused]
        
        if len(active_running) >= max_parallel:
            # ... warning ...
            return
            
        task = widget.task_data
        
        # [NEW] Handle Diagnosis on Retry/Manual Start
        # Check if failed OR has error log
        # Robust check: 'state' attr OR status text
        current_status = widget.lbl_status.text()
        is_failed = getattr(widget, 'state', '') == 'failed' or "Failed" in current_status
        has_log = hasattr(widget, 'last_error_log') and widget.last_error_log
        
        if is_failed:
             log = widget.last_error_log if has_log else "No Error Log Available. (Unknown Error)"
             
             # Only show if it's not a cancellation
             if "Cancelled" not in log:
                 suggestion, fix_params = self.analyze_error_suggestion(log)
                 
                 # Force a fix option if none returned (Safety Mode)
                 if not fix_params:
                     # Check for Input Errors specifically
                     if "Error opening input" in log or "Invalid argument" in log:
                         suggestion = "無法開啟輸入檔案。可能是路徑包含特殊字元、檔案被佔用或損壞。\n\n即使重置參數也可能無法解決輸入問題，但您可以嘗試 [安全重置] 作為最後手段。"
                     else:
                         suggestion = (suggestion + "\n\n💡 系統無法識別具體錯誤，但您可以嘗試 [安全重置] (H.264/AAC)。") if suggestion else "未知錯誤。\n\n💡 建議：嘗試 [安全重置]。"
                     
                     fix_params = {"vcodec": "libx264", "acodec": "aac", "container": "mp4"}
                 
                 dlg = SmartFailureDialog(log, suggestion, fix_params, self)
                 if dlg.exec():
                     if dlg.apply_fix and fix_params:
                         # Apply fix to task params
                         for k, v in fix_params.items():
                             task[k] = v
                         widget.last_error_log = "" # Clear after fix applied
                         # Update UI Tag to show it was fixed?
                         widget.lbl_info.setText(f"{task.get('container')} / {task.get('vcodec')} (Reset)")
                         # Reset stats to allow immediate retry logic to pick it up properly
                         widget.lbl_status.setText("Pending") 
                         widget.lbl_status.setStyleSheet("color: #ffa726;")
                         widget.state = "pending"
                 else:
                     # If user rejects the dialog, don't start the transcode
                     return

        # Ensure task is removed from pending list if it was there
        if task in self.pending_tasks:
            self.pending_tasks.remove(task)
            
        self.is_processing = True
        self.btn_start_all.setEnabled(False)
        self.run_transcode(task, single_run=True)

    def process_next_task(self):
        """Fills up available slots up to max_parallel_tasks."""
        # [FIX] Re-entry Guard
        if getattr(self, '_pumping_queue', False):
             return
        self._pumping_queue = True
        
        try:
            max_parallel = self.settings.get("max_parallel_tasks", 1)
            active_count = len(self.workers)
            
            skipped_count = 0
            MAX_SKIPS = 3 # [FIX] Throttle: Only try 3 cluster claims per cycle to prevent UI freeze
            
            while active_count < max_parallel:
                # [NEW] Strict Assignment: Scan for first task assigned to ME
                candidate_task = None
                candidate_index = -1
                
                my_id = self.cluster_mgr.node_id
                my_role = self.settings.get("cluster_role", "Worker")
                
                for i, t in enumerate(self.pending_tasks):
                    # [v27.10.70] Self-Healing: If task status is 'Processing' but NO LOCAL WORKER is running it,
                    # allowed to pick it back up if assigned to me.
                    t_status = t.get("status", "")
                    is_terminal = any(s in t_status for s in ["Done", "完成", "Completed", "Failed", "失敗"])
                    is_active = any(s in t_status for s in ["Processing", "Transcoding", "Running", "進行中"])
                    
                    # [v27.10.72] Skip terminal tasks immediately
                    if is_terminal:
                        continue
                        
                    # Check for existing local worker
                    has_local_worker = False
                    if "widget" in t:
                        has_local_worker = t["widget"] in self.workers
                    
                    cf = t.get("cluster_filename")
                    assigned = t.get("assigned_to") or t.get("claimed_by")
                    
                    # [v27.10.72] Refined Orphan/Double check:
                    # ONLY heal if it's strictly 'Active' and we are missing a local worker.
                    if is_active:
                        if assigned == my_id and not has_local_worker:
                            # [v27.10.72] Completion Race-Condition Protection:
                            # Check if we literally JUST finished this task (cache check)
                            recent_finished = getattr(self, '_recently_finished', {})
                            if t.get('base_name') in recent_finished:
                                # debug_log(f"Self-Healing Suppressed: Task {t.get('base_name')} recently finished.")
                                continue

                            debug_log(f"Self-Healing: Orphan active task assigned to me found: {t.get('base_name')}. Restarting...")
                            # Proceed as candidate
                        else:
                            continue
                    
                    if cf:
                        # Cluster Task: Check Assignment
                        assigned = t.get("assigned_to") or t.get("claimed_by")
                        
                        # [FIX v27.10.68] Master processes ALL tasks when alone OR matching assignment
                        # Priority 1: Tasks assigned to me
                        if assigned == my_id:
                            # [FIX v27.10.5] Explicit Log for assigned startup
                            debug_log(f"Master starting assigned cluster task: {t.get('base_name')}")
                            candidate_task = t
                            candidate_index = i
                            break
                        
                        # Priority 2: Unassigned tasks (Master claims them)
                        if not assigned and my_role == "Master":
                            # [FIX v27.10.31] Universal Master Hogging Fallback
                            # Master should NOT claim ANY unassigned task if workers are available,
                            # regardless of source_type (Manual or Watch).
                            online_non_master = [nid for nid, data in self.cluster_mgr.get_all_nodes().items() 
                                               if nid != my_id and "Online" in data.get("status", "")]
                            
                            broadcast_time_str = t.get("broadcast_time")
                            is_old = False
                            if broadcast_time_str:
                                try:
                                    bt = datetime.datetime.fromisoformat(broadcast_time_str)
                                    if (datetime.datetime.now() - bt).total_seconds() > 10: 
                                        is_old = True
                                except: pass
                                
                            # [v27.10.50] DECENTRALIZED MASTER: Remove artificial 10s wait.
                            # Master and Workers compete for unassigned tasks equally.
                            debug_log(f"Master claiming unassigned task: {t.get('base_name')}")
                            candidate_task = t
                            candidate_index = i
                            break
                        
                        # Priority 3: Tasks assigned to offline nodes (Master reclaims)
                        # Check if assigned node is still online
                        assigned_node_info = self.cluster_mgr.get_all_nodes().get(assigned)
                        if assigned_node_info:
                            last_seen = assigned_node_info.get("last_seen", 0)
                            # [v27.3.10] Type Safety for numeric vs string timestamp
                            if isinstance(last_seen, str):
                                try:
                                    last_seen = datetime.datetime.fromisoformat(last_seen).timestamp()
                                except: last_seen = 0
                                
                            time_since_seen = time.time() - last_seen
                            if time_since_seen > 30:  # [v27.10.52] 30s reclaim (was 60s) for faster failover
                                debug_log(f"Master reclaiming task from offline node {assigned}: {t.get('base_name')}")
                                t["assigned_to"] = None
                                t["claimed_by"] = None
                                candidate_task = t
                                candidate_index = i
                                break
                        else:
                            # Node not in known_nodes = offline
                            if assigned: # Only if it WAS assigned (and not None)
                                debug_log(f"Master reclaiming task from unknown/offline node {assigned}: {t.get('base_name')}")
                                t["assigned_to"] = None
                                t["claimed_by"] = None
                                t["status"] = "Pending" # [v27.10.70] Reset status to Pending for re-allocation
                                candidate_task = t
                                candidate_index = i
                                break
                        
                        # If assigned to active worker, skip this task

                    else:
                        # Manual/Local Task: Always eligible for processing
                        candidate_task = t
                        candidate_index = i
                        break
                        
                if not candidate_task:
                    break # No duties for me right now
                    
                # We found a job
                task = candidate_task
                self.pending_tasks.pop(candidate_index)
                
                # Double check source existence (Shared Storage Check)
                source = task.get("source")
                if source:
                    # [v27.3] Skip reachability check if task is already Done or Processing elsewhere.
                    # This prevents false "Source Missing" failures when one node finishes and moves the file.
                    current_status = task.get("status") or ""
                    if "Done" in current_status or "完成" in current_status or "Processing" in current_status:
                        debug_log(f"Skipping reachability check for task {task.get('base_name')} with status: {current_status}")
                    else:
                        # [FIX] Normalize for Windows UNC (Handle mixed slashes / vs \)
                        source_check = source.replace('/', '\\')
                        if not os.path.exists(source_check):
                            # [FIX v27.9.1] Network Share Resilience
                            # os.path.exists can fail on UNC paths due to permissions/timeouts even if file is readable.
                            # We LOG a warning but ALLOW the task to proceed. FFmpeg will fail if it's truly missing.
                            debug_log(f"WARNING: Source check failed for {source_check}. Proceeding anyway... (Network Path?)")
                            
                            # Update UI to show we are "Attempting" despite check failure
                            widget = task.get("widget")
                            if widget:
                                widget.lbl_status.setText("Processing (Network)")
                                widget.lbl_status.setStyleSheet("color: #ffd54f; font-weight: bold;")
                            
                            # continue # <--- REMOVED: Do not skip. Let run_transcode try.

                # [NEW] Update widget to show "Processing" immediately
                widget = task.get("widget")
                if widget:
                    widget.lbl_status.setText("Processing")
                    widget.lbl_status.setStyleSheet("color: #ffd54f; font-weight: bold;")
                    
                    # [FIX] Show Full Worker ID (e.g. TVP-1221001644)
                    my_worker_id = getattr(self.cluster_mgr, 'node_id', 'Local')
                    widget.lbl_node.setText(f"{my_worker_id}")

                self.run_transcode(task, single_run=True)
                active_count += 1
                
        except Exception as e:
            debug_log(f"Process Queue Error: {e}")
            import traceback
            traceback.print_exc()
        finally:
             self._pumping_queue = False


        # [FIX] Auto-Poll Queue (User Issue: "Worker IDLE")
        # If we still have pending tasks but couldn't fill all slots (e.g. waiting for assignment),
        # schedule another check in a few seconds to pick up newly assigned tasks
        if self.pending_tasks and active_count < max_parallel:
            QTimer.singleShot(3000, self.process_next_task)
            debug_log(f"Queue polling: {len(self.pending_tasks)} tasks pending, will recheck in 3s")

        if not self.pending_tasks and active_count == 0:
            self.is_processing = False
            self.btn_start_all.setEnabled(True)

    def run_transcode(self, task, single_run=False):
        # [v27.10.6.5] Protection against deleted widgets [Phase 12]
        import shiboken6
        if not task or "widget" not in task or not shiboken6.isValid(task.get("widget")):
             print("DEBUG: Task widget or data invalid. Skipping run_transcode.")
             return

        # [FIX] Removed premature worker check - it prevented retries and caused stalls
        # Duplicate worker handling is done properly at line 3866-3870 before worker creation
        
        debug_log(f"[RUN_TRANSCODE] START: {task.get('base_name')}")
        widget = task["widget"]
        self.current_running_task = task # Store ref for cancellation
        from core.transcoder import Transcoder
        from core.gpu_detector import get_gpu_encoders, get_best_h264_encoder
        
        gpu_info = get_gpu_encoders()
        tx = Transcoder()
        debug_log(f"[RUN_TRANSCODE] Probing duration...")
        duration = tx.get_duration(task["source"])
        debug_log(f"[RUN_TRANSCODE] Duration: {duration}s")
        
        # [FIX v27.10.8] Fail fast if probe fails to avoid 0% stall
        if duration == 0 and not task.get("growing"):
            debug_log("[RUN_TRANSCODE] ERROR: Probe failed (Duration 0).")
            if hasattr(widget, 'set_failed'):
                widget.set_failed("無法解析影片資訊(路徑錯誤或碼流損壞)")
            
            # [v27.10.76] Broadcast failure with cluster_status=Failed so Master auto-reassigns
            if hasattr(self, 'cluster_mgr'):
                f_update = task.copy()
                if "widget" in f_update: del f_update["widget"]
                if "mirror_widget" in f_update: del f_update["mirror_widget"]
                f_update["status"] = "Failed"
                f_update["cluster_status"] = "Failed"  # [v27.10.76] KEY: triggers Master reassignment
                f_update["error"] = "Probe failed (Duration 0)"
                self.cluster_mgr.broadcast_task(f_update)
            
            # Release the task slot so queue can continue
            if task["widget"] in self.workers:
                self.workers.pop(task["widget"], None)
            QTimer.singleShot(2000, self.process_next_task)
            return
        
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
            "in_point": self.format_ms(task.get("in_point", 0)),
            "out_point": self.format_ms(task.get("out_point", 0)),
            "bitrate": str(task.get("bitrate", "5000")).rstrip('k') + "k",
            "audio_gain": task.get("audio_gain", 0.0),
            "resolution": task.get("resolution") if task.get("resolution") and "x" in str(task.get("resolution")) else None,
            "fps": task.get("fps"),
            "audio_ch": task.get("audio_ch"),
            "acodec": task.get("acodec", "aac") # Pass Audio Codec
        }
        
        target_duration = duration
        in_p = task.get("in_point", 0)
        out_p = task.get("out_point", 0)
        
        if out_p and in_p:
            target_duration = (out_p - in_p) / 1000.0
            transcode_params["duration"] = str(target_duration) # Use duration for safer trimming
        elif in_p:
            target_duration = duration - (in_p / 1000.0)

        # Determine Extension
        ext = task.get("container", "mp4").lower()
        if not ext.startswith("."): ext = "." + ext
        
        # [FIX] Dynamic Output Directory Resolution
        # Use Priority:
        # 1. Worker Specific Path (if set in settings)
        # 2. Manual Output Dir (for manual adds)
        # 3. Same as Source (Default for manual tasks ONLY)
        
        worker_out = self.settings.get("worker_output_path", "").strip()
        manual_out = getattr(self, 'output_dir', None)
        is_auto = task.get("source_type") != "Manual"

        # [NEW] 監控任務使用 _TEMP 資料夾進行轉碼，完成後再移動到最終目錄
        if is_auto:
            # 決定最終輸出目錄
            if worker_out and os.path.isdir(worker_out):
                 final_output_dir = worker_out
            
            # [FIX v27.10.5] Force Activity Update so Cluster knows we are busy
            self.update_cluster_activity()

            if worker_out and os.path.isdir(worker_out):
                 final_output_dir = worker_out
            elif manual_out and os.path.isdir(manual_out):
                 final_output_dir = manual_out
            else:
                  # [v27.10.72] Restore factory default: Output to source dir if no global path set
                  final_output_dir = os.path.dirname(task["source"])
                  debug_log(f"[RUN_TRANSCODE] No global output, defaulting to source dir: {final_output_dir}")
            
            # 儲存最終輸出目錄供轉碼完成後使用
            task["final_output_dir"] = final_output_dir
            
            # [FIX] Try to use _TEMP, but fallback to direct output if it fails
            try:
                # 轉碼期間使用來源監控資料夾下的 TEMP
                src_watch_folder = os.path.dirname(task["source"])
                temp_dir = os.path.join(src_watch_folder, "TEMP")
                debug_log(f"[RUN_TRANSCODE] Checking TEMP: {temp_dir}")
                
                if not os.path.exists(temp_dir):
                    debug_log(f"[RUN_TRANSCODE] Creating TEMP directory...")
                    os.makedirs(temp_dir)
                    debug_log(f"[RUN_TRANSCODE] Created TEMP directory: {temp_dir}")
                
                task["output_dir"] = temp_dir
                debug_log(f"[RUN_TRANSCODE] Will transcode to TEMP: {temp_dir}, then move to: {final_output_dir}")
            except Exception as e:
                # Fallback: use final output dir directly if TEMP creation fails
                debug_log(f"[RUN_TRANSCODE] Failed to create TEMP: {e}, using direct output")
                if final_output_dir:
                    task["output_dir"] = final_output_dir
                else:
                    task["output_dir"] = os.path.dirname(task["source"])
            
        else:
            # 手動任務：直接使用最終輸出目錄
            if manual_out and os.path.isdir(manual_out):
                task["output_dir"] = manual_out
            else:
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

        debug_log(f"[RUN_TRANSCODE] Creating worker...")
        worker = TranscodeWorker(cmd, target_duration)
        worker.progress_signal.connect(lambda p, t: self.update_task_progress(task["widget"], p, t))
        worker.finished_signal.connect(lambda s, m: self.on_transcode_finished_worker(task, s, m, single_run))
        # [FIX] Do NOT connect finished->deleteLater automatically. 
        # We must manage lifecycle explicitly in on_transcode_complete to prevent race conditions (Crash at 99%)
        # worker.finished.connect(worker.deleteLater) 
        debug_log(f"[RUN_TRANSCODE] Starting worker...")
        worker.start()
        debug_log(f"[RUN_TRANSCODE] Worker started successfully")
        
        self.workers[task["widget"]] = worker # Track worker
        task["widget"].lbl_status.setText("Transcoding...")
        
        self.update_cluster_activity()
        task["widget"].set_started() # Ensure start time is recorded for UI
        
        # [NEW] Broadcast Processing Status
        if hasattr(self, 'cluster_mgr'):
            cluster_update = task.copy()
            if "widget" in cluster_update: del cluster_update["widget"]
            cluster_update["status"] = "Processing"
            cluster_update["claimed_by"] = self.cluster_mgr.node_id
            self.cluster_mgr.broadcast_task(cluster_update)
            
        debug_log(f"[RUN_TRANSCODE] COMPLETE")

    def on_nav_clicked(self, clicked_btn):
        try:
            print(f"DEBUG_NAV: Clicked {clicked_btn.text()}")
            # Uncheck others
            for btn in [self.btn_home, self.btn_dash, self.btn_watch, self.btn_cluster, self.btn_settings]:
                btn.setChecked(btn == clicked_btn)
            
            # Switch Page
            if clicked_btn == self.btn_home:
                 self.stack.setCurrentIndex(0) 
            elif clicked_btn == self.btn_dash:
                 self.refresh_dashboard_from_snapshot() # Sync from FS
                 self.stack.setCurrentIndex(4) # Original Dashboard Page
            elif clicked_btn == self.btn_watch:
                 self.refresh_watch_list_ui()
                 self.stack.setCurrentIndex(1)
            elif clicked_btn == self.btn_cluster:
                 print("DEBUG_NAV: Switching to Cluster Page (Index 2)")
                 self.refresh_cluster_ui()
                 self.stack.setCurrentIndex(2)
            elif clicked_btn == self.btn_settings:
                 self.stack.setCurrentIndex(3)
        except Exception as e:
            print(f"ERROR_NAV: Navigation failed: {e}")
            import traceback
            traceback.print_exc()
            QMessageBox.critical(self, "Navigation Error", f"Failed to switch page:\n{e}")


    def browse_cluster_path(self):
        """Opens a folder dialog to select the cluster sync path."""
        path = QFileDialog.getExistingDirectory(self, "選擇集群同步路徑")
        if path:
            self.edit_cluster_path.setText(path)

    def browse_worker_output_path(self):
        """Opens a folder dialog to select the worker-specific output path."""
        path = QFileDialog.getExistingDirectory(self, "選擇監控任務專用輸出路徑")
        if path:
            self.edit_worker_out.setText(path)

    def save_global_settings_ui(self):
        new_cluster_path_raw = self.edit_cluster_path.text().strip()
        if not new_cluster_path_raw:
             QMessageBox.warning(self, "設定失敗", "集群同步路徑不能為空。")
             return

        # Resolve Absolute Path for cluster path
        if not os.path.isabs(new_cluster_path_raw):
             # Resolve relative to app dir
             base_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.getcwd()
             new_cluster_path = os.path.abspath(os.path.join(base_dir, new_cluster_path_raw))
             self.edit_cluster_path.setText(new_cluster_path)
        else:
             new_cluster_path = new_cluster_path_raw
        # Validate Cluster Path exists and is writable
        if not os.path.exists(new_cluster_path):
             try:
                 os.makedirs(new_cluster_path, exist_ok=True)
             except Exception as e:
                 QMessageBox.critical(self, "路徑無效", 
                     f"無法建立或存取集群路徑：\n{new_cluster_path}\n\n"
                     f"原因：{e}\n\n"
                     "💡 補強做法：\n"
                     "1. 請檢查網路磁碟機是否已正確映射 (如 Z:\\)。\n"
                     "2. 若使用 UNC 路徑 (\\\\server\\share)，請確保當前使用者具有讀寫權限。")
                 return

        old_cluster_path = self.settings.get("cluster_path", "")
        
        self.settings.set("cluster_path", new_cluster_path)
        
        # Parallel Tasks logic
        self.settings.set("max_parallel_tasks", self.spin_parallel.value())

        # Worker Output Path
        self.settings.set("worker_output_path", self.edit_worker_out.text())

        # [FIX v27.10.58] Save Node Alias - was missing from this function!
        new_alias = self.edit_node_alias.text().strip()
        self.settings.set("worker_alias", new_alias)
        
        self.save_settings()

        # [NEW v27.10.58] Immediately push new alias to cluster heartbeat file
        if hasattr(self, 'cluster_mgr') and new_alias:
            try:
                node_id = self.cluster_mgr.node_id
                self.node_aliases[node_id] = new_alias  # Local map update
                # Push new alias into worker thread settings for next heartbeat
                self.cluster_mgr.update_worker_settings({"worker_alias": new_alias})
                # Retroactively refresh all task widgets on this node
                def _refresh(lw):
                    for i in range(lw.count()):
                        item = lw.item(i)
                        w = lw.itemWidget(item)
                        if not w: continue
                        td = getattr(w, 'task_data', None)
                        if not td: continue
                        wid = td.get("worker_id", "") or ""
                        wuuid = td.get("worker_uuid", "") or ""
                        if wid == node_id or wuuid == node_id:
                            w.lbl_node.setText(new_alias)
                            w.lbl_node.setToolTip(f"{new_alias}\n({node_id})")
                _refresh(self.manual_task_list)
                _refresh(self.auto_task_list)
                debug_log(f"[v27.10.58] Alias instantly applied: {new_alias} for node {node_id}")
            except Exception as ae:
                debug_log(f"Alias instant update error: {ae}")

        

        # Determine effective role for logic below
        current_role = self.settings.get("cluster_role", "Worker")

        # Apply Logic: If Worker, stop watch engine
        # Apply Logic based on CURRENT detected role

        if current_role == "Worker":
            if self.watch_engine.isRunning():
                # print("Settings: Status is Worker. Stopping Watch Engine...")
                self.watch_engine.stop()
                self.watch_engine.wait(1000)
        else:
            if not self.watch_engine.isRunning():
                # print("Settings: Status is Master. Starting Watch Engine...")
                self.watch_engine.start()

        # [FIX] Restart Cluster Manager if Path Changed (Role changes handle themselves via signal now)
        if new_cluster_path != old_cluster_path:
             print(f"Settings: Config changed (Path or Role). Restarting Manager...")
             if hasattr(self, 'cluster_mgr'):
                  self.cluster_mgr.restart(new_cluster_path)
                  
                  # Clear UI to prevent stale data
                  if hasattr(self, 'node_list'):
                       self.node_list.clear()

        msg = (f"集群設定已儲存！\n\n"
               f"目前狀態: {current_role} (Auto)\n"
               f"同步路徑: {new_cluster_path}\n\n"
               "✅ 其他節點現在應能透過此路徑與本機同步。")
        QMessageBox.information(self, "設定成功", msg)

    def do_factory_reset(self, silent=False):
        """[v27.10.66] Robust Selective Reset. Allows wiping cluster nodes while preserving configs."""
        self._reset_in_progress = True 
        
        mode = "full" # default
        if not silent:
            from PySide6.QtWidgets import QMessageBox, QPushButton, QHBoxLayout, QFrame, QLabel, QApplication
            msg = QMessageBox(self)
            msg.setWindowTitle("重置與清除 (Factory Reset / Cleanup)")
            msg.setText("請選擇重置模式：")
            
            btn_cluster = msg.addButton("節點清零 (Cluster Node Reset)", QMessageBox.ActionRole)
            btn_cluster.setToolTip("僅清除殘留節點與集群快取，保留監控資料夾、格式與歷史紀錄。")
            
            btn_full = msg.addButton("全面重置 (Standard Factory Reset)", QMessageBox.DestructiveRole)
            btn_full.setToolTip("清除所有設定與歷史紀錄。")
            
            btn_cancel = msg.addButton("取消 (Cancel)", QMessageBox.RejectRole)
            
            msg.setIcon(QMessageBox.Warning)
            msg.setStyleSheet("QLabel { color: white; } QPushButton { min-width: 160px; padding: 8px; }")
            
            msg.exec()
            clicked = msg.clickedButton()
            
            if clicked == btn_cancel:
                self._reset_in_progress = False
                return
            elif clicked == btn_cluster:
                mode = "cluster"
            else:
                mode = "full"

        debug_log(f"Factory Reset initiated. Mode: {mode}")
        
        # 1. Shutdown Logging early to release debug.log
        import logging
        try:
            logging.shutdown()
            for h in logging.root.handlers[:]:
                h.close()
                logging.root.removeHandler(h)
        except: pass

        # 2. Cluster Data Cleanup
        try:
            c_path = self.settings.get("cluster_path")
            if c_path and os.path.exists(c_path):
                import shutil
                # Files/Folders to ALWAYS wipe in both modes
                to_wipe = ["nodes", "master.lock"] 
                # Files to wipe ONLY in FULL mode
                if mode == "full":
                    to_wipe.extend(["tasks", "watch_config.json"])
                
                for sub in to_wipe:
                    p = os.path.join(c_path, sub)
                    if os.path.exists(p):
                        try:
                            if os.path.isdir(p): shutil.rmtree(p, ignore_errors=True)
                            else: os.remove(p)
                        except: pass
        except Exception as e:
            debug_log(f"Factory Reset: Cluster cleanup error - {e}")
             
        # 3. Local Data & Settings Cleanup
        if mode == "full":
             from core.settings import DATA_DIR, BASE_DIR, SETTINGS_FILE, CURRENT_VERSION
             # Local Data Cleanup
             if os.path.exists(DATA_DIR):
                 import shutil
                 try:
                     for filename in os.listdir(DATA_DIR):
                         file_path = os.path.join(DATA_DIR, filename)
                         try:
                             if os.path.isfile(file_path): os.unlink(file_path)
                             elif os.path.isdir(file_path): shutil.rmtree(file_path, ignore_errors=True)
                         except: pass
                 except: pass
                 
             # [v27.10.66] Selective Settings Wipe: Preserve NOTHING in full mode
             legacy_files = [
                 "watch_folder_history.json", "processed_files.json", 
                 "settings.json", "debug.log", "cleared_tasks.json", "watch_config.json"
             ]
             for f in legacy_files:
                 p = os.path.join(BASE_DIR, f)
                 if os.path.exists(p):
                     try: os.remove(p)
                     except: pass
                     
             # Fresh start settings.json
             try:
                 import json
                 with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                     json.dump({"app_version": CURRENT_VERSION}, f, indent=4)
             except: pass
        else:
             # Cluster Reset Mode: Only clear node IDs and local ephemeral state
             self.settings.set("cluster_node_id", "") # Force re-generate ID on next start
             self.settings.set("saved_queue", []) 
             self.settings.save()

        # 4. Relaunch Application (Suicide & Restart)
        try:
             import sys, subprocess
             QApplication.quit()
             
             # Purification: Remove PyInstaller env vars to prevent DLL load failures
             restart_env = os.environ.copy()
             if "_MEIPASS" in restart_env:
                 del restart_env["_MEIPASS"]

             if getattr(sys, 'frozen', False):
                 subprocess.Popen([sys.executable], env=restart_env, creationflags=0x00000008)
             else:
                 subprocess.Popen([sys.executable] + sys.argv, env=restart_env, creationflags=0x00000008)
             
             os._exit(0)
        except: 
             os._exit(0)

    def update_cluster_activity(self):
        """Unified method to report current node activity to cluster."""
        active_count = len(self.workers)
        status = "Idle"
        if active_count > 0:
            status = f"Busy ({active_count} tasks)"
                
        # [FIX] Enable activity reporting for correct load balancing
        if hasattr(self, 'cluster_mgr'):
            self.cluster_mgr.set_local_activity(status, active_count)
        
        # Also update Dashboard Badge
        self.update_dashboard_badge()

    def apply_recommended_concurrency(self):
        from core.gpu_detector import get_recommended_concurrency
        val = get_recommended_concurrency()
        self.spin_parallel.setValue(val)
        if hasattr(self, 'statusBar'):
            self.statusBar().showMessage(f"已根據硬體套用建議並行數: {val}", 3000)

    def refresh_watch_list_ui(self):
        print("DEBUG_UI: Refreshing Watch List UI...")
        self.watch_list.clear()
        
        # [v27.3] Role Feedback: Show Master's Alias if we are a Worker
        current_role = self.settings.get("cluster_role", "Worker")
        if current_role == "Worker":
            master_id = self.cluster_mgr.master_id
            master_alias = self.node_aliases.get(master_id, master_id) if master_id else "Searching..."
            header_item = QListWidgetItem(self.watch_list)
            header_item.setFlags(Qt.NoItemFlags) # Non-selectable
            header_item.setSizeHint(QSize(0, 30))
            lbl = QLabel(f"🔒 監控目錄由主節點管理 (Managed by Master: {master_alias})")
            lbl.setStyleSheet("color: #FFA726; font-weight: bold; padding-left: 10px; font-style: italic; background: #333;")
            self.watch_list.addItem(header_item)
            self.watch_list.setItemWidget(header_item, lbl)

        watch_folders = self.settings.get("watch_folders", [])
        print(f"DEBUG_UI: Found {len(watch_folders)} watch folders in settings.")
        for i, wf in enumerate(watch_folders):
            print(f"DEBUG_UI: Adding Watch Folder: {wf}")
            item = QListWidgetItem(self.watch_list)
            item.setSizeHint(QSize(0, 80)) # Increase height for custom widget
            
            # Use Custom Row Widget
            row_widget = WatchFolderRowWidget(wf, i, self)
            self.watch_list.addItem(item)
            self.watch_list.setItemWidget(item, row_widget)
            
        # self.watch_engine.file_detected.connect(self.on_watch_folder_detected)
        # self.watch_engine.snapshot_ready.connect(self.populate_dashboard_ui) 
        
        # [FIX] Strict Role Enforcement: Only Master runs Watch Engine
        if hasattr(self, 'watch_engine'):
            current_role = self.settings.get("cluster_role", "Worker") # [v27.10.19] Safe default to Worker
            is_master = (current_role == "Master")
            
            # [v27.10.20] Sync Sidebar Visibility
            if hasattr(self, 'btn_dash'): self.btn_dash.setVisible(is_master)
            if hasattr(self, 'btn_watch'): self.btn_watch.setVisible(is_master)

            if is_master:
                if len(watch_folders) > 0 and not self.watch_engine.isRunning():
                    print(f"DEBUG_UI: Master Role Detected. Starting Watch Folder Engine...")
                    self.watch_engine.start()
                elif len(watch_folders) == 0 and self.watch_engine.isRunning():
                    self.watch_engine.stop()
            else:
                if self.watch_engine.isRunning():
                    print(f"DEBUG_UI: Worker Role Detected. Stopping Watch Folder Engine...")
                    self.watch_engine.stop()
        
        # [v27.10.49] Auto-hide failover alert on successful promotion
        if current_role == "Master" and hasattr(self, '_failover_notification'):
             self._failover_notification.hide()
        
        # [v27.7] Reset Clear Button to initial state on refresh
        self.reset_clear_button()


    def on_master_stale_detected(self):
        """[v27.10.20] Handle Master failure."""
        if self.settings.get("cluster_role") == "Master": return
        
        # Show a non-blocking prompt or button
        if not hasattr(self, '_failover_notification'):
             from PySide6.QtWidgets import QPushButton, QHBoxLayout, QFrame
             self._failover_notification = QFrame(self)
             self._failover_notification.setStyleSheet("background: #b71c1c; border-radius: 8px;")
             self._failover_notification.setFixedHeight(50)
             
             layout = QHBoxLayout(self._failover_notification)
             layout.setContentsMargins(15, 0, 15, 0)
             
             lbl = QLabel("⚠️ 偵測到主機斷線：系統將在 30 秒內自動接管。 (Master Offline: Auto-promoting...)", self._failover_notification)
             lbl.setStyleSheet("color: white; font-weight: bold;")
             
             btn = QPushButton("立即接管 (Promote Now)", self._failover_notification)
             btn.setFixedWidth(150)
             btn.setStyleSheet("background: white; color: #b71c1c; font-weight: bold; border: none; height: 30px;")
             btn.clicked.connect(self.perform_manual_failover)
             
             layout.addWidget(lbl)
             layout.addStretch()
             layout.addWidget(btn)
             
             # [v27.10.21 Refined] Place notification in the Unified Main Page (Index 0)
             # Since Workers are locked to Page 0, they MUST see it there.
             if hasattr(self, 'trans_layout'):
                  self.trans_layout.insertWidget(0, self._failover_notification)
             else:
                  # Fallback
                  self.params_layout.insertWidget(0, self._failover_notification)
        
        self._failover_notification.show()

    def perform_manual_failover(self):
        if hasattr(self, '_failover_notification'):
             self._failover_notification.hide()
        
        # 1. Inherit Settings
        success = self.cluster_mgr.load_master_backup_settings()
        
        # 2. Grab Lock
        self.cluster_mgr.promote_to_master()
        
        msg = "已接管為主機。已繼承上一台主機設定。" if success else "已接取為主機 (未發現備份設定)。"
        QMessageBox.information(self, "接管成功", msg)
        
        # 3. Refresh UI
        self.refresh_watch_list_ui()
        self.on_role_changed("Master")

    def append_watch_log(self, msg):
        """[v27.10.77] Append a message to the Dashboard watch log panel."""
        if hasattr(self, 'watch_log'):
            self.watch_log.appendPlainText(msg)
            # Auto-scroll to bottom
            sb = self.watch_log.verticalScrollBar()
            sb.setValue(sb.maximum())

    def on_watch_folder_detected(self, file_path, folder_name, is_repeat=False):
        """
        Triggered when WatchFolderEngine detects a new stable file.
        Uses a background thread to prevent UI freezing during metadata probe.
        """
        # [v27.3] Reinforce Role Check: Double check role before starting probe
        my_role = self.settings.get("cluster_role", "Worker") # Default to Worker for safety
        if my_role != "Master":
             print(f"DEBUG_UI: Ignored watch folder event - Local node is Worker ({my_role})")
             return

        print(f"DEBUG_UI: Watch Folder Event Triggered: {os.path.basename(file_path)}")
        
        # [v27.10.64] RESTORE: Instant Placeholder Task for Dashboard
        # "監控目錄檔案拷貝完成不會即時顯示" -> FIX by adding placeholder immediately.
        import uuid
        placeholder_id = str(uuid.uuid4())[:8]
        
        base_name = os.path.basename(file_path)
        if is_repeat:
            import time
            ts = time.strftime("%H%M%S")
            base_name = os.path.splitext(base_name)[0] + f"_{ts}" + os.path.splitext(base_name)[1]

        # Add to UI immediately
        placeholder_data = {
            "source": file_path,
            "base_name": base_name,
            "source_type": f"Watch: {folder_name}",
            "cluster_status": "Probing",
            "placeholder_id": placeholder_id,
            "status": "探測中 (Probing...)"
        }
        self.add_task_to_queue(
            source_path=file_path, 
            source_type=f"Watch: {folder_name}", 
            base_name_override=base_name,
            extra_data={"placeholder_id": placeholder_id, "status": "探測中 (Probing...)"}
        )

        # Launch Background Thread
        thread = WatchTaskCreationThread(file_path, folder_name, self, is_repeat)
        thread.placeholder_id = placeholder_id # Pass id
        thread.task_ready.connect(self.on_watch_task_ready)
        thread.task_failed.connect(self.on_watch_task_probe_failed) # [v27.10.74]
        
        if not hasattr(self, '_watch_task_threads'): self._watch_task_threads = []
        # Cleanup old threads
        self._watch_task_threads = [t for t in self._watch_task_threads if t.isRunning()]
        self._watch_task_threads.append(thread)
        thread.start()

    def on_watch_task_probe_failed(self, placeholder_id):
        """[v27.10.74] Called when WatchTaskCreationThread aborts/errors. Updates stuck placeholder."""
        print(f"DEBUG_UI: Probe failed for placeholder {placeholder_id}. Cleaning up.")
        if not placeholder_id: return
        for i in range(self.auto_task_list.count()):
            item = self.auto_task_list.item(i)
            w = self.auto_task_list.itemWidget(item)
            if not w: continue
            wd = getattr(w, 'task_data', {})
            if wd.get('placeholder_id') == placeholder_id:
                wd['status'] = 'Failed'
                wd['cluster_status'] = 'Failed'
                if hasattr(w, 'lbl_status'):
                    w.lbl_status.setText("探測失敗 (Probe Failed)")
                    w.lbl_status.setStyleSheet("color: #ef5350; font-weight: bold;")
                break

        """Called when background thread finishes metadata probe and deduplication."""
        source = task_data.get("source")
        if source:
             norm_s = os.path.normpath(source).lower()
             dismissed = self.settings.get("dismissed_dashboard_items", [])
             if any(os.path.normpath(d).lower() == norm_s for d in dismissed):
                  # [v27.10.36] Rule 2: Allow fresh Watch detections. Corrected 'Watch' prefix check.
                  if not str(task_data.get("source_type", "")).startswith("Watch"):
                       print(f"DEBUG_UI: Watch Task Ready but DISMISSED: {task_data.get('base_name')}")
                       return

        print(f"DEBUG_UI: Watch Task Ready: {task_data.get('base_name')}")
        
        # [v27.10.27] Update Placeholder Task
        p_id = task_data.get("placeholder_id")
        existing_widget = None
        if p_id:
            for i in range(self.auto_task_list.count()):
                item = self.auto_task_list.item(i)
                w = self.auto_task_list.itemWidget(item)
                if w and getattr(w, 'task_data', {}).get("placeholder_id") == p_id:
                    existing_widget = w
                    break
        
        if existing_widget:
            # Update existing placeholder instead of adding new
            wd = existing_widget.task_data
            wd.update(task_data)
            # Remove probing status
            if "placeholder_id" in wd: del wd["placeholder_id"]
            existing_widget.lbl_name.setText(task_data.get("base_name"))
            existing_widget.lbl_status.setText("Pending")
            # Update tooltip
            existing_widget.set_task_data(wd)
        else:
            # [REGRESSION FIX v27.10.65] Fallback: If placeholder missing, add normally.
            print(f"DEBUG_UI: Placeholder missing for {task_data.get('base_name')}. Adding normally.")
            self.add_task_to_queue(
                source_path=task_data.get("source"),
                source_type=task_data.get("source_type", "Watch"),
                base_name_override=task_data.get("base_name"),
                extra_data=task_data
            )
        
        if hasattr(self, 'cluster_mgr'):
            # [RESTORED v27.10.31] Broadcast to Cluster
            cluster_update = task_data.copy()
            if "widget" in cluster_update: del cluster_update["widget"]
            cf = self.cluster_mgr.broadcast_task(cluster_update)
            if cf:
                task_data["cluster_filename"] = cf
            
            # Sync to local UI
            self.on_cluster_task_synced(task_data)
            
            # [v27.10.68] Immediate trigger for processing
            if not self.is_processing:
                debug_log("DEBUG_UI: Watch Task detected. Triggering queue pump.")
                self.is_processing = True # [FIX v27.10.69] Enable processing to allow queue pumping
                QTimer.singleShot(100, self.process_next_task) 
        
        # [v27.10.62] Snapshot Cleanup: If a real task is now ready, 
        # remove any "WatchFolder" placeholder that might have been added by the snapshot refresh.
        # This prevents duplicate entries in the same list.
        for i in range(self.auto_task_list.count()):
             item = self.auto_task_list.item(i)
             w = self.auto_task_list.itemWidget(item)
             if w:
                  w_data = getattr(w, 'task_data', {})
                  if w_data.get("source_type") == "WatchFolder":
                       if os.path.normpath(w_data.get("source", "")).lower() == norm_s:
                            self.auto_task_list.takeItem(i)
                            break

    def refresh_cluster_ui(self):
        """[v27.10.12] Unified Cluster Refresh. Ensures node_list is visible and populated."""
        if not hasattr(self, 'node_list') or not hasattr(self, 'cluster_mgr'):
            return
            
        try:
            # Update Header Info
            role = self.settings.get("cluster_role", "Master")
            path = self._get_safe_cluster_path()
            self.lbl_node_info.setText(f"本機辨識碼 (Local Node): {self.cluster_mgr.node_id}  |  角色: [{role}]  |  同步路徑: {path}")
            
            # Get Nodes
            nodes = self.cluster_mgr.get_all_nodes() or {}
            
            # [FIX] If list is empty but we have nodes, or vice-versa, force a repopulate
            # For simplicity in v27.10.12, we use the "Smooth Update" pattern via on_cluster_node_updated
            # but we must ensure node_list is not somehow hidden.
            
            if not nodes:
                 self.node_list.clear()
                 placeholder = QListWidgetItem("Waiting for cluster heartbeat...")
                 placeholder.setForeground(QColor("#888"))
                 self.node_list.addItem(placeholder)
                 return

            # Update nodes one by one (Smooth Update avoids flicker)
            for nid, data in sorted(nodes.items()):
                self.on_cluster_node_updated(data)
                
        except Exception as e:
            print(f"ERROR_UI: refresh_cluster_ui failed: {e}")
            traceback.print_exc()

    def _get_safe_cluster_path(self):
        if hasattr(self, 'cluster_mgr'): return self.cluster_mgr._cluster_path
        return self.settings.get('cluster_path', 'Unknown')

    def refresh_cluster_ui_timer(self):
        """Timer calls this to refresh UI ONLY if the cluster page is visible."""
        # [FIX] Always update badge regardless of active page
        self.update_dashboard_badge()
        
        if self.stack.currentIndex() == 2: # Cluster Status Page
            self.refresh_cluster_ui()

    def refresh_dashboard_from_snapshot(self):
        """Requests Async Snapshot from Watch Engine."""
        # 1. Show Loading State (Optional)
        # self.auto_task_list.clear() # Maybe don't clear until ready to prevent flicker
        
        # 2. Checks
        if not hasattr(self, 'watch_engine') or not self.watch_engine:
            return
            
        # 3. Request (Non-Blocking)
        self.watch_engine.request_snapshot()
        print("Dashboard: Requested Async Snapshot...")

    def populate_dashboard_ui(self, snapshot_data):
        """Callback when data is ready."""
        # print(f"Dashboard: Received Snapshot with {len(snapshot_data.get('pending',[]))} pending items.")
        
        # [FIX] Do NOT clear auto_task_list (Queue). Clear History List instead.
        if hasattr(self, 'dashboard_history_list'):
            self.dashboard_history_list.clear() # Clear logic moved here
        else:
            return
        
        # 1. Pending (running_paths logic same as before)
        running_paths = set()
        for w in self.workers.keys():
            t = getattr(w, 'task_data', {})
            src = t.get("source")
            if src: running_paths.add(os.path.normpath(src).lower())
            
        if getattr(self, 'current_running_task', None):
            src = self.current_running_task.get("source")
            if src: running_paths.add(os.path.normpath(src).lower())
            
        # Add Pending
        for item in snapshot_data.get('pending', []):
             self.add_snapshot_item_to_ui(item, 'pending', running_paths)
             
        # Add Done
        for item in snapshot_data.get('done', []):
             self.add_snapshot_item_to_ui(item, 'done', running_paths)
             
        # Add Error
        for item in snapshot_data.get('error', []):
             self.add_snapshot_item_to_ui(item, 'error', running_paths)

    # [Helper extracted]
    def add_snapshot_item_to_ui(self, item, category, running_paths):
            path = item['path']
            norm_path = os.path.normpath(path).lower()
            
            # [NEW v27.4] Filter dismissed items (Dashboard Persistence)
            dismissed = self.settings.get("dismissed_dashboard_items", [])
            if any(os.path.normpath(d).lower() == norm_path for d in dismissed):
                return
            
            # [v27.10.62] Also check cleared_tasks (Persistent History)
            task_id = self.get_task_identifier(item)
            if task_id in self.cleared_tasks:
                 return
            
            if norm_path in running_paths: return # Skip running
            
            # [FIX] Also skip if it's already in the pending_tasks queue
            is_in_queue = False
            for pt in self.pending_tasks:
                pt_src = pt.get("source")
                if pt_src and os.path.normpath(pt_src).lower() == norm_path:
                    is_in_queue = True
                    break
            
            # [RECOVERY] If pending in snapshot but not in queue, we MUST add it to queue
            # Otherwise it's a ghost task that never runs.
            if category == 'pending' and not is_in_queue:
                # Add to internal queue
                task_data = {
                    "source": path,
                    "base_name": item['base_name'],
                    "source_type": "WatchFolder", 
                    "worker_id": "Auto", # Will be claimed by Cluster/Local
                    "status": "Pending",
                    "progress": 0,
                    "timestamp": time.time(),
                    "retry_count": item.get("retry_count", 0),
                    "claimed_by": item.get("claimed_by"), # Restore claim info
                    "assigned_to": item.get("assigned_to")
                }
                self.pending_tasks.append(task_data)
                
                # Trigger Queue Pump
                if not self.is_processing:
                    QTimer.singleShot(500, self.process_next_task)
                
                is_in_queue = True # Now it is
            
            # Logic Split: Pending -> Auto Task List, Done/Error -> History List
            target_list = None
            
            if category == 'pending':
                target_list = self.auto_task_list
            elif category in ['done', 'error']:
                 if hasattr(self, 'dashboard_history_list'):
                     target_list = self.dashboard_history_list
                 else:
                     target_list = self.auto_task_list # Fallback
            
            if not target_list: return

            # [v27.10.63] User Requirement: "No coexistence for same source"
            # Deduplication (Check all lists: Manual, Auto, History)
            duplicate_found = False
            all_lists = [self.manual_task_list, self.auto_task_list]
            if hasattr(self, 'dashboard_history_list'):
                all_lists.append(self.dashboard_history_list)
            
            for lst in all_lists:
                for i in range(lst.count()):
                    it = lst.item(i)
                    w = lst.itemWidget(it)
                    if w:
                        w_data = getattr(w, 'task_data', {})
                        existing_source = w_data.get("source", "")
                        existing_source_norm = os.path.normpath(existing_source).lower() if existing_source else ""
                        
                        # [FIX] Path-based Dedup (Priority)
                        if existing_source_norm == norm_path:
                            duplicate_found = True
                            break
                        
                        # Fallback: Name-based (Legacy)
                        if w_data.get("base_name") == item['base_name']:
                            duplicate_found = True
                            break
                if duplicate_found: break
            if duplicate_found: return
            
            # [v27.10.62] Correct Source Label Mapping
            source_label = "Watch: " + item.get("folder_name", "WatchFolder")
            
            # Create Widget
            w_item = QListWidgetItem(target_list)
            
            # Enrichment: Find preset info for WatchFolder metadata display
            raw_worker = item.get("claimed_by", "Auto")
            worker_display = self.node_aliases.get(raw_worker, raw_worker)
            
            enriched_data = {
                "source": path,
                "base_name": item['base_name'],
                "source_type": source_label, # [v27.10.63] Consistently Use Label
                "worker_id": worker_display, # [v27.10.48] Use Friendly Alias
                "worker_uuid": raw_worker
            }
            
            # [NEW v27.5] Lookup Preset for accurate metadata display
            wf_list = self.settings.get("watch_folders", [])
            preset_name = None
            for wf in wf_list:
                w_p = wf.get("path")
                if w_p and os.path.normpath(path).lower().startswith(os.path.normpath(w_p).lower()):
                    preset_name = wf.get("preset")
                    break
            
            if preset_name:
                all_presets = self.settings.get("presets", {})
                p_data = all_presets.get(preset_name)
                if p_data:
                    meta_keys = ["container", "vcodec", "bitrate", "resolution", "fps"]
                    for mk in meta_keys:
                        if mk in p_data: enriched_data[mk] = p_data[mk]

            if category == 'done':
                w_item.setSizeHint(QSize(0, 36))
                widget = TaskProgressWidget(f"{item['base_name']}")
                widget.set_task_data(enriched_data) # [FIX v27.5] Apply enriched metadata
                widget.set_done(path, self.player, "Done")  # [v27.10.51] set_done handles all styles
                
            elif category == 'error':
                 w_item.setSizeHint(QSize(0, 36))
                 widget = TaskProgressWidget(f"[錯誤] {item['base_name']}")
                 widget.set_task_data(enriched_data) # [FIX v27.5] Apply enriched metadata
                 widget.setStyleSheet("QFrame#TaskRow { background-color: #302020; } QLabel { color: #ff8a80; }")
                 widget.lbl_status.setText("Failed")
                 widget.progress.setValue(100)
                 widget.progress.setStyleSheet("QProgressBar::chunk { background-color: #d32f2f; }")
                 widget.setToolTip(item.get("log_content", "Error"))
                 
            else:
                 # Pending Display
                 w_item.setSizeHint(QSize(0, 36))
                 # Use claimed info if available
                 claimed = item.get("claimed_by")
                 display_name = item['base_name'] # [FIX] Removed [待機] prefix
                 
                 widget = TaskProgressWidget(display_name)
                 widget.set_task_data(enriched_data) # [FIX v27.5] Apply enriched metadata
                 widget.lbl_status.setText("Pending")
                 
                 if claimed:
                     # [v27.10.56] Use alias from node_aliases map
                     node_display = self.node_aliases.get(claimed, claimed.split('-')[0])
                     widget.lbl_node.setText(node_display)
                     widget.lbl_node.setStyleSheet("color: #BB86FC; font-weight: bold;")
                     widget.lbl_status.setText("Claimed")
                     widget.lbl_status.setStyleSheet("color: #BB86FC; font-weight: bold;")

            
            # Common Data Binding
            task_data = {
                "source": path,
                "base_name": item['base_name'],
                "source_type": "WatchFolder", 
                "worker_id": enriched_data.get("worker_id", "Auto"), # Use Alias from enrichment
                "status": "Pending" if category == 'pending' else category.capitalize(),
                "progress": 0 if category == 'pending' else 100,
            }
            # Merge enriched metadata (container, vcodec, etc.)
            task_data.update(enriched_data)
            
            # [v27.10.47] Apply consolidated data to widget
            widget.set_task_data(task_data)
            
            # [v27.10.51] Done style re-applied by set_done() above - no need to duplicate here
            if category == 'done':
                # Ensure text is consistent (set_done sets '完成 (Done)')
                widget.lbl_status.setText("完成 (Done)")
                widget.lbl_status.setStyleSheet("color: #4CAF50; font-weight: bold; border: none;")
            
            # Connect Signals
            widget.transcode_requested.connect(lambda w=widget: self.transcode_single_item(w))
            widget.pause_requested.connect(lambda w=widget: self.pause_task(w))
            widget.resume_requested.connect(lambda w=widget: self.resume_task(w))
            widget.stop_requested.connect(lambda w=widget: self.stop_current_task(w))
            widget.removed.connect(lambda w=widget: self.remove_task_by_widget(w))
            widget.switch_page_requested.connect(self.show_transcoder_page)
            
            # [CRITICAL FIX] Always set widget on the target list
            target_list.setItemWidget(w_item, widget)
            
            # Store widget ref if we recovered it to queue
            if category == 'pending' and 'task_data' in locals():
                 # Find the dict in pending_tasks and update widget ref
                 for pt in self.pending_tasks:
                     if pt.get("source") == path:
                         pt["widget"] = widget
                         break



    def show_transcoder_page(self):
        """Switches to the Transcoder page (Index 0)."""
        self.stack.setCurrentIndex(0)
        # Update sidebar
        self.btn_home.setChecked(True)
        self.btn_dash.setChecked(False)
        self.btn_watch.setChecked(False)
        self.btn_cluster.setChecked(False)
        self.btn_settings.setChecked(False)

    def show_watch_folder_page(self): # Compatibility or removed
        self.on_nav_clicked(self.btn_watch)

    def show_cluster_page(self): # Compatibility or removed
        self.on_nav_clicked(self.btn_cluster)

    def add_watch_folder_ui(self):
        path = QFileDialog.getExistingDirectory(self, "選擇監控資料夾")
        if not path: return
        
        # 1. Name first (for convenience)
        name, ok = QInputDialog.getText(self, "監控任務名稱", "請輸入識別名稱:", QLineEdit.Normal, os.path.basename(path))
        if not (ok and name): return
        
        # 2. Preset Selection (Custom Large List)
        dlg = PresetSelectorDialog(self)
        if dlg.exec():
            preset = dlg.get_selection()
            if preset:
                current = self.settings.get("watch_folders", [])
                current.append({"name": name, "path": path, "preset": preset, "enabled": True})
                self.settings.set("watch_folders", current)
                self.save_settings()
                self.refresh_watch_list_ui()

    def edit_watch_folder(self, index):
        """Edits an existing watch folder entry."""
        watch_folders = self.settings.get("watch_folders", [])
        if not (0 <= index < len(watch_folders)): return
        
        wf = watch_folders[index]
        
        # 1. Edit Path
        path = QFileDialog.getExistingDirectory(self, "更改監控資料夾", wf.get("path"))
        if not path: return
        
        # 2. Edit Name
        name, ok = QInputDialog.getText(self, "更改監控名稱", "請輸入新名稱:", QLineEdit.Normal, wf.get("name"))
        if not (ok and name): return
        
        # 3. Edit Preset (Custom Large List)
        dlg = PresetSelectorDialog(self, initial_selection=wf.get("preset"))
        if dlg.exec():
            preset = dlg.get_selection()
            if preset:
                wf["path"] = path
                wf["name"] = name
                wf["preset"] = preset
                self.settings.set("watch_folders", watch_folders)
                self.save_settings()
                self.refresh_watch_list_ui()
                print(f"Watch Folder {index} updated.")

    def delete_watch_folder(self, index):
        """Removes a watch folder entry."""
        watch_folders = self.settings.get("watch_folders", [])
        if 0 <= index < len(watch_folders):
            reply = QMessageBox.question(self, "刪除確認", f"確定要移除監控 '{watch_folders[index].get('name')}' 嗎？", 
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                watch_folders.pop(index)
                self.settings.set("watch_folders", watch_folders)
                self.save_settings()
                self.refresh_watch_list_ui()

    def open_watch_folder_sub(self, index, sub_name):
        # [NEW v27.4] Opens _DONE or _TEMP subfolder in Explorer
        watch_folders = self.settings.get("watch_folders", [])
        if 0 <= index < len(watch_folders):
            wf = watch_folders[index]
            base_path = wf.get("path")
            if base_path:
                sub_path = os.path.join(base_path, sub_name)
                if not os.path.exists(sub_path):
                    try: 
                        os.makedirs(sub_path, exist_ok=True)
                        debug_log(f"Created subfolder: {sub_path}")
                    except Exception as e: 
                        debug_log(f"Failed to create subfolder {sub_path}: {e}")
                
                if os.path.exists(sub_path):
                    import subprocess
                    # Using startfile is safe on Windows
                    try:
                        os.startfile(os.path.normpath(sub_path))
                    except Exception as e:
                        debug_log(f"os.startfile failed for {sub_path}: {e}")
                        # Fallback for some network paths
                        norm_p = os.path.normpath(sub_path)
                        subprocess.Popen(f'explorer "{norm_p}"')
                else:
                    QMessageBox.warning(self, "存取錯誤", f"無法存取或建立路徑: {sub_path}\n請檢查權限及網路連線。")



    def update_task_progress(self, widget, percent, text):
        try:
            if not widget or getattr(widget, 'stopped', False): return # Block updates if stopped
            
            # [FIX] Race Condition Guard: If status is Done/Completed, Ignore late progress
            # This prevents 99% progress signal overwriting the 100% completion state
            lbl = getattr(widget, 'lbl_status', None)
            if lbl and (lbl.text() == "Done" or lbl.text() == "Completed" or lbl.text() == "完成 (Done)" or getattr(widget, 'state', '') == 'done'):
                # Force 100% just in case it was missed or reverted
                if widget.progress.value() < 100:
                     widget.progress.setValue(100)
                return

            # [FIX] Force UI out of "Probing" state when remote node starts sending actual progress
            if lbl and percent > 0 and ("探測中" in lbl.text() or "Probing" in lbl.text()):
                lbl.setText("Transcoding...")
                lbl.setStyleSheet("color: #4CAF50;")
                widget.state = "running"



            if percent == -1: # Indeterminate
                widget.progress.setRange(0, 0)
                widget.progress.setStyleSheet("QProgressBar { text-align: center; color: white; background-color: #222; border: none; border-radius: 0px; height: 26px; padding: 0px; }") 
            else:
                widget.progress.setValue(percent)
                # [v27.10.52] Do NOT override QSS here - widget already has unified style from __init__
                # This prevents style flickering and ensures consistent bar width
            if text:
                widget.progress.setFormat(text) # [MODIFIED] Set text on progress bar
            
            # [NEW] Sync Mirror Widget (if exists)
            task = getattr(widget, 'task_data', {})
            mirror = task.get("mirror_widget")
            if mirror:
                # Sync Progress
                mirror.progress.setRange(widget.progress.minimum(), widget.progress.maximum())
                mirror.progress.setValue(widget.progress.value())
                mirror.progress.setFormat(widget.progress.text())
                mirror.progress.setStyleSheet(widget.progress.styleSheet())
                
                # Sync Status Text & Color
                mirror.lbl_status.setText(widget.lbl_status.text())
                mirror.lbl_status.setStyleSheet(widget.lbl_status.styleSheet())

            # [NEW] Broadcast Progress to Cluster (Throttled)
            if hasattr(self, 'cluster_mgr') and percent >= 0:
                last_broad = task.get("last_broadcast_progress", -1)
                # Only broadcast every 2% to avoid spamming
                if abs(percent - last_broad) >= 2 or percent == 100:
                    task["last_broadcast_progress"] = percent
                    cluster_update = task.copy()
                    if "widget" in cluster_update: del cluster_update["widget"]
                    if "mirror_widget" in cluster_update: del cluster_update["mirror_widget"]
                    cluster_update["status"] = "Processing"
                    cluster_update["progress"] = percent
                    cluster_update["claimed_by"] = self.cluster_mgr.node_id
                    self.cluster_mgr.broadcast_task(cluster_update)
                
                # Check mirror again safely for status updates
                if mirror:
                    mirror.lbl_status.setText(widget.lbl_status.text())
                    mirror.lbl_status.setStyleSheet(widget.lbl_status.styleSheet())
                
                # Sync Icons (Approximate: we can just copy state if we had a precise way, 
                # but manually setting icons for specific states is safer)
                # We can't easily copy icon pixmaps directly to override logic, 
                # but we can trigger state updates if we tracked state changes explicitly.
                # For progress, this is sufficient. Status text covers most.
                
        except RuntimeError:
            pass # Widget likely deleted
        except Exception as e:
            debug_log(f"update_task_progress Error: {e}\n{traceback.format_exc()}")
        
        # [v27.10.72] Completion Race-Condition Protection
        if not hasattr(self, '_recently_finished'): self._recently_finished = {}
        bn = task.get("base_name")
        if bn:
            self._recently_finished[bn] = time.time()
            # Clean up old entries (> 60s)
            now = time.time()
            self._recently_finished = {k: v for k, v in self._recently_finished.items() if now - v < 60}

        # [REMOVED] Enable Play Result at 5% - User now wants ONLY at 100% DONE


    def on_transcode_finished_worker(self, task, success, msg, single_run):
        try:
            widget = task["widget"]
            # Optimization: Worker is already finished, but we keep ref until complete logic is done
            worker = self.workers.get(widget)
            
            if success:
               self.on_transcode_complete(0, QProcess.NormalExit, task, single_run)
               self.update_cluster_activity() # [v27.10.29] Immediate success reporting
            else:
                # [NEW] Intelligent Auto-Retry for Automated Tasks (WatchFolder/Node)
                source_type = task.get("source_type", "Manual")
                
                # Retry Logic
                if source_type != "Manual" and "Cancelled" not in msg:
                    retries = task.get("retry_count", 0)
                    if retries < 2:
                        task["retry_count"] = retries + 1
                        debug_log(f"Auto-Retry Task: {task.get('base_name')} (Attempt {retries+1}) triggered by {source_type}")
                        if widget:
                            # [FIX v27.9.10] Widget Validity Check to prevent crash
                            try:
                                # [FIX] Show Reason in Retry Status
                                short_reason, _ = self.analyze_error_suggestion(msg)
                                short_err = short_reason.split('\n')[0][:15] if short_reason else "Error"
                                
                                widget.lbl_status.setText(f"Retrying ({retries+1}): {short_err}...")
                                widget.lbl_status.setStyleSheet("color: #ffa726;")
                            except RuntimeError:
                                # Widget was deleted, skip UI update
                                pass
                        
                        # Do NOT archive yet. Wait for retry.
                        QTimer.singleShot(3000, lambda: self.run_transcode(task, single_run))
                        return
                    else:
                        # [NEW] Retries exhausted. Update UI with final reason.
                        if widget:
                            try:
                                reason, _ = self.analyze_error_suggestion(msg)
                                final_reason = reason.split('\n')[0] if reason else "Transcode Failed"
                                widget.lbl_status.setText(f"失敗: {final_reason}")
                                widget.lbl_status.setStyleSheet("color: #ef5350; font-weight: bold;")
                                widget.state = "failed"
                                widget.last_error_log = msg

                                # [FIX v27.10.69] Broadcast Final Failure to Cluster
                                if hasattr(self, 'cluster_mgr'):
                                    f_update = task.copy()
                                    if "widget" in f_update: del f_update["widget"]
                                    if "mirror_widget" in f_update: del f_update["mirror_widget"]
                                    f_update["status"] = "Failed"
                                    f_update["error"] = final_reason
                                    self.cluster_mgr.broadcast_task(f_update)
                            except RuntimeError:
                                # Widget was deleted, skip UI update
                                pass

                # [NEW] Archive to _ERROR ONLY AFTER Retries Exhausted
                src_path = task.get("source")
                
                # [FIX v27.9.5] Skip Archiving if file access failed (Prevents UI Freeze on network timeout)
                if "no such file" in msg.lower() or "permission denied" in msg.lower():
                     debug_log(f"Skipping Archive because source is inaccessible: {src_path}")
                elif source_type != "Manual" and src_path and os.path.exists(src_path):
                    try:
                        src_dir = os.path.dirname(src_path)
                        err_dir = os.path.join(src_dir, "ERROR")
                        if not os.path.exists(err_dir):
                            os.makedirs(err_dir)
                            
                        file_name = os.path.basename(src_path)
                        dest_path = os.path.join(err_dir, file_name)
                        
                        # Collision handling
                        if os.path.exists(dest_path):
                             base, ext = os.path.splitext(file_name)
                             timestamp = time.strftime("%Y%m%d_%H%M%S")
                             dest_path = os.path.join(err_dir, f"{base}_{timestamp}{ext}")
                        
                        # Write Error Log to File
                        log_path = os.path.splitext(dest_path)[0] + ".log"
                        try:
                            with open(log_path, 'w', encoding='utf-8') as lf:
                                lf.write(msg)
                        except: 
                            pass
                        
                        print(f"Archiving Failed Source: {src_path} -> {dest_path}")
                        success, f_msg = self._safe_move(src_path, dest_path)
                        if not success:
                            print(f"Error Archive Failed: {f_msg}")
                    except Exception as e:
                        print(f"Error Archive Exception: {e}")

            # CLEANUP WORKER IN FAILURE PATH
            if widget in self.workers:
                worker = self.workers.pop(widget)
                try:
                    worker.progress_signal.disconnect()
                    worker.finished_signal.disconnect()
                except: pass
                worker.deleteLater()

            # [FIX v27.10.5] Update Cluster Activity immediately upon completion
            self.update_cluster_activity()

            if not single_run:
                self.process_next_task()
            else:
                self.is_processing = False
        except Exception as e:
            debug_log(f"on_transcode_finished_worker Critical Error: {e}\n{traceback.format_exc()}")
            # Critical protection: always cleanup worker on error
            if widget in self.workers:
                worker = self.workers.pop(widget)
                # [NEW] Clear Activity
                self.cluster_mgr.set_local_activity("Idle")
                self.cluster_mgr.sync()
                worker.deleteLater()
            self.is_processing = False
            self.process_next_task()
        
        # [FIX] Smart Activity Update
        self.update_cluster_activity()
           
    def analyze_error_suggestion(self, log_output):
        """Analyzes FFmpeg log to provide actionable fixes."""
        if not log_output:
            return ("未知錯誤", None)
            
        log_lower = log_output.lower()
        
        # [NEW] Invalid Data (The focus of the fix)
        if "invalid data found" in log_lower:
            return ("檔案毀損或資料異常 (Invalid Data)。\n\n💡 建議：請檢查來源檔案是否完整，或是否為不支援的變體格式。", None)
            
        if "no such file" in log_lower:
            return ("找不到來源檔案或網路路徑錯誤。\n\n💡 建議：請確認 NAS 連線與檔案是否被移動。", None)

        # 1. FPS / Standard Mismatch
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

        # 4. MPEG Container Audio Codec mismatch (The error in log)
        if "mpeg" in log_lower and "unsupported audio codec" in log_lower:
             return ("MPEG 封裝格式不支援目前選取的音訊編碼 (如 AAC)。\n\n💡 建議：自動將音訊改為 MPEG 支援的 AC3 或 MP2 編碼以確保相容性。", 
                     {"acodec": "ac3"})

        if "permission denied" in log_lower:
             return ("輸出目錄無寫入權限，或磁碟空間不足。\n\n💡 請檢查目標資料夾權限。", None)

        if "unknown codec" in log_lower:
             return ("來源檔編碼無法識別，這常見於損壞的素材。\n\n💡 建議：使用主介面的 [重新解碼 (Re-Decode)] 按鈕嘗試修復。", None)

        if "invalid argument" in log_lower or "error splitting the argument list" in log_lower:
             return ("參數傳遞錯誤 (Invalid Argument)。這通常發生在路徑包含特殊字元、括號，或某些轉碼參數超出了編碼器限制。\n\n💡 建議方案：嘗試簡化輸出檔名，並套用 [相容模式]。" ,
                     {"container": "mp4", "vcodec": "libx264", "acodec": "aac"})

        if "error initializing output stream" in log_lower or "codec initialization failed" in log_lower:
             return ("編碼器初始化失敗。這可能是因為選定的硬體加速器 (如 NVENC/QSV) 正在被其他程式佔用，或來源解析度超出了硬體限制。\n\n💡 建議方案：將編碼器切換為相容性最高的 CPU 軟體編碼 (libx264)。",
                     {"vcodec": "libx264"})
            
        # 5. Default "Safety Mode" Suggestion
        # If we can't find a specific error, offer a force-compatibility fix instead of nothing.
        return ("雖然無法定位具體成因，但這類錯誤通常與路徑中的特殊字元或不相容的編碼參數有關。\n\n💡 建議方案：套用 [相容模式 (Safety Fix)]，將容器強制設為 MP4 並使用標準 H.264 編碼再次嘗試。", 
                {"container": "mp4", "vcodec": "libx264", "acodec": "aac"})

    # [FIX] Added missing history method
    def add_to_history(self, source, output, info=""):
        """Adds completed task details to global history."""
        try:
            # 1. Add to Settings History (MRU)
            if hasattr(self, 'settings'):
                self.settings.add_source_history(source)
                self.settings.add_output_history(output)
            
            # 2. Log
            print(f"History Added: {source} -> {output} ({info})")
            
        except Exception as e:
            print(f"ERROR: Failed to add to history: {e}")

    def on_transcode_complete(self, exit_code, exit_status, task, single_run):
        try:
            widget = task["widget"]
            
            # [FIX] Safety check: Widget might be deleted (e.g. Cleared List)
            import shiboken6
            if not widget or not shiboken6.isValid(widget):
                print(f"DEBUG: Widget for task {task.get('source_path')} is generated but UI object deleted. Skipping update.")
                self.process_next_task()
                return

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
                    speed_x = target_duration / (duration_sec if duration_sec > 0 else 1)
                    speed_text += f" ({speed_x:.1f}x)"
                
                # [REMOVED] Manual UI Update - Now handled by set_done after file move
                # [REMOVED] Mirror Update - Now handled by set_done after file move
                
                 # Add to History (Only if successful)
                # [NEW] Move from TEMP to Final Output Directory (for watch folder tasks)
                final_dest = task.get("final_output_dir")
                temp_output = task.get("output_path_ref")
                
                if final_dest and temp_output and os.path.exists(temp_output):
                    try:
                        # 確保最終輸出目錄存在
                        if not os.path.exists(final_dest):
                            os.makedirs(final_dest)
                        
                        # 計算最終路徑
                        final_path = os.path.join(final_dest, os.path.basename(temp_output))
                        
                        # 處理檔名衝突
                        if os.path.exists(final_path):
                            base, ext = os.path.splitext(final_path)
                            timestamp = time.strftime("%Y%m%d_%H%M%S")
                            final_path = f"{base}_{timestamp}{ext}"
                        
                        # [v27.10.48] Robust Move
                        success, m_msg = self._safe_move(temp_output, final_path)
                        if success:
                            debug_log(f"Moved completed file from TEMP to final output: {final_path}")
                        else:
                            debug_log(f"Failed to move from TEMP to final output: {m_msg}")
                            # Keep original ref if move failed
                            final_path = temp_output
                        
                        # 更新任務的輸出路徑引用為最終路徑
                        task["output_path_ref"] = final_path
                        output_path = final_path
                        
                    except Exception as move_err:
                        debug_log(f"Failed to move from TEMP to final output: {move_err}")
                        # 如果移動失敗，保持在 TEMP（至少轉碼成功了）
                        pass
                
                # [FIX] Final UI Update via set_done to ensure 100% and Play Button
                widget.set_done(output_path, self.player, speed_text)
                
                # [NEW] Broadcast Completion to Cluster
                if hasattr(self, 'cluster_mgr'):
                    cluster_update = task.copy()
                    if "widget" in cluster_update: del cluster_update["widget"]
                    cluster_update["status"] = "Done"
                    cluster_update["output_path"] = output_path
                    cluster_update["perf"] = speed_text
                    self.cluster_mgr.broadcast_task(cluster_update)
                
                # Mirror Update
                try:
                    mirror = task.get("mirror_widget")
                    import shiboken6
                    if mirror and shiboken6.isValid(mirror):
                         mirror.set_done(output_path, self.player, speed_text)
                except: pass

                # [FIX] Unify source key (Watch tasks use 'source', others may use 'source_path')
                rec_source = task.get("source") or task.get("source_path")
                self.add_to_history(rec_source, output_path, speed_text)
                
                # Check Auto-Play
                # [FIX] Safety check for missing UI element
                if hasattr(self, 'chk_auto_play') and self.chk_auto_play.isChecked():
                    if os.path.exists(output_path):
                        self.on_video_loaded(output_path, is_result=True)
            
                # [NEW] Auto-Archive Source File (Watch Folder / Automation Only)
                # Requirement: Move completed task source to _DONE or _ERROR
                source_type = task.get("source_type", "Manual")
                src_path = task.get("source") or task.get("source_path")
                
                if source_type != "Manual" and src_path and os.path.exists(src_path):
                    try:
                        print(f"DEBUG: Auto-Archiving {src_path}")
                        src_dir = os.path.dirname(src_path)
                        done_dir = os.path.join(src_dir, "DONE")
                        
                        if not os.path.exists(done_dir):
                            os.makedirs(done_dir)
                            
                        file_name = os.path.basename(src_path)
                        dest_path = os.path.join(done_dir, file_name)
                        
                        # Collision handling
                        if os.path.exists(dest_path):
                            base, ext = os.path.splitext(file_name)
                            timestamp = time.strftime("%Y%m%d_%H%M%S")
                            dest_path = os.path.join(done_dir, f"{base}_{timestamp}{ext}")
                            
                        print(f"Archiving Source: {src_path} -> {dest_path}")
                        
                        try:
                            success, a_msg = self._safe_move(src_path, dest_path)
                            if success:
                                print("Source archived successfully.")
                            else:
                                print(f"Archive Failed: {a_msg}")
                        except Exception as move_err:
                            print(f"Archive Failed: {move_err}")
                            
                    except Exception as archive_e:
                        print(f"Auto-Archive Error: {archive_e}")
            
            # [RESTORE] Update Widget State
            if widget and hasattr(widget, 'set_finished'):
                widget.set_finished()

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
            
            self.update_cluster_activity() # [v27.10.29] Final reduction call

            if not single_run:
                self.process_next_task()
            else:
                # If single task finished, check if we can fill slots from queue
                # if we were in "Start All" mode before.
                if self.is_processing:
                    self.process_next_task()
                elif not self.workers:
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
                font-family: 'Microsoft JhengHei', 'Segoe UI', sans-serif;
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
        if getattr(self, '_reset_in_progress', False):
            return
            
        to_save = []
        keys_to_save = ["source", "in_point", "out_point", "base_name", "output_dir", "sequence", "bitrate", "container", "vcodec", "acodec", "audio_gain", "resolution", "fps", "audio_ch", "assigned_to", "claimed_by"]
        
        dismissed = self.settings.get("dismissed_dashboard_items", [])
        dismissed_norms = [os.path.normpath(d).lower() for d in dismissed]

        # Save from BOTH lists
        def collect_from_list(list_widget, skip_manual_mirrors=False):
            c = list_widget.count()
            for i in range(c):
                 item = list_widget.item(i)
                 widget = list_widget.itemWidget(item)
                 if widget and widget.task_data:
                     status = widget.lbl_status.text()
                     
                     # [v27.7.1] Strictly ignore completed/failed tasks for persistence
                     # Only 'Pending', 'Transcoding...', 'Waiting...' etc should be saved.
                     ignore_parts = ["Done", "完成", "Failed", "失敗", "Error", "錯誤", "Cancelled", "取消"]
                     if any(p in status for p in ignore_parts):
                         continue
                         
                     path = widget.task_data.get("source") or widget.task_data.get("source_path")
                     if path and os.path.normpath(path).lower() in dismissed_norms:
                         continue

                     # [FIX] Do NOT save Manual Mirrors from Auto List (Duplicate Prevention)
                     s_type = widget.task_data.get("source_type", "Manual")
                     if skip_manual_mirrors and s_type == "Manual":
                         continue
                         
                     safe_data = {}
                     for k in keys_to_save:
                          if k in widget.task_data:
                               safe_data[k] = widget.task_data[k]
                      
                     safe_data["source_type"] = s_type
                     to_save.append(safe_data)

        collect_from_list(self.manual_task_list, skip_manual_mirrors=False)
        collect_from_list(self.auto_task_list, skip_manual_mirrors=True) # Skip mirrors here        
        try:
            self.settings.set("saved_queue", to_save)
            self.settings.save()  # Force immediate save
            print(f"DEBUG: Saved {len(to_save)} pending tasks to settings (Purged Finished/Failed).")
        except Exception as e:
            print(f"Error saving queue: {e}")
    
    def auto_save(self):
        """Periodic auto-save to prevent data loss"""
        try:
            self.save_pending_tasks()
            print("DEBUG: Auto-save completed")
        except Exception as e:
            print(f"Auto-save error: {e}")

    # [REMOVED DUPLICATE/BLOCKING refresh_dashboard_from_snapshot]
    # The valid, async version is defined earlier.

        
        # 1. Collect all items.
        # 2. If 'Running' (in self.workers), keep.
        # 3. If 'Manual Mirror', keep (and update?).
        # 4. Others -> Remove.
        # 5. Add new from Snapshot.
        
            

    def load_cleared_tasks(self):
        """Load list of cleared task identifiers to filter them out on restart"""
        if os.path.exists(self.cleared_tasks_file):
            try:
                with open(self.cleared_tasks_file, "r", encoding="utf-8") as f:
                    return set(json.load(f))  # Set for O(1) lookup
            except:
                return set()
        return set()
    
    def save_cleared_tasks(self):
        """Persist cleared tasks list"""
        try:
            import json
            with open(self.cleared_tasks_file, "w", encoding="utf-8") as f:
                json.dump(list(self.cleared_tasks), f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"ERROR: Failed to save cleared tasks: {e}")
    
    def mark_task_as_cleared(self, task_identifier):
        """Add task to cleared list and persist"""
        self.cleared_tasks.add(task_identifier)
        self.save_cleared_tasks()
        print(f"DEBUG: Marked task as cleared: {task_identifier}")
    
    def load_pending_tasks(self):
        saved = self.settings.get("saved_queue", [])
        if not saved: return
        
        print(f"DEBUG: Restoring {len(saved)} tasks (before filtering)...")
        filtered_count = 0
        for task_data in saved:
             # Sanity check
             if not task_data.get("base_name"): continue
             
             # [v27.10.0] FILTER CLEARED TASKS - prevent stale tasks after restart
             task_id = self.get_task_identifier(task_data)
             if task_id in self.cleared_tasks:
                 print(f"DEBUG: Skipping cleared task: {task_data.get('base_name')}")
                 filtered_count += 1
                 continue
             
             # [v27.3.4] SKIP GHOST RECORDS on Load
             source = task_data.get("source") or task_data.get("source_path")
             if source and not os.path.exists(source):
                  if task_data.get("source_type", "Manual") != "Manual":
                       print(f"DEBUG: Skipping ghost record (Source Missing): {task_data.get('base_name')}")
                       continue
             
             final_base = task_data["base_name"]
             
             # [ROUTING] Select List Widget based on Source Type
             source_type = task_data.get("source_type", "Manual")
             
             # [FIX] Restore ALL tasks (Manual & Auto)
             # This ensures WatchFolder tasks that didn't finish are resumed on restart.
             if source_type != "Manual":
                  target_list = self.auto_task_list
             else:
                  target_list = self.manual_task_list
             
             item = QListWidgetItem(target_list)
             item.setSizeHint(QSize(0, 36))
             widget = TaskProgressWidget(final_base)
             # Restore source and worker for saved tasks
             task_data["source_type"] = source_type
             task_data["worker_id"] = task_data.get("worker_id", "-")
             
             # [FIX] Restore Assignment
             if "assigned_to" in task_data:
                 task_data["assigned_to"] = task_data["assigned_to"]
             if "claimed_by" in task_data:
                 task_data["claimed_by"] = task_data["claimed_by"]
                 
             widget.set_task_data(task_data) 
             widget.lbl_status.setText("Pending")
             widget.removed.connect(self.remove_task_by_widget)
             widget.transcode_requested.connect(self.transcode_single_item)
             widget.resume_requested.connect(self.resume_task)
             widget.stop_requested.connect(self.stop_current_task)
             widget.switch_page_requested.connect(self.show_transcoder_page) # [NEW] Switch to Player Page
             
             target_list.addItem(item)
             target_list.setItemWidget(item, widget)
             task_data["widget"] = widget
             
             # [NEW] Restore Progress Text/Color
             prog = task_data.get("progress", 0)
             widget.progress.setValue(prog)
             if prog >= 100:
                  widget.lbl_status.setText("Done")
                  widget.lbl_status.setStyleSheet("color: #4CAF50;")
             
             self.pending_tasks.append(task_data)
             
        print(f"DEBUG: Restored {len(self.pending_tasks)} tasks ({filtered_count} filtered as cleared)")
        
        if self.pending_tasks:
             self.btn_start_all.setEnabled(True)
             
             # [FIX] Auto-Resume: Trigger queue to pick up restored 'Claimed' or 'Pending' tasks
             # This handles the case where app crashed while processing.
             QTimer.singleShot(1000, self.process_next_task)
    
    def get_task_identifier(self, task_data):
        """Generate unique identifier for a task based on source+basename"""
        source = task_data.get("source") or task_data.get("source_path", "")
        basename = task_data.get("base_name", "")
        # Use normalized path + basename as identifier
        return f"{os.path.normpath(source).lower()}::{basename}"

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

        QTimer.singleShot(500, self.start_transcoding_queue)



    def on_cluster_watch_config_synced(self, remote_config):
        """Updates local settings with Master's broadcasted watch folder config."""
        # Only apply if we are NOT Master (Master is the source of truth)
        if self.settings.get("cluster_role") != "Master":
             import json
             local_current = self.settings.get("watch_folders", [])
             
             # Check if actually changed
             if local_current != remote_config:
                 debug_log("Settings: Received Remote Watch Config Update.")
                 self.settings.set("watch_folders", remote_config)
                 self.settings.save()
                 self.refresh_watch_list_ui()
                 
                 if hasattr(self, 'statusBar'):
                     self.statusBar().showMessage("📈 已同步集群視窗設定 (Watch Folders Synced)", 3000)

    def on_cluster_task_synced(self, task_data):
        """Handler for tasks broadcasted by other nodes."""
        # 1. Basic Validation
        task_base = task_data.get("base_name")
        raw_source = task_data.get("source") or task_data.get("source_path")
        source_path = os.path.normpath(raw_source).lower() if raw_source else ""
        if not task_base: return

        # [v27.7.2] Strict Dismissal Check for Cluster Sync
        if source_path:
            norm_s = source_path
            dismissed = self.settings.get("dismissed_dashboard_items", [])
            if any(os.path.normpath(d).lower() == norm_s for d in dismissed):
                # [v27.8.2] Rule 2: Allow fresh Watch detections even if path was dismissed
                if not str(task_data.get("source_type", "")).startswith("Watch:"):
                    return

        # [DASHBOARD] Show all cluster tasks to all nodes (Centralized View)
        assigned_to = task_data.get("assigned_to")
        claimed_by = task_data.get("claimed_by")
        target_node = claimed_by or assigned_to
        
        # [v27.10.14] Aggressive Sync Trigger
        # Force update even if data looks similar, to Ensure UI state is consistent across nodes
        pass

        # 3. Check for existing widget
        existing_widget = None
        for i in range(self.auto_task_list.count()):
            item = self.auto_task_list.item(i)
            w = self.auto_task_list.itemWidget(item)
            if w:
                wd = getattr(w, 'task_data', {})
                # Try match by hash-based cluster filename
                w_cf = wd.get("cluster_filename")
                c_cf = task_data.get("cluster_filename")
                
                if w_cf and c_cf and w_cf == c_cf:
                    existing_widget = w
                    break
                
                # Fallback to path+name match
                w_source = os.path.normpath(wd.get("source", "")).lower()
                c_source = source_path
                if w_source == c_source and wd.get("base_name") == task_base:
                    existing_widget = w
                    break

        # [v27.10.6.2] Aggressive Ghost Pruning REMOVED to fix batch task creation instability.
        # We rely on Factory Reset and Manual Clearing now.

        # 4. Handle Update or Add
        status_check = task_data.get("status") or task_data.get("cluster_status", "Pending")

        if existing_widget:
            # UPDATE EXISTING
            wd = getattr(existing_widget, 'task_data', {})
            wd.update(task_data)
            
            # Update Node Display
            if target_node:
                alias = self.node_aliases.get(target_node, target_node)
                if hasattr(existing_widget, 'lbl_node'):
                     existing_widget.lbl_node.setText(alias)

            # [FIX] Conflict resolution: If someone else claimed it, we stop trying to run it locally
            if target_node and target_node != self.cluster_mgr.node_id:
                 # Search in pending_tasks by source/basename match [Improved Sync]
                 for t_pending in list(self.pending_tasks):
                      p_src = t_pending.get("source") or t_pending.get("source_path")
                      if os.path.normpath(p_src).lower() == source_path and t_pending.get("base_name") == task_base:
                           self.pending_tasks.remove(t_pending)
                           break
            
            # [NEW v27.10.33] Ensure assignment is synced to the pending_tasks list so process_next_task sees it
            found_in_pending = False
            for t_pending in self.pending_tasks:
                 raw_p_src = t_pending.get("source") or t_pending.get("source_path")
                 p_src = os.path.normpath(raw_p_src).lower() if raw_p_src else ""
                 if p_src == source_path and t_pending.get("base_name") == task_base:
                      t_pending["assigned_to"] = assigned_to
                      t_pending["claimed_by"] = claimed_by
                      t_pending["cluster_status"] = task_data.get("cluster_status")
                      found_in_pending = True
                      break
            
            # [FIX v27.10.33] Robust Transition: If placeholder was ready but NOT in pending_tasks yet,
            # and it's assigned to ME, add it NOW so it can actually start.
            if not found_in_pending and target_node == self.cluster_mgr.node_id:
                 is_for_me = True
                 # We let the add_task_to_queue below handle the actual append

            # UI State Transition (Buttons, Colors)
            if status_check in ["Done", "Completed"]:
                 out_path = task_data.get("output_path") or wd.get("output_path")
                 perf = task_data.get("perf") or "Done"
                 if hasattr(existing_widget, 'set_done'):
                      existing_widget.set_done(out_path, self.player, perf)
            elif status_check in ["Processing", "Transcoding"]:
                 if hasattr(existing_widget, 'set_started') and getattr(existing_widget, 'state', '') != 'running':
                      existing_widget.set_started()
                 # [FIX] If the state was already running but text is stuck on Probing, force reset it
                 elif hasattr(existing_widget, 'lbl_status') and ("探測中" in existing_widget.lbl_status.text() or "Probing" in existing_widget.lbl_status.text()):
                      existing_widget.lbl_status.setText("Transcoding...")
                      existing_widget.lbl_status.setStyleSheet("color: #4CAF50;")
            elif status_check in ["Failed", "Error"]:
                 err = task_data.get("error") or "Failed"
                 if hasattr(existing_widget, 'set_failed'):
                      existing_widget.set_failed(err)
            else:
                 if hasattr(existing_widget, 'lbl_status'):
                     existing_widget.lbl_status.setText(status_check)

            # Progress Bar (Rounding Fix)
            prog = task_data.get("progress")
            if prog is not None:
                p_val = round(float(prog))
                existing_widget.progress.setValue(p_val)
                
                # [v27.10.14] Force Status Update for 100% Stuck
                # If progress is 100% but backend still says "Transcoding", override it.
                if p_val >= 100:
                     if status_check.startswith("Transcoding"):
                         if hasattr(existing_widget, 'lbl_status'):
                             existing_widget.lbl_status.setText("Finalizing...")
                             existing_widget.lbl_status.setStyleSheet("color: #81d4fa; font-weight: bold;")

            # [FIX] Execution Trigger: If assigned to ME and Pending, start it!
            if assigned_to == self.cluster_mgr.node_id and (status_check == "Pending" or status_check == "Assigned"):
                if wd not in self.pending_tasks:
                     self.pending_tasks.append(wd)
                print(f"Cluster: Task {task_base} assigned to ME. Triggering Queue.")
                QTimer.singleShot(500, self.process_next_task)
            return

        if status_check in ["Done", "Completed"]:
             # If brand-new task is already Done, we could add it to history_list
             # For now, let's just ignore to keep the UI clean, or add to history_list
             return

        # Filtering against local UI lists (manual)
        for i in range(self.manual_task_list.count()):
            it = self.manual_task_list.item(i)
            w = self.manual_task_list.itemWidget(it)
            if w:
                h_data = getattr(w, 'task_data', {})
                if h_data.get("base_name") == task_base: return

        # All checks passed -> Add to LOCAL QUEUE
        origin = task_data.get("node_origin", "Remote")
        source_type = task_data.get("source_type", "WatchFolder")
        
        # [FIX v27.10.26] Strict Execution Responsibility
        # Only add to local 'pending_tasks' if assigned to ME.
        # Otherwise, add to UI ONLY for dashboard visibility.
        is_for_me = (assigned_to == self.cluster_mgr.node_id)
        
        self.add_task_to_queue(
            source_path=source_path,
            full_duration=True,
            source_type=source_type,
            worker_id=target_node or "Cluster", 
            base_name_override=task_base,
            extra_data={
                "broadcast_time": task_data.get("broadcast_time"),
                "node_origin": origin,
                "cluster_filename": task_data.get("cluster_filename"),
                "claimed_by": claimed_by,
                "assigned_to": assigned_to,
                "status": status_check
            },
            skip_queue=not is_for_me
        )

        # [FIX] If brand new task is already assigned to ME, pump the queue immediately
        if assigned_to == self.cluster_mgr.node_id and (status_check == "Pending" or status_check == "Assigned"):
             QTimer.singleShot(1000, self.process_next_task)
    
    
    


    def on_cluster_task_removed(self, cluster_filename):
        """
        [v27.9.4] Handles task deletion synchrnonization.
        If a task file is removed from the cluster, we must remove it from the UI.
        """
        # debug_log(f"Cluster Task Removed: {cluster_filename}")
        
        # 1. Check BOTH lists for deletion
        targets = [self.manual_task_list, self.auto_task_list]
        for target_list in targets:
            if not target_list: continue
            for i in range(target_list.count()):
                item = target_list.item(i)
                widget = target_list.itemWidget(item)
                if widget:
                    wd = getattr(widget, 'task_data', {})
                    if wd.get("cluster_filename") == cluster_filename:
                        # Found it! Remove it.
                        debug_log(f"Sync Deletion: Removing {wd.get('base_name')} from List.")
                        target_list.takeItem(i)
                        
                        # Also clean up worker if running locally
                        if widget in self.workers:
                            w = self.workers.pop(widget)
                            w.stop()
                            w.deleteLater()
                        return

        # 2. Check History List (Optional)
        # History items might not have 'cluster_filename' preserved unless we added it.
        # But usually we don't sync deletions of Done tasks from history list via cluster sync
        # because history is local.
        
        
    def on_cluster_node_updated(self, node_data):
        """Update cluster node status in the Dashboard (e.g. status bar)."""
        # print(f"DEBUG_UI: on_cluster_node_updated called with {node_data}")
        node_id = node_data.get("node_id")
        alias = node_data.get("alias") or ""
        if alias and alias != node_id:
            self.node_aliases[node_id] = alias  # [NEW] Populate Alias Map
            # [v27.10.57] Retroactive refresh: push alias to existing widgets
            try:
                def refresh_list_aliases(list_widget):
                    for i in range(list_widget.count()):
                        item = list_widget.item(i)
                        w = list_widget.itemWidget(item)
                        if not w: continue
                        td = getattr(w, 'task_data', None)
                        if not td: continue
                        wid = td.get("worker_id", "") or ""
                        wuuid = td.get("worker_uuid", "") or ""
                        # Match by node_id (full UUID or short name)
                        if wid == node_id or wuuid == node_id or alias == wid:
                            curr = w.lbl_node.text()
                            if curr != alias:
                                w.lbl_node.setText(alias)
                                w.lbl_node.setToolTip(f"{alias}\n({node_id})")
                refresh_list_aliases(self.manual_task_list)
                refresh_list_aliases(self.auto_task_list)
            except Exception as e:
                debug_log(f"Alias refresh error: {e}")
        else:
            if node_id not in self.node_aliases:
                self.node_aliases[node_id] = node_id  # fallback


        status = node_data.get("status", "")
        
        # Search for existing row
        found_item = None
        found_widget = None
        for i in range(self.node_list.count()):
            item = self.node_list.item(i)
            widget = self.node_list.itemWidget(item)
            if getattr(widget, 'node_id', '') == node_id:
                found_item = item
                found_widget = widget
                break

        # Case 1: Node Removed OR Timed Out -> Delete Row from List
        # [FIX] Auto-hide "Offline (Removed)" nodes only. Show "Timeout" for debugging.
        if status == "Offline (Removed)":
            if found_item:
                row = self.node_list.row(found_item)
                self.node_list.takeItem(row)
            return # Done
        
        # [v27.10.37] REINFORCE: Clock Skew Tolerance (1 year / Huge Future Skew)
        # If the manager says it's Online, don't show Timeout even if logic here is strict.
        if status == "Offline (Timeout)":
            last_seen = node_data.get("last_seen")
            if last_seen:
                try:
                    # Robust float/int extraction
                    if isinstance(last_seen, str):
                        try: ts = float(last_seen)
                        except: ts = 0
                    else:
                        ts = last_seen
                        
                    secs = time.time() - ts
                    # [v27.10.43] Sync with Manager: 60s threshold
                    IS_ONE_YEAR = abs(abs(secs) - 31536000) < 3600
                    if abs(secs) <= 60 or IS_ONE_YEAR: 
                         status = "Online"
                         node_data["status"] = "Online"
                except: pass

        if status == "Offline (Timeout)":
            debug_log(f"DEBUG_UI: Node {node_id} is Timed Out (Last Seen: {node_data.get('last_seen')})")


        # Case 2: Update Existing
        if found_widget:
            found_widget.update_state(node_data)
        else:
            # Case 3: New Node -> Add directly
            if status != "Offline (Removed)":
                is_me = (node_id == self.cluster_mgr.node_id)
                item = QListWidgetItem(self.node_list)
                widget = ClusterNodeRowWidget(node_id, node_data, is_local=is_me)
                item.setSizeHint(widget.sizeHint())
                self.node_list.addItem(item)
                self.node_list.setItemWidget(item, widget)
            
        if hasattr(self, 'statusBar'):
            self.statusBar().showMessage(f"Cluster: node {node_id} active", 1000)

    def on_role_changed(self, new_role):
        """[v27.10.19 UNIFIED] Called when ClusterManager detects a role change."""
        print(f"Cluster: Role Changed -> {new_role}")
        
        # 1. Update Settings & Logic state
        self.settings.set("cluster_role", new_role)
        
        if new_role == "Master":
             # [v27.10.49] Autonomous Inheritance
             # If taking over, grab the last known Master configuration
             if self.cluster_mgr.load_master_backup_settings():
                  print("Role -> Master: Successfully inherited configuration from backup.")
                  # Update internal worker if it's already running (though usually handled by engine start)
                  if hasattr(self, 'refresh_watch_list_ui'):
                       self.refresh_watch_list_ui()
        
        # 2. Update UI (Header/Status)
        if hasattr(self, 'lbl_role_status'):
            self.lbl_role_status.setText(f"目前狀態: {new_role} (Auto)")
            if new_role == "Master":
                self.lbl_role_status.setStyleSheet("color: #4CAF50; font-weight: bold; font-size: 14px;")
            else:
                self.lbl_role_status.setStyleSheet("color: #FF9800; font-weight: bold; font-size: 14px;")
        
        # [v27.10.22] FULL REVERT (User Request):
        # Restore "Original Master" UI for EVERYONE.
        is_master = (new_role == "Master") # Logically still tracked
        
        # 2b. Role-Specific Visibility -> RESTORED TO FULL VISIBILITY
        for widget_name in ['sidebar', 'params_panel', 'player', 'btn_large_add', 'btn_start_all']:
            if hasattr(self, widget_name):
                getattr(self, widget_name).setVisible(True)
                
        # [NEW] Ensure Sidebar buttons are always visible
        for btn in [self.btn_dash, self.btn_watch, self.btn_cluster, self.btn_settings, self.btn_home]:
            if hasattr(self, btn.objectName()) or btn:
                btn.setVisible(True)

        # Do NOT force switch pages anymore.
        
        # 3. Refresh Cluster Status Page
        if hasattr(self, 'refresh_cluster_ui'):
             self.refresh_cluster_ui()

        # 4. Start/Stop Watch Engine Control
        if hasattr(self, 'watch_engine'):
            if is_master:
                 if not self.watch_engine.isRunning():
                     print("Role -> Master: Starting Watch Engine")
                     self.watch_engine.start()
            else:
                 if self.watch_engine.isRunning():
                     print("Role -> Worker: Stopping Watch Engine")
                     self.watch_engine.stop()

    def closeEvent(self, event):
        """[v27.10.66] Merged robust cleanup. Saves state and fully releases handles to prevent Temp Dir warnings."""
        if getattr(self, '_reset_in_progress', False):
            event.accept()
            return
            
        try:
            print("Shutting down ProTranscoder...")
            # 1. Save state first (while handles are open)
            if hasattr(self, 'player') and hasattr(self, 'current_source') and self.current_source:
                try:
                    pos = self.player.media_player.position()
                    self.settings.update_history(self.current_source, pos)
                except: pass
            
            try: self.save_pending_tasks()
            except: pass
            try: self.save_settings()
            except: pass
            
            # 2. Stop Timers
            if hasattr(self, 'auto_save_timer'): self.auto_save_timer.stop()
            if hasattr(self, 'dongle_monitor_timer'): self.dongle_monitor_timer.stop()
            if hasattr(self, 'cluster_refresh_timer'): self.cluster_refresh_timer.stop()
                
            # 3. Force player and its sub-threads to shut down
            if hasattr(self, 'player') and self.player:
                try: self.player.shutdown()
                except: pass
                
            # 4. Stop all active transcode worker threads
            if hasattr(self, 'workers'):
                try:
                    import shiboken6
                    for widget, worker in list(self.workers.items()):
                        try:
                            if shiboken6.isValid(worker):
                                worker.blockSignals(True)
                                worker.kill() # [v27.10.66] Use kill() like line 1795 did
                                if not worker.wait(300):
                                    worker.terminate()
                                worker.deleteLater()
                        except: pass
                    self.workers.clear()
                except: pass
            
            # 5. Stop Watch Engine & Probe Threads
            if hasattr(self, 'watch_engine'):
                try:
                    self.watch_engine.stop()
                    self.watch_engine.wait(1000)
                except: pass
            
            if hasattr(self, '_watch_task_threads'):
                for t in self._watch_task_threads:
                    if t.isRunning():
                        t.quit()
                        t.wait(500)

            # 6. Stop Cluster Manager
            if hasattr(self, 'cluster_mgr'):
                try: self.cluster_mgr.stop()
                except: pass
            
            # 7. [v27.10.66] CRITICAL: Shutdown Logging and Close Handlers
            # This is the most common cause of "Failed to remove temporary directory"
            # because logging holds a file lock on debug.log inside the temp dir.
            try:
                import logging
                logging.shutdown()
                # Manually clear handlers to be 100% sure
                for h in logging.root.handlers[:]:
                    try:
                        h.close()
                        logging.root.removeHandler(h)
                    except: pass
            except: pass
            
        except Exception as e:
            print(f"Shutdown error: {e}")
            
        print("DEBUG: closeEvent complete")
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

    # [DELETED] Consolidated into on_role_changed
                     
    def stop_current_task(self, widget):
        if widget in self.workers:
            print("Requesting Stop...")
            self.workers[widget].stop()

    def toggle_watch_folder(self, index, new_state):
        watch_folders = self.settings.get("watch_folders", [])
        if 0 <= index < len(watch_folders):
            watch_folders[index]["enabled"] = new_state
            self.settings.set("watch_folders", watch_folders)
            self.settings.set("watch_folders", watch_folders)
            self.settings.save()

    def show_clear_context_menu(self, pos):
        """Show context menu for Reset History"""
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #2b2b2b; color: #eee; border: 1px solid #444; }
            QMenu::item { padding: 5px 20px; }
            QMenu::item:selected { background-color: #3d3d3d; }
        """)
        
        action_reset = menu.addAction("重置刪除記錄 (Reset Deleted History)")
        action_reset.triggered.connect(self.reset_cleared_history)
        
        btn = self.sender() if self.sender() else self.btn_clear_list
        menu.exec(btn.mapToGlobal(pos))

    def reset_cleared_history(self):
        """Clears the exclusion list so deleted tasks can be re-imported."""
        confirm = QMessageBox.question(
            self, 
            "重置記錄 (Reset History)", 
            "確定要清除所有「已刪除任務」的記錄嗎？\n\n清除後，之前刪除的任務如果檔案仍存在，將會再次被偵測並加入佇列。\n(這可以解決『第八集不出現』的問題)",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if confirm == QMessageBox.Yes:
            self.cleared_tasks.clear()
            self.save_cleared_tasks()
            QMessageBox.information(self, "已重置", "刪除記錄已清空。\n請稍候獲重新掃描 Watch Folder。")

            
            # Refresh specific row or whole list? simple refresh whole list
            self.refresh_watch_list_ui()
            
            # [FIX] Push new config to Cluster Worker so it syncs to other nodes (if Master)
            if hasattr(self, 'cluster_mgr'):
                 self.cluster_mgr.update_worker_settings({"watch_folders": watch_folders})
            
            # Engine handles the "enabled" flag dynamically in its loop
