import os
import time
import ast
import hashlib
import platform
import threading
import ctypes

try:
    import pygetwindow as gw
except ImportError:
    gw = None

class ContextEngine:
    def __init__(self, workspace_path=os.getcwd()):
        self.workspace_path = workspace_path
        self.last_active_window = ""
        self.last_modified_file = None
        self.last_error_signature = None 
        
        # Buffer Integration (VS Code / Unsaved Changes)
        self.active_buffer_path = None
        self.active_buffer_content = None
        self.active_buffer_timestamp = 0
        
    def get_active_window_title(self):
        try:
            if gw:
                win = gw.getActiveWindow()
                if win:
                    self.last_active_window = win.title
                    return win.title
            
            # Fallback for Windows
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            self.last_active_window = buf.value
            return buf.value
        except Exception:
            return "Unknown"

    def update_buffer(self, file_path, content):
        """
        updates internal state from external editor (VS Code extension)
        """
        self.active_buffer_path = file_path
        self.active_buffer_content = content
        self.active_buffer_timestamp = time.time()
        print(f"ContextEngine: Buffer updated for {os.path.basename(file_path)}")

    def get_last_modified_file(self, extensions=['.py', '.js', '.ts', '.css', '.html']):
        # If we have a recent buffer update (within last 30 seconds), prefer that
        if self.active_buffer_path and (time.time() - self.active_buffer_timestamp < 30):
             return self.active_buffer_path

        try:
            most_recent_file = None
            most_recent_time = 0
            
            # SEARCH STRATEGY:
            # 1. Current CWD
            # 2. Parent directory (if we are in a subdir like 'cora')
            search_paths = [self.workspace_path]
            parent_dir = os.path.dirname(self.workspace_path)
            if os.path.basename(self.workspace_path) in ['cora', 'src', 'app']:
                search_paths.append(parent_dir)

            for search_path in search_paths:
                for root, dirs, files in os.walk(search_path):
                    # optimized skip
                    if '.git' in dirs: dirs.remove('.git')
                    if 'venv' in dirs: dirs.remove('venv')
                    if '__pycache__' in dirs: dirs.remove('__pycache__')
                    if 'node_modules' in dirs: dirs.remove('node_modules')
                    
                    for file in files:
                        _, ext = os.path.splitext(file)
                        if ext in extensions:
                            full_path = os.path.join(root, file)
                            try:
                                mtime = os.path.getmtime(full_path)
                                if mtime > most_recent_time:
                                    most_recent_time = mtime
                                    most_recent_file = full_path
                            except OSError:
                                continue
            
            self.last_modified_file = most_recent_file
            return most_recent_file
        except Exception as e:
            # print(f"File Scan Error: {e}")
            return None

    def validate_syntax(self, file_path, content=None):
        """
        Generic syntax checker that dispatches to specific parsers based on extension.
        """
        if not file_path: return None
        
        _, ext = os.path.splitext(file_path)
        ext = ext.lower()
        
        if ext == '.py':
            return self.validate_python_syntax(file_path, content)
        # Placeholder for JS/TS/Other parsers
        # elif ext in ['.js', '.ts']:
        #     return self.validate_js_syntax(file_path, content)
            
        return None

    def validate_python_syntax(self, file_path, content=None):
        """
        Parses Python file or content string to detect syntax errors.
        """
        try:
            # Prefer content string if provided, else read file
            if content is None:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
            
            if not content.strip(): 
                return None

            ast.parse(content)
            return None # No errors
            
        except SyntaxError as e:
            return {
                "type": "SyntaxError",
                "message": e.msg,
                "file": file_path,
                "line": e.lineno,
                "text": e.text, # The failing code snippet
                "context": self.get_file_context(file_path, e.lineno, content)
            }
        except Exception as e:
            return None

    def get_file_context(self, path, line_no=0, content=None, context_lines=20):
        """
        Extracts lines around the error from content string or file.
        """
        try:
            if content is None:
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                     lines = f.readlines()
            else:
                 lines = content.splitlines(keepends=True)
            
            if line_no > 0:
                start = max(0, line_no - context_lines // 2 - 1)
                end = min(len(lines), line_no + context_lines // 2)
            else:
                # If no line number (e.g. general context), return head or generic chunks
                start = 0
                end = min(len(lines), context_lines)
            
            return "".join(lines[start:end])
        except Exception:
            return ""

    def generate_error_signature(self, error_data):
        if not error_data: return None
        # Include the code text itself so edits trigger updates
        text_snippet = error_data.get('text', '') or ''
        sig_str = f"{error_data['type']}:{error_data['file']}:{error_data['line']}:{error_data['message']}:{text_snippet.strip()}"
        return hashlib.md5(sig_str.encode()).hexdigest()


    def get_idle_time(self):
        """
        Returns the number of seconds since the last user input (mouse or keyboard).
        """
        try:
            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
                
            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
                return millis / 1000.0
        except:
            pass
        return 0

    def get_context_snapshot(self):
        title = self.get_active_window_title().lower()
        
        # Dual-Mode Classification
        mode_primary = "general"
        mode_secondary = "unknown"
        
        # 1. Developer Mode (Code Editors)
        if any(x in title for x in ["visual studio code", "pycharm", "sublime", "vim", "atom", "spyder", "antigravity", "(persisted)"]):
             mode_primary = "developer"
             mode_secondary = "coding"
        
        # 2. Terminal Mode
        elif any(x in title for x in ["cmd", "powershell", "terminal", "bash", "zsh", "ubuntu", "wsl"]):
             mode_primary = "developer" # Changed to Dev (broad category)
             mode_secondary = "terminal"
             
        # 3. Writing/Productivity Mode
        elif any(x in title for x in ["word", "docs", "writer", "notion", "obsidian", "notes", "notepad", "text editor"]):
             mode_primary = "writing"
             mode_secondary = "writing"
        
        # 4. Email/Communication
        elif any(x in title for x in ["outlook", "gmail", "slack", "discord", "telegram", "mail"]):
             mode_primary = "writing"
             mode_secondary = "email"

        # 5. Reading Mode (PDFs, E-Books)
        elif any(x in title for x in [".pdf", "acrobat", "reader", "epub", "kindle", "mobi", "djvu", "calibre", "foxit"]):
             mode_primary = "reading"
             mode_secondary = "pdf"

        # 6. General Browsing
        elif any(x in title for x in ["chrome", "edge", "firefox", "brave", "safari", "scout", "opera"]):
             mode_primary = "general" 
             mode_secondary = "browser"

        # 7. Chat Mode (Cora App)
        # IMPORTANT: Only match Cora's CHAT window, NOT the suggestion overlay.
        # "Cora Suggestion" must NOT trigger internal mode — it would pause the proactive loop.
        cora_ui_titles = ["cora ai"]
        is_cora_ui = any(t == title or t == title.strip() for t in cora_ui_titles)
        if is_cora_ui or title == "assistant":
             return {
                 "window_title": self.last_active_window,
                 "mode": "internal",
                 "mode_primary": "internal",
                 "mode_secondary": "internal",
                 "file_path": None,
                 "file_content": None,
                 "error": None,
                 "error_signature": None
             }

        snapshot = {
            "window_title": self.last_active_window,
            "mode": mode_primary, # For backward compat
            "mode_primary": mode_primary,
            "mode_secondary": mode_secondary,
            "file_path": None,
            "file_content": None,
            "error": None,
            "error_signature": None
        }

        # --- LOGIC PER MODE ---

        # DEVELOPER: buffer/file based
        if mode_primary == "developer":
            # 1. Try to extract filename from Window Title (VS Code / PyCharm style)
            # e.g. "test.py - Project - Visual Studio Code" or "main.py - Antigravity"
            active_file_candidate = None
            
            # Simple heuristic: Look for substrings ending in common extensions
            parts = title.split(' ')
            # DEBUG
            # print(f"DEBUG Title Parts: {parts}")
            for part in parts:
                # Clean decoration characters (VS Code dirty dot '●', asterisks, brackets)
                clean_part = part.strip(" ●*•[]()")
                
                if any(clean_part.endswith(ext) for ext in ['.py', '.js', '.ts', '.html', '.css', '.java', '.c', '.cpp']):
                    found_candidate = False
                    search_paths = [self.workspace_path]
                    parent_dir = os.path.dirname(self.workspace_path)
                    if os.path.basename(self.workspace_path) in ['cora', 'src', 'app']:
                        search_paths.append(parent_dir)

                    for search_path in search_paths:
                        for root, _, files in os.walk(search_path):
                            if clean_part in files:
                                # Found it!
                                active_file_candidate = os.path.join(root, clean_part)
                                # print(f"DEBUG Found Candidate: {active_file_candidate} in {root}")
                                found_candidate = True
                                break
                        if found_candidate: break
                    
                    if active_file_candidate: break
            
            # 2. If no title match, fallback to Buffer or Last Modified
            last_file = self.active_buffer_path if self.active_buffer_path else (active_file_candidate or self.get_last_modified_file())
            
            if last_file:
                snapshot["file_path"] = last_file
                
                # Determine Content Source
                current_content = None
                
                # If this matches our active buffer, use memory content
                if (self.active_buffer_path and 
                    os.path.normpath(last_file) == os.path.normpath(self.active_buffer_path) and
                    self.active_buffer_content):
                    
                    current_content = self.active_buffer_content
                else:
                    # Fallback to disk read
                    try:
                        with open(last_file, 'r', encoding='utf-8', errors='ignore') as f:
                            current_content = f.read()
                    except:
                        pass
                
                snapshot["file_content"] = current_content

                # PROACTIVE: Check errors (Generic Syntax Validation)
                error = self.validate_syntax(last_file, content=current_content)
                if error:
                    snapshot["error"] = error
                    snapshot["error_signature"] = self.generate_error_signature(error)

        # TERMINAL: file based (fallback)
        elif mode_secondary == "terminal":
             # Try to get the file usage context just in case they are running a file
             snapshot["file_path"] = self.get_last_modified_file()

        return snapshot
