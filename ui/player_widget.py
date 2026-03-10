from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
                               QSlider, QLabel, QStyle, QSizePolicy, QFileDialog, QStackedLayout, QSpinBox, QStyleOptionSlider,
                               QAbstractSpinBox, QToolButton, QToolTip)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtGui import QIcon, QAction, QPainter, QColor, QBrush, QPen, QFont, QLinearGradient, QPixmap, QImage, QPainterPath
from PySide6.QtCore import Qt, QUrl, QTimer, Signal, QRect, QPoint, QThread, QRectF, QSize
import random
import os
import subprocess
import ctypes # For Window Embedding

from core.analyzer import AudioLevelAnalyzer

class StereoVUMeter(QWidget): # Kept name for compatibility, but logic is Multi-channel
    def __init__(self):
        super().__init__()
        self.setFixedWidth(120) # Widen for labels
        self.levels = [-100.0] * 4 
        self.peaks = [-100.0] * 4
        self.is_realtime = False
        self.color_mode = "blue" # Default Blue (Source)
        
    def set_color_mode(self, mode):
        # mode: "blue" (Scan/Source) or "green" (Play/Result)
        self.color_mode = mode
        self.update()
        
    def setRealtime(self, val):
        self.is_realtime = val
        self.update()
        
    def setLevels(self, levels):
        # Expect list of floats
        if not isinstance(levels, list):
            levels = [levels, levels]

        while len(levels) < 4: levels.append(-100.0)

        # Apply VU Ballistics (Smoothing)
        # alpha=0.45 for snappier response (300ms to 99%)
        alpha = 0.45
        
        for i in range(4):
            target = levels[i]
            if len(self.levels) <= i: self.levels.append(target)
            current = self.levels[i]
            
            # Smoothing
            self.levels[i] = current + (target - current) * alpha

            # Peak Hold
            if levels[i] > self.peaks[i]:
                self.peaks[i] = levels[i]
            else:
                 # [OPTIMIZED] Fast decay (3.0dB per frame) for clean look
                 self.peaks[i] = max(self.peaks[i] - 3.0, levels[i])

        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)

            w = self.width()
            h = self.height()

            if w <= 0 or h <= 0: return

            # Dark Background
            painter.fillRect(0, 0, w, h, QColor(0, 0, 0))

            # Vertical Margins for labels
            top_margin = 25
            bottom_margin = 20
            inner_h = h - top_margin - bottom_margin
            
            if inner_h <= 0: return

            # Layout - SHIFT LEFT to prevent right-side clipping
            bar_w = 10 
            gap = 2
            group_gap = 6
            left_margin = 32 # Shifted from 48
            
            x1 = left_margin
            x2 = x1 + bar_w + gap
            x3 = x2 + bar_w + group_gap
            x4 = x3 + bar_w + gap
            bar_positions = [x1, x2, x3, x4]
            
            left_ticks_x = left_margin - 3
            right_ticks_x = x4 + bar_w + 3

            def db_to_y(val):
                min_db = -60
                max_db = 0
                pct = (val - min_db) / (max_db - min_db)
                pct = max(0, min(1, pct))
                return int(round(h - bottom_margin - (pct * inner_h)))

            def draw_bar(x, level, peak):
                painter.fillRect(x, top_margin, bar_w, inner_h, QColor(25, 25, 25))
                y_peak = db_to_y(peak)
                
                block_h = 2 
                gap_h = 1 
                step = block_h + gap_h
                curr_y = h - bottom_margin - step
                
                while curr_y >= top_margin:
                    pct = (h - bottom_margin - curr_y) / inner_h
                    val = -60 + (60 * pct)
                    
                    val = -60 + (60 * pct)
                    if self.color_mode == "green":
                         # Green Color Scheme (Result)
                         if val <= -18.0: c = QColor(0, 200, 0)
                         elif val <= -10.0: c = QColor(255, 200, 0)
                         else: c = QColor(255, 40, 40)
                    else:
                         # Blue Color Scheme (Source)
                         if val <= -18.0: c = QColor(0, 150, 255) # Light Blue
                         elif val <= -10.0: c = QColor(255, 200, 0) # Yellow (Same as Green mode)
                         else: c = QColor(255, 40, 40) # Red Peak

                    # [v27.10.91] Add 0.5dB epsilon to ensure the block renders exactly on the tick line 
                    # without falling 1 pixel short due to step truncation
                    if val <= level + 0.5:
                        painter.fillRect(x, curr_y, bar_w, block_h, c)
                    else:
                        painter.fillRect(x, curr_y, bar_w, block_h, QColor(40, 40, 40))
                    curr_y -= step

                if peak > -58:
                    painter.setPen(QPen(QColor(255, 255, 255), 1))
                    painter.drawLine(x, y_peak, x+bar_w, y_peak)
                
            for i in range(4):
                 if i < len(self.levels):
                    draw_bar(bar_positions[i], self.levels[i], self.peaks[i])
            
            # Draw SCALES
            font = QFont("Segoe UI")
            font.setPixelSize(9) # Use Pixel Size for consistency across DPI
            painter.setFont(font)
            dbfs_ticks = [0, -10, -18, -23, -30, -40, -60] # [v27.10.92] Realign 0 VU to -18 dBFS for Taiwan Broadcast Standard
            fm = painter.fontMetrics()
            v_off = fm.ascent() // 2 - 1

            # Headers (Concentrated at top)
            header_font = QFont("Segoe UI")
            header_font.setPixelSize(10)
            header_font.setBold(True)
            painter.setFont(header_font)
            painter.setPen(QColor(200, 200, 200))
            # Center Headers over the scale columns
            painter.drawText(left_ticks_x - 18, top_margin - 6, "VU")
            painter.drawText(right_ticks_x + 2, top_margin - 6, "dB")
            
            painter.setFont(QFont("Segoe UI", 7)) # Restore normal font

            for db in dbfs_ticks:
                y = db_to_y(db)
                is_ref = (db == -23)
                is_tone = (db == -18)
                t_len = 8 if (db==0 or is_ref or is_tone) else 5
                
                if db == 0: color = QColor(255, 60, 60)
                elif is_ref: color = QColor(200, 200, 200)
                elif is_tone: color = QColor(0, 255, 255) # Cyan for 1kHz Reference
                else: color = QColor(150, 150, 150)
                painter.setPen(color)
                
                # Right Side: dBFS (Number only)
                painter.drawLine(right_ticks_x, y, right_ticks_x + t_len, y)
                painter.drawText(right_ticks_x + t_len + 4, y + v_off, str(db))
                
                # Left Side: VU (Ref -18dBFS = 0VU) (Number only)
                vu_val = db + 18
                label = f"{vu_val:+d}" if vu_val != 0 else "0"
                if is_ref: painter.setPen(QColor(150, 150, 150)) # -5 VU
                elif is_tone: painter.setPen(QColor(0, 255, 0)) # Highlight 0 VU Green
                
                painter.drawLine(left_ticks_x, y, left_ticks_x - t_len, y)
                painter.drawText(left_ticks_x - t_len - fm.horizontalAdvance(label) - 2, y + v_off, label)

            # Status
            painter.setFont(QFont("Segoe UI", 7, QFont.Bold))
            if self.is_realtime:
                painter.setPen(QColor(0, 255, 0))
                painter.drawText(2, h - 4, "REAL")
            else:
                painter.setPen(QColor(100, 100, 100))
                painter.drawText(2, h - 4, "SCAN..")
        finally:
            painter.end()

class FPSWorker(QThread):
    result_ready = Signal(float)

    def __init__(self, file_path):
        super().__init__()
        self.file_path = file_path
        self.process = None

    def stop(self):
        if self.process:
            try:
                self.process.terminate()
            except: pass

    def run(self):
        try:
            cmd = ["ffprobe", "-v", "0", "-of", "csv=p=0", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-analyzeduration", "10000000", "-probesize", "10000000", self.file_path]
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                
            # [v27.10.17] Fix Ghost Windows
            flags = 0x08000000 if os.name == 'nt' else 0
            self.process = subprocess.Popen(cmd, capture_output=True, text=True, startupinfo=startupinfo, creationflags=flags)
            res, _ = self.process.communicate()
            res = res.strip()
            
            fps = 25.0
            if "/" in res:
                num, den = map(int, res.split("/"))
                fps = num / den if den != 0 else 25.0
            else:
                fps = float(res) if res else 25.0
            self.result_ready.emit(fps)
        except Exception:
            self.result_ready.emit(25.0)

class ThumbnailWorker(QThread):
    result_ready = Signal(QPixmap)

    def __init__(self, file_path, pos_ms):
        super().__init__()
        self.file_path = file_path
        self.pos_ms = pos_ms
        self.process = None

    def stop(self):
        if self.process:
            try:
                self.process.terminate()
            except: pass

    def run(self):
        try:
            # -ss before -i is faster. -f image2 pipe:1 outputs raw image to stdout.
            ss_time = self.pos_ms / 1000.0
            # Use mjpeg format explicitly for pipe output
            cmd = [
                "ffmpeg", "-ss", str(ss_time), "-i", self.file_path,
                "-frames:v", "1", "-c:v", "mjpeg", "-f", "mjpeg", "-",
                "-loglevel", "error"
            ]
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            # [v27.10.17] Fix Ghost Windows
            flags = 0x08000000 if os.name == 'nt' else 0
            self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, startupinfo=startupinfo, creationflags=flags)
            try:
                # Add Timeout to prevent freeze on corrupt files
                image_data, _ = self.process.communicate(timeout=1.5) 
                
                if image_data:
                    pixmap = QPixmap()
                    if pixmap.loadFromData(image_data):
                        self.result_ready.emit(pixmap)
            except subprocess.TimeoutExpired:
                print("Thumbnail gen timed out - killing")
                self.process.kill()
        except Exception as e:
            print(f"Thumbnail error: {e}")

class FloatingTimeLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        # Use ToolTip flag to ensure it floats above NATIVE windows (HW Video)
        self.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("""
            QLabel {
                color: #ffffff !important; 
                background-color: rgba(0, 0, 0, 240); 
                border-radius: 4px; 
                padding: 5px 10px; 
                font-family: 'Consolas', monospace;
                font-weight: bold;
                font-size: 13px;
                border: 1px solid #ffffff; 
            }
        """)
        self.adjustSize()
        self.hide()

class ClickableOverlay(QWidget):
    clicked = Signal()
    def __init__(self, parent=None):
        super().__init__(parent)
        # Force hit-test validity with nearly transparent background
        self.setStyleSheet("background-color: rgba(255, 255, 255, 1);")
    
    def mousePressEvent(self, event):
        # Only trigger if clicking empty space (not children like Slider)
        self.clicked.emit()
        event.accept()
        super().mousePressEvent(event)





class ClickableVideoWidget(QVideoWidget):
    clicked = Signal()
    def __init__(self, parent=None):
        super().__init__(parent)
    
    def mousePressEvent(self, event):
        self.clicked.emit()
        event.accept()
        # Do NOT call super().mousePressEvent(event) to avoid native window conflicts


class TrimSlider(QSlider):
    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.mark_in = None
        self.mark_out = None
        self.qc_markers = [] # List of ms timestamps
        self.duration = 0

    def set_markers(self, mark_in, mark_out, duration):
        self.mark_in = mark_in
        self.mark_out = mark_out
        self.duration = duration
        self.update()

    def set_qc_markers(self, markers):
        """Set a list of dictionaries [{'time': ms, 'label': str}] to show as anomaly markers"""
        self.qc_markers = markers if markers else []
        self.setMouseTracking(True)
        self.update()

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if not self.qc_markers or self.duration <= 0: return
        
        pos_ratio = event.pos().x() / self.width()
        hover_ms = int(pos_ratio * self.duration)
        
        tolerance = max(self.duration * 0.01, 500) # Give it 1% of duration or at least half a second hit area
        
        for m in self.qc_markers:
            t = m["time"] if isinstance(m, dict) else m
            label = m["label"] if isinstance(m, dict) else "QC Marker"
            
            if abs(hover_ms - t) < tolerance:
                s = int(t / 1000)
                time_str = f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"
                QToolTip.showText(event.globalPos(), f"{label} @ {time_str}", self)
                return
        QToolTip.hideText()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.duration <= 0: return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        y_pos = (self.height() // 2) - 2
        
        # 1. Draw QC Anomaly Markers (Orange dots)
        if self.qc_markers:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 152, 0)) # Orange
            for m in self.qc_markers:
                ms = m["time"] if isinstance(m, dict) else m
                if 0 <= ms <= self.duration:
                    x = int((ms / self.duration) * self.width())
                    painter.drawEllipse(x - 2, y_pos - 6, 4, 4) # Small dot above the bar

        # 2. Draw Trim Range
        in_x = 0
        out_x = self.width()
        
        valid_range = False
        if self.mark_in is not None:
             in_pos = self.mark_in / self.duration
             in_x = int(in_pos * self.width())
             valid_range = True
        
        if self.mark_out is not None:
             out_pos = self.mark_out / self.duration
             out_x = int(out_pos * self.width())
             valid_range = True
             
        if valid_range:
            # Clamp
            in_x = max(0, in_x)
            out_x = min(self.width(), out_x)
            width = max(0, out_x - in_x)
            
            # Draw Range Bar (Green Highlight)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 200, 83, 180)) # Green semi-transparent
            painter.drawRect(in_x, y_pos, width, 4)
            
            # Draw Markers (Vertical Lines)
            painter.setPen(QColor(0, 255, 0))
            if self.mark_in is not None:
                painter.drawLine(in_x, y_pos - 4, in_x, y_pos + 8)
            if self.mark_out is not None:
                painter.drawLine(out_x, y_pos - 4, out_x, y_pos + 8)
            
        painter.end()


class VideoPlayerWidget(QWidget):
    videoLoaded = Signal(str, bool)

    def __init__(self):
        super().__init__()
        self.setFocusPolicy(Qt.StrongFocus) # For keyboard shortcuts
        self.setup_ui()
        self.in_point = None
        self.out_point = None
        self.original_source = None
        self.current_file = None
        self.last_result_file = None # Track result for toggling
        self.ffplay_path = "ffplay" # Default, will be injected
        self.source_position = 0
        self.result_position = 0 # Track result position
        self._is_loading_new = False
        self.fps = 25.0
        
        # Timer for floating label update during playback
        self.float_timer = QTimer(self)
        self.float_timer.timeout.connect(self.update_floating_time)
        # self.float_timer.start(50) # [DISABLED] Only update on drag
        
        self._thread_pool = [] # Zombie thread keeper

    def set_qc_markers(self, markers):
        """Pass markers to the custom slider"""
        if hasattr(self, 'slider'):
            self.slider.set_qc_markers(markers)

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(5)

        # A. Main Row (Video + VU Side Panel)
        upper_row = QHBoxLayout()
        upper_row.setSpacing(2)
        
        # 1. Video Container (Includes Video + Control Bar below it)
        self.video_container = QWidget()
        self.video_container.setStyleSheet("background-color: black;")
        vc_layout = QVBoxLayout(self.video_container)
        vc_layout.setContentsMargins(0, 0, 0, 0)
        vc_layout.setSpacing(0)
        
        # Layer 0: Video Output
        # Using Stack just for Preview Overlay (Thumbnail) which needs to cover video
        self.video_stack_widget = QWidget()
        self.video_stack = QStackedLayout(self.video_stack_widget)
        self.video_stack.setStackingMode(QStackedLayout.StackAll)
        
        # Layer 1: Native Video Widget (Standard, Non-Clickable)
        self.video_widget = QVideoWidget()
        self.video_stack.addWidget(self.video_widget)
        
        # Layer 2: Preview Overlay (Thumbnail)
        self.preview_overlay = QLabel()
        self.preview_overlay.setAlignment(Qt.AlignCenter)
        self.preview_overlay.setScaledContents(True)
        self.preview_overlay.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        # Transparent for mouse events so clicks go through to SHIELD
        self.preview_overlay.setAttribute(Qt.WA_TransparentForMouseEvents) 
        self.preview_overlay.hide()
        self.video_stack.addWidget(self.preview_overlay)
        
        # Layer 3: Input Shield (Transparent Button)
        # COVERS the entire video area and captures all clicks safely
        self.btn_click_shield = QPushButton()
        self.btn_click_shield.setFlat(True)
        self.btn_click_shield.setStyleSheet("background-color: transparent; border: none;")
        self.btn_click_shield.setCursor(Qt.PointingHandCursor)
        self.btn_click_shield.clicked.connect(self.toggle_playback)
        self.video_stack.addWidget(self.btn_click_shield)
        
        # Ensure Shield is TOP
        self.btn_click_shield.raise_()
        
        # Add Video Stack to Container
        vc_layout.addWidget(self.video_stack_widget, 1) # Expand
        
        # --- Control Bar Container (Below Video) ---
        self.control_bar = QWidget()
        self.control_bar.setStyleSheet("""
            QWidget {
                background-color: #1a1a1a; 
                border-top: 1px solid #333;
            }
            QLabel {
                background-color: transparent;
                color: #ffffff;
                font-family: 'Consolas', monospace;
                font-weight: bold;
                font-size: 13px;
                border: none;
                padding: 0px 5px;
            }
        """)
        self.control_bar.setFixedHeight(36)
        
        bar_layout = QHBoxLayout(self.control_bar)
        bar_layout.setContentsMargins(10, 0, 10, 0)
        bar_layout.setSpacing(10)
        
        # Current Time Label
        self.lbl_current_time = QLabel("00:00:00:00")
        bar_layout.addWidget(self.lbl_current_time)
        
        # Deinterlace Preview Button (Pop-out)
        # Deinterlace Toggle Button (In-Place)
        # Deinterlace Preview Button (Pop-out with Slider)
        self.btn_deint = QToolButton()
        # "Icon Size and Style consistent with Head/Tail buttons" -> 26x26
        # Use existing geometric icon
        self.btn_deint.setIcon(self.create_geometric_icon("eye", "#E0E0E0", size=32))
        self.btn_deint.setToolTip("去交織預覽視窗 (Deinterlace Window)\n開啟獨立視窗檢查場序")
        self.btn_deint.setFixedSize(26, 26) 
        self.btn_deint.setStyleSheet(self.get_btn_style("transparent")) 
        self.btn_deint.clicked.connect(self.open_deinterlace_window)
        bar_layout.addWidget(self.btn_deint)
        
        # Jump Buttons - Use geometric icons (black/white style)
        btn_style = "QToolButton { background-color: transparent; border: 1px solid #444; border-radius: 3px; } QToolButton:hover { background-color: #444; border-color: #888; }"
        
        self.btn_seek_in = QToolButton()
        self.btn_seek_in.setIcon(self.create_geometric_icon("skip_back", "#E0E0E0", size=24))
        self.btn_seek_in.setIconSize(QSize(18, 18))
        self.btn_seek_in.setToolTip("回到開頭 (Go to Start)")
        self.btn_seek_in.setFixedSize(26, 26)
        self.btn_seek_in.setStyleSheet(btn_style)
        self.btn_seek_in.clicked.connect(self.seek_to_in)
        bar_layout.addWidget(self.btn_seek_in)

        self.btn_seek_out = QToolButton()
        self.btn_seek_out.setIcon(self.create_geometric_icon("skip_forward", "#E0E0E0", size=24))
        self.btn_seek_out.setIconSize(QSize(18, 18))
        self.btn_seek_out.setToolTip("跳至結尾 (Go to End)")
        self.btn_seek_out.setFixedSize(26, 26)
        self.btn_seek_out.setStyleSheet(btn_style)
        self.btn_seek_out.clicked.connect(self.seek_to_out)
        bar_layout.addWidget(self.btn_seek_out)
        
        # Clear IN/OUT Button
        self.btn_clear_markers = QToolButton()
        self.btn_clear_markers.setIcon(self.create_geometric_icon("x", "#ff6b6b", size=24))
        self.btn_clear_markers.setIconSize(QSize(18, 18))
        self.btn_clear_markers.setToolTip("清除入出點 (Clear IN/OUT)")
        self.btn_clear_markers.setFixedSize(26, 26)
        self.btn_clear_markers.setStyleSheet("QToolButton { background-color: transparent; border: 1px solid #444; border-radius: 3px; } QToolButton:hover { background-color: #442222; border-color: #ff6b6b; }")
        self.btn_clear_markers.clicked.connect(self.clear_in_out_points)
        bar_layout.addWidget(self.btn_clear_markers)

        # QC Jump Buttons (Visible only when QC markers present)
        self.btn_prev_qc = QToolButton()
        self.btn_prev_qc.setIcon(self.create_geometric_icon("skip_back", "#FF9800", size=24)) 
        self.btn_prev_qc.setIconSize(QSize(18, 18))
        self.btn_prev_qc.setToolTip("上一個異常點 (Prev Anomaly)")
        self.btn_prev_qc.setFixedSize(26, 26)
        self.btn_prev_qc.setStyleSheet("QToolButton { background-color: transparent; border: 1px solid #FF9800; border-radius: 3px; } QToolButton:hover { background-color: #332200; border-color: #FFB74D; }")
        self.btn_prev_qc.hide() 
        self.btn_prev_qc.clicked.connect(self.jump_to_prev_qc)
        bar_layout.addWidget(self.btn_prev_qc)

        self.btn_next_qc = QToolButton()
        self.btn_next_qc.setIcon(self.create_geometric_icon("skip_forward", "#FF9800", size=24))
        self.btn_next_qc.setIconSize(QSize(18, 18))
        self.btn_next_qc.setToolTip("下一個異常點 (Next Anomaly)")
        self.btn_next_qc.setFixedSize(26, 26)
        self.btn_next_qc.setStyleSheet("QToolButton { background-color: transparent; border: 1px solid #FF9800; border-radius: 3px; } QToolButton:hover { background-color: #332200; border-color: #FFB74D; }")
        self.btn_next_qc.hide()
        self.btn_next_qc.clicked.connect(self.jump_to_next_qc)
        bar_layout.addWidget(self.btn_next_qc)

        # Timeline Slider
        self.slider = TrimSlider(Qt.Horizontal)
        self.slider.setCursor(Qt.PointingHandCursor)
        self.slider.setStyleSheet("""
            QSlider::groove:horizontal {
                border: 1px solid #444;
                height: 6px;
                background: #333;
                margin: 0px;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #ffffff;
                border: 1px solid #777;
                width: 14px;
                height: 14px;
                margin: -4px 0;
                border-radius: 7px;
            }
            QSlider::sub-page:horizontal {
                background: #0078d4;
                border-radius: 3px;
            }
            QSlider::add-page:horizontal {
                background: #2a2a2a;
                border-radius: 3px;
            }
        """)
        self.slider.sliderMoved.connect(self.set_position)
        self.slider.sliderPressed.connect(self.on_slider_pressed)
        self.slider.sliderReleased.connect(self.on_slider_released)
        self.slider.valueChanged.connect(self.update_floating_time)
        bar_layout.addWidget(self.slider)
        
        # Duration Label
        self.lbl_total_time = QLabel("00:00:00:00")
        bar_layout.addWidget(self.lbl_total_time)
        
        vc_layout.addWidget(self.control_bar)
        
        upper_row.addWidget(self.video_container, 1)

        # Floating Time Label (Parented to self to float over everything including video bottom)
        self.lbl_float_time = FloatingTimeLabel(self)



        # 2. Side Panel (VU Meter)
        side_panel = QWidget()
        side_panel.setFixedWidth(120) # Match VU meter width to avoid clipping
        side_layout = QVBoxLayout(side_panel)
        side_layout.setContentsMargins(2, 0, 2, 0)
        
        self.vu = StereoVUMeter()
        side_layout.addWidget(self.vu)
        
        self.sb_vu_offset = QSpinBox()
        self.sb_vu_offset.setRange(-1000, 1000)
        self.sb_vu_offset.setValue(150)
        self.sb_vu_offset.setSuffix(" ms")
        self.sb_vu_offset.setStyleSheet("background-color: #333; color: white; border: none;")
        self.sb_vu_offset.valueChanged.connect(self.set_vu_offset)
        side_layout.addWidget(self.sb_vu_offset)
        
        upper_row.addWidget(side_panel)
        
        main_layout.addLayout(upper_row, 1) # Video Area takes mostly space

        # B. Bottom Info Row (IN / OUT / DUR) - Below Screen
        data_layout = QHBoxLayout()
        data_layout.setContentsMargins(5, 0, 5, 0)
        
        base_style = "color: #ffffff; font-weight: bold; padding: 4px 8px; border-radius: 4px; font-size: 12px; border: 1px solid #333;"
        
        self.lbl_in = QLabel("IN: --")
        self.lbl_in.setStyleSheet(base_style)
        
        self.lbl_out = QLabel("OUT: --")
        self.lbl_out.setStyleSheet(base_style)
        
        self.lbl_dur = QLabel("DUR: --")
        self.lbl_dur.setStyleSheet(base_style)
        
        data_layout.addWidget(self.lbl_in)
        data_layout.addStretch()
        data_layout.addWidget(self.lbl_out)
        data_layout.addStretch()
        data_layout.addWidget(self.lbl_dur)
        
        main_layout.addLayout(data_layout)

        # Setup Media Player
        self.media_player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.media_player.setAudioOutput(self.audio_output)
        self.media_player.setVideoOutput(self.video_widget)
        
        # Signals
        self.media_player.positionChanged.connect(self.update_position)
        self.media_player.durationChanged.connect(self.update_duration)
        self.media_player.mediaStatusChanged.connect(self._on_media_ready)
        
        self.vu_timer = QTimer(self)
        self.vu_timer.timeout.connect(self.update_vu)
        self.vu_timer.start(20) # 50fps for smooth segments
        self.vu_offset_ms = 120 # Reduced for tighter response

    def set_qc_markers(self, markers_data):
        """Pass QC marker timestamps to the UI components (like slider)"""
        if hasattr(self, 'slider') and hasattr(self.slider, 'set_qc_markers'):
            self.slider.set_qc_markers(markers_data)
        self._cached_qc_markers = markers_data
        
        if hasattr(self, 'btn_prev_qc'):
            if markers_data:
                self.btn_prev_qc.show()
                self.btn_next_qc.show()
            else:
                self.btn_prev_qc.hide()
                self.btn_next_qc.hide()

    def jump_to_prev_qc(self):
        if not getattr(self, '_cached_qc_markers', None): return
        current_ms = self.media_player.position()
        prev_ms = 0
        for m in reversed(self._cached_qc_markers):
            t = m["time"] if isinstance(m, dict) else m
            if t < current_ms - 500: # 500ms buffer so we don't get stuck on current marker
                prev_ms = t
                break
        self.media_player.setPosition(prev_ms)

    def jump_to_next_qc(self):
        if not getattr(self, '_cached_qc_markers', None): return
        current_ms = self.media_player.position()
        next_ms = self.media_player.duration()
        for m in self._cached_qc_markers:
            t = m["time"] if isinstance(m, dict) else m
            if t > current_ms + 500:
                next_ms = t
                break
        self.media_player.setPosition(next_ms)

    def update_floating_time(self):
        # [MODIFIED] Only show if slider is being dragged
        if not self.slider.isSliderDown():
            self.lbl_float_time.hide()
            return

        if self.slider.isVisible():
            val = self.slider.value()
            self.lbl_float_time.setText(self.format_time(val))
            self.lbl_float_time.adjustSize()
            
            # Accurate Handle Position
            opt = QStyleOptionSlider()
            self.slider.initStyleOption(opt)
            rect = self.slider.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self.slider)
            
            # Slider Position (Global Screen Coordinates)
            # Critical: Use slider.mapToGlobal for top-level window positioning
            slider_pos = self.slider.mapToGlobal(QPoint(0, 0))
            
            # Calculate Label Position
            handle_center_x = slider_pos.x() + rect.x() + rect.width() / 2
            lbl_x = int(handle_center_x - self.lbl_float_time.width() / 2)
            # Position ABOVE the slider
            lbl_y = slider_pos.y() + rect.y() - self.lbl_float_time.height() - 8
            
            self.lbl_float_time.move(lbl_x, lbl_y)
            if not self.lbl_float_time.isVisible():
                 self.lbl_float_time.show()

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
        return """
            QToolButton { 
                background: transparent; 
                border: 1px solid #555; 
                border-radius: 4px; 
            } 
            QToolButton:hover { background: #444; border: 1px solid #777; }
            QToolButton:pressed { background: #222; }
        """

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
        
        elif shape == "skip_back":
            painter.setBrush(QColor(color))
            painter.setPen(Qt.NoPen)
            # Left Bar (Thickness 3)
            # x=2, y=2, w=3, h=s-4
            painter.drawRect(m, m+2, 3, s-4)
            
            # Left Triangle
            # Pointing Left. Tip at x=7 (m+5). Base at m+s-2.
            path = QPainterPath()
            path.moveTo(m+s-2, m+2)        # Top-Right
            path.lineTo(m+s-2, m+s-2)      # Bottom-Right
            path.lineTo(m+5, m+s/2)        # Left Tip
            path.closeSubpath()
            painter.drawPath(path)
        
        elif shape == "skip_forward":
            painter.setBrush(QColor(color))
            painter.setPen(Qt.NoPen)
            # Right Bar (Thickness 3)
            # Mirror of Left Bar. x = size - m - 3.
            painter.drawRect(size - m - 3, m+2, 3, s-4)
            
            # Right Triangle
            # Pointing Right. Tip at x = size - m - 5. Base at m+2.
            path = QPainterPath()
            path.moveTo(m+2, m+2)          # Top-Left
            path.lineTo(m+2, m+s-2)        # Bottom-Left
            path.lineTo(size - m - 5, m+s/2) # Right Tip
            path.closeSubpath()
            painter.drawPath(path)
            
        elif shape == "close" or shape == "x":
            painter.setPen(QPen(QColor(color), 3))
            painter.drawLine(m+4, m+4, m+s-4, m+s-4)
            painter.drawLine(m+s-4, m+4, m+4, m+s-4)

        elif shape == "eye":
            painter.setPen(QPen(QColor(color), 2))
            painter.setBrush(Qt.NoBrush)
            
            # Almond shape (More "Standard")
            path = QPainterPath()
            # Start left
            path.moveTo(m, m + s/2)
            # Top curve
            path.quadTo(m + s/2, m, m + s, m + s/2)
            # Bottom curve
            path.quadTo(m + s/2, m + s, m, m + s/2)
            painter.drawPath(path)
            
            # Pupil (Centered, correct size)
            painter.setBrush(QColor(color))
            painter.setPen(Qt.NoPen)
            # Size 35% of container
            ps = s * 0.35
            painter.drawEllipse(QRectF(m + s/2 - ps/2, m + s/2 - ps/2, ps, ps))
            
            # Reflection (Tiny white dot for "Gloss")
            painter.setBrush(QColor("white"))
            painter.drawEllipse(QRectF(m + s/2 + ps/4, m + s/2 - ps/4, ps/4, ps/4))
            
        painter.end()
        return QIcon(pixmap)


    def on_slider_pressed(self):
        self.media_player.pause()
        self.update_floating_time()

    def on_slider_released(self):
        val = self.slider.value()
        self.media_player.setPosition(val)
        self.lbl_float_time.hide()
        
        # [NEW] Restart Analyzer on Seek (to avoid lag/freeze)
        self.restart_analyzer(val)

    def restart_analyzer(self, start_ms):
        if not self.current_file: return
        
        # Debounce/Check if significantly different? 
        # For now, just do it.
        start_sec = start_ms / 1000.0
        # print(f"DEBUG: Restarting Analyzer from {start_sec}s")
        
        if hasattr(self, 'analyzer') and self.analyzer:
             self._safe_stop_thread(self.analyzer)
        
        # NOTE: We keep existing dict data, but new data will overwrite/fill
        from core.analyzer import AudioLevelAnalyzer
        self.analyzer = AudioLevelAnalyzer(self.current_file, interval=0.025, start_time=start_sec)
        self.analyzer.level_found.connect(self.on_level_found)
        self.analyzer.start()

    
    # ... inside setup_ui or init ...
    # We need to import QStyleOptionSlider


    def seek_to_in(self):
        # Logic: If IN point set, go to IN. Else go to Start.
        target = 0
        if getattr(self, '_is_playing_result', False):
            target = 0
        elif self.in_point is not None:
            target = self.in_point
        else:
            target = 0 # Start of file
            
        # Optimization: Don't seek if already very close (e.g. within 100ms)
        current = self.media_player.position()
        if abs(current - target) < 100:
            return

        self.media_player.setPosition(target)
        self.media_player.pause()
        self.restart_analyzer(target)
        self.update_vu(force=True)
            
    def seek_to_out(self):
        # Logic: If OUT point set, go to OUT. Else go to End.
        dur = self.media_player.duration()
        target = max(0, dur - 40) # Default to end (last frame)
        
        if getattr(self, '_is_playing_result', False):
             pass # Already target set to end
        elif self.out_point is not None:
            target = self.out_point
            
        # Optimization: Don't seek if already very close
        current = self.media_player.position()
        if abs(current - target) < 100:
            return

        self.media_player.setPosition(target)
        self.media_player.pause()
        self.restart_analyzer(target)
        self.update_vu(force=True)

    def update_trim_labels(self):
        # Helper to style labels
        def set_style(lbl, active):
            base = "padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; border: 1px solid #333;"
            if active:
                # Highlight: White Background, Black Text
                lbl.setStyleSheet(base + "background-color: #eee; color: #000; border: 1px solid #fff;")
            else:
                lbl.setStyleSheet(base + "background-color: transparent; color: #777;")

        dirty_mark = " *" if getattr(self, '_is_dirty', False) else ""
        
        in_txt = self.format_time(self.in_point) if self.in_point is not None else "--"
        out_txt = self.format_time(self.out_point) if self.out_point is not None else "--"
        
        dur = 0
        if self.in_point is not None and self.out_point is not None:
            dur = self.out_point - self.in_point
        dur_txt = self.format_time(dur) if dur > 0 else "--"
        
        # Update Trim Labels
        if hasattr(self, 'lbl_in'):
            self.lbl_in.setText(f"IN: {in_txt}{dirty_mark}")
            set_style(self.lbl_in, self.in_point is not None)
            
        if hasattr(self, 'lbl_out'):
            self.lbl_out.setText(f"OUT: {out_txt}{dirty_mark}")
            set_style(self.lbl_out, self.out_point is not None)
        
        # [NEW] Force Slider Marker Update
        # Use slider maximum if duration() is not yet available (for responsiveness)
        disp_dur = 0
        if hasattr(self, 'media_player') and self.media_player:
            disp_dur = self.media_player.duration()
            
        if disp_dur <= 0: 
            # Fallback to slider max if loaded
            if hasattr(self, 'slider'):
                disp_dur = self.slider.maximum()
        
        if disp_dur > 0 and hasattr(self, 'slider'):
            self.slider.set_markers(self.in_point, self.out_point, disp_dur)
        
        if hasattr(self, 'lbl_dur'):
            self.lbl_dur.setText(f"DUR: {dur_txt}")
            set_style(self.lbl_dur, dur > 0)
        
        # [NEW] Update button tooltips based on marker state
        # Only update if NOT playing result (result always shows 到頭/到尾)
        if not getattr(self, '_is_playing_result', False):
            if self.in_point is not None:
                self.btn_seek_in.setToolTip("跳轉至入點 (Go to IN)")
            else:
                self.btn_seek_in.setToolTip("回到片頭 (Go to Start)")
            
            if self.out_point is not None:
                self.btn_seek_out.setToolTip("跳轉至出點 (Go to OUT)")
            else:
                self.btn_seek_out.setToolTip("跳至片尾 (Go to End)")

    def open_deinterlace_window(self):
        if not self.current_file: return
        
        # [v27.10.13] Singleton: Close/Raise existing window
        if hasattr(self, 'deint_win') and self.deint_win:
            try:
                if self.deint_win.isVisible():
                    self.deint_win.activateWindow()
                    self.deint_win.raise_()
                    return
                else:
                    self.deint_win.close()
            except: pass

        # Pause native player
        self.media_player.pause()
        
        # Open Window
        pos = self.media_player.position()
        self.deint_win = DeinterlaceWindow(self.current_file, self.ffplay_path, pos, self.media_player.duration(), self)
        self.deint_win.closed.connect(self.on_deinterlace_closed)
        self.deint_win.setAttribute(Qt.WA_DeleteOnClose) # Cleanup on close
        self.deint_win.show()
        
        # Force focus to new window
        self.deint_win.activateWindow()
        self.deint_win.raise_()

    def on_deinterlace_closed(self, pos, was_paused):
        print(f"DEBUG: on_deinterlace_closed called with pos={pos}, suspended={was_paused}")
        # Ensure video widget is visible
        self.video_widget.show()
        self.video_widget.raise_()
        
        # Ensure click shield is on top for playback control
        self.btn_click_shield.raise_()
        
        # Restore position
        if pos >= 0:
            self.media_player.setPosition(pos)
        
        # Restore Playback State
        if not was_paused:
            self.media_player.play()
        else:
            # If paused, we seeked, so we might need to briefly play to show the frame
            # But let's try just pausing first to respect user intent.
            # If QMediaPlayer supports pause-seek, it should show frame.
            self.media_player.pause()
            # Optional: Trick to force update if screen is black
            # self.media_player.play()
            # QTimer.singleShot(10, self.media_player.pause)
            
        print(f"DEBUG: Restored state paused={was_paused}")

    def format_time(self, ms):
        if ms is None: return "00:00:00:00"
        fps = getattr(self, 'fps', 25)
        frames = int((ms % 1000) * fps / 1000.0)
        seconds = (ms // 1000) % 60
        minutes = (ms // (1000 * 60)) % 60
        hours = (ms // (1000 * 60 * 60))
        return "%02d:%02d:%02d:%02d" % (hours, minutes, seconds, frames)

    def load_video(self, file_path, is_result=False, start_pos=-1):
        norm_path = os.path.normpath(file_path)
        
        # [NEW] Save current position before switching
        if self.current_file:
             if hasattr(self, 'media_player') and self.media_player:
                 if self._paths_match(self.current_file, self.original_source) and not getattr(self, '_is_playing_result', False):
                     self.source_position = self.media_player.position()
                 elif getattr(self, '_is_playing_result', False):
                     self.result_position = self.media_player.position()

        target_pos = 0
        if not is_result:
            if start_pos >= 0:
                target_pos = start_pos
            elif self.original_source and self._paths_match(norm_path, self.original_source):
                target_pos = self.source_position
            
            # Restore markers if returning to the same original source
            if self.original_source and self._paths_match(norm_path, self.original_source):
                if hasattr(self, 'saved_source_in'):
                    self.in_point = self.saved_source_in
                if hasattr(self, 'saved_source_out'):
                    self.out_point = self.saved_source_out
                
                # Restore UI Markers
                self.set_in_out(self.in_point, self.out_point)
                self._is_dirty = False # Reset dirty state on re-load matching source
            else:
                # NEW source loaded - clear everything
                self.in_point = None
                self.out_point = None
                self.set_in_out(None, None)
                self.source_position = 0
                self._is_dirty = False

            self.original_source = norm_path
        else:
            # Switching TO result - save current source markers first
            if not getattr(self, '_is_playing_result', False):
                self.saved_source_in = self.in_point
                self.saved_source_out = self.out_point
            
            target_pos = start_pos if start_pos >= 0 else 0

        self.current_file = norm_path
        self.pending_seek_pos = target_pos
        self._is_playing_result = is_result # Track for EndOfMedia behavior
        
        # Orphan and safely stop existing workers
        self._safe_stop_thread(getattr(self, 'fps_worker', None))
        self.fps_worker = None

        self._safe_stop_thread(getattr(self, 'analyzer', None))
        self.analyzer = None

        self._safe_stop_thread(getattr(self, 'thumb_worker', None))
        self.thumb_worker = None
        
        # Ensure pool doesn't grow infinitely (Force kill old zombies if too many)
        if len(self._thread_pool) > 10:
             for z in self._thread_pool[:]:
                 if z.isRunning():
                     z.terminate()
                     z.wait(10)
        
        self.audio_levels = {} # Changed to Dict for sparse access
        if hasattr(self, 'vu'):
            self.vu.setRealtime(False)
        self._is_loading_new = True 
        
        # Use UniqueConnection to prevent duplicates without needing to disconnect first
        if hasattr(self, 'media_player') and self.media_player:
            try:
                self.media_player.mediaStatusChanged.connect(self.on_media_status_changed, Qt.UniqueConnection)
            except RuntimeError:
                pass # Already connected
        
        self.fps = 25.0
        self.fps_worker = FPSWorker(norm_path)
        self.fps_worker.result_ready.connect(self.on_fps_ready)
        self.fps_worker.start()

        self.analyzer = AudioLevelAnalyzer(norm_path, interval=0.025)
        self.analyzer.level_found.connect(self.on_level_found)
        self.analyzer.start()
        
        if is_result:
            self.last_result_file = norm_path
            # Reset Trim Markers when viewing result (full file)
            self.set_in_out(None, None)
            
            # Update UI for Result View
            if hasattr(self, 'btn_seek_in'):
                self.btn_seek_in.setToolTip("回到開頭 (To Start)")
            if hasattr(self, 'btn_seek_out'):
                self.btn_seek_out.setToolTip("跳至結尾 (To End)")
            # [NEW] Set VU to Green
            if hasattr(self, 'vu'):
                self.vu.set_color_mode("green")
        else:
            # Update UI for Source View
            # Tooltips will be set by update_trim_labels() based on marker state
            # [NEW] Set VU to Blue
            if hasattr(self, 'vu'):
                self.vu.set_color_mode("blue")
                
        if hasattr(self, 'media_player') and self.media_player:
            self.media_player.setSource(QUrl.fromLocalFile(norm_path))
            self.media_player.pause() # Ensure it starts paused

    def set_in_out(self, in_point, out_point):
        """Set In/Out points programmatically and update UI"""
        self.in_point = in_point
        self.out_point = out_point
        self.update_trim_labels()
        
        # Update Slider Visualization
        if hasattr(self, 'media_player') and self.media_player:
            duration = self.media_player.duration()
            if duration > 0 and hasattr(self, 'slider'):
                self.slider.set_markers(in_point, out_point, duration)
                
        # Update Buttons Tooltips & Icons to reflect state
        if hasattr(self, 'btn_seek_in'):
            if in_point is not None:
                 self.btn_seek_in.setToolTip(f"跳轉至入點 (Go to IN)\n{self.format_time(in_point)}")
                 self.btn_seek_in.setIcon(self.create_geometric_icon("skip_back", "#E0E0E0", size=24))
            else:
                 self.btn_seek_in.setToolTip("回到開頭 (Go to Start)")
                 self.btn_seek_in.setIcon(self.create_geometric_icon("skip_back", "#E0E0E0", size=24)) # Same icon, different tip

        if hasattr(self, 'btn_seek_out'):
            if out_point is not None:
                 self.btn_seek_out.setToolTip(f"跳轉至出點 (Go to OUT)\n{self.format_time(out_point)}")
            else:
                 self.btn_seek_out.setToolTip("跳至結尾 (Go to End)")
                
        # Ensure we are in paused state to show exact frame
        if hasattr(self, 'media_player') and self.media_player:
            self.media_player.pause()

    def _paths_match(self, p1, p2):
        if not p1 or not p2: return False
        try:
            return os.path.abspath(p1).lower() == os.path.abspath(p2).lower()
        except:
            return False

    def on_fps_ready(self, fps):
        self.fps = fps
        self._is_loading_new = False

    def on_level_found(self, idx, levels):
        # levels is list of floats
        # Using Dict for sparse storage (Seek support)
        self.audio_levels[idx] = levels

    def on_media_status_changed(self, status):
        if status == QMediaPlayer.BufferedMedia or status == QMediaPlayer.LoadedMedia:
            if hasattr(self, 'pending_seek_pos') and self.pending_seek_pos >= 0:
                QTimer.singleShot(150, lambda: self._do_seek(self.pending_seek_pos))
                self.pending_seek_pos = -1 # Reset
                
        elif status == QMediaPlayer.EndOfMedia:
            # Fallback if proactive pause missed
            if getattr(self, '_is_playing_result', False):
                 self.media_player.pause()
                 dur = self.media_player.duration()
                 if dur > 40:
                     self.media_player.setPosition(dur - 40)

                
    def _do_seek(self, pos):
        # 1. Immediate UI setPosition (Priority)
        self.media_player.setPosition(pos)
        self.media_player.pause()
        self.setFocus()

        # 2. Optimized Worker Restarts
        # Prevent redundant thumbnail/analyzer restarts if seeking very small amounts (jitter)
        last_seek = getattr(self, '_last_seek_pos', -1)
        if abs(pos - last_seek) < 100: # Threshold 100ms
             return
        self._last_seek_pos = pos

        # Trigger Thumbnail extraction (Only if not already extracting for this pos)
        if hasattr(self, 'thumb_worker') and self.thumb_worker:
            self._safe_stop_thread(self.thumb_worker)
            
        self.thumb_worker = ThumbnailWorker(self.current_file, pos)
        self.thumb_worker.result_ready.connect(self.on_thumbnail_ready)
        self.thumb_worker.start()
        
        # Audio Analyzer restart
        if hasattr(self, 'analyzer') and self.analyzer:
             self._safe_stop_thread(self.analyzer)
             
        seek_sec = pos / 1000.0
        self.analyzer = AudioLevelAnalyzer(self.current_file, interval=0.025, start_time=seek_sec)
        self.analyzer.level_found.connect(self.on_level_found)
        self.analyzer.start()
        
        self.setFocus()
        self.videoLoaded.emit(self.current_file, self.current_file != self.original_source)

    def on_thumbnail_ready(self, pixmap):
        self.preview_overlay.setPixmap(pixmap)
        QTimer.singleShot(500, self.preview_overlay.hide)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_I:
            self.set_in_point()
        elif key == Qt.Key_O:
            self.set_out_point()
        elif key == Qt.Key_Left:
            self.step_frame(-1)
        elif key == Qt.Key_Right:
            self.step_frame(1)
        elif key == Qt.Key_Up:
            self.step_frame(10)
        elif key == Qt.Key_Down:
            self.step_frame(-10)
        elif key == Qt.Key_Space:
            self.toggle_playback()
        elif key == Qt.Key_Escape:
                    self.result_position = self.media_player.position()
                    restore_pos = getattr(self, 'source_position', 0)
                    self.load_video(self.original_source, is_result=False, start_pos=restore_pos)
        elif key == Qt.Key_F5:
             # [NEW] F5 Refresh Duration Logic (For Growing Files)
             if self.current_file:
                 print("DEBUG: F5 Refresh Triggered")
                 # 1. Update Duration via FFprobe
                 try:
                     cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", self.current_file]
                     res = subprocess.check_output(cmd, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0).decode().strip()
                     new_dur_sec = float(res)
                     new_dur_ms = int(new_dur_sec * 1000)
                     
                     # Force Player Reload to recognize new duration
                     # Otherwise QMediaPlayer clamps seeking to original loaded duration
                     current_pos = self.media_player.position()
                     was_playing = self.media_player.playbackState() == QMediaPlayer.PlayingState
                     
                     print(f"DEBUG: Reloading Source to update duration: {new_dur_ms}ms")
                     
                     # [HARD REFRESH]
                     # 1. Stop and clear to force Qt to release the file handle/context
                     self.media_player.stop()
                     self.media_player.setSource(QUrl()) 
                     
                     # 2. Re-load the source
                     self.media_player.setSource(QUrl.fromLocalFile(self.current_file))
                     
                     # 3. Restore state
                     self.media_player.setPosition(current_pos)
                     if was_playing:
                         self.media_player.play()
                         
                     # 4. Update Slider (CRITICAL: Do this AFTER loading)
                     if new_dur_ms > self.slider.maximum():
                         self.slider.setRange(0, new_dur_ms)
                         self.lbl_total_time.setText(self.format_time(new_dur_ms))
                     
                     # [NEW] Restart Audio Analyzer to capture new audio data
                     # If we don't do this, the VU meter will be static for the new parts
                     if hasattr(self, 'analyzer') and self.analyzer:
                         self._safe_stop_thread(self.analyzer)
                     
                     # Don't clear audio_levels entirely if we want to keep old data, 
                     # BUT AudioLevelAnalyzer scans from start usually.
                     # Let's just restart it cleanly to be safe and simple.
                     self.audio_levels = {} # Reset to Dict
                     self.analyzer = AudioLevelAnalyzer(self.current_file, interval=0.025)
                     self.analyzer.level_found.connect(self.on_level_found)
                     self.analyzer.start()
                     print("DEBUG: Audio Analyzer Restarted")
                         
                 except Exception as e:
                     print(f"F5 Refresh Error: {e}")
        else:
            super().keyPressEvent(event)

    # [NEW] Click-to-Play/Pause Optimization
    def mousePressEvent(self, event):
        # We only want to handle clicks on the video area, but since the QVideoWidget 
        # is a child, it might consume them or transparency lets them through.
        # This handles clicks on the container itself if not obstructed.
        if event.button() == Qt.LeftButton:
            self.toggle_playback()
            event.accept()
        else:
            super().mousePressEvent(event)

    def step_frame(self, count):
        fps = getattr(self, 'fps', 25)
        curr = self.media_player.position()
        self.preview_overlay.hide()
        self.media_player.setPosition(max(0, curr + int(count * 1000 / fps)))

    def set_position(self, position):
        self.preview_overlay.hide()
        self.media_player.setPosition(position)
        self.update_vu(force=True, timestamp=position) # Sync VU on scrub instantly
        
    def set_vu_offset(self, value):
        self.vu_offset_ms = value
        if hasattr(self, 'sb_vu_offset'):
            was_blocked = self.sb_vu_offset.blockSignals(True)
            self.sb_vu_offset.setValue(value)
            self.sb_vu_offset.blockSignals(was_blocked)

    def get_vu_offset(self):
        return getattr(self, 'vu_offset_ms', 150)

    def _safe_stop_thread(self, t):
        """Standardized safe thread stopping mechanism to prevent DestroyedWhileRunning."""
        if not t: return
        try:
            # Block and disconnect to prevent callbacks to dying widget
            t.blockSignals(True)
            try: t.result_ready.disconnect()
            except: pass
            try: t.levels_signal.disconnect()
            except: pass
            
            if hasattr(t, 'stop'):
                t.stop() 
            t.quit()
        except: pass
        
        # Keep python reference alive until physically finished
        if not hasattr(self, '_thread_pool'): self._thread_pool = []
        self._thread_pool.append(t)
        
        # Clean finished threads and schedule deletion
        for z in self._thread_pool[:]:
            if z.isFinished() or not z.isRunning():
                self._thread_pool.remove(z)
                z.deleteLater()

    def shutdown(self):
        """Force cleanup of all resources and threads."""
        self.vu_timer.stop()
        self.media_player.stop()
        
        # Stop all tracked workers
        self._safe_stop_thread(getattr(self, 'fps_worker', None))
        self._safe_stop_thread(getattr(self, 'analyzer', None))
        self._safe_stop_thread(getattr(self, 'thumb_worker', None))
        
        # Final cleanup of the reference pool
        if hasattr(self, '_thread_pool'):
            for t in self._thread_pool:
                t.wait(50) # Tiny wait for OS process release
                t.deleteLater()
            self._thread_pool.clear()

    def set_in_point(self):
        self.in_point = self.media_player.position()
        self._is_dirty = True
        self.update_trim_labels()
        
    def set_out_point(self):
        self.out_point = self.media_player.position()
        self._is_dirty = True
        self.update_trim_labels()
    
    def clear_in_out_points(self):
        """Clear both IN and OUT points"""
        self.in_point = None
        self.out_point = None
        self._is_dirty = False
        self.update_trim_labels()
        # Update slider visualization
        duration = self.media_player.duration()
        if duration > 0:
            self.slider.set_markers(None, None, duration)
        # Reset button tooltips
        self.btn_seek_in.setToolTip("回到片頭 (Go to Start)")
        self.btn_seek_out.setToolTip("跳至片尾 (Go to End)")

    def toggle_playback(self):
        # [OPTIMIZED] Instant response without print lag
        try:
            state = self.media_player.playbackState()
            if state == QMediaPlayer.PlayingState:
                self.media_player.pause()
            else:
                self.media_player.play()
                self.setFocus() # Ensure focus for keyboard
        except:
            pass # Suppress generic errors for speed

    def update_position(self, position):
        if not self.slider.isSliderDown():
            self.slider.setValue(position)
        self.lbl_current_time.setText(self.format_time(position))
        
        # Proactive Pause for Result Playback to avoid "Black Flash" at EndOfMedia
        if getattr(self, '_is_playing_result', False):
            dur = self.media_player.duration()
            if dur > 0 and position >= (dur - 50): # 50ms before end (~1-2 frames)
                 self.media_player.pause()
                 self.media_player.setPosition(dur - 40) # Lock to last frame
        
    def update_duration(self, duration):
        self.slider.setRange(0, duration)
        self.lbl_total_time.setText(self.format_time(duration))
        # [NEW] Refresh Markers if points were set before duration was known
        if duration > 0:
            self.slider.set_markers(self.in_point, self.out_point, duration)
            # Re-apply QC markers once duration is known so paintEvent can calculate X coordinates
            if hasattr(self, '_cached_qc_markers') and self._cached_qc_markers:
                self.slider.set_qc_markers(self._cached_qc_markers)
        
        
    def _on_media_ready(self, status):
        pass

    def update_vu(self, force=False, timestamp=None):
        try:
            if force or self.media_player.playbackState() == QMediaPlayer.PlayingState:
                pos_ms = timestamp if timestamp is not None else self.media_player.position()
                lookup_time = max(0, pos_ms + self.vu_offset_ms)
                
                if hasattr(self, 'audio_levels') and self.audio_levels:
                    idx = int(lookup_time / 25)
                    # Expanded fuzzy lookup for better resilience
                    levels = self.audio_levels.get(idx)
                    if not levels: levels = self.audio_levels.get(idx - 1)
                    if not levels: levels = self.audio_levels.get(idx + 1)
                    if not levels: levels = self.audio_levels.get(idx - 2)
                    if not levels: levels = self.audio_levels.get(idx + 2)
                    
                    if levels:
                        self.vu.set_color_mode("green" if getattr(self, '_is_playing_result', False) else "blue")
                        self.vu.setRealtime(True)
                        self.vu.setLevels(levels)
                    else:
                        # Only reset if we've been missing data for a bit to avoid flickering
                        self.vu.setRealtime(False)
                else:
                    self.vu.setRealtime(False)
                    self.vu.setLevels([-100.0] * 4)
            else:
                self.vu.setLevels([-100.0] * 4)
        except Exception:
            pass



class PreviewWindow(QWidget):
    def __init__(self, file_path, ffplay_path, parent=None):
        super().__init__(parent, Qt.Window) # Independent window
        self.setWindowTitle(f"預覽 (Preview) - {os.path.basename(file_path)}")
        self.resize(1024, 600)
        self.setStyleSheet("background-color: #111; color: #ddd;")
        
        self.file_path = file_path
        self.ffplay_path = ffplay_path
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        
        # Embed a fresh VideoPlayerWidget
        self.player = VideoPlayerWidget()
        self.player.btn_ext_preview.hide() # Hide recursion
        # Hide Trim UI for Preview
        self.player.lbl_in.hide()
        self.player.lbl_out.hide()
        self.player.lbl_dur.hide()
        
        layout.addWidget(self.player, 1)
        
        # Add "Launch Diagnostic" bar
        diag_bar = QHBoxLayout()
        diag_bar.setContentsMargins(10, 5, 10, 5)
        
        lbl_info = QLabel("提示: 此視窗不支援去交織顯示。若需檢查場序(Field Order)或交錯紋路，請點擊右測按鈕:")
        lbl_info.setStyleSheet("color: #888; font-size: 12px;")
        diag_bar.addWidget(lbl_info)
        diag_bar.addStretch()
        
        btn_ffplay = QPushButton("🚀 啟動 FFplay (去交織檢查)")
        btn_ffplay.setCursor(Qt.PointingHandCursor)
        btn_ffplay.setStyleSheet("background-color: #333; border: 1px solid #555; padding: 5px 10px; border-radius: 4px;")
        btn_ffplay.clicked.connect(self.launch_ffplay)
        diag_bar.addWidget(btn_ffplay)
        
        layout.addLayout(diag_bar)
        
        # Deinterlace Worker
        self.deinterlace_worker = DeinterlaceWorker(file_path, self.player.ffplay_path.replace("ffplay", "ffmpeg"))
        self.deinterlace_worker.frame_ready.connect(self.on_deint_frame)
        
        # Hook Slider
        self.player.slider.sliderPressed.connect(self.on_slider_pressed)
        self.player.slider.sliderReleased.connect(self.on_slider_released)
        self.player.slider.valueChanged.connect(self.on_slider_changed)
        
        # Load Video
        self.player.load_video(file_path)
        
        # Start in "Deinterlace Mode" check?
        # User wants "Click button -> Have Deinterlace".
        # So we should default to Deinterlaced scrubbing?
        self.is_scrubbing = False

    def on_slider_pressed(self):
        self.is_scrubbing = True
        self.player.media_player.pause()
        
    def on_slider_released(self):
        self.is_scrubbing = False
        
    def on_slider_changed(self, val):
        if self.is_scrubbing:
            # Request Deinterlaced Frame
             self.deinterlace_worker.request_frame(val)
             
    def on_deint_frame(self, image):
        # Overlay the deinterlaced frame
        pix = QPixmap.fromImage(image)
        self.player.preview_overlay.setPixmap(pix)
        self.player.preview_overlay.show()
        # Ensure it covers the video widget
        self.player.preview_overlay.raise_()

    def launch_ffplay(self):
         try:
            cmd = [
                self.ffplay_path,
                "-window_title", f"Deinterlace Check - {os.path.basename(self.file_path)}",
                "-vf", "bwdif",
                "-x", "1280", "-y", "720",
                self.file_path
            ]
            # [v27.10.17] Fix Ghost Windows
            flags = 0x08000000 if os.name == 'nt' else 0
            subprocess.Popen(cmd, creationflags=flags)
         except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
            
    def closeEvent(self, event):
        self.deinterlace_worker.stop()
        self.player.shutdown()
        event.accept()

class DeinterlaceWindow(QWidget):
    closed = Signal(int, bool) # Pos, IsPaused # Signal emitting final timestamp on close

    def __init__(self, file_path, ffplay_path, start_ms, duration, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle(f"去交織預覽 (Deinterlaced Preview) - {os.path.basename(file_path)}")
        self.resize(1024, 640)
        self.setStyleSheet("background-color: #111; color: #ddd;")
        
        self.file_path = file_path
        self.ffplay_path = ffplay_path
        self.start_ms = start_ms
        self.duration = duration
        
        # Layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        
        # Video Container
        self.video_container = QWidget()
        self.video_container.setStyleSheet("background-color: black;")
        layout.addWidget(self.video_container, 1)
        
        # Scrubbing Preview Label (Hidden by default)
        self.preview_label = QLabel(self.video_container)
        self.preview_label.setStyleSheet("background-color: #111;")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setScaledContents(True) # Stretch to fit
        self.preview_label.hide()
        
        # Initialize Deinterlace Worker for Scrubbing
        ffmpeg_path = self.ffplay_path.replace("ffplay", "ffmpeg") # Simple heuristic
        if not os.path.exists(ffmpeg_path) and os.name == 'nt':
             # Try appending .exe if missing
             if not ffmpeg_path.endswith(".exe"): ffmpeg_path += ".exe"
             
        self.scrub_worker = DeinterlaceWorker(self.file_path, ffmpeg_path)
        self.scrub_worker.frame_ready.connect(self.update_scrub_preview)

        
        
        # Controls Group
        ctl = QWidget()
        ctl.setFixedHeight(50)
        ctl_layout = QHBoxLayout(ctl)
        ctl_layout.setContentsMargins(10, 5, 10, 5)
        
        # Time Label
        self.lbl_time = QLabel("00:00:00:00")
        self.lbl_time.setStyleSheet("color: #fff; font-size: 14px; font-family: monospace;")
        ctl_layout.addWidget(self.lbl_time)
        
        # Slider
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, self.duration)
        self.slider.setValue(self.start_ms)
        self.slider.setStyleSheet("""
             QSlider::groove:horizontal { height: 6px; background: #333; border-radius: 3px; }
             QSlider::handle:horizontal { background: #fff; width: 14px; margin: -4px 0; border-radius: 7px; }
             QSlider::sub-page:horizontal { background: #0078d4; border-radius: 3px; }
        """)
        self.slider.sliderPressed.connect(self.on_slider_pressed)
        self.slider.sliderMoved.connect(self.on_slider_moved)
        self.slider.sliderReleased.connect(self.on_slider_released)
        self.slider.valueChanged.connect(self.update_time_label)
        ctl_layout.addWidget(self.slider)
        
        layout.addWidget(ctl)
        
        # Internal State
        self.ffplay_process = None
        self.embedded_hwnd = None
        self.embed_title = f"DeintWin_{random.randint(10000, 99999)}"
        self.is_seeking = False
        self.playback_start_time = None  # Track when playback started
        self.playback_start_pos = 0      # Position when playback started
        self.is_paused = False           # Track pause state
        self.pause_time = None           # Track when paused
        
        # Playback position update timer
        self.position_timer = QTimer(self)
        self.position_timer.timeout.connect(self.update_playback_position)
        
        # Start Delayed (Wait for show)
        QTimer.singleShot(100, self.start_ffplay)
    
    def create_geometric_icon(self, shape, color, size=24):
        """Draw simple geometric icons (copied for independence)"""
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        
        pen = QPen(QColor(color))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(QColor(color))
        
        m = 4 # Margin
        s = size - 2*m
        
        if shape == "play":
            painter.setPen(Qt.NoPen)
            path = QPainterPath()
            path.moveTo(m + 2, m)
            path.lineTo(m + s, m + s/2)
            path.lineTo(m + 2, m + s)
            path.closeSubpath()
            painter.drawPath(path)
            
        elif shape == "pause":
            painter.setPen(Qt.NoPen)
            w = s / 3
            painter.drawRect(int(m), int(m), int(w), int(s))
            painter.drawRect(int(m + 2*w), int(m), int(w), int(s))
            
        painter.end()
        return QIcon(pixmap)

    # toggle_pause removed by user request

            
    def start_ffplay(self):
        # Reset state
        self.is_paused = False

        
        self.stop_ffplay()
        
        start_sec = self.slider.value() / 1000.0
        
        # Prevent "Flicker": Force startup size to match container
        w = self.video_container.width()
        h = self.video_container.height()
        
        # Ensure valid dimensions
        if w < 100: w = 1024
        if h < 100: h = 576
            
        cmd = [
            self.ffplay_path,
            "-window_title", self.embed_title,
            "-vf", "bwdif",
            "-ss", f"{start_sec:.3f}",
            "-x", str(w), "-y", str(h), # Force size
            "-noborder",
            "-loglevel", "quiet",
            self.file_path
        ]

        # [v27.10.13] Hide startup window to avoid taskbar icon flicker before embedding
        startupinfo = None
        creationflags = 0
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0 # SW_HIDE (0)
            creationflags = subprocess.CREATE_NO_WINDOW 
        
        self.ffplay_process = subprocess.Popen(cmd, startupinfo=startupinfo, creationflags=creationflags)
        
        import time
        self.playback_start_time = time.time()
        self.playback_start_pos = self.slider.value()
        # DELAYED START: self.position_timer.start(100)
        # We start the timer only when window is actually embedded to prevent desync on slow startup
        
        # Embed Loop
        self.embed_retries = 0
        QTimer.singleShot(50, self.check_embed)
        
    def check_embed(self):
        if not self.ffplay_process or self.ffplay_process.poll() is not None: return

        hwnd = ctypes.windll.user32.FindWindowW(None, self.embed_title)
        if hwnd:
             self.embedded_hwnd = hwnd
             parent_hwnd = int(self.video_container.winId())
             
             try:
                 import win32gui, win32con
                 # Use win32gui for better integer handling (avoid OverflowError)
                 style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
                 style = (style & ~win32con.WS_POPUP) | win32con.WS_CHILD
                 win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)
                 win32gui.SetParent(hwnd, parent_hwnd)
                 
                 self.resize_embed()
                 win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                 
                 # Hide preview once video is ready
                 self.preview_label.hide()
             except Exception as e:
                 print(f"Embed Error: {e}")
             
             # Start Sync Timer NOW (compensate for startup delay)
             import time
             self.playback_start_time = time.time()
             self.position_timer.start(100)
        else:
             self.embed_retries += 1
             if self.embed_retries < 20: QTimer.singleShot(50, self.check_embed)

    def resize_embed(self):
        if hasattr(self, 'embedded_hwnd') and self.embedded_hwnd:
            w = self.video_container.width()
            h = self.video_container.height()
            ctypes.windll.user32.MoveWindow(self.embedded_hwnd, 0, 0, w, h, True)
            
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resize_embed()
        if hasattr(self, 'preview_label'):
            self.preview_label.resize(self.video_container.size())
        
    def closeEvent(self, event):
        self.stop_ffplay()
        if hasattr(self, 'scrub_worker'):
            self.scrub_worker.stop()
        self.closed.emit(self.slider.value(), False) # Notify parent with state
        event.accept()
        
    def stop_ffplay(self):
        self.position_timer.stop()  # Stop position tracking
        if self.ffplay_process:
            try: self.ffplay_process.kill()
            except: pass
            self.ffplay_process = None
            self.embedded_hwnd = None

    def on_slider_pressed(self):
        self.is_seeking = True
        self.stop_ffplay()  # Stop playback completely during seek
        
        # Show scrubbing preview
        self.preview_label.raise_()
        self.preview_label.show()
        # Request initial frame
        self.scrub_worker.request_frame(self.slider.value())

    def on_slider_moved(self, value):
        # Update time label
        self.update_time_label(value)
        # Request frame for scrubbing
        self.scrub_worker.request_frame(value)
        
    def on_slider_released(self):
        self.is_seeking = False
        # Restart ffplay at new position (unavoidable for seeking)
        self.start_ffplay()
        
    def update_scrub_preview(self, image):
        if self.is_seeking:
            self.preview_label.setPixmap(QPixmap.fromImage(image))
    
    def update_time_label(self, ms):
        """Update time label with current slider position"""
        h = ms // 3600000
        m = (ms % 3600000) // 60000
        s = (ms % 60000) // 1000
        f = int((ms % 1000) * 29.97 / 1000)
        self.lbl_time.setText(f"{h:02d}:{m:02d}:{s:02d}:{f:02d}")
    
    def update_playback_position(self):
        """Update slider and time based on elapsed playback time"""
        import time
        if self.is_seeking or self.playback_start_time is None:
            return
        
        # Check if ffplay is still running
        if self.ffplay_process and self.ffplay_process.poll() is not None:
            self.position_timer.stop()
            return
        
        # Calculate current position based on elapsed time
        elapsed_ms = int((time.time() - self.playback_start_time) * 1000)
        current_pos = self.playback_start_pos + elapsed_ms
        
        # Clamp to duration
        if current_pos > self.duration:
            current_pos = self.duration
        
        # Update slider and time label (without triggering seek)
        self.slider.blockSignals(True)
        self.slider.setValue(current_pos)
        self.slider.blockSignals(False)
        self.update_time_label(current_pos)

class DeinterlaceWorker(QThread):
    frame_ready = Signal(QImage)
    
    def __init__(self, file_path, ffmpeg_path):
        super().__init__()
        self.file_path = file_path
        self.ffmpeg_path = ffmpeg_path
        self.request_ts = -1
        self.mutex = QThread.currentThread() # Simple guard? No
        self.running = True
        self.start()
        
    def request_frame(self, ms):
        self.request_ts = ms
        
    def run(self):
        last_processed = -1
        while self.running:
            if self.request_ts != -1 and self.request_ts != last_processed:
                target_ms = self.request_ts
                last_processed = target_ms
                
                try:
                    sec = target_ms / 1000.0
                    cmd = [
                        self.ffmpeg_path,
                        "-ss", f"{sec:.3f}",
                        "-i", self.file_path,
                        "-vf", "bwdif",
                        "-frames:v", "1",
                        "-f", "image2",   # BMP format
                        "-vcodec", "bmp",
                        "-"
                    ]
                    
                    # Create startup info to hide window
                    startupinfo = None
                    if os.name == 'nt':
                        startupinfo = subprocess.STARTUPINFO()
                        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    
                    # [v27.10.17] Fix Ghost Windows
                    flags = 0x08000000 if os.name == 'nt' else 0
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, startupinfo=startupinfo, creationflags=flags)
                    raw_data, _ = proc.communicate()
                    
                    if raw_data:
                        img = QImage()
                        if img.loadFromData(raw_data):
                            self.frame_ready.emit(img)
                            
                except:
                    pass
            QThread.msleep(20)

    def stop(self):
        self.running = False
        self.wait()

