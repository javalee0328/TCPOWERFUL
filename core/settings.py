import json
import os
import sys
import time

def debug_log(msg):
    try:
        # Resolve log path same as settings to ensure visibility
        if getattr(sys, 'frozen', False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.getcwd()
        with open(os.path.join(base, "debug.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - [SETTINGS] {msg}\n")
    except:
        pass

if getattr(sys, 'frozen', False):
    # Running as compiled exe
    BASE_DIR = os.path.dirname(sys.executable)
else:
    # Running as script
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

class SettingsManager:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(SettingsManager, cls).__new__(cls)
            cls._instance.settings = {
                "output_dir": "",
                "vcodec": "h264_nvenc",
                "bitrate": "5000k",
                "watch_folders": [],
                "playback_history": {},
                "source_history": [],
                "output_history": []
            }
            debug_log(f"Initializing SettingsManager. Path: {SETTINGS_FILE}")
            cls._instance.load()
        return cls._instance

    def update_history(self, file_path, position):
        if not file_path: return
        norm_key = os.path.normpath(file_path)
        history = self.settings.get("playback_history", {})
        history[norm_key] = position
        self.settings["playback_history"] = history
        self.settings["playback_history"] = history
        self.save()

    def add_source_history(self, path):
        self._add_to_history("source_history", path)

    def add_output_history(self, path):
        self._add_to_history("output_history", path)

    def _add_to_history(self, key, path):
        if not path: return
        try:
             path = os.path.normpath(path)
             history = self.settings.get(key, [])
             if not isinstance(history, list): history = []
             
             if path in history:
                 history.remove(path)
             history.insert(0, path)
             history = history[:10]
             self.settings[key] = history
             self.save()
        except Exception as e:
             debug_log(f"History update error: {e}")

    def get_history_position(self, file_path):
        if not file_path: return 0
        norm_key = os.path.normpath(file_path)
        history = self.settings.get("playback_history", {})
        return history.get(norm_key, 0)

    def load(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.settings.update(data)
                    debug_log(f"Loaded settings: {len(data)} keys.")
            except Exception as e:
                debug_log(f"Error loading settings: {e}")
        else:
            debug_log("Settings file not found. Using defaults.")

    def save(self):
        try:
            # Optimize: Try saving everything first (Fast Path)
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                try:
                    json.dump(self.settings, f, indent=4, ensure_ascii=False)
                    return # Success
                except TypeError:
                    pass # Fallback to slow path

            # Slow Path: Filter non-serializable data
            clean_settings = {}
            for k, v in self.settings.items():
                try:
                    json.dumps(v)
                    clean_settings[k] = v
                except (TypeError, OverflowError):
                     if isinstance(v, list):
                        clean_list = [x for x in v if self._is_serializable(x)]
                        clean_settings[k] = clean_list
                     elif isinstance(v, dict):
                         # Simple Cleanup (1 level deep)
                         clean_dict = {dk: dv for dk, dv in v.items() if self._is_serializable(dv)}
                         clean_settings[k] = clean_dict
                     else:
                        clean_settings[k] = str(v)

            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(clean_settings, f, indent=4, ensure_ascii=False)
                
        except Exception as e:
            debug_log(f"Error saving settings: {e}")

    def _is_serializable(self, val):
        try:
            json.dumps(val)
            return True
        except:
            return False

    def get(self, key, default=None):
        return self.settings.get(key, default)

    def set(self, key, value):
        self.settings[key] = value
        self.save()
