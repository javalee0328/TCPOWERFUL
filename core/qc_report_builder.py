import os
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# We need a font that supports Chinese characters for the report. 
# Microsoft JhengHei (msjh.ttc) is standard on traditional Chinese Windows.
FONT_PATH = "C:\\Windows\\Fonts\\msjh.ttc"

def register_fonts():
    try:
        # Register Microsoft JhengHei
        pdfmetrics.registerFont(TTFont('MSJH', FONT_PATH))
        return True
    except Exception as e:
        print(f"Failed to load font {FONT_PATH}: {e}")
        return False

class QCReportBuilder:
    def __init__(self):
        self.font_loaded = register_fonts()
        self.styles = getSampleStyleSheet()
        
        # Base styles
        font_name = 'MSJH' if self.font_loaded else 'Helvetica'
        
        self.styles.add(ParagraphStyle(name='Title_Zh', 
                                     fontName=font_name, 
                                     fontSize=24, 
                                     alignment=1, # Center
                                     spaceAfter=20))
                                     
        self.styles.add(ParagraphStyle(name='Heading1_Zh',
                                     fontName=font_name,
                                     fontSize=16,
                                     textColor=colors.darkblue,
                                     spaceAfter=10,
                                     spaceBefore=15))
                                     
        self.styles.add(ParagraphStyle(name='Normal_Zh',
                                     fontName=font_name,
                                     fontSize=11,
                                     spaceAfter=6))

        self.styles.add(ParagraphStyle(name='Error_Zh',
                                     fontName=font_name,
                                     fontSize=11,
                                     textColor=colors.red,
                                     spaceAfter=6))
                                     
        self.styles.add(ParagraphStyle(name='Pass_Zh',
                                     fontName=font_name,
                                     fontSize=11,
                                     textColor=colors.green,
                                     spaceAfter=6))

    def generate_pdf(self, qc_data, output_path):
        """Generates the PDF QC report."""
        doc = SimpleDocTemplate(output_path, pagesize=A4,
                                rightMargin=40, leftMargin=40,
                                topMargin=40, bottomMargin=30)
        
        story = []
        
        # 1. Main Title
        story.append(Paragraph("廣播級檔案檢測報告 (Broadcast QC Report)", self.styles['Title_Zh']))
        story.append(Spacer(1, 10))

        # 2. Section A: Basic Info
        story.append(Paragraph("第一部分：素材基本信息 (Section A: Material Info)", self.styles['Heading1_Zh']))
        
        info = qc_data.get("info", {})
        
        # Helper to get info safely
        def g(key, default=""): return str(info.get(key, default))
        
        # 3-column layout matching screenshot (6 columns total: Label, Value, Label, Value, Label, Value)
        basic_data = [
            ["檔案名", qc_data.get("file", "Unknown"), "TC首格", g('start_tc'), "時長", g('duration_str')],
            ["解析度", g("video_resolution"), "顏色取樣", g("color_sampling"), "碼率", g("bitrate_mbps")],
            ["影片編碼", g("video_codec"), "封裝格式", g("format_name"), "", g("dropframe")],
            ["聲音編碼", g("audio_codec"), "聲道數", g("audio_channels"), "修改時間", g("mod_time")],
            ["掃瞄方式", g("scan_type"), "顯示比例", g("aspect_ratio"), "創建時間", g("create_time")],
            ["幀率", g("fps"), "AFD", g("afd"), "檔案大小", g("size_mb")],
            ["色域", g("color_primaries"), "高動態範圍", g("color_transfer"), "音頻採樣率", g("audio_sample_rate")],
        ]
        
        # Adjust ColWidths for A4 Portrait (Total Width ~ 500)
        # 60 (lbl) + 110 (val) + 65 (lbl) + 85 (val) + 70 (lbl) + 110 (val) = 500
        t_base = Table(basic_data, colWidths=[60, 110, 65, 85, 70, 110])
        
        # Styling to match the screenshot form
        t_style = TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), 'MSJH' if self.font_loaded else 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            
            # Label Columns (0, 2, 4) styling (Grey background, Right-aligned text, Black font)
            ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
            ('ALIGN', (2, 0), (2, -1), 'RIGHT'),
            ('ALIGN', (4, 0), (4, -1), 'RIGHT'),
            
            # Value Columns (1, 3, 5) styling (White background, Left-aligned text, Black font)
            ('ALIGN', (1, 0), (1, -1), 'LEFT'),
            ('ALIGN', (3, 0), (3, -1), 'LEFT'),
            ('ALIGN', (5, 0), (5, -1), 'LEFT'),
            
            # Padding
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (0, -1), 8), # More space after labels
            ('RIGHTPADDING', (2, 0), (2, -1), 8),
            ('RIGHTPADDING', (4, 0), (4, -1), 8),
            
            # Grid (Light grey borders typical of forms)
            ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ('BOX', (0, 0), (-1, -1), 1, colors.grey),
        ])
        t_base.setStyle(t_style)
        story.append(t_base)
        story.append(Spacer(1, 20))

        # 3. Section B: Anomalies
        story.append(Paragraph("第二部分：異常檢測點 (Section B: Anomalies)", self.styles['Heading1_Zh']))
        
        anomalies = qc_data.get("anomalies", [])
        
        if not anomalies:
            story.append(Paragraph("✅ 檢測通過：檔案完美，未發現任何畫面停格、無聲或解碼異常！", self.styles['Pass_Zh']))
        else:
            story.append(Paragraph(f"❌ 警告：共發現 {len(anomalies)} 處廣播異常事故！", self.styles['Error_Zh']))
            story.append(Spacer(1, 10))
            
            # Anomalies Table
            headerRow = ["時間碼 (Time)", "類型 (Type)", "錯誤摘要 (Details)"]
            anomaly_data = [headerRow]
            
            for a in anomalies:
                atype = a.get("type", "Unknown")
                
                # Format time display
                if "time" in a:
                    time_val = f"{a['time']:.2f}s"
                elif "start" in a:
                    time_val = f"{a['start']:.2f}s"
                else:
                    time_val = "N/A"
                    
                # Format details
                if atype == "Silence_End" or atype == "Freeze_End":
                    details = f"持續異常 (Duration): {a.get('duration', 0):.2f}s"
                elif atype == "Black_Frame":
                    details = f"純黑畫面，持續 {a.get('duration', 0):.2f}s"
                elif atype == "Audio_Loudness_Violation":
                    details = a.get("msg", "")
                    time_val = "Global" # Loudness applies globally
                elif atype == "DecodeError":
                    details = a.get("msg", "解碼錯誤")
                else:
                    details = "偵測開始"
                    
                # Translate types for readability
                zh_type = atype
                if "Silence" in atype: zh_type = "無聲 (Silence)"
                elif "Freeze" in atype: zh_type = "停格 (Freeze)"
                elif "Black" in atype: zh_type = "全黑 (Black)"
                elif "Decode" in atype: zh_type = "解碼破圖 (Codec Err)"
                elif "Loudness" in atype: zh_type = "音量違規 (Loudness)"
                
                anomaly_data.append([time_val, zh_type, details])
                
            t_fail = Table(anomaly_data, colWidths=[100, 150, 250])
            t_fail.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.darkred),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, -1), 'MSJH' if self.font_loaded else 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('GRID', (0, 0), (-1, -1), 1, colors.black),
                # Alternate row colors
                ('BACKGROUND', (0, 1), (-1, -1), colors.whitesmoke),
            ]))
            
            # Apply alternating band colors starting from row index 1
            for i in range(1, len(anomaly_data)):
                if i % 2 == 0:
                    t_fail.setStyle(TableStyle([('BACKGROUND', (0, i), (-1, i), colors.lightgrey)]))
                
            story.append(t_fail)

        try:
            doc.build(story)
            return True, output_path
        except Exception as e:
            from traceback import format_exc
            return False, f"PDF Build Error: {str(e)}\n{format_exc()}"

if __name__ == "__main__":
    # Test
    dummy_data = {
        "file": "TEST_BROADCAST_TC.mxf",
        "timestamp": "2026-02-26 14:00:00",
        "info": {
            "size_bytes": 1024*1024*500,
            "duration_sec": 3600,
            "format_name": "mxf",
            "video_codec": "mpeg2video",
            "video_resolution": "1920x1080",
            "audio_codec": "pcm_s16le"
        },
        "anomalies": [
           {"type": "Silence_Start", "time": 15.0},
           {"type": "Silence_End", "time": 17.5, "duration": 2.5},
           {"type": "Audio_Loudness_Violation", "msg": "True Peak (2.5 dBTP) exceeds limit (-2.0 dBTP)."}
        ]
    }
    
    builder = QCReportBuilder()
    success, path = builder.generate_pdf(dummy_data, "test_qc_report.pdf")
    print(f"Success: {success}, Path: {path}")
