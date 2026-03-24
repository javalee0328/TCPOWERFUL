import os
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Float, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

# Local config import
import config

Base = declarative_base()

class MediaFile(Base):
    __tablename__ = "media_files"
    
    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String)
    file_path = Column(String, unique=True, index=True)
    file_hash = Column(String, unique=True, index=True) # SHA-256 for differential backup
    file_size = Column(Integer)
    file_type = Column(String) # image or video
    mime_type = Column(String)
    
    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    taken_at = Column(DateTime) # Extracted from EXIF
    width = Column(Integer)
    height = Column(Integer)
    duration = Column(Float, nullable=True) # For videos
    
    # Extra metadata as JSON (GPS, Camera model, etc.)
    metadata_json = Column(JSON, nullable=True)
    
    thumbnail_path = Column(String, nullable=True)

# Database Engine
engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
