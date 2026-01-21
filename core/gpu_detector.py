import subprocess
import json
import logging
import os

def is_encoder_functional(encoder_name):
    """
    Actually tries to initialize the encoder with a 1-frame dummy task.
    """
    try:
        # Create a 1-second silence/black test
        cmd = [
            "ffmpeg", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
            "-c:v", encoder_name, "-f", "null", "-"
        ]
        flags = 0x08000000 if os.name == 'nt' else 0 # CREATE_NO_WINDOW
        subprocess.run(cmd, capture_output=True, check=True, timeout=5, creationflags=flags)
        return True
    except:
        return False

def get_gpu_encoders():
    """
    Detects available hardware encoders via FFmpeg and VERIFIES functionality.
    """
    encoders = {"nvenc": False, "qsv": False, "amf": False}
    
    try:
        flags = 0x08000000 if os.name == 'nt' else 0
        result = subprocess.run(["ffmpeg", "-encoders"], capture_output=True, text=True, check=True, creationflags=flags)
        output = result.stdout.lower()
        
        if "nvenc" in output and is_encoder_functional("h264_nvenc"):
            encoders["nvenc"] = True
        if "qsv" in output and is_encoder_functional("h264_qsv"):
            encoders["qsv"] = True
        if "amf" in output and is_encoder_functional("h264_amf"):
            encoders["amf"] = True
            
    except Exception as e:
        logging.error(f"Error detecting GPU encoders: {e}")
        
    return encoders

def get_best_h264_encoder(available):
    if available["nvenc"]:
        return "h264_nvenc"
    elif available["qsv"]:
        return "h264_qsv"
    elif available["amf"]:
        return "h264_amf"
    return "libx264"

def get_best_hevc_encoder(available):
    if available["nvenc"]:
        return "hevc_nvenc"
    elif available["qsv"]:
        return "hevc_qsv"
    elif available["amf"]:
        return "hevc_amf"
    return "libx265"

if __name__ == "__main__":
    print("Detecting hardware acceleration...")
    gpu_list = get_gpu_encoders()
    print(f"Available HW Encoders: {gpu_list}")
    print(f"Recommended H.264: {get_best_h264_encoder(gpu_list)}")
