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
    file_path:      Optional[str]  = None
    image:          Optional[bytes]= None
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

        # Classify app and mode
        if any(k in tl for k in ['chrome', 'firefox', 'edge', 'browser']):
            app  = 'browser'
            mode = 'web'
        elif any(k in tl for k in ['word', '.docx', 'document']):
            app  = 'word'
            mode = 'writing'
        elif any(k in tl for k in ['code', 'vscode', 'pycharm', '.py', '.js']):
            app  = 'editor'
            mode = 'coding'
        elif 'claude' in tl:
            app  = 'claude'
            mode = 'ai'
        elif any(k in tl for k in ['youtube', 'video']):
            app  = 'youtube'
            mode = 'video'
        else:
            app  = 'general'
            mode = 'general'

        # Run OCR asynchronously for visible text
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
                
                visible_text = self._ocr_engine(img, title, mode)[:3000]
            except Exception as e:
                print(f"OCR error in extractor: {e}")

        return Context(
            app          = app,
            mode         = mode,
            window_title = title,
            visible_text = visible_text,
            source       = 'window',
            timestamp    = data.get('timestamp', time.time()),
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

    def _from_selection(self, data: dict) -> Context:
        text = data.get('text', '')
        return Context(
            selected_text = text,
            source        = 'selection',
            timestamp     = data.get('timestamp', time.time()),
        )

    def _from_region(self, data: dict) -> Context:
        return Context(
            visible_text = data.get('ocr_text', ''),
            image        = data.get('image'),
            source       = 'region',
            timestamp    = data.get('timestamp', time.time()),
        )
