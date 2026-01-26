from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QListWidget, 
                               QPushButton, QLabel, QMessageBox, QAbstractItemView)
from PySide6.QtCore import Qt

class HistoryManagerDialog(QDialog):
    def __init__(self, settings_manager, history_key, title="Manage History", parent=None):
        super().__init__(parent)
        self.settings = settings_manager
        self.history_key = history_key
        self.setWindowTitle(title)
        self.resize(500, 400)
        self.setStyleSheet("""
            QDialog { background-color: #2b2b2b; color: #ffffff; }
            QListWidget { background-color: #1e1e1e; border: 1px solid #333; color: #ddd; font-size: 14px; }
            QListWidget::item { padding: 8px; }
            QListWidget::item:selected { background-color: #0078d4; }
            QPushButton { background-color: #444; color: white; border: 1px solid #555; padding: 6px 12px; border-radius: 4px; }
            QPushButton:hover { background-color: #555; }
            QLabel { color: #bbb; font-size: 12px; }
        """)
        
        layout = QVBoxLayout(self)
        
        layout.addWidget(QLabel("選取項目以刪除 (Select items to delete):"))
        
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        layout.addWidget(self.list_widget)
        
        # Buttons
        btn_layout = QHBoxLayout()
        
        self.btn_delete = QPushButton("刪除選取 (Delete Selected)")
        self.btn_delete.setStyleSheet("background-color: #d32f2f; border-color: #b71c1c;")
        self.btn_delete.clicked.connect(self.delete_selected)
        
        self.btn_clear_all = QPushButton("全部清除 (Clear All)")
        self.btn_clear_all.clicked.connect(self.clear_all)
        
        self.btn_close = QPushButton("關閉 (Close)")
        self.btn_close.clicked.connect(self.accept)
        
        btn_layout.addWidget(self.btn_delete)
        btn_layout.addWidget(self.btn_clear_all)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_close)
        
        layout.addLayout(btn_layout)
        
        self.load_history()
        
    def load_history(self):
        self.list_widget.clear()
        history = self.settings.get(self.history_key, [])
        for path in history:
            self.list_widget.addItem(path)
            
    def delete_selected(self):
        items = self.list_widget.selectedItems()
        if not items: return
        
        history = self.settings.get(self.history_key, [])
        dirty = False
        for item in items:
            path = item.text()
            if path in history:
                history.remove(path)
                dirty = True
            self.list_widget.takeItem(self.list_widget.row(item))
            
        if dirty:
            self.settings.set(self.history_key, history)
            
    def clear_all(self):
        if self.list_widget.count() == 0: return
        
        ret = QMessageBox.question(self, "Confirm", "確定要清除所有紀錄？ (Clear All?)", 
                                   QMessageBox.Yes | QMessageBox.No)
        if ret == QMessageBox.Yes:
            self.settings.set(self.history_key, [])
            self.list_widget.clear()
