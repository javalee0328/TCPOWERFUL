import os
import hashlib
from datetime import datetime
from PIL import Image, ImageOps
import exifread
import cv2
import config

def get_file_hash(file_path: str) -> str:
    """Calculate SHA-256 hash of a file for deduplication."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        # Read in 64kb chunks
        for byte_block in iter(lambda: f.read(65536), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def extract_metadata(file_path: str):
    """Extract EXIF metadata and other info from media files."""
    ext = os.path.splitext(file_path)[1].lower()
    metadata = {
        "taken_at": None,
        "width": 0,
        "height": 0,
        "duration": None,
        "extra": {}
    }
    
    if ext in config.IMAGE_EXTENSIONS:
        try:
            with open(file_path, 'rb') as f:
                tags = exifread.process_file(f, stop_tag="EXIF DateTimeOriginal")
                if "EXIF DateTimeOriginal" in tags:
                    date_str = str(tags["EXIF DateTimeOriginal"])
                    try:
                        metadata["taken_at"] = datetime.strptime(date_str, '%Y:%m:%d %H:%M:%S')
                    except ValueError:
                        pass
            
            with Image.open(file_path) as img:
                metadata["width"], metadata["height"] = img.size
                # Handle rotation from EXIF
                try:
                    exif = img._getexif()
                    if exif:
                        metadata["extra"]["exif"] = {str(k): str(v) for k, v in exif.items() if isinstance(v, (str, int))}
                except:
                    pass
        except Exception as e:
            print(f"Error processing image metadata: {e}")
            
    elif ext in config.VIDEO_EXTENSIONS:
        try:
            cap = cv2.VideoCapture(file_path)
            if cap.isOpened():
                metadata["width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                metadata["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                fps = cap.get(cv2.CAP_PROP_FPS)
                frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                if fps > 0:
                    metadata["duration"] = frame_count / fps
            cap.release()
        except Exception as e:
            print(f"Error processing video metadata: {e}")
            
    return metadata

def generate_thumbnail(file_path: str, thumb_path: str, size=(400, 400)):
    """Generate a thumbnail for images or videos."""
    ext = os.path.splitext(file_path)[1].lower()
    
    if ext in config.IMAGE_EXTENSIONS:
        try:
            with Image.open(file_path) as img:
                # Handle orientation
                img = ImageOps.exif_transpose(img)
                img.thumbnail(size)
                img.save(thumb_path, "JPEG", quality=85)
                return True
        except Exception as e:
            print(f"Image thumbnail failed: {e}")
            
    elif ext in config.VIDEO_EXTENSIONS:
        try:
            cap = cv2.VideoCapture(file_path)
            if cap.isOpened():
                # Read the first frame (or seek to 1 second)
                cap.set(cv2.CAP_PROP_POS_MSEC, 1000)
                success, frame = cap.read()
                if success:
                    # Convert BGR to RGB
                    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    img = Image.fromarray(frame_rgb)
                    img.thumbnail(size)
                    img.save(thumb_path, "JPEG")
                    cap.release()
                    return True
            cap.release()
        except Exception as e:
            print(f"Video thumbnail failed: {e}")
            
    return False
