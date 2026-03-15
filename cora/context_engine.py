import os
import time
import ast
import hashlib
import platform
import threading
import ctypes
import re

try:
    import pygetwindow as gw
except ImportError:
    gw = None


# ---------------------------------------------------------------------------
# Browser / app name suffixes to strip when parsing window titles
# ---------------------------------------------------------------------------
_BROWSER_SUFFIXES = [
    "google chrome", "chromium", "mozilla firefox", "firefox",
    "microsoft edge", "edge", "brave", "opera", "safari", "vivaldi",
]

_SITE_PATTERNS = {
    "youtube":       ("youtube",   "video"),
    "netflix":       ("video",     "video"),
    "twitch":        ("video",     "streaming"),
    "reddit":        ("browser",   "forum"),
    "twitter":       ("browser",   "social"),
    "x.com":         ("browser",   "social"),
    "instagram":     ("browser",   "social"),
    "facebook":      ("browser",   "social"),
    "linkedin":      ("browser",   "professional"),
    "github":        ("developer", "repository"),
    "stackoverflow": ("developer", "forum"),
    "medium":        ("reading",   "article"),
    "wikipedia":     ("reading",   "article"),
    "docs.google":   ("document",  "writing"),
    "notion":        ("document",  "writing"),
    "gmail":         ("writing",   "email"),
    "outlook":       ("writing",   "email"),
    "whatsapp":    ("messaging", "chat"),
    "telegram":    ("messaging", "chat"),
    "discord":     ("messaging", "chat"),
    "slack":       ("messaging", "work"),
    "teams":       ("messaging", "work"),
    "chatgpt":     ("browser",   "ai"),
    "claude":        ("browser",   "ai"),
    "openrouter":    ("browser",   "ai"),
    "perplexity":    ("browser",   "ai"),
    "gemini":        ("browser",   "ai"),
    "huggingface":   ("developer", "ai"),
    "arxiv":         ("reading",   "article"),
    "news":          ("reading",   "article"),
    "bbc":           ("reading",   "news"),
    "cnn":           ("reading",   "news"),
}


def _parse_window_title(raw_title: str) -> dict:
    """
    Parse a raw window title into structured fields.

    Returns:
        {
          "page_title":   "Never Gonna Give You Up",   # content before site/browser
          "site_name":    "YouTube",                    # detected site
          "browser_name": "Google Chrome",              # detected browser
          "app_name":     "Visual Studio Code",         # detected app (non-browser)
          "mode_primary": "video",
          "mode_secondary": "video",
          "raw": "Never Gonna Give You Up - YouTube — Google Chrome"
        }
    """
    result = {
        "page_title":     "",
        "site_name":      "",
        "browser_name":   "",
        "app_name":       "",
        "mode_primary":   "general",
        "mode_secondary": "unknown",
        "raw":            raw_title,
    }

    lower = raw_title.lower()

    # ── Detect browser ────────────────────────────────────────────────
    for b in _BROWSER_SUFFIXES:
        if b in lower:
            # Capitalise nicely
            result["browser_name"] = b.title()
            break

    # ── Split title into parts (separators: " - ", " — ", " | ", " · ") ──
    parts = re.split(r"\s*[-—|·]\s*", raw_title)
    parts = [p.strip() for p in parts if p.strip()]

    # Remove browser name from parts
    if result["browser_name"]:
        parts = [
            p for p in parts
            if p.lower() != result["browser_name"].lower()
            and p.lower() not in _BROWSER_SUFFIXES
        ]

    # ── Detect site / app from parts (right → left, last meaningful part) ──
    detected_site = ""
    for part in reversed(parts):
        part_lower = part.lower()
        for key, (mode_p, mode_s) in _SITE_PATTERNS.items():
            if key in part_lower:
                detected_site             = part
                result["site_name"]       = part
                result["mode_primary"]    = mode_p
                result["mode_secondary"]  = mode_s
                break
        if detected_site:
            break

    # Page title = everything before the site/browser part
    if detected_site and parts:
        page_parts = []
        for p in parts:
            if p == detected_site:
                break
            page_parts.append(p)
        result["page_title"] = " — ".join(page_parts) if page_parts else parts[0]
    elif parts:
        result["page_title"] = parts[0]

    return result


class ContextEngine:
    def __init__(self, workspace_path=os.getcwd()):
        self.workspace_path         = workspace_path
        self.last_active_window     = ""
        self.last_modified_file     = None
        self.last_error_signature   = None

        # Parsed title cache
        self._last_parsed_title: dict = {}

        # Buffer Integration (VS Code / Unsaved Changes)
        self.active_buffer_path      = None
        self.active_buffer_content   = None
        self.active_buffer_timestamp = 0

        # Caching mechanisms (Fix 2 & 3)
        self._snapshot_cache      = None
        self._snapshot_cache_time = 0.0
        self._snapshot_ttl        = 0.3
        
        self._last_file_cache      = None
        self._last_file_cache_time = 0.0

    def get_active_window_title(self):
        try:
            if gw:
                win = gw.getActiveWindow()
                if win:
                    self.last_active_window = win.title
                    return win.title

            hwnd   = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buf    = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            self.last_active_window = buf.value
            return buf.value
        except Exception:
            return "Unknown"

    def update_buffer(self, file_path, content):
        self.active_buffer_path      = file_path
        self.active_buffer_content   = content
        self.active_buffer_timestamp = time.time()
        print(f"ContextEngine: Buffer updated for {os.path.basename(file_path)}")

    def get_last_modified_file(self, extensions=None):
        now = time.time()
        if self._last_file_cache and (now - self._last_file_cache_time) < 5.0:
            return self._last_file_cache

        if extensions is None:
            extensions = ['.py', '.js', '.ts', '.css', '.html']

        if self.active_buffer_path and (time.time() - self.active_buffer_timestamp < 30):
            return self.active_buffer_path

        try:
            most_recent_file = None
            most_recent_time = 0

            search_paths = [self.workspace_path]
            parent_dir   = os.path.dirname(self.workspace_path)
            if os.path.basename(self.workspace_path) in ['cora', 'src', 'app']:
                search_paths.append(parent_dir)

            for search_path in search_paths:
                for root, dirs, files in os.walk(search_path):
                    for skip in ('.git', 'venv', '__pycache__', 'node_modules'):
                        if skip in dirs:
                            dirs.remove(skip)
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
            if most_recent_file:
                print(f"ContextEngine: Last modified: {os.path.basename(most_recent_file)}")
            
            self._last_file_cache = most_recent_file
            self._last_file_cache_time = time.time()
            return most_recent_file
        except Exception:
            return None

    def validate_syntax(self, file_path, content=None):
        if not file_path:
            return None
        _, ext = os.path.splitext(file_path)
        if ext.lower() == '.py':
            return self.validate_python_syntax(file_path, content)
        return None

    def validate_python_syntax(self, file_path, content=None):
        try:
            if content is None:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
            if not content.strip():
                return None
            ast.parse(content)
            return None
        except SyntaxError as e:
            return {
                "type":    "SyntaxError",
                "message": e.msg,
                "file":    file_path,
                "line":    e.lineno,
                "text":    e.text,
                "context": self.get_file_context(file_path, e.lineno, content),
            }
        except Exception:
            return None

    def get_file_context(self, path, line_no=0, content=None, context_lines=20):
        try:
            if content is None:
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
            else:
                lines = content.splitlines(keepends=True)

            if line_no > 0:
                start = max(0, line_no - context_lines // 2 - 1)
                end   = min(len(lines), line_no + context_lines // 2)
            else:
                start = 0
                end   = min(len(lines), context_lines)

            return "".join(lines[start:end])
        except Exception:
            return ""

    def generate_error_signature(self, error_data):
        if not error_data:
            return None
        text_snippet = error_data.get('text', '') or ''
        sig_str = (
            f"{error_data['type']}:{error_data['file']}:"
            f"{error_data['line']}:{error_data['message']}:{text_snippet.strip()}"
        )
        return hashlib.md5(sig_str.encode()).hexdigest()

    def get_idle_time(self):
        try:
            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

            lii        = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                millis = ctypes.windll.kernel32.GetTickCount64() - lii.dwTime
                return millis / 1000.0
        except Exception:
            pass
        return 0

    # ------------------------------------------------------------------
    # Main snapshot  ← rich title parsing added
    # ------------------------------------------------------------------

    def get_selected_text(self) -> str:
        """Only check for selected text when user has been idle 1+ seconds."""
        try:
            idle = self.get_idle_time()
            if idle < 1.0:
                return ""  # user is actively typing/clicking, don't interrupt

            import win32clipboard, win32con, win32api

            # Save current clipboard content
            try:
                win32clipboard.OpenClipboard()
                try:
                    old = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                except Exception:
                    old = ""
                win32clipboard.CloseClipboard()
            except Exception:
                return ""

            # Simulate Ctrl+C
            win32api.keybd_event(0x11, 0, 0, 0)
            win32api.keybd_event(0x43, 0, 0, 0)
            win32api.keybd_event(0x43, 0, 0x0002, 0)
            win32api.keybd_event(0x11, 0, 0x0002, 0)
            time.sleep(0.1)

            try:
                win32clipboard.OpenClipboard()
                try:
                    selected = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                except Exception:
                    selected = ""
                win32clipboard.CloseClipboard()
            except Exception:
                return ""

            if selected and selected != old and len(selected.strip()) > 3:
                print(f"ContextEngine: Selected text detected: '{selected[:60]}'")
                return selected.strip()
            return ""
        except Exception:
            return ""

    def get_context_snapshot(self):
        now = time.time()
        if self._snapshot_cache and (now - self._snapshot_cache_time) < self._snapshot_ttl:
            # Invalidate immediately if window has changed
            cached_title = self._snapshot_cache.get('window_title', '')
            try:
                current_raw = self.get_active_window_title()
                if current_raw != cached_title:
                    pass  # fall through to rebuild snapshot
                else:
                    return self._snapshot_cache
            except Exception:
                return self._snapshot_cache

        raw_title = self.get_active_window_title()
        lower     = raw_title.lower()

        # ── Parse window title into structured info ───────────────────
        parsed               = _parse_window_title(raw_title)
        self._last_parsed_title = parsed

        mode_primary   = "general"
        mode_secondary = "unknown"

        # ── 1. Developer (Code Editors) ───────────────────────────────
        if any(x in lower for x in [
            "visual studio code", "pycharm", "sublime", "vim", "atom",
            "spyder", "antigravity", "(persisted)",
        ]):
            mode_primary   = "developer"
            mode_secondary = "coding"

        # ── 2. Terminal ───────────────────────────────────────────────
        elif any(x in lower for x in [
            "cmd", "powershell", "terminal", "bash", "zsh", "ubuntu", "wsl",
        ]):
            mode_primary   = "developer"
            mode_secondary = "terminal"

        # ── 3. Document editors ───────────────────────────────────────
        elif any(x in lower for x in [
            "word", "writer", "notion", "obsidian", "notes",
            "notepad", "text editor",
        ]):
            # "docs" is handled below under browser (Google Docs)
            mode_primary   = "document"
            mode_secondary = "writing"

        # ── 4. Spreadsheets ───────────────────────────────────────────
        elif any(x in lower for x in ["excel", "sheets", "calc", "spreadsheet"]):
            mode_primary   = "spreadsheet"
            mode_secondary = "data"

        # ── 5. Email / Comms apps (non-browser) ───────────────────────
        elif any(x in lower for x in ["outlook", "slack", "discord", "telegram"]):
            mode_primary   = "writing"
            mode_secondary = "email"

        # ── 6. PDF / Reading apps ─────────────────────────────────────
        elif any(x in lower for x in [
            ".pdf", "acrobat", "epub", "kindle", "mobi",
            "calibre", "foxit", "powerpoint", "keynote", "prezi",
        ]):
            mode_primary   = "reading"
            mode_secondary = (
                "presentation"
                if any(x in lower for x in ["powerpoint", "keynote", "prezi"])
                else "pdf"
            )

        # ── 7. Browser — use parsed site to sub-classify ──────────────
        elif parsed["browser_name"] or any(x in lower for x in [
            "chrome", "firefox", "edge", "brave", "opera", "safari",
            "google chrome", "mozilla firefox",
        ]) or (lower.strip() == "claude"):
            # Inherit rich classification from _parse_window_title if detected
            if parsed["mode_primary"] != "general":
                mode_primary   = parsed["mode_primary"]
                mode_secondary = parsed["mode_secondary"]
            else:
                # Generic browser — still better than "general"
                mode_primary   = "browser"
                mode_secondary = "web"

        # ── 8. Standalone video players ───────────────────────────────
        elif any(x in lower for x in ["vlc", "mpv", "media player"]):
            mode_primary   = "video"
            mode_secondary = "video"

        # ── Internal Cora UI guard ────────────────────────────────────
        cora_ui_titles = ["cora ai"]
        if any(t == lower.strip() for t in cora_ui_titles) or lower.strip() == "assistant":
            return {
                "window_title":   raw_title,
                "mode":           "internal",
                "mode_primary":   "internal",
                "mode_secondary": "internal",
                "page_title":     "",
                "site_name":      "",
                "browser_name":   "",
                "file_path":      None,
                "file_content":   None,
                "error":          None,
                "error_signature": None,
            }

        # ── Build snapshot ────────────────────────────────────────────
        snapshot = {
            "window_title":   raw_title,
            "mode":           mode_primary,
            "mode_primary":   mode_primary,
            "mode_secondary": mode_secondary,

            # NEW: rich title fields for observer / overlay use
            "page_title":   parsed["page_title"],    # e.g. "Never Gonna Give You Up"
            "site_name":    parsed["site_name"],     # e.g. "YouTube"
            "browser_name": parsed["browser_name"],  # e.g. "Google Chrome"

            "file_path":    None,
            "file_content": None,
            "error":        None,
            "error_signature": None,
        }

        # ── Developer: file + syntax check ───────────────────────────
        if mode_primary in ("developer", "document"):
            active_file_candidate = None
            if mode_primary == "developer":
                parts = lower.split(' ')
                for part in parts:
                    clean_part = part.strip(" ●*•[]()")
                    if any(clean_part.endswith(ext) for ext in [
                        '.py', '.js', '.ts', '.html', '.css', '.java', '.c', '.cpp',
                    ]):
                        search_paths = [self.workspace_path]
                        parent_dir   = os.path.dirname(self.workspace_path)
                        if os.path.basename(self.workspace_path) in ['cora', 'src', 'app']:
                            search_paths.append(parent_dir)

                        found = False
                        for search_path in search_paths:
                            for root, _, files in os.walk(search_path):
                                if clean_part in files:
                                    active_file_candidate = os.path.join(root, clean_part)
                                    found = True
                                    break
                            if found:
                                break
                        if active_file_candidate:
                            break

            last_file = (
                self.active_buffer_path or
                active_file_candidate or
                self.get_last_modified_file()
            )

            if last_file:
                snapshot["file_path"] = last_file

                if (
                    self.active_buffer_path
                    and os.path.normpath(last_file) == os.path.normpath(self.active_buffer_path)
                    and self.active_buffer_content
                ):
                    current_content = self.active_buffer_content
                else:
                    try:
                        with open(last_file, 'r', encoding='utf-8', errors='ignore') as f:
                            current_content = f.read()
                    except Exception:
                        current_content = None

                snapshot["file_content"] = current_content
                error = self.validate_syntax(last_file, content=current_content)
                if error:
                    snapshot["error"]           = error
                    snapshot["error_signature"] = self.generate_error_signature(error)

        elif mode_secondary == "terminal":
            snapshot["file_path"] = self.get_last_modified_file()

        # Only check selection in non-developer modes to avoid clipboard interference
        if mode_primary not in ('developer', 'internal'):
            snapshot["selected_text"] = self.get_selected_text()
        else:
            snapshot["selected_text"] = ""

        self._snapshot_cache = snapshot
        self._snapshot_cache_time = time.time()
        return snapshot
