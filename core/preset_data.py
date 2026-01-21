
# Presets derived from User Attachments (Broadcast Standards)
# Keys are formatted as "NO. Name" to keep order

PRESETS = {
    "01. MOV DVCPRO 50M (720x480)": {"container": "mov", "vcodec": "dvvideo", "bitrate": "50000k", "resolution": "720x480", "audio_ch": 4, "fps": "29.97"},
    "02. MOV DVCPRO 50M (720x576)": {"container": "mov", "vcodec": "dvvideo", "bitrate": "50000k", "resolution": "720x576", "audio_ch": 4, "fps": "25"},
    "03. MOV XDCAMHD 35M (1440x1080)": {"container": "mov", "vcodec": "mpeg2video", "bitrate": "35000k", "resolution": "1440x1080", "audio_ch": 4, "fps": "29.97"},
    "04. MOV XDCAMHD 50M (1920x1080)": {"container": "mov", "vcodec": "mpeg2video", "bitrate": "50000k", "resolution": "1920x1080", "audio_ch": 4, "fps": "29.97"},
    
    "05. MXF DVCPRO 50M (720x480)": {"container": "mxf", "vcodec": "dvvideo", "bitrate": "50000k", "resolution": "720x480", "audio_ch": 4, "fps": "29.97"},
    "06. MXF DVCPRO 25M (720x576)": {"container": "mxf", "vcodec": "dvvideo", "bitrate": "25000k", "resolution": "720x576", "audio_ch": 4, "fps": "25"},
    "07. MXF XDCAMHD 25M (1440x1080)": {"container": "mxf", "vcodec": "mpeg2video", "bitrate": "25000k", "resolution": "1440x1080", "audio_ch": 4, "fps": "29.97"},
    "08. MXF XDCAMHD 50M (1920x1080)": {"container": "mxf", "vcodec": "mpeg2video", "bitrate": "50000k", "resolution": "1920x1080", "audio_ch": 8, "fps": "29.97"},
    
    "09. MP4 H264 10M (640x360)": {"container": "mp4", "vcodec": "libx264", "bitrate": "10000k", "resolution": "640x360", "audio_ch": 2, "fps": "29.97"},
    "10. M2P H264 6M (720x480)": {"container": "m2p", "vcodec": "libx264", "bitrate": "6000k", "resolution": "720x480", "audio_ch": 2, "fps": "29.97"},
    "11. MPG H264 10M (720x480)": {"container": "mpg", "vcodec": "libx264", "bitrate": "10000k", "resolution": "720x480", "audio_ch": 2, "fps": "29.97"},
    "12. MPG H264 6M (720x576)": {"container": "mpg", "vcodec": "libx264", "bitrate": "6000k", "resolution": "720x576", "audio_ch": 2, "fps": "25"},
    
    "13. MPG H264 8M (1440x1080)": {"container": "mpg", "vcodec": "libx264", "bitrate": "8000k", "resolution": "1440x1080", "audio_ch": 2, "fps": "29.97"},
    "14. MPG H264 8M (1920x1080)": {"container": "mpg", "vcodec": "libx264", "bitrate": "8000k", "resolution": "1920x1080", "audio_ch": 2, "fps": "29.97"},
    
    "15. MPG DVD 5M (720x480)": {"container": "mpg", "vcodec": "mpeg2video", "bitrate": "5000k", "resolution": "720x480", "audio_ch": 2, "fps": "29.97"},
    "16. MPG DVD 7.5M (720x480)": {"container": "mpg", "vcodec": "mpeg2video", "bitrate": "7500k", "resolution": "720x480", "audio_ch": 2, "fps": "29.97"},
    "17. MPG DVD 8M (720x480)": {"container": "mpg", "vcodec": "mpeg2video", "bitrate": "8000k", "resolution": "720x480", "audio_ch": 2, "fps": "29.97"},
    
    "18. VOB DVD 8M (720x480)": {"container": "vob", "vcodec": "mpeg2video", "bitrate": "8000k", "resolution": "720x480", "audio_ch": 2, "fps": "29.97"},
    "19. VOB DVD 8M (720x480) 16:9": {"container": "vob", "vcodec": "mpeg2video", "bitrate": "8000k", "resolution": "720x480", "audio_ch": 2, "fps": "29.97"},
    "20. VOB DVD 7M (720x480)": {"container": "vob", "vcodec": "mpeg2video", "bitrate": "7000k", "resolution": "720x480", "audio_ch": 2, "fps": "29.97"},
    "21. VOB DVD 7M (720x480) 4:3": {"container": "vob", "vcodec": "mpeg2video", "bitrate": "7000k", "resolution": "720x480", "audio_ch": 2, "fps": "29.97"},
    "22. VOB DVD 7M (720x576) PAL": {"container": "vob", "vcodec": "mpeg2video", "bitrate": "7000k", "resolution": "720x576", "audio_ch": 2, "fps": "25"},
    "23. VOB DVD 7.5M (720x576) PAL": {"container": "vob", "vcodec": "mpeg2video", "bitrate": "7500k", "resolution": "720x576", "audio_ch": 2, "fps": "25"},
    
    "25. MP4 H264 3.5M (1280x720)": {"container": "mp4", "vcodec": "libx264", "bitrate": "3500k", "resolution": "1280x720", "audio_ch": 2, "fps": "29.97"},
    "26. MPG DVD 7M (720x480)": {"container": "mpg", "vcodec": "mpeg2video", "bitrate": "7000k", "resolution": "720x480", "audio_ch": 2, "fps": "29.97"},
    
    "27. TS H264 5M (720x480)": {"container": "ts", "vcodec": "libx264", "bitrate": "5000k", "resolution": "720x480", "audio_ch": 2, "fps": "29.97"},
    "28. TS H264 10M (1920x1080)": {"container": "ts", "vcodec": "libx264", "bitrate": "10000k", "resolution": "1920x1080", "audio_ch": 2, "fps": "29.97"},
    "29. TS H264 10M (1440x1080)": {"container": "ts", "vcodec": "libx264", "bitrate": "10000k", "resolution": "1440x1080", "audio_ch": 2, "fps": "29.97"},
    
    "30. MOV XDCAMHD 50M (1920x1080) 25P": {"container": "mov", "vcodec": "mpeg2video", "bitrate": "50000k", "resolution": "1920x1080", "audio_ch": 4, "fps": "25"},
    "31. MOV XDCAMHD 35M (1440x1080) 25P": {"container": "mov", "vcodec": "mpeg2video", "bitrate": "35000k", "resolution": "1440x1080", "audio_ch": 4, "fps": "25"},
    
    "32. AVI DV 30M (720x576)": {"container": "avi", "vcodec": "dvvideo", "bitrate": "30000k", "resolution": "720x576", "audio_ch": 2, "fps": "25"},
    
    "33. TS H264 10M (1920x1080) MOD": {"container": "ts", "vcodec": "libx264", "bitrate": "10000k", "resolution": "1920x1080", "audio_ch": 2, "fps": "29.97"},
    "34. TS H264 10M (1440x1080) MOD": {"container": "ts", "vcodec": "libx264", "bitrate": "10000k", "resolution": "1440x1080", "audio_ch": 2, "fps": "29.97"},
    "35. MPG H264 2M (352x480)": {"container": "mpg", "vcodec": "libx264", "bitrate": "2000k", "resolution": "352x480", "audio_ch": 2, "fps": "29.97"},
    
    "48. MP4 H264 18M (1920x1080)": {"container": "mp4", "vcodec": "libx264", "bitrate": "18000k", "resolution": "1920x1080", "audio_ch": 2, "fps": "29.97"},
    "49. MP4 H264 8M (720x480)": {"container": "mp4", "vcodec": "libx264", "bitrate": "8000k", "resolution": "720x480", "audio_ch": 2, "fps": "29.97"},
    "50. WMV 512K (480x360)": {"container": "wmv", "vcodec": "wmv2", "bitrate": "512k", "resolution": "480x360", "audio_ch": 2, "fps": "29.97"},
    
    "53. TS H264 8M (1920x1080) VOD": {"container": "ts", "vcodec": "libx264", "bitrate": "8000k", "resolution": "1920x1080", "audio_ch": 2, "fps": "29.97"},
    "56. MXF XDCAMHD 50M (1920x1080) 8ch": {"container": "mxf", "vcodec": "mpeg2video", "bitrate": "50000k", "resolution": "1920x1080", "audio_ch": 8, "fps": "29.97"},
    "57. MP4 H264 1M (1920x1080)": {"container": "mp4", "vcodec": "libx264", "bitrate": "1000k", "resolution": "1920x1080", "audio_ch": 2, "fps": "29.97"}
}
