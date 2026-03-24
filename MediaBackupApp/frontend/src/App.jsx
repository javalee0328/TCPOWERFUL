import React, { useState, useEffect, useRef } from 'react';
import axios from 'axios';
import { 
  Upload, 
  Trash2, 
  Image as ImageIcon, 
  Video as VideoIcon, 
  Plus, 
  Check, 
  X, 
  RefreshCw, 
  Search,
  Maximize2
} from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';

const API_BASE = '/api'; // Proxied to localhost:8000

const App = () => {
  const [media, setMedia] = useState([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [selectedMedia, setSelectedMedia] = useState(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [notification, setNotification] = useState(null);
  
  const fileInputRef = useRef(null);

  useEffect(() => {
    fetchMedia();
  }, []);

  const fetchMedia = async () => {
    try {
      setLoading(true);
      const res = await axios.get(`${API_BASE}/media`);
      setMedia(res.data);
    } catch (err) {
      showNotification('error', '無法載入備份列表');
    } finally {
      setLoading(false);
    }
  };

  const showNotification = (type, message) => {
    setNotification({ type, message });
    setTimeout(() => setNotification(null), 3000);
  };

  const handleUploadClick = () => {
    fileInputRef.current?.click();
  };

  const handleFileChange = async (e) => {
    const files = Array.from(e.target.files);
    if (files.length === 0) return;

    setUploading(true);
    setUploadProgress(0);
    let uploadedCount = 0;
    let skippedCount = 0;

    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      const formData = new FormData();
      formData.append('file', file);

      try {
        const res = await axios.post(`${API_BASE}/upload`, formData, {
          onUploadProgress: (progressEvent) => {
            const currentTotal = (i / files.length) * 100;
            const itemProgress = (progressEvent.loaded / progressEvent.total) * (100 / files.length);
            setUploadProgress(currentTotal + itemProgress);
          }
        });

        if (res.data.status === 'already_exists') {
          skippedCount++;
        } else {
          uploadedCount++;
        }
      } catch (err) {
        console.error('Upload failed for', file.name, err);
      }
    }

    setUploading(false);
    setUploadProgress(0);
    fetchMedia();
    
    const msg = `備份完成！成功：${uploadedCount}，省略重複：${skippedCount}`;
    showNotification('success', msg);
    
    // Deletion prompt for desktop (conceptually handled by user seeing success)
    if (uploadedCount > 0) {
      if (confirm('備份成功！是否要在手機/本地手動刪除這些檔案以釋放空間？')) {
        // Just a reminder for iOS
      }
    }
  };

  const handleDelete = async (id) => {
    if (!confirm('確定要從雲端刪除此檔案嗎？此動作無法復原。')) return;
    try {
      await axios.delete(`${API_BASE}/media/${id}`);
      setMedia(media.filter(m => m.id !== id));
      setSelectedMedia(null);
      showNotification('success', '檔案已刪除');
    } catch (err) {
      showNotification('error', '刪除失敗');
    }
  };

  const filteredMedia = media.filter(m => 
    m.filename.toLowerCase().includes(searchTerm.toLowerCase())
  );

  return (
    <div className="min-h-screen p-4 md:p-8 flex flex-col items-center">
      {/* Header */}
      <header className="w-full max-w-6xl flex flex-col md:flex-row justify-between items-center gap-4 mb-12 glass p-6 rounded-3xl">
        <div className="flex items-center gap-3">
          <div className="p-3 bg-blue-600 rounded-2xl shadow-lg shadow-blue-500/20">
            <RefreshCw className="text-white w-6 h-6" />
          </div>
          <div>
            <h1 className="text-2xl font-bold bg-gradient-to-r from-white to-white/60 bg-clip-text text-transparent">
              Media Backup Center
            </h1>
            <p className="text-sm text-white/50">Windows / Mac / iOS 雲端極速備份</p>
          </div>
        </div>

        <div className="flex items-center gap-4 w-full md:w-auto">
          <div className="relative flex-1 md:w-64">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-white/30" />
            <input 
              type="text" 
              placeholder="搜尋檔案..."
              className="w-full pl-10 pr-4 py-2 bg-white/5 border border-white/10 rounded-xl focus:outline-none focus:border-blue-500 transition-all"
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
            />
          </div>
          <button 
            disabled={uploading}
            onClick={handleUploadClick}
            className="btn-primary flex items-center gap-2 whitespace-nowrap"
          >
            {uploading ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
            {uploading ? `上傳中 ${Math.round(uploadProgress)}%` : '備份照片/影片'}
          </button>
          <input 
            type="file" 
            multiple 
            accept="image/*,video/*" 
            className="hidden" 
            ref={fileInputRef}
            onChange={handleFileChange}
          />
        </div>
      </header>

      {/* Main Content */}
      <main className="w-full max-w-6xl">
        {loading ? (
          <div className="flex flex-col items-center justify-center h-64 gap-4">
            <RefreshCw className="w-8 h-8 animate-spin text-blue-500" />
            <p className="text-white/40">正在巡覽備份清單...</p>
          </div>
        ) : filteredMedia.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-64 glass rounded-3xl p-12 text-center">
            <ImageIcon className="w-16 h-16 text-white/10 mb-4" />
            <p className="text-white/40 text-lg font-medium">尚無備份檔案</p>
            <p className="text-white/20 text-sm">點擊右上角按鈕開始第一次備份</p>
          </div>
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-4">
            {filteredMedia.map((m) => (
              <motion.div 
                layoutId={`card-${m.id}`}
                key={m.id}
                onClick={() => setSelectedMedia(m)}
                className="glass-card aspect-square relative group cursor-pointer"
              >
                <img 
                  src={`${API_BASE}/media/thumbnail/${m.id}`} 
                  alt={m.filename}
                  className="w-full h-full object-cover"
                  loading="lazy"
                />
                <div className="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center">
                  <Maximize2 className="text-white w-8 h-8" />
                </div>
                {m.file_type === 'video' && (
                  <div className="absolute top-2 right-2 p-1 bg-black/50 backdrop-blur-md rounded-lg">
                    <VideoIcon className="text-white w-4 h-4" />
                  </div>
                )}
              </motion.div>
            ))}
          </div>
        )}
      </main>

      {/* Detail View Modal */}
      <AnimatePresence>
        {selectedMedia && (
          <motion.div 
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/90 backdrop-blur-sm"
          >
            <motion.div 
              layoutId={`card-${selectedMedia.id}`}
              className="relative max-w-4xl w-full max-h-[90vh] glass rounded-3xl overflow-hidden flex flex-col"
            >
              <div className="absolute top-4 right-4 z-10 flex gap-2">
                <button 
                  onClick={() => handleDelete(selectedMedia.id)}
                  className="p-3 bg-red-500/80 hover:bg-red-500 text-white rounded-2xl transition-all"
                >
                  <Trash2 className="w-5 h-5" />
                </button>
                <button 
                  onClick={() => setSelectedMedia(null)}
                  className="p-3 bg-white/10 hover:bg-white/20 text-white rounded-2xl transition-all"
                >
                  <X className="w-5 h-5" />
                </button>
              </div>

              <div className="flex-1 min-h-0 bg-black/40 flex items-center justify-center">
                {selectedMedia.file_type === 'video' ? (
                  <video 
                    src={`${API_BASE}/media/file/${selectedMedia.id}`} 
                    controls 
                    autoPlay
                    className="max-w-full max-h-full"
                  />
                ) : (
                  <img 
                    src={`${API_BASE}/media/file/${selectedMedia.id}`} 
                    className="max-w-full max-h-full object-contain"
                  />
                )}
              </div>

              <div className="p-6">
                <h3 className="text-lg font-bold mb-1 truncate">{selectedMedia.filename}</h3>
                <div className="flex items-center gap-4 text-sm text-white/50">
                  <span>{(selectedMedia.file_size / 1024 / 1024).toFixed(2)} MB</span>
                  {selectedMedia.taken_at && (
                    <span>拍攝於: {new Date(selectedMedia.taken_at).toLocaleString()}</span>
                  )}
                  {selectedMedia.duration && (
                    <span>長度: {Math.round(selectedMedia.duration)}s</span>
                  )}
                </div>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Notification Toast */}
      <AnimatePresence>
        {notification && (
          <motion.div 
            initial={{ y: 50, opacity: 0 }}
            animate={{ y: 0, opacity: 1 }}
            exit={{ y: 50, opacity: 0 }}
            className={`fixed bottom-8 left-1/2 -translate-x-1/2 px-6 py-3 rounded-2xl shadow-2xl z-[60] flex items-center gap-2 ${
              notification.type === 'success' ? 'bg-green-600' : 'bg-red-600'
            }`}
          >
            {notification.type === 'success' ? <Check className="w-4 h-4" /> : <X className="w-4 h-4" />}
            <span className="font-medium">{notification.message}</span>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Footnote */}
      <footer className="mt-auto pt-12 text-white/20 text-xs">
        &copy; 2026 Media Backup Center. 專為您的隱私與速度而生。
      </footer>
    </div>
  );
};

export default App;
