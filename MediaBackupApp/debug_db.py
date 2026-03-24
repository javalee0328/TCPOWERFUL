import sqlite3
import os

db_path = "c:/Users/jerry.lee/PycharmProjects/pythonProject/MediaBackupApp/media_database.db"
if not os.path.exists(db_path):
    print(f"DB not found at {db_path}")
    sys.exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("SELECT id, filename, file_hash, file_type, taken_at FROM media_files")
rows = cursor.fetchall()
for row in rows:
    print(row)
conn.close()
