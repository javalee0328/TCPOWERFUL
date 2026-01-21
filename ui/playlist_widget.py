
class PlaylistItemWidget(QWidget):
    removed = Signal()
    
    def __init__(self, text, tooltip="", parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 0, 5, 0)
        layout.setSpacing(5)
        
        # Label
        self.lbl = QLabel(text)
        self.lbl.setStyleSheet("color: #ddd; background: transparent; font-size: 13px;")
        self.lbl.setAttribute(Qt.WA_TransparentForMouseEvents) # Pass clicks to parent list item? 
        # Actually if we use setItemWidget, the widget captures mouse events.
        # But we want row selection to work.
        # QListWidget should handle selection if we don't block it.
        # But QLabel might swallow it? No, QLabel is usually fine.
        
        layout.addWidget(self.lbl, 1)
        
        # Close Button (X)
        self.btn_close = QToolButton()
        self.btn_close.setText("✕")
        self.btn_close.setFixedSize(24, 24)
        self.btn_close.setStyleSheet("""
            QToolButton { 
                background: transparent; 
                border: none; 
                color: #aaa; 
                font-weight: bold; 
                font-size: 14px; 
            } 
            QToolButton:hover { 
                color: #ff5252; 
                background: rgba(255, 255, 255, 0.1); 
                border-radius: 12px; 
            }
        """)
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.clicked.connect(self.removed.emit)
        
        layout.addWidget(self.btn_close)
