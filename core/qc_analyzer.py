import os
import re
import json
import logging
import subprocess
from datetime import datetime

class BroadcastQC:
    def __init__(self, executable_path="ffmpeg", ffprobe_path="ffprobe"):
        self.ffmpeg_cmd = executable_path
        self.ffprobe_cmd = ffprobe_path
        self.logger = logging.getLogger("QCAnalyzer")
        
    def analyze(self, file_path, progress_callback=None):
        """
        Runs comprehensive QC on a given file.
        Returns a dict of anomalies and file info.
        """
        self.logger.info(f"Starting Broadcast QC analysis for: {file_path}")
        if not os.path.exists(file_path):
            self.logger.error(f"File not found: {file_path}")
            return {"error": "File not found"}

        info = self._get_basic_info(file_path)
        # Run deep anomaly detection
        anomalies = self._detect_anomalies(file_path, duration_sec=info.get("duration_sec", 0.0), progress_callback=progress_callback)
        
        # [NEW] Metrics Summary for UI Dashboard
        metrics = {
            "freeze_count": len([a for a in anomalies if "Freeze" in a["type"]]),
            "mosaic_count": len([a for a in anomalies if "DecodeError" in a["type"]]),
            "black_count": len([a for a in anomalies if "Black" in a["type"]]),
            "silence_count": len([a for a in anomalies if "Silence" in a["type"]]),
            "lufs_i": None,
            "peak_db": None
        }
        
        # Extract loudness from specific anomaly entries if they exist
        for a in anomalies:
            if "Audio_Loudness_Info" in a["type"]: # We'll add this type below
                metrics["lufs_i"] = a.get("lufs")
                metrics["peak_db"] = a.get("peak")

        return {
            "file": os.path.basename(file_path),
            "path": file_path,
            "info": info,
            "anomalies": anomalies,
            "metrics": metrics,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

    def _get_basic_info(self, file_path):
        """Extracts basic info like duration, size, and codec using ffprobe."""
        cmd = [
            self.ffprobe_cmd,
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            file_path
        ]
        try:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                
            out = subprocess.check_output(cmd, encoding='utf-8', errors='replace', stderr=subprocess.STDOUT, startupinfo=startupinfo)
            data = json.loads(out)
            
            format_info = data.get("format", {})
            streams = data.get("streams", [])
            
            video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
            audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})
            
            file_size_bytes = int(format_info.get("size", 0))
            duration_sec = float(format_info.get("duration", 0))
            
            # [NEW] Extract Start Timecode
            start_tc = video_stream.get("tags", {}).get("timecode", "00:00:00:00")
            fps_str = video_stream.get("r_frame_rate", "0/0")
            
            # Helper for framerate math
            try:
                num, den = map(int, fps_str.split('/'))
                fps_val = round(num / den, 2) if den != 0 else 0
            except:
                fps_val = 0
            
            # Helper for bitrate
            v_bitrate = int(video_stream.get("bit_rate", format_info.get("bit_rate", 0)))
            v_bitrate_mbps = f"{round(v_bitrate / 1000000)}Mb" if v_bitrate else "Unknown"
            
            # Helper for Audio Sample Depth & Tracks
            audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
            audio_stream_count = len(audio_streams)
            
            a_codec = audio_stream.get("codec_name", "Unknown").upper()
            sample_fmt = audio_stream.get("sample_fmt", "")
            if "s32" in sample_fmt: a_codec += " 32bit"
            elif "s24" in sample_fmt: a_codec += " 24bit"
            elif "s16" in sample_fmt: a_codec += " 16bit"
            elif "flt" in sample_fmt: a_codec += " 32bit Float"
            
            channels = int(audio_stream.get("channels", 0)) if audio_stream else 0
            audio_mode = "MONO" if channels == 1 else "STEREO" if channels == 2 else f"{channels}CH"
            
            # Scan type mapping
            field_order = video_stream.get("field_order", "progressive")
            scan_type = "Progressive"
            if field_order in ["tb", "tff"]: scan_type = "FirstFieldTop"
            elif field_order in ["bt", "bff"]: scan_type = "FirstFieldBottom"
            
            # Drop frame check (simplistic heuristic based on framerate and standard TC notation)
            is_dropframe = "dropframe" if fps_val == 29.97 and ';' in start_tc else "non-dropframe"
            
            # Format Duration HH:MM:SS
            h = int(duration_sec // 3600)
            m = int((duration_sec % 3600) // 60)
            s = int(duration_sec % 60)
            f = int((duration_sec - int(duration_sec)) * (fps_val if fps_val > 0 else 25))
            duration_str = f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"
            
            # Dates
            import time
            mtime = time.strftime('%Y/%m/%d', time.localtime(os.path.getmtime(file_path)))
            ctime = time.strftime('%Y/%m/%d', time.localtime(os.path.getctime(file_path)))
            
            # Frame Rate Parsing Helper
            fps_str = video_stream.get("r_frame_rate", "0/0")
            try:
                num, den = map(int, fps_str.split('/'))
                fps_val = round(num / den, 3) if den != 0 else 0.0
            except:
                fps_val = 0.0
                
            # Broadcast Safe Ranges & Spaces
            color_range = video_stream.get("color_range", "tv") # tv (16-235) or pc (0-255)
            color_space = video_stream.get("color_space", "bt709")
            color_primaries = video_stream.get("color_primaries", "bt709")
            
            # Pixel format cleanup
            pix_fmt = video_stream.get("pix_fmt", "yuv420p").upper()
            if "422" in pix_fmt: pix_fmt = "YUVP_422"
            
            # Extract 21 Extended Info Fields
            return {
                "file_name": os.path.basename(file_path),
                "start_tc": start_tc,
                "duration_str": duration_str,
                "video_resolution": f"{video_stream.get('width', 0)}*{video_stream.get('height', 0)}",
                "color_sampling": pix_fmt,
                "bitrate_mbps": v_bitrate_mbps,
                "video_codec": video_stream.get("codec_name", "Unknown").upper(),
                "format_name": format_info.get("format_name", "Unknown").upper().split(',')[0],
                "dropframe": is_dropframe,
                "audio_codec": a_codec,
                "audio_channels": str(audio_stream.get("channels", "Unknown")),
                "audio_track_count": audio_stream_count,
                "audio_mode": audio_mode,
                "mod_time": mtime,
                "scan_type": scan_type,
                "aspect_ratio": video_stream.get("display_aspect_ratio", "16:9"),
                "create_time": ctime,
                "fps": str(fps_val),
                "afd": "UNKNOW", # Hard fallback matching user screenshot
                "size_mb": f"{round(file_size_bytes / (1024*1024))}M",
                "color_primaries": color_primaries.upper().replace("BT", "ITU R"),
                "color_transfer": video_stream.get("color_transfer", "BT.709").upper(),
                "audio_sample_rate": f"{round(int(audio_stream.get('sample_rate', 0)) / 1000, 1)}kHz",
                # Metrics for Broadcast Standard Validation
                "raw_color_range": color_range,
                "raw_color_space": color_space,
                # Needed for backwards compatibility
                "size_bytes": file_size_bytes,
                "duration_sec": round(duration_sec, 3)
            }
        except Exception as e:
            self.logger.error(f"Error getting basic info: {e}")
            return {"error": str(e)}

    def _detect_anomalies(self, file_path, duration_sec=0.0, progress_callback=None):
        """
        Runs a full-decode pass using ffmpeg to detect:
        1. Silence (-50dB, >1s)
        2. Freezes (-60dB, >1s)
        3. Black frames (>0.5s)
        4. Decoding Errors
        5. Audio Loudness (EBU R128)
        """
        anomalies = []
        
        # We run multiple filters in one pass
        # - silencedetect=noise=-50dB:d=1
        # - freezedetect=n=-60dB:d=1
        # - blackdetect=d=0.5:pix_th=0.00
        # - ebur128=peak=true
        
        cmd = [
            self.ffmpeg_cmd,
            "-v", "info",            # Show info so filters can output detections
            "-err_detect", "explode", # Immediately highlight decode errors
            "-i", file_path,
            "-vf", "freezedetect=n=-60dB:d=1,blackdetect=d=0.5:pix_th=0.00",
            "-af", "silencedetect=noise=-50dB:d=1,ebur128=peak=true",
            "-f", "null",
            "-"
        ]
        
        try:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                
            process = subprocess.Popen(cmd, stderr=subprocess.PIPE, encoding='utf-8', errors='replace', startupinfo=startupinfo)
            
            # Read line by line
            import math
            audio_peak = -float('inf')
            lufs_i = -float('inf')
            last_freeze_duration = 0.0
            last_silence_start = None
            
            for line in iter(process.stderr.readline, ''):
                line = line.strip()
                if not line:
                    continue
                
                # Progress Reporting
                if progress_callback and duration_sec > 0 and "time=" in line:
                    try:
                        # Extract time=HH:MM:SS.ms string
                        time_match = re.search(r"time=([\d:.]+)", line)
                        if time_match:
                            time_str = time_match.group(1)
                            h, m, s = time_str.split(':')
                            curr_time_sec = int(h) * 3600 + int(m) * 60 + float(s)
                            
                            percent = (curr_time_sec / duration_sec) * 100
                            # Map 0-100% video decoding to 10%-70% UI progress bar
                            scaled_percent = int(10 + (percent * 0.6)) 
                            progress_callback(scaled_percent, f"分析中 (Analyzing {int(percent)}%)...")
                    except Exception:
                        pass
                
                # Decoding error catch
                if "Error while decoding" in line or "corrupt" in line.lower() or "missing picture" in line.lower():
                    anomalies.append({"type": "DecodeError", "msg": line})
                
                # Silence detect parsing
                # [silencedetect] silence_start: 3.2
                # [silencedetect] silence_end: 5.5 | silence_duration: 2.3
                if "silence_start" in line:
                    match = re.search(r"silence_start:\s+([\d\.]+)", line)
                    if match:
                        last_silence_start = float(match.group(1))
                elif "silence_end" in line:
                    match = re.search(r"silence_end:\s+([\d\.]+)\s+\|\s+silence_duration:\s+([\d\.]+)", line)
                    if match:
                        end_t = float(match.group(1))
                        dur = float(match.group(2))
                        # [FIX] Ignore silence that occurs at the very end of the file (within 0.5s of EOF)
                        if duration_sec > 0 and end_t >= duration_sec - 0.5:
                            last_silence_start = None # Discard trailing silence
                        else:
                            if last_silence_start is not None:
                                anomalies.append({"type": "Silence_Start", "time": last_silence_start})
                                last_silence_start = None
                            anomalies.append({"type": "Silence_End", "time": end_t, "duration": dur})
                        
                # Freeze detect parsing
                # [freezedetect] lavfi.freezedetect.freeze_start: 2.5
                # [freezedetect] lavfi.freezedetect.freeze_duration: 2.3
                # [freezedetect] lavfi.freezedetect.freeze_end: 4.8
                elif "freeze_start" in line:
                    match = re.search(r"freeze_start:\s+([\d\.]+)", line)
                    if match:
                        anomalies.append({"type": "Freeze_Start", "time": float(match.group(1))})
                elif "freeze_duration" in line:
                    match = re.search(r"freeze_duration:\s+([\d\.]+)", line)
                    if match:
                        last_freeze_duration = float(match.group(1))
                elif "freeze_end" in line:
                    match = re.search(r"freeze_end:\s+([\d\.]+)", line)
                    if match:
                        anomalies.append({"type": "Freeze_End", "time": float(match.group(1)), "duration": last_freeze_duration})
                        last_freeze_duration = 0.0

                # Black detect parsing
                # [blackdetect] black_start:5.04 black_end:6.52 black_duration:1.48
                elif "black_start" in line:
                    match = re.search(r"black_start:([\d\.]+)\s+black_end:([\d\.]+)\s+black_duration:([\d\.]+)", line)
                    if match:
                        anomalies.append({
                            "type": "Black_Frame", 
                            "start": float(match.group(1)),
                            "end": float(match.group(2)),
                            "duration": float(match.group(3))
                        })
                        
                # EBU R128 parsing
                # [Parsed_ebur128_x] Summary:
                #   Integrated loudness:
                #     I:         -20.5 LUFS
                #   True peak:
                #     Peak:      -1.2 dBFS
                elif "I:         " in line and "LUFS" in line:
                    match = re.search(r"I:\s+([-\d\.]+)\s+LUFS", line)
                    if match:
                        lufs_i = float(match.group(1))
                elif "Peak:      " in line and "dBFS" in line:
                    match = re.search(r"Peak:\s+([-\d\.]+)\s+dBFS", line)
                    if match:
                        curr_peak = float(match.group(1))
                        audio_peak = max(audio_peak, curr_peak)
                        
            process.wait()

            # [NEW] Add a summary entry for audio metrics even if no violation
            if lufs_i != -float('inf') or audio_peak != -float('inf'):
                anomalies.append({
                    "type": "Audio_Loudness_Info",
                    "lufs": lufs_i if lufs_i != -float('inf') else None,
                    "peak": audio_peak if audio_peak != -float('inf') else None
                })
            
            # Post-process Audio Loudness against Broadcast Standards
            # Standard: True Peak <= -2.0 dBFS (or dBTP), Integrated = -23 or -24 LUFS
            if audio_peak > -2.0 and audio_peak != -float('inf'):
                anomalies.append({"type": "Audio_Loudness_Violation", "msg": f"True Peak ({audio_peak} dBTP) exceeds limit (-2.0 dBTP)."})
            
            if lufs_i != -float('inf'):
                if lufs_i > -22.0 or lufs_i < -25.0:
                    anomalies.append({"type": "Audio_Loudness_Violation", "msg": f"Integrated Loudness ({lufs_i} LUFS) is outside standard -23~-24 LUFS range."})

        except Exception as e:
            self.logger.error(f"Error during anomaly detection: {e}")
            anomalies.append({"type": "Exception", "msg": str(e)})

        # [NEW] Broadcast Video Standards Metadata Checks (NTSC/ITU-R)
        try:
            # We can retrieve the basic info we just parsed by calling _get_basic_info again or passing it down. 
            # For robustness, we will do a fast ffprobe extraction here tailored for these specific broadcast flags 
            # since _detect_anomalies doesn't receive the `info` dict natively.
            out = subprocess.check_output([
                self.ffprobe_cmd, "-v", "quiet", "-print_format", "json", "-show_streams", "-select_streams", "v:0", file_path
            ], encoding='utf-8', errors='replace', startupinfo=startupinfo)
            data = json.loads(out)
            if data.get("streams"):
                v_stream = data["streams"][0]
                width = int(v_stream.get("width", 0))
                color_range = v_stream.get("color_range", "unknown")
                color_space = v_stream.get("color_space", "unknown")
                fps_str = v_stream.get("r_frame_rate", "0/0")
                
                try:
                    num, den = map(int, fps_str.split('/'))
                    fps_val = round(num / den, 3) if den != 0 else 0.0
                except:
                    fps_val = 0.0

                # 1. Broadcast Safe Gamut (YUV 16-235)
                # 'pc' or 'jpeg' implies 0-255 Full Range. Broadcast standard is 'tv' or 'mpeg' (16-235).
                if color_range == "pc" or color_range == "jpeg":
                    anomalies.append({
                        "type": "NTSC_Video_Standard_Violation", 
                        "msg": f"Color Range is Full (0-255), violating Broadcast TV Legal Gamut (16-235)."
                    })
                
                # 2. HD Color Space Matching (ITU-R BT.709 vs BT.601)
                # 1920x1080 Must be BT.709. If it's BT.601 (smpte170m, etc), colors will shift on TV.
                if width >= 1280:
                    if color_space in ["smpte170m", "smpte240m", "bt470bg", "bt601"]:
                        anomalies.append({
                            "type": "NTSC_Video_Standard_Violation", 
                            "msg": f"HD Video ({width}p) using SD Color Space ({color_space}). Expected BT.709."
                        })
                        
                # 3. NTSC/PAL Standard Framerate Checks
                # Standard broadcast frame rates: 23.976, 24, 25, 29.97, 30, 50, 59.94, 60
                valid_framerates = [23.976, 24.0, 25.0, 29.970, 30.0, 50.0, 59.940, 60.0]
                # Allow a small floating point margin (0.01)
                if fps_val > 0.0 and not any(abs(fps_val - v) < 0.01 for v in valid_framerates):
                    anomalies.append({
                        "type": "NTSC_Video_Standard_Violation", 
                        "msg": f"Non-standard broadcast framerate detected: {fps_val} fps."
                    })

        except Exception as e:
            self.logger.error(f"Error checking NTSC video standards: {e}")

        return anomalies

if __name__ == "__main__":
    import sys
    # For testing isolation
    # python core/qc_analyzer.py <path_to_video>
    if len(sys.argv) > 1:
        qc = BroadcastQC()
        res = qc.analyze(sys.argv[1])
        print(json.dumps(res, indent=4, ensure_ascii=False))
