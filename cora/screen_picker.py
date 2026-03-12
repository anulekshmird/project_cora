import io
import mss
from PIL import Image
from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtCore import Qt, pyqtSignal, QRect
from PyQt6.QtGui import QPainter, QColor, QCursor, QPen, QFont


class ScreenPicker(QWidget):
    region_selected = pyqtSignal(int, int, bytes, str)
    cancelled       = pyqtSignal()

    def __init__(self, observer, parent=None):
        super().__init__(parent)
        self.observer      = observer
        self._start_point  = None
        self._end_point    = None
        self._is_drawing   = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("Cora Picker")
        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 80))
        if self._is_drawing and self._start_point and self._end_point:
            # Convert global to local for painting
            local_start = self.mapFromGlobal(self._start_point)
            local_end   = self.mapFromGlobal(self._end_point)
            rect = QRect(local_start, local_end).normalized()
            painter.setPen(QPen(QColor(59, 130, 246), 2))
            painter.fillRect(rect, QColor(59, 130, 246, 30))
            painter.drawRect(rect)
        painter.setPen(QColor(255, 255, 255, 210))
        font = QFont("Segoe UI", 12)
        painter.setFont(font)
        painter.drawText(
            self.rect(),
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter,
            "\n  🎯 Click an element or drag to select a region  •  Esc to cancel"
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # Use global position for accurate screen coordinates
            pos = event.globalPosition().toPoint()
            self._start_point = pos
            self._end_point   = pos
            self._is_drawing  = True
            self.update()

    def mouseMoveEvent(self, event):
        if self._is_drawing:
            self._end_point = event.globalPosition().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._end_point  = event.globalPosition().toPoint()
            self._is_drawing = False
            self.hide()
            QApplication.processEvents()

            # Get GLOBAL screen position
            x1 = self._start_point.x()
            y1 = self._start_point.y()
            x2 = self._end_point.x()
            y2 = self._end_point.y()

            # Single click → tight region around exact click point
            if abs(x2 - x1) < 10 and abs(y2 - y1) < 10:
                cx, cy = x1, y1
                x1 = max(0, cx - 150)
                y1 = max(0, cy - 30)   # tight vertical — just one line height
                x2 = cx + 150
                y2 = cy + 30
            
            # Small delay so overlay fully hides before capture
            import time
            time.sleep(0.2)
            self._capture_region(x1, y1, x2, y2)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            self.close()

    def _detect_content_type(self, text: str) -> str:
        t = text.strip()
        if not t:
            return "visual"
        words = t.split()

        # Error detection — highest priority
        error_keywords = [
            "error", "exception", "traceback", "failed", "undefined",
            "syntaxerror", "typeerror", "nameerror", "valueerror",
            "cannot", "could not", "no module", "line "
        ]
        if any(k in t.lower() for k in error_keywords):
            return "error"

        # Code detection — before data, because code contains numbers too
        code_keywords = [
            "def ", "class ", "import ", "return ", "function ",
            "const ", "let ", "var ", "=>", "()", "==",
            "!=", "+=", "if ", "for ", "while ", "print(",
            "self.", ".py", "{}",  "#", "//", "/*", "*/",
            "setAttr", "QtCore", "PyQt", "Widget", "Layout",
            "WindowType", "Signal", "pyqtSignal",
        ]
        code_matches = sum(1 for k in code_keywords if k in t)
        if code_matches >= 2:
            return "code"

        # Data/numbers detection
        import re
        number_count = len(re.findall(r'\b\d+\.?\d*\b', t))
        if number_count >= 4 and code_matches == 0:
            return "data"

        # Word, sentence, paragraph
        if len(words) <= 3:
            return "word"
        elif len(words) <= 25:
            return "sentence"
        else:
            return "paragraph"

    def _build_chips(self, content_type: str, text: str) -> list:
        """Return smart chips based on content type."""
        preview = text[:60] + "..." if len(text) > 60 else text
        chips = {
            "word": [
                {"label": "Synonyms",     "hint": f"Give synonyms for: {preview}"},
                {"label": "Define",       "hint": f"Define the word: {preview}"},
                {"label": "Fix Spelling", "hint": f"Check spelling of: {preview}"},
                {"label": "Use in sentence", "hint": f"Use '{preview}' in an example sentence"},
            ],
            "sentence": [
                {"label": "Fix Grammar",    "hint": f"Fix grammar in: {preview}"},
                {"label": "Rewrite",        "hint": f"Rewrite this more clearly: {preview}"},
                {"label": "Check Passive",  "hint": f"Is this passive voice? Fix if so: {preview}"},
                {"label": "Make Formal",    "hint": f"Make this more formal: {preview}"},
            ],
            "paragraph": [
                {"label": "Summarize",      "hint": f"Summarize this paragraph: {preview}"},
                {"label": "Improve",        "hint": f"Improve clarity and flow: {preview}"},
                {"label": "Fix Grammar",    "hint": f"Fix all grammar issues in: {preview}"},
                {"label": "Expand",         "hint": f"Expand this with more detail: {preview}"},
            ],
            "code": [
                {"label": "Explain Code",   "hint": f"Explain what this code does: {preview}"},
                {"label": "Fix Bugs",       "hint": f"Find and fix bugs in: {preview}"},
                {"label": "Optimize",       "hint": f"Suggest optimizations for: {preview}"},
                {"label": "Add Comments",   "hint": f"Add docstrings and comments to: {preview}"},
            ],
            "error": [
                {"label": "Fix Error",      "hint": f"Fix this error: {preview}"},
                {"label": "Explain Cause",  "hint": f"Explain what caused: {preview}"},
                {"label": "Find Solution",  "hint": f"Find the solution to: {preview}"},
            ],
            "data": [
                {"label": "Analyze",        "hint": f"Analyze this data: {preview}"},
                {"label": "Explain",        "hint": f"Explain these numbers: {preview}"},
                {"label": "Summarize",      "hint": f"Summarize key figures in: {preview}"},
            ],
            "visual": [
                {"label": "Describe",       "hint": "Describe what is in this region"},
                {"label": "Explain",        "hint": "Explain what this shows"},
                {"label": "Ask Question",   "hint": "I have a question about this"},
            ],
        }
        return chips.get(content_type, chips["visual"])

    def _capture_region(self, x1, y1, x2, y2):
        try:
            import mss
            from PIL import Image
            import io

            with mss.mss() as sct:
                # Get primary monitor to handle DPI scaling
                monitor = sct.monitors[1]
                region = {
                    "top":    max(0, int(min(y1, y2))),
                    "left":   max(0, int(min(x1, x2))),
                    "width":  max(20, int(abs(x2 - x1))),
                    "height": max(20, int(abs(y2 - y1))),
                }
                # Clamp to monitor bounds
                region["width"]  = min(region["width"],  monitor["width"]  - region["left"])
                region["height"] = min(region["height"], monitor["height"] - region["top"])

                sct_img = sct.grab(region)
                img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

            # Scale up small captures for better OCR
            if img.width < 400:
                scale = 400 // img.width + 1
                img   = img.resize(
                    (img.width * scale, img.height * scale),
                    Image.LANCZOS
                )

            ocr_text = ""
            try:
                from ocr_engine import extract_text_for_window
                ocr_text = extract_text_for_window(
                    image        = img,
                    window_title = "",
                    mode_primary = "general",
                ).strip()[:2000]
            except Exception as e:
                print(f"Picker OCR error: {e}")

            buf = io.BytesIO()
            img.save(buf, format='PNG')
            image_bytes = buf.getvalue()

            content_type = self._detect_content_type(ocr_text)
            print(f"Picker: type={content_type}, OCR={len(ocr_text)}ch: '{ocr_text[:60]}'")
            self.region_selected.emit(int(x1), int(y1), image_bytes, ocr_text)
            self.close()

        except Exception as e:
            print(f"Picker capture error: {e}")
            self.cancelled.emit()
            self.close()
