import os
import csv
from datetime import datetime
import threading

class QCBatchReporter:
    """
    Handles appending individual QC results to a daily batch CSV report.
    This allows broadcasters to review dozens of files in a single unified spreadsheet.
    """
    _lock = threading.Lock()

    @staticmethod
    def append_to_daily_report(output_dir, qc_data):
        """
        Appends a QC result to the daily CSV report located in output_dir.
        """
        if not output_dir or not os.path.exists(output_dir):
            return

        date_str = datetime.now().strftime("%Y-%m-%d")
        report_filename = f"QC_Batch_Report_{date_str}.csv"
        report_path = os.path.join(output_dir, report_filename)

        # 1. Gather Metrics & Info
        metrics = qc_data.get("metrics", {})
        info = qc_data.get("info", {})
        anomalies = qc_data.get("anomalies", [])
        
        status = "Pass"
        error_msgs = []
        
        for anomaly in anomalies:
            typ = anomaly.get("type", "")
            if typ == "Audio_Loudness_Info":
                continue # Just info, not a failure
                
            if typ in ["Decode Error", "Loudness Error", "Exception", "Audio_Loudness_Violation"]:
                # Log specific message if available
                msg = anomaly.get("msg", typ)
                if typ == "Audio_Loudness_Violation":
                    error_msgs.append("音量超標 (Audio Violation)")
                else:
                    error_msgs.append(msg)
                status = "Fail"
                
        if metrics.get("mosaic_count", 0) > 0 or metrics.get("freeze_count", 0) > 0:
            status = "Warning" if status != "Fail" else "Fail"

        # 2. Format Basic Info
        v_info = f"{info.get('video_codec', '?')} | {info.get('video_resolution', '?')} | {info.get('fps', '?')}"
        
        # 3. Format Duration
        dur_sec = info.get("duration_sec", 0)
        hrs = int(dur_sec // 3600)
        mins = int((dur_sec % 3600) // 60)
        secs = int(dur_sec % 60)
        dur_str = f"{hrs:02d}:{mins:02d}:{secs:02d}"

        # 4. Format Audio Levels
        lufs = metrics.get("lufs_i")
        peak = metrics.get("peak_db")
        audio_str = f"{lufs} / {peak}" if lufs is not None else "-"

        # 5. Summarize Anomaly Details
        remarks = []
        if error_msgs:
            remarks.extend(error_msgs)
            
        if metrics.get("mosaic_count", 0) > 0: remarks.append(f"馬賽克 x{metrics['mosaic_count']}")
        if metrics.get("freeze_count", 0) > 0: remarks.append(f"停格 x{metrics['freeze_count']}")
        if metrics.get("black_count", 0) > 0: remarks.append(f"黑畫面 x{metrics['black_count']}")
        if metrics.get("silence_count", 0) > 0: remarks.append(f"靜音 x{metrics['silence_count']}")
        
        # Catch-all for other anomalies if we haven't added anything yet but status is Fail/Warning
        if not remarks and anomalies:
            remarks.append("包含其他異常 (Contains Other Anomalies)")
            
        remarks_str = ", ".join(remarks) if remarks else "正常"

        row = [
            qc_data.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            qc_data.get("file", "Unknown"),
            status,
            v_info,
            metrics.get("mosaic_count", 0),
            metrics.get("freeze_count", 0),
            info.get("start_tc", "00:00:00:00"),
            dur_str,
            audio_str,
            remarks_str
        ]

        file_exists = os.path.isfile(report_path)

        with QCBatchReporter._lock:
            try:
                with open(report_path, mode='a', newline='', encoding='utf-8-sig') as csvfile:
                    writer = csv.writer(csvfile)
                    if not file_exists:
                        writer.writerow([
                            "檢測時間 (Time)", 
                            "檔案名稱 (File)", 
                            "狀態 (Status)", 
                            "影音資訊 (Video/FPS)", 
                            "馬賽克 (Mosaic)", 
                            "停格 (Freeze)", 
                            "起始時碼 (Start TC)", 
                            "時長 (Duration)", 
                            "音量 (LUFS/Peak)", 
                            "異常摘要 (Remarks)"
                        ])
                    writer.writerow(row)
                
                # [NEW] Generate the Unified PDF Table after appending
                try:
                    pdf_filename = f"QC_Batch_Report_{date_str}.pdf"
                    pdf_path = os.path.join(output_dir, pdf_filename)
                    QCBatchReporter._generate_daily_pdf(report_path, pdf_path, date_str)
                except Exception as pdf_e:
                    import logging
                    logging.getLogger("QCBatchReporter").error(f"Non-fatal error building PDF: {pdf_e}")
                
            except Exception as e:
                import logging
                logging.getLogger("QCBatchReporter").error(f"Failed to write batch report: {e}")

    @staticmethod
    def _generate_daily_pdf(csv_path, pdf_path, date_str):
        """Generates a landscape PDF table from the daily CSV report."""
        try:
            from reportlab.lib.pagesizes import A4, landscape
            from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
            from reportlab.lib import colors
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            import core.qc_report_builder as qcrb  # To reuse font loading
            
            font_loaded = qcrb.register_fonts()
            font_name = 'MSJH' if font_loaded else 'Helvetica'
            
            styles = getSampleStyleSheet()
            title_style = ParagraphStyle(name='Title_Zh', 
                                         fontName=font_name, 
                                         fontSize=18, 
                                         alignment=1, 
                                         spaceAfter=20)
            
            # [NEW] Style for table cells to enable text wrapping
            cell_style = ParagraphStyle(name='Cell_Zh',
                                        fontName=font_name,
                                        fontSize=9,
                                        alignment=1) # Center alignment
            
            # Read CSV Data
            table_data = []
            with open(csv_path, mode='r', encoding='utf-8-sig') as f:
                reader = csv.reader(f)
                for i, row in enumerate(reader):
                    # Validate row length to prevent tuple index out of range
                    row = [str(cell) for cell in row]
                    while len(row) < 10:
                        row.append("-")
                    row = row[:10]
                    # Wrap each cell in a Paragraph for text wrapping, except header which we might format differently but Paragraph works fine for both
                    wrapped_row = [Paragraph(cell, cell_style) for cell in row]
                    table_data.append(wrapped_row)
            
            # Need at least header + 1 data row
            if len(table_data) < 2:
                return
                
            doc = SimpleDocTemplate(pdf_path, pagesize=landscape(A4),
                                    rightMargin=20, leftMargin=20,
                                    topMargin=30, bottomMargin=30)
            
            styles = getSampleStyleSheet()
            title_style = ParagraphStyle(name='Title_Zh', 
                                         fontName=font_name, 
                                         fontSize=18, 
                                         alignment=1, 
                                         spaceAfter=20)
            
            story = []
            story.append(Paragraph(f"廣播級檔案檢測總表 (QC Batch Report) - {date_str}", title_style))
            
            # ColWidths: Tune to Landscape A4 (Total width ~800 points)
            col_widths = [110, 160, 50, 120, 50, 40, 70, 60, 70, 80]
            
            t = Table(table_data, colWidths=col_widths, repeatRows=1)
            
            # Styling the table
            style = TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#2C3E50")),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('FONTNAME', (0, 0), (-1, -1), font_name),
                ('FONTSIZE', (0, 0), (-1, 0), 10),
                ('FONTSIZE', (0, 1), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ])
            
            # Color code the rows based on status
            for i, row in enumerate(table_data):
                if i == 0: continue
                
                # row[2] is a Paragraph object. Use .text to get the string content
                status_cell = row[2]
                status = getattr(status_cell, 'text', '') if hasattr(status_cell, 'text') else str(status_cell)
                
                row_bg_color = None
                if "Warning" in status:
                    row_bg_color = colors.HexColor("#f39c12")
                    style.add('BACKGROUND', (2, i), (2, i), row_bg_color) # Orange
                    style.add('TEXTCOLOR', (2, i), (2, i), colors.white)
                elif "Fail" in status:
                    row_bg_color = colors.HexColor("#e74c3c")
                    style.add('BACKGROUND', (2, i), (2, i), row_bg_color) # Red
                    style.add('TEXTCOLOR', (2, i), (2, i), colors.white)
                else: 
                    style.add('TEXTCOLOR', (2, i), (2, i), colors.HexColor("#27ae60")) # Green
                
                # Zebra striping
                if i % 2 == 0:
                    style.add('BACKGROUND', (0, i), (-1, i), colors.HexColor("#f8f9fa"))
                    if row_bg_color:
                        style.add('BACKGROUND', (2, i), (2, i), row_bg_color) # Preserve priority colors

            t.setStyle(style)
            story.append(t)
            
            doc.build(story)
        except Exception as e:
            import logging
            logging.getLogger("QCBatchReporter").error(f"Failed to build PDF report: {e}")
