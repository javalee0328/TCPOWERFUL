import subprocess
import re
import os
from PySide6.QtCore import QThread, Signal

class AudioLevelAnalyzer(QThread):
    finished = Signal() # Done scanning
    level_found = Signal(int, list) # (index, [ch1, ch2, ch3, ch4...])

    def __init__(self, file_path, interval=0.025, start_time=0.0):
        super().__init__()
        self.file_path = file_path
        self.interval = interval
        self.start_time = start_time
        self.is_aborted = False
        self.process = None

    def stop(self):
        self.is_aborted = True
        if self.process:
             try:
                 self.process.terminate()
             except:
                 pass

    def run(self):
        cmd = [
            "ffmpeg", 
            "-ss", str(self.start_time),
            "-copyts", # Preserve timestamps for correct sync when seeking
            "-i", self.file_path, 
            "-vn", "-sn", "-dn", "-ignore_unknown",
            # Multi-channel stats (Remove forced stereo)
            "-af", f"astats=length={self.interval}:metadata=1:reset=1,ametadata=print",
            "-f", "null", "-"
        ]
        # print(f"DEBUG: Analyzer Cmd: {cmd}")
        
        try:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            self.process = subprocess.Popen(
                cmd, 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                startupinfo=startupinfo
            )
            
            # Temporary storage for current frame's levels
            # Map index -> level
            current_levels = {}
            current_time_ms = int(self.start_time * 1000)
            
            # Regex for capturing channel index and level
            # lavfi.astats.1.Peak_level=-50.0 
            re_level = re.compile(r"lavfi\.astats\.(\d+)\.Peak_level=([-\d\.]+)")
            
            while True:
                if self.is_aborted: break
                    
                line = self.process.stderr.readline()
                if not line and self.process.poll() is not None: break
                if not line: continue

                line = line.strip()
                
                # Critical: Use ffmpeg's time for sync
                # ametadata output can be 'pts_time:0.123' or 'pts_time=0.123'
                if "pts_time" in line:
                    try:
                        # Split by both separators
                        part = line.split("pts_time")[1]
                        val_str = part.strip().lstrip("=:").split()[0]
                        t = float(val_str)
                        current_time_ms = int(t * 1000)
                        # print(f"DEBUG: Parsed time {current_time_ms}ms from line: {line}")
                    except: 
                        # print(f"DEBUG: Failed parse time: {line}")
                        pass

                m = re_level.search(line)
                if m:
                    ch_idx = int(m.group(1)) # 1-based usually
                    val_str = m.group(2) 
                    try:
                        val = float(val_str)
                    except (ValueError, TypeError):
                        val = -100.0
                            
                    current_levels[ch_idx] = val
                
                # End of block
                if "Overall.Peak_level" in line:
                    max_ch = max(current_levels.keys()) if current_levels else 2
                    levels = []
                    for i in range(1, max(5, max_ch + 1)): 
                         levels.append(current_levels.get(i, -100.0))
                    
                    # Emit index to match player_widget expectation (0, 1, 2...)
                    idx = int(round(current_time_ms / 25.0))
                    self.level_found.emit(idx, levels)
                    current_levels = {}
            
            # Cleanup
            
            # Cleanup
            if self.process:
                try:
                    self.process.terminate()
                    self.process.wait()
                except: pass

            self.finished.emit()
            
        except Exception as e:
            print(f"Analyzer error: {e}")
            self.finished.emit()
