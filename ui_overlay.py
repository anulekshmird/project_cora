import sys
import json
import re
from PyQt6.QtCore import Qt, pyqtSignal, QPropertyAnimation, QPoint, QEasingCurve, QRect, QSize, QTimer
from PyQt6.QtGui import QIcon, QPainter, QColor, QBrush, QPainterPath
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QGraphicsOpacityEffect,
    QLineEdit
)


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builders  (module-level so they are easy to unit-test)
# ─────────────────────────────────────────────────────────────────────────────

def _build_chip_prompt(task: str, screen_ctx: str, reason: str, win_title: str) -> str:
    """
    Build a clean, context-rich prompt for any suggestion chip action.
    Passes the FULL screen_context (not truncated to 300 chars) and
    explicitly forbids the Error/Cause/Fix/Commands structure for
    non-error tasks so the LLM returns natural prose.
    """
    clean_ctx = re.sub(r'\n{3,}', '\n\n', screen_ctx.strip())
    clean_ctx = clean_ctx[:3000]

    return f"""You are Cora, a helpful desktop AI assistant.

TASK: {task}

ACTIVE APPLICATION: {win_title or 'Unknown'}
WHAT CORA NOTICED: {reason}

VISIBLE SCREEN CONTENT:
{clean_ctx if clean_ctx else '(no text captured — respond based on the task above)'}

RESPONSE RULES:
- Respond directly and helpfully in clear prose or bullet points.
- Do NOT use the Error / Cause / Fix / Commands structure unless the
  task is explicitly about fixing a code or terminal error.
- Do NOT output JSON.
- Do NOT add preamble like "Sure!" or "Of course!".
- Keep the response focused and concise."""


def _build_error_prompt(action_type: str, data: dict) -> tuple:
    """
    Build the prompt + display string for error chip actions.
    Strips raw JSON blobs and excessive newlines from error_context
    so the LLM receives clean, readable input.
    Returns (display_text, prompt_text).
    """
    error_file    = data.get('error_file',    'Unknown')
    error_line    = data.get('error_line',    '?')
    error_msg     = data.get('error_message', '') or data.get('reason', '')
    error_context = data.get('error_context', '') or data.get('code', '')

    # Sanitise error_context
    if isinstance(error_context, dict):
        error_context = json.dumps(error_context, indent=2)
    error_context = re.sub(r'\n{4,}', '\n\n', str(error_context).strip())
    error_context = error_context[:2000]

    header = (
        f"FILE:  {error_file}\n"
        f"LINE:  {error_line}\n"
        f"ERROR: {error_msg}\n\n"
        f"CODE:\n{error_context}\n\n"
    )

    if action_type == "fix_error":
        prompt = (
            header +
            "TASK: Explain the fix in one sentence, then provide the fully corrected code.\n\n"
            "Use this exact format:\n\n"
            "⚠ Error\n"
            f"{error_msg}\n\n"
            "Fix\n"
            "Brief explanation here.\n\n"
            "Commands\n"
            "```python\n"
            "# corrected code here\n"
            "```"
        )
        display = "Fixing Syntax Error..."

    elif action_type == "explain_error":
        prompt = (
            header +
            "TASK: Explain what caused this error and why it occurs. "
            "Write in clear prose — no code block needed unless it helps illustrate the point."
        )
        display = "Explaining Error..."

    elif action_type == "show_code":
        prompt = (
            header +
            "TASK: Provide ONLY the corrected code block. No explanation, no prose."
        )
        display = "Showing Corrected Code..."

    else:
        return "", ""

    return display, prompt


# ─────────────────────────────────────────────────────────────────────────────
# ProactiveBubble
# ─────────────────────────────────────────────────────────────────────────────

class ProactiveBubble(QWidget):
    ask_cora_clicked = pyqtSignal(str, str)  # display, prompt
    dismissed        = pyqtSignal()
    snoozed          = pyqtSignal(int)        # minutes

    STATE_IDLE       = "idle"
    STATE_THINKING   = "thinking"
    STATE_ERROR      = "error"
    STATE_SUGGESTION = "suggestion"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Cora Suggestion")

        self.current_data          = None
        self.is_expanded           = False
        self.is_read_more_expanded = False
        self.orb_state             = self.STATE_IDLE

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.fade_out)
        self.is_hovered = False

        self.screen_geo            = QApplication.primaryScreen().availableGeometry()
        self.bubble_size           = 70
        self.panel_width           = 320
        self.panel_height          = 240
        self.panel_expanded_height = 340
        self.margin                = 20

        self.setGeometry(0, 0, 0, 0)

        # ── Main layout ──────────────────────────────────────────────
        self.main_layout = QHBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(15)
        self.main_layout.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom
        )

        # ── Panel ────────────────────────────────────────────────────
        self.panel = QFrame()
        self.panel.setObjectName("panel")
        self.panel.setFixedSize(self.panel_width, self.panel_height)
        self.panel.setStyleSheet("""
            QFrame#panel {
                background-color: rgba(15, 23, 42, 0.95);
                border: 1px solid #334155;
                border-radius: 12px;
            }
            QLabel { color: #cbd5e1; font-family: 'Segoe UI', sans-serif; }
            QLabel#header  { font-weight: bold; font-size: 14px; color: white; }
            QLabel#content { font-size: 13px; line-height: 1.4; color: #cbd5e1; }
        """)

        self.panel_layout = QVBoxLayout(self.panel)
        self.panel_layout.setContentsMargins(20, 15, 20, 15)
        self.panel_layout.setSpacing(8)

        self.header_label = QLabel("Suggestion")
        self.header_label.setObjectName("header")

        self.content_label = QLabel("Content...")
        self.content_label.setObjectName("content")
        self.content_label.setWordWrap(True)
        self.content_label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )

        self.read_more_btn = QPushButton("Read more")
        self.read_more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.read_more_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent; border: none;
                color: #3b82f6; font-size: 11px;
                text-decoration: underline; padding: 2px; text-align: left;
            }
            QPushButton:hover { color: #60a5fa; }
        """)
        self.read_more_btn.clicked.connect(self.toggle_read_more)
        self.read_more_btn.hide()

        self.dynamic_btns_layout = QHBoxLayout()
        self.dynamic_btns_layout.setSpacing(8)

        # Permanent buttons
        self.dismiss_btn = QPushButton("Dismiss")
        self.dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dismiss_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent; border: 1px solid #475569;
                color: #94a3b8; border-radius: 6px;
                padding: 6px 12px; font-size: 12px;
            }
            QPushButton:hover {
                background-color: rgba(255,255,255,0.05);
                color: #cbd5e1; border-color: #64748b;
            }
        """)
        self.dismiss_btn.clicked.connect(self.on_dismiss)

        self.action_btn = QPushButton("Action")
        self.action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action_btn.setStyleSheet("""
            QPushButton {
                background-color: #3b82f6; color: white; border: none;
                border-radius: 6px; padding: 6px 12px;
                font-weight: bold; font-size: 12px;
            }
            QPushButton:hover { background-color: #2563eb; }
        """)
        self.action_btn.clicked.connect(self.on_action)

        self.ask_input = QLineEdit()
        self.ask_input.setPlaceholderText("Ask about this...")
        self.ask_input.setClearButtonEnabled(True)
        self.ask_input.setStyleSheet("""
            QLineEdit {
                background-color: #1e293b; border: 1px solid #334155;
                color: #e2e8f0; border-radius: 8px; padding: 8px 12px;
                font-size: 12px; font-family: 'Segoe UI', sans-serif;
            }
            QLineEdit:focus { border-color: #3b82f6; }
        """)
        self.ask_input.returnPressed.connect(self.on_ask_input_submit)

        self.panel_layout.addWidget(self.header_label)
        self.panel_layout.addWidget(self.content_label)
        self.panel_layout.addWidget(self.read_more_btn)
        self.panel_layout.addLayout(self.dynamic_btns_layout)
        self.panel_layout.addWidget(self.ask_input)

        # ── Orb ─────────────────────────────────────────────────────
        self.bubble_btn = QPushButton()
        self.bubble_btn.setFixedSize(self.bubble_size, self.bubble_size)
        self.bubble_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bubble_btn.clicked.connect(self.toggle_expand)

        self.main_layout.addWidget(self.panel)
        self.main_layout.addWidget(self.bubble_btn)
        self.panel.hide()

        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.opacity_effect)
        self.anim = QPropertyAnimation(self.opacity_effect, b"opacity")
        self.anim.setDuration(300)

        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._pulse_tick)
        self._pulse_phase = 0

    # ── Drag ─────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton:
            if hasattr(self, '_drag_pos') and self._drag_pos:
                self.move(event.globalPosition().toPoint() - self._drag_pos)
                event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def enterEvent(self, event):
        self.is_hovered = True
        if self.hide_timer.isActive():
            self.hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.is_hovered = False
        if self.current_data:
            if self.current_data.get('type') != 'syntax_error':
                self.hide_timer.start(3000)
        super().leaveEvent(event)

    # ── Orb state machine ────────────────────────────────────────────

    def _set_orb_state(self, state):
        self.orb_state = state
        self._pulse_timer.stop()

        if state == self.STATE_IDLE:
            self.bubble_btn.setText("")
            self.bubble_btn.setIcon(QIcon())
            self.bubble_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: rgba(15,23,42,0.7);
                    border: 2px solid rgba(99,133,180,0.4);
                    border-radius: {self.bubble_size//2}px;
                    border-image: url(icon.png) 0 0 0 0 stretch;
                }}
                QPushButton:hover {{
                    background-color: rgba(15,23,42,0.95);
                    border-color: #60a5fa;
                }}
            """)
            self._pulse_timer.start(2000)

        elif state == self.STATE_ERROR:
            self.bubble_btn.setText("⚠️")
            self.bubble_btn.setIcon(QIcon())
            self.bubble_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: #7f1d1d;
                    border: 2px solid #ef4444;
                    border-radius: {self.bubble_size//2}px;
                }}
                QPushButton:hover {{
                    background-color: #991b1b; border-color: #fca5a5;
                }}
            """)
            self._pulse_timer.start(800)

        elif state == self.STATE_THINKING:
            self.bubble_btn.setText("⏳")
            self.bubble_btn.setIcon(QIcon())
            self.bubble_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: #1e293b;
                    border: 2px solid #f59e0b;
                    border-radius: {self.bubble_size//2}px;
                }}
                QPushButton:hover {{
                    background-color: #334155; border-color: #fbbf24;
                }}
            """)
            self._pulse_timer.start(600)

        elif state == self.STATE_SUGGESTION:
            self.bubble_btn.setText("")
            self.bubble_btn.setIcon(QIcon())
            self.bubble_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: #0f172a;
                    border: 2px solid #3b82f6;
                    border-radius: {self.bubble_size//2}px;
                    border-image: url(icon.png) 0 0 0 0 stretch;
                }}
                QPushButton:hover {{ border: 2px solid #60a5fa; }}
            """)

    def _pulse_tick(self):
        try:
            self._pulse_phase = (self._pulse_phase + 1) % 2
            if self.orb_state == self.STATE_IDLE:
                bc = "rgba(99,133,180,0.6)" if self._pulse_phase == 0 else "rgba(99,133,180,0.2)"
                self.bubble_btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: rgba(15,23,42,0.7);
                        border: 2px solid {bc};
                        border-radius: {self.bubble_size//2}px;
                        border-image: url(icon.png) 0 0 0 0 stretch;
                    }}
                    QPushButton:hover {{ border-color: #60a5fa; }}
                """)
            elif self.orb_state == self.STATE_ERROR:
                bg = "#7f1d1d" if self._pulse_phase == 0 else "#991b1b"
                bc = "#ef4444" if self._pulse_phase == 0 else "#fca5a5"
                self.bubble_btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {bg};
                        border: 2px solid {bc};
                        border-radius: {self.bubble_size//2}px;
                    }}
                """)
            elif self.orb_state == self.STATE_THINKING:
                bc = "#f59e0b" if self._pulse_phase == 0 else "#fbbf24"
                self.bubble_btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: #1e293b;
                        border: 2px solid {bc};
                        border-radius: {self.bubble_size//2}px;
                    }}
                """)
        except RuntimeError:
            self._pulse_timer.stop()

    # ── Layout ───────────────────────────────────────────────────────

    def update_layout_pos(self):
        self.screen_geo = QApplication.primaryScreen().availableGeometry()
        total_w = self.bubble_size + self.margin
        current_panel_h = (
            self.panel_expanded_height if self.is_read_more_expanded
            else self.panel_height
        )
        if self.is_expanded:
            total_w += self.panel_width + 15
            self.panel.setFixedSize(self.panel_width, current_panel_h)
        total_h = max(self.bubble_size, current_panel_h) + self.margin
        x = self.screen_geo.x() + self.screen_geo.width()  - total_w
        y = self.screen_geo.y() + self.screen_geo.height() - total_h
        self.setGeometry(x, y, total_w, total_h)

    # ── Show suggestion ──────────────────────────────────────────────

    def show_suggestion(self, data):
        try:
            self._show_suggestion_inner(data)
        except RuntimeError as e:
            print(f"UI Safety: {e}")
        except Exception as e:
            print(f"UI Error in show_suggestion: {e}")

    def _show_suggestion_inner(self, data):
        is_already_visible = self.isVisible() and self.opacity_effect.opacity() > 0.9

        if not is_already_visible:
            self.anim.stop()
            self.opacity_effect.setOpacity(1.0)
            try:
                self.anim.finished.disconnect()
            except Exception:
                pass

        if not isinstance(data, dict):
            data = {"type": "fallback", "reason": str(data), "suggestions": []}
        data.setdefault("type",           "general")
        data.setdefault("reason",         "Suggestion")
        data.setdefault("suggestions",    [])
        data.setdefault("screen_context", "")
        data.setdefault("error_context",  "")

        self.current_data   = data
        suggestion_type     = data.get('type', 'general')
        reason              = data.get('reason', 'No details')
        reason_long         = data.get('reason_long', '')

        if suggestion_type == 'syntax_error' and "Analyzing" not in reason:
            self.is_expanded = True
            self.panel.show()
        elif not is_already_visible:
            self.is_expanded = False
            self.panel.hide()

        self.content_label.setText(reason)
        if reason_long:
            self.read_more_btn.show()
            self.read_more_btn.setText("Read more")
        else:
            self.read_more_btn.hide()

        self.is_read_more_expanded = False
        self.ask_input.clear()

        # ── Header / orb per type ────────────────────────────────────
        TYPE_CONFIG = {
            'syntax_error':           ('⚠️ Syntax Error',      'Fix Error',  self.STATE_ERROR),
            'writing_suggestion':     ('✍️ Writing Tip',        'Improve',    self.STATE_SUGGESTION),
            'reading_suggestion':     ('📖 Reading Assistant',  'Ask',        self.STATE_SUGGESTION),
            'pdf_suggestion':         ('📄 PDF Assistant',      'Summarize',  self.STATE_SUGGESTION),
            'spreadsheet_suggestion': ('📊 Spreadsheet',        'Analyze',    self.STATE_SUGGESTION),
            'youtube_suggestion':     ('▶️ Video Assistant',    'Explain',    self.STATE_SUGGESTION),
            'browser_suggestion':     ('🌐 Browser Assistant',  'Summarize',  self.STATE_SUGGESTION),
            'developer_suggestion':   ('💻 Code Assistant',     'Analyze',    self.STATE_SUGGESTION),
            'presentation_suggestion': ('📊 Presentation',      'Improve', self.STATE_SUGGESTION),
            'ai_suggestion':           ('🤖 AI Tool Assistant', 'Help',    self.STATE_SUGGESTION),
        }
        header_text, action_text, orb_state = TYPE_CONFIG.get(
            suggestion_type, ('✨ Cora Suggestion', 'View', self.STATE_SUGGESTION)
        )
        self.header_label.setText(header_text)
        self.action_btn.setText(action_text)

        if suggestion_type == 'syntax_error':
            if "Analyzing" in reason or "Fetching" in reason:
                self._set_orb_state(self.STATE_THINKING)
            else:
                self._set_orb_state(self.STATE_ERROR)
        elif suggestion_type == 'writing_suggestion':
            self._set_orb_state(self.STATE_SUGGESTION)
            self.bubble_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: #4c1d95; border: 2px solid #8b5cf6;
                    border-radius: {self.bubble_size//2}px;
                }}
                QPushButton:hover {{
                    background-color: #5b21b6; border-color: #a78bfa;
                }}
            """)
            self.bubble_btn.setText("✍️")
            self.bubble_btn.setIcon(QIcon())
        else:
            self._set_orb_state(orb_state)

        # ── Dynamic chips ────────────────────────────────────────────
        for btn in (self.dismiss_btn, self.action_btn):
            if btn.parent():
                btn.setParent(None)

        while self.dynamic_btns_layout.count():
            item = self.dynamic_btns_layout.takeAt(0)
            w = item.widget()
            if w:
                try:
                    if w.parent():
                        w.deleteLater()
                except RuntimeError:
                    pass

        if suggestion_type == 'syntax_error':
            self._add_chip_buttons([
                {"label": "Fix Error", "action": "fix_error"},
                {"label": "Explain",   "action": "explain_error"},
                {"label": "Show Code", "action": "show_code"},
            ])
        else:
            suggestions = data.get('suggestions', [])
            if not suggestions:
                FALLBACKS = {
                    'writing_suggestion':     [
                        {"label": "Summarize",       "hint": "Summarize this content"},
                        {"label": "Fix Grammar",     "hint": "Fix grammar issues"},
                        {"label": "Improve Clarity", "hint": "Improve text clarity"},
                    ],
                    'reading_suggestion':     [
                        {"label": "Summarize Page",   "hint": "Summarize the visible page"},
                        {"label": "Explain Concepts", "hint": "Explain key concepts on this page"},
                        {"label": "Key Points",       "hint": "Extract key points as bullets"},
                    ],
                    'pdf_suggestion':         [
                        {"label": "Summarize Page", "hint": "Summarize the visible PDF page"},
                        {"label": "Key Points",     "hint": "Extract key points"},
                        {"label": "Explain Terms",  "hint": "Explain technical terms"},
                    ],
                    'spreadsheet_suggestion': [
                        {"label": "Explain Formula", "hint": "Explain visible formulas"},
                        {"label": "Analyze Data",    "hint": "Find patterns in visible data"},
                    ],
                    'youtube_suggestion':     [
                        {"label": "Explain Topic", "hint": "Explain the video topic from title"},
                        {"label": "Key Points",    "hint": "Extract visible subtitle points"},
                    ],
                    'browser_suggestion':     [
                        {"label": "Summarize", "hint": "Summarize this page"},
                        {"label": "Key Ideas", "hint": "Extract key ideas from this page"},
                    ],
                    'developer_suggestion':   [
                        {"label": "Explain Code", "hint": "Explain the visible code"},
                        {"label": "Find Issues",  "hint": "Identify potential issues in the code"},
                    ],
                    'presentation_suggestion': [
                        {"label": "Improve Slide",  "hint": "Improve the current slide content"},
                        {"label": "Speaker Notes",  "hint": "Write speaker notes for this slide"},
                        {"label": "Summarize Deck", "hint": "Summarize the presentation so far"},
                    ],
                    'ai_suggestion': [
                        {"label": "Improve Prompt",  "hint": "Help me write a better prompt"},
                        {"label": "Follow-up Ideas", "hint": "Suggest follow-up questions"},
                        {"label": "Summarize Chat",  "hint": "Summarize the current conversation"},
                    ],
                }
                suggestions = FALLBACKS.get(suggestion_type, [
                    {"label": "Explain",   "hint": "Explain the visible content"},
                    {"label": "Summarize", "hint": "Summarize what is on screen"},
                ])
            self._add_suggestion_chips(suggestions)

        self.update_layout_pos()
        self.show()
        self.raise_()
        QApplication.processEvents()

        if not is_already_visible:
            self.opacity_effect.setOpacity(0)
            self.anim.setStartValue(0.0)
            self.anim.setEndValue(1.0)
            self.anim.start()

        if suggestion_type == 'syntax_error':
            self.hide_timer.stop()
        elif suggestion_type == 'message':
            self.hide_timer.start(5000)
        else:
            self.hide_timer.start(10000)

    # ── Chip builders ────────────────────────────────────────────────

    def _make_chip(self, label, tooltip=""):
        btn = QPushButton(label)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(tooltip)
        btn.setStyleSheet("""
            QPushButton {
                background-color: #0f172a; border: 1px solid #3b82f6;
                color: #60a5fa; border-radius: 6px;
                padding: 5px 10px; font-size: 11px;
            }
            QPushButton:hover { background-color: #1e3a8a; color: white; }
        """)
        return btn

    def _add_chip_buttons(self, actions):
        for info in actions:
            btn = self._make_chip(info["label"])
            def make_cb(at):
                return lambda: self._handle_error_chip(at)
            btn.clicked.connect(make_cb(info["action"]))
            self.dynamic_btns_layout.addWidget(btn)
        self.dynamic_btns_layout.addWidget(self.dismiss_btn)
        self.dismiss_btn.show()

    def _add_suggestion_chips(self, suggestions):
        for sugg in suggestions:
            label = sugg.get('label', 'Action')
            hint  = sugg.get('hint', label)
            btn   = self._make_chip(label, hint)
            def make_cb(h):
                return lambda: self.trigger_reading_action(h)
            btn.clicked.connect(make_cb(hint))
            self.dynamic_btns_layout.addWidget(btn)
        self.dynamic_btns_layout.addWidget(self.dismiss_btn)
        self.dismiss_btn.show()

    # ── Error chip handler ───────────────────────────────────────────

    def _handle_error_chip(self, action_type):
        if not self.current_data:
            return
        display, prompt = _build_error_prompt(action_type, self.current_data)
        if not prompt:
            return
        self.ask_cora_clicked.emit(display, prompt)
        self.hide_bubble()

    # ── Panel interaction ────────────────────────────────────────────

    def show_message(self, title, message):
        self._show_suggestion_inner({'reason': message, 'type': 'message'})
        self.header_label.setText(title)

    def toggle_expand(self):
        if not hasattr(self, 'current_data') or self.current_data is None:
            self._show_suggestion_inner({
                "type":        "general",
                "reason":      "I'm observing your activity. Ask me anything below!",
                "suggestions": [],
            })
            self.header_label.setText("Cora Assistant")
            self.is_expanded = True
            self.panel.show()
            self.update_layout_pos()
            return

        self.is_expanded = not self.is_expanded
        if self.is_expanded:
            self.panel.show()
        else:
            self.panel.hide()
            self.is_read_more_expanded = False
        self.update_layout_pos()

    def toggle_read_more(self):
        if not self.current_data:
            return
        self.is_read_more_expanded = not self.is_read_more_expanded
        if self.is_read_more_expanded:
            full = (
                f"{self.current_data.get('reason','')}\n\n"
                f"{self.current_data.get('reason_long','')}"
            )
            self.content_label.setText(full)
            self.read_more_btn.setText("Show less")
        else:
            self.content_label.setText(self.current_data.get('reason', ''))
            self.read_more_btn.setText("Read more")
        self.update_layout_pos()

    def on_dismiss(self):
        self.dismissed.emit()
        self.hide_timer.stop()
        self.fade_out(force=True)

    def enter_idle_mode(self):
        self.anim.stop()
        self.opacity_effect.setOpacity(1.0)
        self.is_expanded = False
        self.panel.hide()
        self._set_orb_state(self.STATE_IDLE)
        self.update_layout_pos()
        self.show()

    def hide_bubble(self):
        self.fade_out(force=True)

    def fade_out(self, force=False):
        if self.is_hovered and not force:
            self.hide_timer.start(2000)
            return
        self.anim.stop()
        self.anim.setStartValue(self.opacity_effect.opacity())
        self.anim.setEndValue(0.0)
        self.anim.finished.connect(self._on_fade_finished)
        self.anim.start()

    def _on_fade_finished(self):
        try:
            self.anim.finished.disconnect(self._on_fade_finished)
        except Exception:
            pass
        self.enter_idle_mode()

    # ── Action handlers ──────────────────────────────────────────────

    def on_action(self):
        if not self.current_data:
            return

        suggestion_type = self.current_data.get('type', 'general')

        if suggestion_type == 'syntax_error':
            self._handle_error_chip("fix_error")
            return

        reason     = self.current_data.get('reason', '')
        screen_ctx = self.current_data.get('screen_context', '')
        win_title  = self.current_data.get('window_title', '')

        if suggestion_type == 'writing_suggestion':
            task    = f"Improve the writing: {reason}"
            display = "Improving Writing..."
        else:
            task    = f"Explain or summarize: {reason}"
            display = "Analyzing..."

        prompt = _build_chip_prompt(task, screen_ctx, reason, win_title)
        self.ask_cora_clicked.emit(display, prompt)
        self.hide_bubble()

    def on_ask_input_submit(self):
        text = self.ask_input.text().strip()
        if not text:
            return

        screen_ctx = win_title = reason = ""
        if self.current_data:
            screen_ctx = (
                self.current_data.get('screen_context', '') or
                self.current_data.get('error_context', '')
            )
            reason    = self.current_data.get('reason', '')
            win_title = self.current_data.get('window_title', '')

        prompt  = _build_chip_prompt(text, screen_ctx, reason, win_title)
        display = text[:50] + "…" if len(text) > 50 else text

        self.ask_cora_clicked.emit(display, prompt)
        self.ask_input.clear()
        self.hide_bubble()

    def trigger_reading_action(self, hint: str):
        """
        Called when any non-error suggestion chip is clicked.
        Uses _build_chip_prompt() with FULL screen_context
        and explicit no-JSON / no-Error-format instructions.
        """
        if not self.current_data:
            return

        screen_ctx = self.current_data.get('screen_context', '')
        reason     = self.current_data.get('reason', '')
        win_title  = self.current_data.get('window_title', '')

        print(f"Chip: '{hint}' | screen_ctx={len(screen_ctx)} chars")

        prompt  = _build_chip_prompt(hint, screen_ctx, reason, win_title)
        display = f"{hint}…"

        self.ask_cora_clicked.emit(display, prompt)
        self.hide_bubble()