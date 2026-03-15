import os
import json
import time
import io
import mss
import threading
from PIL import Image
from PyQt6.QtCore import QObject, pyqtSignal

class ObserverSignal(QObject):
    suggestion_ready = pyqtSignal(object)
    prepare_capture  = pyqtSignal()
    finished_capture = pyqtSignal()
    error_resolved   = pyqtSignal()

class Observer:
    def __init__(self):
        self.running = True
        self.signals = ObserverSignal()
        self.chats_dir = os.path.join(os.getcwd(), "chats")
        if not os.path.exists(self.chats_dir):
            os.makedirs(self.chats_dir)

        self.current_session_id = None
        self.chat_history = []
        
        # We still need context_engine for the bridge server
        import context_engine
        self.context_engine = context_engine.ContextEngine()
        
        self.create_new_session()

    def create_new_session(self):
        import uuid
        self.current_session_id = str(uuid.uuid4())[:8]
        self.chat_history = []
        print(f"Created new session: {self.current_session_id}")
        self.save_session()

    def switch_session(self, session_id):
        filepath = os.path.join(self.chats_dir, f"{session_id}.json")
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                    self.chat_history = data.get('history', [])
                self.current_session_id = session_id
                print(f"Switched to session: {session_id}")
                return True
            except Exception as e:
                print(f"Error loading session: {e}")
        return False

    def get_sessions(self):
        sessions = []
        if not os.path.exists(self.chats_dir):
            return []
        for f in os.listdir(self.chats_dir):
            if not f.endswith(".json"):
                continue
            sid = f.replace(".json", "")
            title = f"Chat {sid}"
            try:
                with open(os.path.join(self.chats_dir, f), 'r') as fh:
                    data = json.load(fh)
                    if data.get('title'):
                        title = data['title']
                    else:
                        for msg in data.get('history', []):
                            if msg['role'] == 'user':
                                txt = msg['content'].split("USER:")[-1].strip()[:30]
                                title = txt if txt else title
                                break
            except Exception:
                pass
            sessions.append({'id': sid, 'title': title})
        return sessions

    def delete_session(self, session_id):
        filepath = os.path.join(self.chats_dir, f"{session_id}.json")
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                print(f"Deleted session: {session_id}")
                if self.current_session_id == session_id:
                    self.create_new_session()
                return True
        except Exception as e:
            print(f"Error deleting session: {e}")
        return False

    def save_session(self):
        if not self.current_session_id:
            return
        filepath = os.path.join(self.chats_dir, f"{self.current_session_id}.json")
        try:
            clean_history = [
                {k: v for k, v in msg.items() if k != 'images'}
                for msg in self.chat_history
            ]
            data = {'id': self.current_session_id, 'history': clean_history}
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    existing = json.load(f)
                    if 'title' in existing:
                        data['title'] = existing['title']
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Error saving session: {e}")

    def read_file_content(self, path):
        try:
            if not path:
                return None
            ext = os.path.splitext(path)[1].lower()
            if ext in ('.pdf', '.docx', '.pptx'):
                from ocr_engine import extract_from_file
                return extract_from_file(path)
            valid_exts = [
                '.txt', '.py', '.md', '.json', '.html', '.css', '.js',
                '.csv', '.bat', '.sh', '.xml', '.yaml', '.yml', '.ini', '.log',
            ]
            if ext not in valid_exts:
                return f"[File type '{ext}' not supported. Path: {path}]"
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read(100_000)
        except Exception as e:
            return f"[Error reading file: {e}]"

    def stop(self):
        self.running = False