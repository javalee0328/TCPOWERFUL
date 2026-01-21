# Backup of original player_widget.py (content with line numbers for reference)
1: from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
2:                                 QSlider, QLabel, QStyle, QSizePolicy, QFileDialog, QStackedLayout, QSpinBox, QStyleOptionSlider,
3:                                 QAbstractSpinBox, QToolButton)
4: from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
5: from PySide6.QtMultimediaWidgets import QVideoWidget
6: from PySide6.QtGui import QIcon, QAction, QPainter, QColor, QBrush, QPen, QFont, QLinearGradient, QPixmap, QImage, QPainterPath
7: from PySide6.QtCore import Qt, QUrl, QTimer, Signal, QRect, QPoint, QThread, QRectF, QSize
8: import random
9: import os
10: import subprocess
11: import ctypes # For Window Embedding
12: 
13: from core.analyzer import AudioLevelAnalyzer
14: 
15: class StereoVUMeter(QWidget): # Kept name for compatibility, but logic is Multi-channel
... (rest of file omitted for brevity)
