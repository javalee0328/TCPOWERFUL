import os
import time
import shutil
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from sqlalchemy.orm import Session
import config
import database
import processor
from database import MediaFile, SessionLocal

class SyncHandler(FileSystemEventHandler):
    def __init__(self, delete_after_sync=False):
        self.delete_after_sync = delete_after_sync
        # Initialize DB on start
        database.init_db()

    def on_created(self, event):
        if not event.is_directory:
            self.process_file(event.src_path)

    def process_file(self, file_path):
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in config.ALL_EXTENSIONS:
            return

        print(f"[*] New file detected: {file_path}")
        # Wait a bit for file to be fully written
        time.sleep(1)
        
        try:
            db = SessionLocal()
            file_hash = processor.get_file_hash(file_path)
            
            # Check for duplicates
            existing = db.query(MediaFile).filter(MediaFile.file_hash == file_hash).first()
            if existing:
                print(f"[!] File already exists in backup: {existing.filename}")
                if self.delete_after_sync:
                    os.remove(file_path)
                    print(f"[-] Deleted local duplicate: {file_path}")
                return

            # Process new file
            filename = os.path.basename(file_path)
            final_filename = f"{file_hash}{ext}"
            final_path = os.path.join(config.STORAGE_DIR, final_filename)
            
            # Copy to storage
            shutil.copy2(file_path, final_path)
            
            # Metadata and thumbnail
            meta = processor.extract_metadata(final_path)
            thumb_name = f"thumb_{file_hash}.jpg"
            thumb_path = os.path.join(config.THUMBNAIL_DIR, thumb_name)
            processor.generate_thumbnail(final_path, thumb_path)
            
            # Save to DB
            new_media = MediaFile(
                filename=filename,
                file_path=final_path,
                file_hash=file_hash,
                file_size=os.path.getsize(final_path),
                file_type="video" if ext in config.VIDEO_EXTENSIONS else "image",
                mime_type=f"media/{ext[1:]}",
                taken_at=meta["taken_at"],
                width=meta["width"],
                height=meta["height"],
                duration=meta["duration"],
                metadata_json=meta["extra"],
                thumbnail_path=thumb_path
            )
            db.add(new_media)
            db.commit()
            print(f"[+] Successfully backed up: {filename}")
            
            if self.delete_after_sync:
                os.remove(file_path)
                print(f"[-] Cleaned up local file: {file_path}")
                
        except Exception as e:
            print(f"[E] Error syncing {file_path}: {e}")
        finally:
            db.close()

def start_watcher(path_to_watch, delete_after_sync=False):
    if not os.path.exists(path_to_watch):
        os.makedirs(path_to_watch)
        
    event_handler = SyncHandler(delete_after_sync=delete_after_sync)
    observer = Observer()
    observer.schedule(event_handler, path_to_watch, recursive=False)
    observer.start()
    print(f"[*] Started watching: {path_to_watch}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    # Default watch folder
    watch_dir = os.path.join(os.path.dirname(config.BASE_DIR), "local_sync")
    start_watcher(watch_dir, delete_after_sync=True)
