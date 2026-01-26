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
    def __init__(self, ffmpeg_path=None, ffprobe_path=None):
        self.ffmpeg_path = ffmpeg_path if ffmpeg_path else self._resolve_tool("ffmpeg.exe")
        self.ffprobe_path = ffprobe_path if ffprobe_path else self._resolve_tool("ffprobe.exe")
        self.ffplay_path = self._resolve_tool("ffplay.exe")
        debug_log(f"Transcoder Init: ffmpeg='{self.ffmpeg_path}', ffprobe='{self.ffprobe_path}', ffplay='{self.ffplay_path}'")

    def _resolve_tool(self, tool_name):
        # 1. Bundled (PyInstaller _internal) or Development
        # Check adjacent to sys.executable (The .exe file)
        if getattr(sys, 'frozen', False):
            exe_dir = os.path.dirname(sys.executable)
            
            # Check root adjacent (most likely for user)
            scan_paths = [
                os.path.join(exe_dir, tool_name),
                os.path.join(exe_dir, 'core', tool_name),
                os.path.join(exe_dir, 'bin', tool_name)
            ]
            
            # Also check MEIPASS if we happened to bundle it (future proof)
            if hasattr(sys, '_MEIPASS'):
                 scan_paths.append(os.path.join(sys._MEIPASS, 'core', tool_name))
            
            for p in scan_paths:
                if os.path.exists(p):
                    return p
                    
        # 2. Local Dev (Explicit Project Path? No, usually in PATH or bin)
        # 3. Fallback to System PATH
        return tool_name.replace(".exe", "")

    def get_duration(self, input_path):
        """Returns duration in seconds using ffprobe."""
        try:
            cmd = [self.ffprobe_path, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", input_path]
            debug_log(f"Probe: {cmd}")
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            dur = float(result.stdout.strip())
            debug_log(f"Probe Result: {dur}")
            return dur
        except Exception as e:
            debug_log(f"Probe Failed: {e}")
            return 0

    def construct_command(self, input_path, output_path, params):
        """
        Builds FFmpeg command list.
        params schema: {
            "vcodec": "h264_nvenc",
            "acodec": "aac",
            "in_point": "00:00:10",
            "out_point": "00:00:20",
            "bitrate": "5000k",
            "growing": True
        }
        """
        cmd = [self.ffmpeg_path, "-progress", "-", "-nostats"]
        
        # Growing file support (Low delay/real-time)
        if params.get("growing"):
            cmd += ["-re"] 

        # In Point
        if params.get("in_point"):
            cmd += ["-ss", params["in_point"]]

        cmd += ["-i", input_path]

        # Out Point (relative to in_point if using -t, or absolute if using -to)
        # Fix for Broken Timestamps (TS files): Prefer -t (Duration) over -to (Timestamp)
        if params.get("duration"):
            cmd += ["-t", str(params["duration"])]
        elif params.get("out_point") and params["out_point"] != "00:00:00" and params["out_point"] != "00:00:00.000":
            cmd += ["-to", params["out_point"]]

        # Video Params
        cmd += ["-c:v", params.get("vcodec", "libx264")]
        cmd += ["-b:v", params.get("bitrate", "2000k")]
        cmd += ["-pix_fmt", "yuv420p"] # Standard for compatibility
        
        # Resolution
        if params.get("resolution"):
            cmd += ["-s", params["resolution"]]
            
        # FPS
        if params.get("fps"):
            cmd += ["-r", str(params["fps"])]

        # Audio Params
        # Force PCM for MXF if not specified (AAC is invalid in MXF)
        acodec = params.get("acodec", "aac")
        is_mxf = output_path.lower().endswith(".mxf")
        
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
        if output_path.lower().endswith(".mp4"):
            cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof"]

        # Output
        cmd += ["-strict", "unofficial"]
        cmd += ["-y", output_path]
        
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
