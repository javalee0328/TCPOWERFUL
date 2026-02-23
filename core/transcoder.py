import subprocess
import os
import time

import sys

def debug_log(msg):
    try:
        log_path = os.path.join(os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.getcwd(), "debug.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - [CORE] {msg}\n")
    except:
        pass

class Transcoder:
    # [v27.10.40] Class-level cache to avoid redundant disk/IO scans
    _tool_cache = {}

    def __init__(self, ffmpeg_path=None, ffprobe_path=None):
        self.ffmpeg_path = ffmpeg_path if ffmpeg_path else self._resolve_tool("ffmpeg.exe")
        self.ffprobe_path = ffprobe_path if ffprobe_path else self._resolve_tool("ffprobe.exe")
        self.ffplay_path = self._resolve_tool("ffplay.exe")
        
        # Only log init once per session if possible, or keep it quiet unless debug is needed
        # debug_log(f"Transcoder Init: ffmpeg='{self.ffmpeg_path}', ffprobe='{self.ffprobe_path}', ffplay='{self.ffplay_path}'")

    def _resolve_tool(self, tool_name):
        """Locates FFmpeg tools reliably across environments with caching."""
        if tool_name in self._tool_cache:
            return self._tool_cache[tool_name]

        # 1. Bundled (PyInstaller) or Local adjacent
        scan_paths = []
        
        # Determine base search directory
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        scan_paths = [
            os.path.join(base_dir, tool_name),
            os.path.join(base_dir, 'core', tool_name),
            os.path.join(base_dir, 'bin', tool_name),
            os.path.abspath(os.path.join(base_dir, "..", tool_name)),
            os.path.abspath(os.path.join(base_dir, "..", "core", tool_name)),
            os.path.join(os.getcwd(), tool_name),
            os.path.join(os.getcwd(), 'core', tool_name),
        ]
        
        # MEIPASS check
        if hasattr(sys, '_MEIPASS'):
             scan_paths.append(os.path.join(sys._MEIPASS, 'core', tool_name))
             scan_paths.append(os.path.join(sys._MEIPASS, tool_name))
        
        for p in scan_paths:
            if os.path.exists(p):
                # Prevention: Don't let it resolve to the App Executable itself
                if getattr(sys, 'frozen', False) and os.path.samefile(p, sys.executable):
                    continue
                self._tool_cache[tool_name] = p
                return p
        
        # 2. PATH lookup (Last resort)
        import shutil
        sys_path = shutil.which(tool_name)
        if sys_path:
             # Double check to prevent recursion
             is_me = False
             if getattr(sys, 'frozen', False) and os.path.exists(sys.executable):
                 try:
                    if os.path.samefile(sys_path, sys.executable):
                        is_me = True
                 except: pass
             
             if not is_me:
                 self._tool_cache[tool_name] = sys_path
                 return sys_path

        # 3. Only log WARNING if absolutely NOT found anywhere
        debug_log(f"CRITICAL WARNING: Tool '{tool_name}' not found in any standard location or PATH.")
        return None # Failed to resolve
    def get_duration(self, input_path):
        """Returns duration in seconds using ffprobe. [v27.10.46] Added retries for Network/NAS."""
        src = os.path.normpath(input_path)
        
        for attempt in range(3):
            try:
                cmd = [self.ffprobe_path, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", src]
                debug_log(f"Probe (Attempt {attempt+1}): {cmd}")
                result = subprocess.run(
                    cmd, 
                    capture_output=True, 
                    text=True, 
                    encoding='utf-8',
                    errors='replace',
                    check=True, 
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
                )
                dur = float(result.stdout.strip())
                if dur > 0:
                    return dur
            except Exception as e:
                debug_log(f"Probe Attempt {attempt+1} Failed: {e}")
            
            if attempt < 2:
                time.sleep(1.0) # Wait a bit before retry
        
        return 0

    def construct_command(self, input_path, output_path, params):
        """Builds FFmpeg command list."""
        # [FIX v27.10.8] Conservative Pathing: use normpath
        src = os.path.normpath(input_path)
        dst = os.path.normpath(output_path)
        
        # Start command
        cmd = [self.ffmpeg_path, "-progress", "-", "-nostats"]
        
        # Growing file support
        if params.get("growing"):
            cmd += ["-re"] 

        # In Point
        if params.get("in_point") and params.get("in_point") != "00:00:00.000":
            cmd += ["-ss", params["in_point"]]

        cmd += ["-i", src]

        # Duration/Out point
        if params.get("duration"):
            cmd += ["-t", str(params["duration"])]

        # Video
        cmd += ["-c:v", params.get("vcodec", "libx264")]
        if params.get("bitrate"):
            cmd += ["-b:v", params["bitrate"]]
        if params.get("fps"):
            cmd += ["-r", str(params["fps"])]
        if params.get("resolution"):
            cmd += ["-s", params["resolution"]]
        
        # Audio
        acodec = params.get("acodec", "aac")
        is_mxf = dst.lower().endswith(".mxf")
        
        if is_mxf and acodec == "aac":
            acodec = "pcm_s16le"
            
        cmd += ["-c:a", acodec]
        
        if is_mxf:
            # MXF requires 48kHz audio in almost all profiles. Force it.
            cmd += ["-ar", "48000"]
        
        # Only apply bitrate for compressed formats (aac, mp3), NOT pcm
        if "pcm" not in acodec:
             cmd += ["-b:a", "128k"]
             
        # Audio Gain
        gain = params.get("audio_gain", 0.0)
        if gain == 'auto':
            # EBU R128 / Web Standard (-16 LUFS, -1.5 TP)
            cmd += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
        elif isinstance(gain, (int, float)) and gain != 0.0:
            cmd += ["-af", f"volume={gain}dB"]

        # Metadata optimization (Fragmentation for Seeking while Writing)
        # Fixes "Cannot play growing MP4" without slow second pass
        if dst.lower().endswith(".mp4"):
            cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof"]

        # Output
        cmd += ["-strict", "unofficial"]
        cmd += ["-y", dst]
        
        return cmd

    def run_job(self, cmd_callback=None):
        # Implementation for running and tracking progress would go here
        pass

if __name__ == "__main__":
    tx = Transcoder()
    mock_params = {
        "vcodec": "h264_nvenc",
        "in_point": "00:00:05",
        "out_point": "00:00:15",
        "bitrate": "8000k"
    }
    print("Example Command:")
    print(" ".join(tx.construct_command("test.mxf", "output.mp4", mock_params)))
