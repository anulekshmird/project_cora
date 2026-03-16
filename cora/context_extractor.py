"""
Layer 2: CONTEXT EXTRACTOR
Converts raw OS events into structured Context objects.
Runs in background thread. No LLM calls.
"""
import time
import threading
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Context:
    app:            str            = "unknown"
    mode:           str            = "general"
    window_title:   str            = ""
    selected_text:  str            = ""
    visible_text:   str            = ""
    url:            Optional[str]  = None
    page_title:     Optional[str]  = None
    file_path:      Optional[str]  = None
    image:          Optional[bytes]= None
    activity:       str            = "general_browsing"
    needs:          list           = field(default_factory=list)
    source:         str            = "window"  # window|selection|region|ocr
    timestamp:      float          = field(default_factory=time.time)

    def is_empty(self) -> bool:
        return not any([
            self.selected_text,
            self.visible_text,
            self.url,
            self.file_path,
        ])

    def best_text(self) -> str:
        """Returns highest-priority text content."""
        return (
            self.selected_text or
            self.visible_text  or
            self.url           or
            self.window_title  or
            ""
        )

    def identity_hash(self) -> int:
        """Create a unique hash representing the visual 'state' of this context."""
        # Include window title to prevent collisions between different apps with same/empty text
        return hash((self.app, self.window_title, self.best_text().strip(), self.activity))


class ContextExtractor:
    """Converts raw event data into Context objects asynchronously."""

    def __init__(self, ocr_engine=None):
        self._ocr_engine = ocr_engine
        self._executor   = None

    def extract_async(self, event_type: str, event_data: dict,
                      callback) -> None:
        """Extract context in background thread, call callback with Context."""
        thread = threading.Thread(
            target=self._extract,
            args=(event_type, event_data, callback),
            daemon=True,
        )
        thread.start()

    def _extract(self, event_type: str, event_data: dict, callback) -> None:
        try:
            ctx = self._build_context(event_type, event_data)
            callback(ctx)
        except Exception as e:
            print(f"ContextExtractor error: {e}")

    def _build_context(self, event_type: str, event_data: dict) -> Context:
        from system_observer import SystemEvent

        if event_type == SystemEvent.WINDOW_CHANGED:
            return self._from_window(event_data)
        elif event_type == SystemEvent.TEXT_SELECTED:
            return self._from_selection(event_data)
        elif event_type == SystemEvent.REGION_CAPTURED:
            return self._from_region(event_data)
        else:
            return Context()

    def _from_window(self, data: dict) -> Context:
        title = data.get('window_title', '')
        tl    = title.lower()
        use_window_only = data.get('use_window_capture', False)

        is_claude = 'claude' in tl

        # Skip OCR entirely for Claude/AI windows
        if is_claude:
            return Context(
                app          = 'claude',
                mode         = 'ai',
                window_title = title,
                visible_text = '',
                source       = 'window',
                timestamp    = data.get('timestamp', time.time()),
            )

    def _classify_and_enrich(self, title: str) -> dict:
        tl = title.lower()
        # Classify app and mode
        if any(k in tl for k in ['youtube', 'video', 'watch']):
            app, mode = 'youtube', 'video'
        elif any(k in tl for k in ['chrome', 'firefox', 'edge', 'browser']):
            app, mode = 'browser', 'web'
        elif any(k in tl for k in ['word', '.docx', 'document']):
            app, mode = 'word', 'writing'
        elif any(k in tl for k in ['notepad', 'sublime', 'text editor', 'edit']):
             app, mode = 'text_editor', 'coding'
        elif any(k in tl for k in ['code', 'vscode', 'pycharm', 'intellij', 'studio', '.py', '.js', '.ts', '.html', '.css', '.go', '.rs', '.txt', '.md', '.json', '.yaml', '.yml', '.requirements']):
            app, mode = 'editor', 'coding'
        elif 'claude' in tl:
            app, mode = 'claude', 'ai'
        elif 'terminal' in tl or 'powershell' in tl or 'cmd.exe' in tl:
            app, mode = 'terminal', 'terminal'
        elif any(k in tl for k in ['cora suggestion', 'cora ai', 'cora picker']):
            app, mode = 'idle', 'general'
        elif not title or any(k in tl for k in ['taskbar', 'system tray', 'desktop', 'program manager']):
            app, mode = 'idle', 'general'
        else:
            app, mode = 'general', 'general'

        file_path = None
        url = None
        page_title = title
        
        import re
        
        # Refined parsing for better specificity
        # TRY FILENAME EXTRACTION FOR ALL APPS (especially editors/writing)
        if app in ('editor', 'text_editor', 'word', 'general'):
            # More robust regex: look for words with extensions. 
            # Avoid picking up full sentences by limiting length and character set.
            # Example: main.py, setup.py, document.docx, README.md, script.js
            match = re.search(r'([a-zA-Z0-9_\-\.\+]+\.[a-zA-Z0-9]{1,10})', title)
            if match:
                file_path = match.group(1)
                page_title = file_path
        
        if app == 'youtube':
            # Titles usually: "Video Name - YouTube" or "YouTube - Video Name"
            parts = re.split(r'\s*[-—|]\s*', title)
            if len(parts) > 1:
                if 'youtube' in parts[-1].lower():
                    page_title = " — ".join(parts[:-1])
                elif 'youtube' in parts[0].lower():
                    page_title = " — ".join(parts[1:])
        
        elif app == 'browser':
            # Try to catch domain
            match = re.search(r'([a-z0-9]+([\-.][a-z0-9]+)*\.[a-z]{2,5})', tl)
            if match:
                url = match.group(1)
            
            # Extract specific content from title
            parts = re.split(r'\s*[-—|]\s*', title)
            if len(parts) > 1:
                # Common pattern: Page Title - Site Name - Browser
                # or: Search Query - Google Search
                site_found = False
                for i, part in enumerate(reversed(parts)):
                    pl = part.lower()
                    if any(b in pl for b in ['chrome', 'firefox', 'edge', 'google search', 'new tab']):
                        continue
                    if not site_found:
                        site_found = True
                        # This might be the site name, skip it for page title if there are more parts
                        if i < len(parts) - 1:
                            continue
                    
                # Clean up: remove "Google Search" etc.
                clean_parts = [p for p in parts if not any(x in p.lower() for x in ['google search', 'chrome', 'edge', 'firefox', 'new tab'])]
                if clean_parts:
                    page_title = " — ".join(clean_parts)

        return {
            "app": app,
            "mode": mode,
            "file_path": file_path,
            "url": url,
            "page_title": page_title
        }

    def _from_window(self, data: dict) -> Context:
        title = data.get('window_title', '')
        use_window_only = data.get('use_window_capture', False)

        enrich = self._classify_and_enrich(title)
        app = enrich['app']
        mode = enrich['mode']
        
        # Skip OCR entirely for Claude/AI windows
        if app == 'claude':
            return Context(
                app          = 'claude',
                mode         = 'ai',
                window_title = title,
                source       = 'window',
                timestamp    = data.get('timestamp', time.time()),
            )

        # Run OCR asynchronously for visible_text
        visible_text = ""
        if self._ocr_engine and app not in ('claude',):
            try:
                img = None
                if use_window_only:
                    img = ContextHelpers.capture_active_window_image()
                
                if not img:
                    import mss
                    from PIL import Image
                    with mss.mss() as sct:
                        monitor = sct.monitors[1]
                        sct_img = sct.grab(monitor)
                        img = Image.frombytes(
                            "RGB", sct_img.size, sct_img.bgra, "raw", "BGRX"
                        )
                
                visible_text = self._ocr_engine(
                    image=img, 
                    window_title=title, 
                    mode_primary=mode
                )[:3000]
                
                # Store image bytes for Gemini
                import io
                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format='PNG')
                image_bytes = img_byte_arr.getvalue()
                
            except Exception as e:
                print(f"OCR error in extractor: {e}")
                image_bytes = None
        else:
            image_bytes = None

        # Activity and needs inference
        page_title = title.split(' - ')[0] if ' - ' in title else title
        ctx_temp = {
            "app": app,
            "window_title": title,
            "visible_text": visible_text,
            "page_title": page_title
        }
        activity = self.infer_user_activity(ctx_temp)
        needs = self.get_likely_needs(activity)

        return Context(
            app          = app,
            mode         = mode,
            window_title = title,
            visible_text = visible_text,
            page_title   = enrich['page_title'],
            activity     = activity,
            needs        = needs,
            url          = enrich['url'],
            file_path    = enrich['file_path'],
            image        = image_bytes,
            source       = 'window',
            timestamp    = data.get('timestamp', time.time()),
        )

    def infer_user_activity(self, context: dict) -> str:
        text = (context.get("visible_text") or "").lower()
        title = (context.get("window_title") or "").lower()
        app = (context.get("app") or "").lower()
        page_title = (context.get("page_title") or "").lower()

        if not title or any(k in title for k in ['taskbar', 'system tray', 'desktop', 'program manager']):
            return "idle"
        
        # Specific search detection
        if "google search" in title or "search?" in (context.get("url") or ""):
            return "searching_topic"
            
        if app == "youtube" or "watch" in title:
            return "watching_video"
        if "github" in text or "github" in title:
            return "browsing_repo"
        if any(k in text for k in ["traceback (most recent call last):", "syntaxerror:", "runtimeerror:", "attributeerror:"]):
            return "debugging_error"
        if app == "editor" or any(k in title for k in [".py", ".js", ".ts", ".html", ".css", ".txt", ".md", ".json", "code", "editor", "pycharm"]):
            return "coding"
        if any(k in text for k in ["what is", "overview", "learn", "how to"]):
            return "reading_article"
        if any(k in title for k in ["whatsapp", "telegram", "discord", "slack"]):
            return "chatting"
        if app == "word" or any(x in title for x in ["word", ".docx", "document"]):
            return "writing_document"
        if ".pdf" in title:
            return "reading_pdf"
        
        # Fallback to more descriptive "exploring" if we have some text
        if len(text.strip()) > 50:
            return "exploring_content"

        return "general_browsing"

    def get_likely_needs(self, activity: str) -> list:
        NEEDS_MAP = {
            "reading_article": [
                "explain_topic",
                "summarize_content",
                "extract_key_points",
                "create_notes"
            ],
            "debugging_error": [
                "fix_error",
                "explain_error",
                "suggest_commands",
                "show_corrected_code"
            ],
            "watching_video": [
                "summarize_video",
                "extract_learning_points",
                "explain_topic",
                "create_notes"
            ],
            "chatting": [
                "draft_reply",
                "rewrite_message",
                "summarize_chat",
                "extract_action_items"
            ],
            "browsing_repo": [
                "explain_repo",
                "list_key_files",
                "show_architecture",
                "summarize_project"
            ],
            "coding": [
                "review_code",
                "explain_logic",
                "optimize_performance",
                "write_unit_tests"
            ],
            "writing_document": [
                "improve_grammar",
                "rewrite_for_clarity",
                "summarize_section",
                "suggest_heading"
            ],
            "reading_pdf": [
                "summarize_pdf",
                "explain_concepts",
                "extract_data",
                "translate_text"
            ],
            "idle": [
                "Suggest actions",
                "Check reminders",
                "Explain Cora",
                "Need any help?"
            ]
        }
        return NEEDS_MAP.get(activity, ["general_assistance", "answer_question"])

    def _from_selection(self, data: dict) -> Context:
        text = data.get('text', '')
        return Context(
            selected_text = text,
            source        = 'selection',
            timestamp     = data.get('timestamp', time.time()),
        )

    def _from_region(self, data: dict) -> Context:
        import pygetwindow as gw
        title = ""
        try:
            win = gw.getActiveWindow()
            title = win.title.strip() if win else ""
        except Exception:
            pass
        
        enrich = self._classify_and_enrich(title)
        
        return Context(
            app           = enrich['app'],
            mode          = enrich['mode'],
            window_title  = title,
            page_title    = enrich['page_title'],
            file_path     = enrich['file_path'],
            url           = enrich['url'],
            selected_text = data.get('ocr_text', ''),
            image         = data.get('image'),
            source        = 'region',
            timestamp     = data.get('timestamp', time.time()),
        )


class ContextHelpers:
    """Helper utilities for context extraction."""

    @staticmethod
    def capture_active_window_image():
        """Capture only the currently active window using pygetwindow and mss."""
        import pygetwindow as gw
        import mss
        from PIL import Image
        import time

        try:
            win = gw.getActiveWindow()
            if not win or win.isMinimized:
                return None
            
            # Basic sanity on coordinates
            if win.width < 10 or win.height < 10:
                return None

            with mss.mss() as sct:
                # mss uses screen coordinates. pygetwindow uses screen coordinates on Windows.
                # However, multi-monitor setups might need offset adjustment.
                # For CORA we typically focus on primary monitor (monitor 1).
                
                region = {
                    'top':    win.top,
                    'left':   win.left,
                    'width':  win.width,
                    'height': win.height
                }
                
                # Check for negative coords (multi-monitor)
                if region['top'] < -5000 or region['left'] < -5000:
                    return None

                sct_img = sct.grab(region)
                return Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        except Exception as e:
            print(f"[ContextHelpers] Capture error: {e}")
            return None
