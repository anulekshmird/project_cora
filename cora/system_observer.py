"""
Layer 1: SYSTEM OBSERVER
Only watches the OS and emits events. No LLM calls. No heavy processing.
"""
import time
import threading
from PyQt6.QtCore import QObject, pyqtSignal
import pygetwindow as gw


class SystemEvent:
    WINDOW_CHANGED  = "WINDOW_CHANGED"
    TEXT_SELECTED   = "TEXT_SELECTED"
    REGION_CAPTURED = "REGION_CAPTURED"
    URL_CHANGED     = "URL_CHANGED"
    FILE_OPENED     = "FILE_OPENED"


class SystemObserver(QObject):
    event_emitted = pyqtSignal(str, dict)  # event_type, event_data

    def __init__(self):
        super().__init__()
        self._running         = False
        self._last_window     = ""
        self._last_url        = ""
        self._thread          = None

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def emit_region(self, x, y, image_bytes, ocr_text):
        """Called externally when user picks a screen region."""
        self.event_emitted.emit(SystemEvent.REGION_CAPTURED, {
            'x':          x,
            'y':          y,
            'image':      image_bytes,
            'ocr_text':   ocr_text,
            'timestamp':  time.time(),
        })

    def _loop(self):
        while self._running:
            try:
                self._check_window()
            except Exception as e:
                print(f"SystemObserver error: {e}")
            time.sleep(0.5)

    def _check_window(self):
        try:
            wins = gw.getActiveWindow()
            title = wins.title.strip() if wins else ""
        except Exception:
            title = ""

        if title and title != self._last_window:
            self._last_window = title
            self.event_emitted.emit(SystemEvent.WINDOW_CHANGED, {
                'window_title': title,
                'timestamp':    time.time(),
            })

    def _check_selected_text(self):
        pass  # Disabled — handled by picker only
