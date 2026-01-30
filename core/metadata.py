import subprocess
import json
import os
import sys

def get_video_metadata(file_path):
    """
    Returns a dict with: width, height, codec_name, duration, fps, bitrate
    """
    try:
        cmd = [
            "ffprobe", 
            "-v", "quiet", 
            "-print_format", "json", 
            "-show_format", 
            "-show_streams", 
            "-select_streams", "v:0", # Changed below to select all
            file_path
        ]
        
        # Override cmd to select audio too
        cmd = [
            "ffprobe", 
            "-v", "quiet", 
            "-analyzeduration", "100000000", # 100M
            "-probesize", "100000000",      # 100M
            "-print_format", "json", 
            "-show_format", 
            "-show_streams", 
            file_path
        ]
        
        flags = 0x08000000 if os.name == 'nt' else 0 # CREATE_NO_WINDOW
        
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            encoding='utf-8',
            errors='replace',
            creationflags=flags
        )
        
        data = json.loads(result.stdout)
        
        meta = {
            "width": 0, "height": 0,
            "codec": "Unknown",
            "fps": "0",
            "duration": 0.0,
            "bitrate": "0",
            "format": "Unknown",
            "audio_codec": "None",
            "audio_channels": 0
        }
        
        # Parse Streams
        if "streams" in data:
            for stream in data["streams"]:
                if stream.get("codec_type") == "video":
                    # Only take first video stream
                    if meta["width"] == 0: 
                        meta["width"] = stream.get("width", 0)
                        meta["height"] = stream.get("height", 0)
                        meta["codec"] = stream.get("codec_name", "unknown")
                        meta["codec_tag"] = stream.get("codec_tag_string", "")
                        
                        # FPS
                        avg_frame_rate = stream.get("avg_frame_rate", "0/0")
                        if "/" in avg_frame_rate:
                            n, d = map(int, avg_frame_rate.split('/'))
                            if d > 0:
                                meta["fps"] = f"{n/d:.2f}"
                                
                elif stream.get("codec_type") == "audio":
                    # Only take first audio stream
                    if meta["audio_codec"] == "None":
                        meta["audio_codec"] = stream.get("codec_name", "unknown")
                        meta["audio_channels"] = stream.get("channels", 0)

        # Container Info
        if "format" in data:
            fmt = data["format"]
            meta["format"] = fmt.get("format_name", "unknown")
            if "duration" in fmt:
                meta["duration"] = float(fmt["duration"])
            
            # Capture Overall Bitrate
            br = 0
            if "bit_rate" in fmt:
                try:
                    br = int(fmt["bit_rate"])
                except:
                    br = 0
            
            # Fallback: Sum streams if overall is 0
            if br == 0:
                total_br = 0
                if "streams" in data:
                    for s in data["streams"]:
                        try:
                            total_br += int(s.get("bit_rate", 0))
                        except:
                            pass
                br = total_br
            
            meta["bitrate"] = br # Return raw int (bps)

        return meta
    except Exception as e:
        print(f"Metadata Error: {e}")
        return None
