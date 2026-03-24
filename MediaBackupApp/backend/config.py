import os
from pathlib import Path

# Base directory for the entire app
BASE_DIR = Path(__file__).resolve().parent.parent

# Storage configuration
STORAGE_DIR = BASE_DIR / "storage"
THUMBNAIL_DIR = STORAGE_DIR / "thumbnails"

# Ensure directories exist
os.makedirs(STORAGE_DIR, exist_ok=True)
os.makedirs(THUMBNAIL_DIR, exist_ok=True)

# Database configuration
DATABASE_URL = f"sqlite:///{BASE_DIR}/media_database.db"

# Supported file extensions
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}
ALL_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
