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

# ── Global configuration ───────────────────────────────────────────────────

# Labels (moved inside ProactiveBubble for scope reliability)


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builders
# ─────────────────────────────────────────────────────────────────────────────

def _build_chip_prompt(task: str, screen_ctx: str, reason: str, win_title: str,
                       page_title: str = "", site_name: str = "", selected_text: str = "") -> str:
    clean_ctx = re.sub(r'\n{3,}', '\n\n', screen_ctx.strip())
    clean_ctx = clean_ctx[:3000]

    context_label = page_title or site_name or win_title or "Unknown"

    selected_section = (
        f"\nUSER-SELECTED TEXT (user highlighted this — highest priority):\n"
        f"{'='*50}\n{selected_text}\n{'='*50}\n"
        if selected_text else ""
    )

    return f"""You are Cora, a helpful desktop AI assistant.

TASK: {task}

ACTIVE CONTENT: {context_label}
ACTIVE APPLICATION: {win_title or 'Unknown'}
WHAT CORA NOTICED: {reason}
{selected_section}
SCREEN TEXT (OCR extracted — use this as ground truth):
{clean_ctx if clean_ctx else '(no text captured — respond based on task and active content above)'}

RESPONSE RULES:
- You DO have access to the screen via OCR text above and the image provided.
- NEVER say "I don't have access to your screen" or "I cannot see your screen".
- If screen text is present, base your response on it directly.
- If no screen text, use the ACTIVE CONTENT label to answer.
- Respond directly and helpfully in clear prose or bullet points.
- Do NOT use Error / Cause / Fix / Commands structure unless the task
  is explicitly about fixing a code or terminal error.
- Do NOT output JSON.
- Do NOT add preamble like "Sure!" or "Of course!".
- Keep the response focused and concise."""


def _build_error_prompt(action_type: str, data: dict) -> tuple:
    error_file    = data.get('error_file',    'Unknown')
    error_line    = data.get('error_line',    '?')
    error_msg     = data.get('error_message', '') or data.get('reason', '')
    error_context = data.get('error_context', '') or data.get('code', '')

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
            "Use this exact format — write the actual code, never use placeholders:\n\n"
            "⚠ Error\n"
            f"{error_msg}\n\n"
            "Fix\n"
            "Brief explanation here.\n\n"
            "Commands\n"
            "```python\n"
            "# write the actual corrected code here — never write CODE_BLOCK or placeholders\n"
            "```"
        )
        display = "Fixing Syntax Error..."

    elif action_type == "explain_error":
        prompt = (
            header +
            "TASK: Explain what caused this error and why it occurs. "
            "Write in clear prose — no code block needed unless it helps."
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
    ask_cora_clicked = pyqtSignal(str, str)
    dismissed        = pyqtSignal()
    snoozed          = pyqtSignal(int)
    pick_requested   = pyqtSignal()

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
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("Cora Suggestion")

        self.current_data          = None
        self.is_expanded           = False
        self.is_read_more_expanded = False
        self.orb_state             = self.STATE_IDLE
        self._pending_data         = None
        self.user_bubble_pos       = None # To store dragged position

        # Labels for UI display
        self.APP_LABELS = {
            "editor":  "VS Code",
            "browser": "Web Browser",
            "youtube": "YouTube",
            "word":    "Microsoft Word",
            "claude":  "Claude AI",
            "idle":    "Desktop",
        }
        self.LABELS = {
            "coding":           "Coding",
            "debugging_error":  "Debugging Error",
            "watching_video":   "Watching Video",
            "reading_article":  "Reading Article",
            "reading_pdf":      "Reading PDF",
            "writing_document": "Writing",
            "chatting":         "Chatting",
            "browsing_repo":    "Browsing Repo",
            "searching_topic":  "Searching",
            "general_browsing": "Browsing",
            "idle":             "Need any help?",
        }

        # No auto-dismiss — bubble stays until user clicks Dismiss or context changes
        self._dismiss_timer = QTimer(self)
        self.is_hovered = False

        self.screen_geo            = QApplication.primaryScreen().availableGeometry()
        self.bubble_size           = 70
        self.panel_width           = 340
        self.panel_height          = 270
        self.panel_expanded_height = 370
        self.margin                = 20

        self.setGeometry(0, 0, 0, 0)

        self.main_layout = QHBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(15)
        self.main_layout.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom
        )

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
        self.panel.setMinimumWidth(320)
        self.panel.setMaximumWidth(380)

        self.panel_layout = QVBoxLayout(self.panel)
        self.panel_layout.setContentsMargins(20, 15, 20, 15)
        self.panel_layout.setSpacing(8)

        self.status_label = QLabel("")
        self.status_label.setObjectName("status")
        self.status_label.setStyleSheet("""
            QLabel#status {
                font-size: 11px;
                color: #60a5fa;
                font-weight: bold;
                border-bottom: 1px solid #1e293b;
                padding-bottom: 4px;
                margin-bottom: 4px;
            }
        """)
        self.status_label.setWordWrap(True)

        self.header_label = QLabel("Suggestion")
        self.header_label.setObjectName("header")

        self.content_label = QLabel("Content...")
        self.content_label.setObjectName("content")
        self.content_label.setWordWrap(True)
        self.content_label.setTextFormat(Qt.TextFormat.RichText)
        self.content_label.setMaximumWidth(340)
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

        self.dynamic_btns_layout = QVBoxLayout()
        self.dynamic_btns_layout.setSpacing(6)

        self.ask_input = QLineEdit()
        self.ask_input.setPlaceholderText("Ask about this...")
        self.ask_input.setClearButtonEnabled(True)
        self.ask_input.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.ask_input.setStyleSheet("""
            QLineEdit {
                background-color: #1e293b; border: 1px solid #334155;
                color: #e2e8f0; border-radius: 8px; padding: 8px 12px;
                font-size: 12px; font-family: 'Segoe UI', sans-serif;
            }
            QLineEdit:focus { border-color: #3b82f6; }
        """)
        self.ask_input.returnPressed.connect(self.on_ask_input_submit)

        self.panel_layout.addWidget(self.status_label)
        self.panel_layout.addWidget(self.header_label)
        self.panel_layout.addWidget(self.content_label)
        self.panel_layout.addWidget(self.read_more_btn)
        self.panel_layout.addLayout(self.dynamic_btns_layout)
        self.panel_layout.addWidget(self.ask_input)

        self.bubble_btn = QPushButton()
        self.bubble_btn.setFixedSize(self.bubble_size, self.bubble_size)
        self.bubble_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bubble_btn.clicked.connect(self.toggle_expand)

        self.main_layout.addWidget(self.panel)
        self.main_layout.addWidget(self.bubble_btn)
        self.panel.hide()

        # Drag support
        self.bubble_btn.installEventFilter(self)
        self.panel.installEventFilter(self)

        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.opacity_effect)
        self.opacity_effect.setOpacity(1.0) # Always visible by default

        self.anim = QPropertyAnimation(self.opacity_effect, b"opacity")
        self.anim.setDuration(200) # Faster transition

        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._pulse_tick)
        self._pulse_phase = 0

    # ── Drag ─────────────────────────────────────────────────────────

    def eventFilter(self, obj, event):
        if event.type() == event.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                # Don't return True here so buttons still get press
        elif event.type() == event.Type.MouseMove:
            if event.buttons() == Qt.MouseButton.LeftButton:
                if hasattr(self, '_drag_pos') and self._drag_pos:
                    # Check distance to avoid accidental drag on click
                    delta = event.globalPosition().toPoint() - (self.pos() + self._drag_pos)
                    if delta.manhattanLength() > 5:
                        new_pos = event.globalPosition().toPoint() - self._drag_pos
                        self.move(new_pos)
                        # Store where the BUBBLE button is in global space
                        # In the QHBoxLayout (AlignRight), the bubble is at the right edge
                        self.user_bubble_pos = new_pos + QPoint(self.width() - self.bubble_size, self.height() - self.bubble_size)
                        return True
        elif event.type() == event.Type.MouseButtonRelease:
            self._drag_pos = None
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton:
            if hasattr(self, '_drag_pos') and self._drag_pos:
                new_pos = event.globalPosition().toPoint() - self._drag_pos
                self.move(new_pos)
                self.user_bubble_pos = new_pos + QPoint(self.width() - self.bubble_size, self.height() - self.bubble_size)
                event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        event.accept()

    def enterEvent(self, event):
        self.is_hovered = True
        if self._dismiss_timer.isActive():
            self._dismiss_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.is_hovered = False
        super().leaveEvent(event)

    def _on_auto_dismiss_tick(self):
        # Never auto-close — user must click Dismiss
        self._dismiss_timer.stop()

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
        
        # FIX: total_h should only include panel height if expanded
        actual_content_h = max(self.bubble_size, current_panel_h) if self.is_expanded else self.bubble_size
        total_h = actual_content_h + self.margin
        
        if self.user_bubble_pos:
            # Respect user's dragged position for the ORB
            x = self.user_bubble_pos.x() - (total_w - self.bubble_size)
            y = self.user_bubble_pos.y() - (total_h - self.bubble_size)
        else:
            # Default to bottom-right corner
            x = self.screen_geo.x() + self.screen_geo.width()  - total_w
            y = self.screen_geo.y() + self.screen_geo.height() - total_h
            
        self.setGeometry(x, y, total_w, total_h)
        self.show() # Ensure always visible
        self.raise_()

    def set_context_status(self, ctx_data: object):
        """Update the bubble's description while idle or if panel is visible."""
        # ctx_data is a Context object from context_extractor
    def _get_activity_label(self, ctx_data):
        # Build "Doing" part using context object or dict
        if isinstance(ctx_data, dict):
             activity   = ctx_data.get('activity', 'general_browsing')
             app        = ctx_data.get('app', 'general')
             file_path  = ctx_data.get('file_path')
             page_title = ctx_data.get('page_title')
        else:
             activity   = getattr(ctx_data, 'activity', 'general_browsing')
             app        = getattr(ctx_data, 'app', 'general')
             file_path  = getattr(ctx_data, 'file_path', None)
             page_title = getattr(ctx_data, 'page_title', None)

        app_name = self.APP_LABELS.get(app, app.capitalize())
        
        if activity == "coding" and file_path:
            doing = f"Editing {file_path}"
        elif activity == "writing_document" and file_path:
            doing = f"Writing {file_path}"
        elif activity == "reading_pdf" and file_path:
            doing = f"Reading {file_path}"
        elif activity == "reading_article" and page_title:
            doing = f"Reading: {page_title}"
        elif activity == "watching_video" and page_title:
            doing = f"Watching: {page_title}"
        elif activity == "searching_topic" and page_title:
            doing = f"Searching: {page_title}"
        elif activity == "browsing_repo" and page_title:
            doing = f"Browsing: {page_title}"
        elif activity == "debugging_error":
            doing = f"Debugging Error{' in ' + file_path if file_path else ''}"
        elif activity == "chatting" and app_name:
            doing = f"Chatting on {app_name}"
        else:
            doing = activity.replace("_", " ").capitalize()
            
        return app_name, doing

    def set_context_status(self, ctx_data: object):
        page_title = getattr(ctx_data, 'page_title', None)
        app_name, doing = self._get_activity_label(ctx_data)
        
        # Update the top header label immediately
        full_status = f"<b>{app_name}</b> | {doing}"
        if hasattr(self, 'status_label') and self.status_label.text() != full_status:
            self.status_label.setText(full_status)
            self.status_label.show()
        
        # Update the panel content if IDLE
        if self.orb_state == self.STATE_IDLE:
             self.content_label.setText(f"Cora is observing {doing.lower()} and ready to assist.")
        
        # Format as HTML for premium look
        status_html = f"<b>You’re in:</b> {app_name}<br><b>Looks like:</b> {doing}"
        
        # Update the persistent status label
        if hasattr(self, 'status_label'):
            # Use small bold tags for the persistent status
            persistent_html = f"<b>{app_name}</b> | {doing}"
            if self.status_label.text() != persistent_html:
                self.status_label.setText(persistent_html)
                self.status_label.show()

        # If we are in IDLE state or only basic suggestion, update content_label as a status indicator
        if self.orb_state == self.STATE_IDLE or (self.current_data and self.current_data.get('type') == 'general'):
             self.content_label.setText(status_html)
             if self.orb_state == self.STATE_IDLE:
                 self.header_label.setText("Cora Observer")
        
        # Also update window title in data if it exists
        if self.current_data:
            self.current_data['window_title'] = getattr(ctx_data, 'window_title', '')

    # ── Show suggestion ──────────────────────────────────────────────

    def show_suggestion(self, data: dict):
        try:
            self.current_data = data
            if not self.isVisible():
                self.show()
            # Stop any running animation immediately
            self.anim.stop()
            try:
                self.anim.finished.disconnect()
            except Exception:
                pass
            self._pending_data = None
            # Use singleShot(0) to process on next event loop tick — prevents lag
            QTimer.singleShot(0, lambda: self._safe_show(data))
        except RuntimeError as e:
            print(f"UI Safety: {e}")
        except Exception as e:
            print(f"UI Error in show_suggestion: {e}")

    def _safe_show(self, data):
        try:
            self._show_suggestion_inner(data)
        except Exception as e:
            print(f"UI Error in _safe_show: {e}")

    def _show_suggestion_inner(self, data):
        self._pending_data = None

        if not isinstance(data, dict):
            data = {"type": "fallback", "reason": str(data), "suggestions": []}
        data.setdefault("type",           "general")
        data.setdefault("reason",         "Suggestion")
        data.setdefault("suggestions",    [])
        data.setdefault("screen_context", "")
        data.setdefault("error_context",  "")
        data.setdefault("page_title",     "")
        data.setdefault("site_name",      "")
        data.setdefault("window_title",   "")

        self.current_data   = data
        suggestion_type     = data.get('type', 'general')
        reason              = data.get('reason', 'No details')
        reason_long         = data.get('reason_long', '')

        # Update status label from payload to ensure consistency
        app_name, doing = self._get_activity_label(data)
        
        full_status = f"<b>{app_name}</b> | {doing}"
        if hasattr(self, 'status_label'):
            self.status_label.setText(full_status)
            self.status_label.show()

        # Always reset state for new suggestion
        self.anim.stop()
        try:
            self.anim.finished.disconnect()
        except Exception:
            pass
        self._dismiss_timer.stop()
        self.is_read_more_expanded = False
        self.ask_input.clear()

        # Always keep panel expanded
        self.is_expanded = True
        self.panel.show()

        self.content_label.setText(reason)
        if reason_long:
            self.read_more_btn.show()
            self.read_more_btn.setText("Read more")
        else:
            self.read_more_btn.hide()

        # ── Header / orb per type ────────────────────────────────────
        TYPE_CONFIG = {
            'syntax_error':            ('⚠️ Syntax Error',        'Fix Error',  self.STATE_ERROR),
            'writing_suggestion':      ('✍️ Writing Tip',          'Improve',    self.STATE_SUGGESTION),
            'reading_suggestion':      ('📖 Reading Assistant',    'Ask',        self.STATE_SUGGESTION),
            'pdf_suggestion':          ('📄 PDF Assistant',        'Summarize',  self.STATE_SUGGESTION),
            'spreadsheet_suggestion':  ('📊 Spreadsheet',          'Analyze',    self.STATE_SUGGESTION),
            'youtube_suggestion':      ('▶️ Video Assistant',      'Explain',    self.STATE_SUGGESTION),
            'browser_suggestion':      ('🌐 Browser Assistant',    'Summarize',  self.STATE_SUGGESTION),
            'developer_suggestion':    ('💻 Code Assistant',       'Review',     self.STATE_SUGGESTION),
            'presentation_suggestion': ('🎯 Presentation',         'Improve',    self.STATE_SUGGESTION),
            'ai_suggestion':           ('🤖 AI Tool Assistant',    'Help',       self.STATE_SUGGESTION),
            'picked_suggestion':       ('🎯 Picked Element', 'Ask About This', self.STATE_SUGGESTION),
        }
        header_text, action_text, orb_state = TYPE_CONFIG.get(
            suggestion_type, ('✨ Cora Suggestion', 'View', self.STATE_SUGGESTION)
        )
        self.header_label.setText(header_text)

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
                {"label": "Review Code", "action": "explain_error"},
            ])
        else:
            suggestions = data.get('suggestions', [])
            if not suggestions:
                FALLBACKS = {
                    'writing_suggestion':      [
                        {"label": "Summarize",       "hint": "Summarize this content"},
                        {"label": "Fix Grammar",     "hint": "Fix grammar issues"},
                        {"label": "Improve Clarity", "hint": "Improve text clarity"},
                        {"label": "Check Tone",      "hint": "Analyze the tone of the writing"},
                    ],
                    'reading_suggestion':      [
                        {"label": "Summarize Page",   "hint": "Summarize the visible page"},
                        {"label": "Explain Concepts", "hint": "Explain key concepts on this page"},
                        {"label": "Key Points",       "hint": "Extract key points as bullets"},
                        {"label": "Deep Dive",        "hint": "Provide a deep analysis of the topics mentioned"},
                    ],
                    'pdf_suggestion':          [
                        {"label": "Summarize Page", "hint": "Summarize the visible PDF page"},
                        {"label": "Key Points",     "hint": "Extract key points"},
                        {"label": "Explain Terms",  "hint": "Explain technical terms"},
                        {"label": "OCR Check",      "hint": "Verify text extraction accuracy"},
                    ],
                    'spreadsheet_suggestion':  [
                        {"label": "Explain Formula", "hint": "Explain visible formulas"},
                        {"label": "Analyze Data",    "hint": "Find patterns in visible data"},
                        {"label": "Summarize Sheet", "hint": "Provide an overview of this sheet"},
                        {"label": "Extract Totals",  "hint": "Identify sums or totals on screen"},
                    ],
                    'youtube_suggestion':      [
                        {"label": "Explain Topic", "hint": "Explain the video topic from title"},
                        {"label": "Key Points",    "hint": "Extract visible subtitle points"},
                    ],
                    'browser_suggestion':      [
                        {"label": "Summarize",  "hint": "Summarize this page"},
                        {"label": "Key Ideas",  "hint": "Extract key ideas from this page"},
                        {"label": "Related Topics", "hint": "Find information related to this page"},
                        {"label": "Page Analysis", "hint": "Provide a detailed analysis of the page structure"},
                    ],
                    'developer_suggestion':    [
                        {"label": "Explain Code", "hint": "Explain the visible code"},
                        {"label": "Find Issues",  "hint": "Identify potential issues in the code"},
                        {"label": "Optimize Logic", "hint": "Suggest ways to make this code faster or cleaner"},
                        {"label": "Check for bugs", "hint": "Check the visible code for potential issues or bugs."},
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
                        {"label": "Explain Response", "hint": "Explain the previous response in more detail"},
                    ],
                }
                suggestions = FALLBACKS.get(suggestion_type, [
                    {"label": "Summarize Content",   "hint": "Provide a summary of the visible page"},
                    {"label": "Key Takeaways",       "hint": "Extract the most important points"},
                    {"label": "Deep Analysis",       "hint": "Perform a detailed analysis of the elements"},
                    {"label": "Next Steps",          "hint": "Suggest what to do next based on this content"},
                ])
            self._add_suggestion_chips(suggestions)

        # ── Clear any existing bottom bar first ──
        for i in reversed(range(self.panel_layout.count())):
            item = self.panel_layout.itemAt(i)
            if item and item.widget():
                w = item.widget()
                if hasattr(w, '_is_bottom_bar') and w._is_bottom_bar:
                    w.deleteLater()
                    self.panel_layout.removeItem(item)

        # ── Bottom bar with Dismiss and Pick ──
        bottom_bar = QWidget()
        bottom_bar._is_bottom_bar = True
        bb_layout = QHBoxLayout(bottom_bar)
        bb_layout.setContentsMargins(0, 6, 0, 0)
        bb_layout.setSpacing(8)

        dismiss_btn = QPushButton("✕  Dismiss")
        dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss_btn.setFixedHeight(30)
        dismiss_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: 1px solid #475569;
                color: #94a3b8;
                border-radius: 6px;
                padding: 4px 14px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: rgba(239,68,68,0.15);
                border-color: #ef4444;
                color: #ef4444;
            }
        """)
        dismiss_btn.clicked.connect(self._on_dismiss_clicked)

        pick_btn = QPushButton("🎯  Pick")
        pick_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        pick_btn.setFixedHeight(30)
        pick_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: 1px solid #3b82f6;
                color: #3b82f6;
                border-radius: 6px;
                padding: 4px 12px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: rgba(59,130,246,0.15);
                color: #60a5fa;
                border-color: #60a5fa;
            }
        """)
        pick_btn.clicked.connect(self._on_pick_clicked)

        bb_layout.addWidget(dismiss_btn)
        bb_layout.addWidget(pick_btn)
        bb_layout.addStretch()
        self.panel_layout.addWidget(bottom_bar)

        self.update_layout_pos()
        self.show()
        self.raise_()

        # Always show fresh — but no full fade from 0 if already visible
        current_op = self.opacity_effect.opacity()
        if current_op < 1.0:
            self.anim.stop()
            self.anim.setStartValue(current_op)
            self.anim.setEndValue(1.0)
            self.anim.start()
        else:
            self.opacity_effect.setOpacity(1.0)

        if suggestion_type == 'syntax_error':
            self._dismiss_timer.stop()
        elif suggestion_type == 'message':
            # Auto-dismiss disabled — user must click Dismiss
            # self._dismiss_timer.start(...)
            pass
        else:
            # Persistent: never auto-dismiss unless it's a transient message
            pass

        # Force geometry refresh to prevent frozen UI
        QTimer.singleShot(100, self.update_layout_pos)
        QTimer.singleShot(200, self.update)

    # ── Chip builders ────────────────────────────────────────────────

    def _make_chip(self, label, tooltip=""):
        btn = QPushButton(label)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(tooltip)
        btn.setMaximumWidth(160)
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

    def _on_dismiss_clicked(self):
        self.hide_bubble()
        self._set_orb_state(self.STATE_IDLE)
        self.dismissed.emit()

    def _add_suggestion_chips(self, suggestions):
        # Clear existing chips
        for i in reversed(range(self.dynamic_btns_layout.count())):
            item = self.dynamic_btns_layout.itemAt(i)
            if item and item.widget():
                item.widget().deleteLater()

        # Add chips in rows of 2 (using QVBoxLayout dynamic_btns_layout)
        row_layout = None
        for idx, sug in enumerate(suggestions):
            if idx % 2 == 0:
                row_widget = QWidget()
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(6)
                self.dynamic_btns_layout.addWidget(row_widget)

            chip_btn = QPushButton(sug['label'])
            chip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            chip_btn.setStyleSheet("""
                QPushButton {
                    background-color: transparent;
                    border: 1px solid #3b82f6;
                    color: #e2e8f0;
                    border-radius: 6px;
                    padding: 5px 10px;
                    font-size: 11px;
                    text-align: left;
                }
                QPushButton:hover {
                    background-color: rgba(59,130,246,0.2);
                    color: white;
                }
            """)
            hint = sug.get('hint', sug['label'])
            chip_btn.clicked.connect(
                lambda checked, l=sug['label'], h=hint: self.ask_cora_clicked.emit(l, h)
            )
            row_layout.addWidget(chip_btn)

        if row_layout and len(suggestions) % 2 == 1:
            row_layout.addStretch()

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
        if not self.is_expanded:
            self.is_expanded = True
            self.panel.show()
        else:
            self.hide_bubble()
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

    def enter_idle_mode(self):
        self.anim.stop()
        self.opacity_effect.setOpacity(1.0)
        self.current_data = None
        self.is_expanded = False # FIX: Collapse by default in idle mode
        self.panel.hide() # FIX: Ensure panel is hidden
        self._set_orb_state(self.STATE_IDLE)
        self.update_layout_pos()
        self.show()
        # Show pending suggestion if queued during fade-out
        if self._pending_data:
            QTimer.singleShot(100, lambda: self._show_suggestion_inner(self._pending_data))

    def hide_bubble(self):
        """Collapse panel but keep orb visible."""
        self.current_data = None
        self.is_expanded   = False
        self.panel.hide()
        self.update_layout_pos()
        self.show()

    def fade_out(self, force=False):
        # We don't fade out anymore, we just enter idle mode (collapse)
        self.enter_idle_mode()

    def _on_fade_finished(self):
        pass

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
        page_title = self.current_data.get('page_title', '')
        site_name  = self.current_data.get('site_name', '')

        if suggestion_type == 'writing_suggestion':
            task    = f"Improve the writing: {reason}"
            display = "Improving Writing..."
        else:
            task    = f"Explain or summarize: {reason}"
            display = "Analyzing..."

        prompt = _build_chip_prompt(
            task, screen_ctx, reason, win_title, page_title, site_name,
            selected_text=self.current_data.get('selected_text', '') if self.current_data else ''
        )
        self.ask_cora_clicked.emit(display, prompt)
        self.hide_bubble()

    def on_ask_input_submit(self):
        text = self.ask_input.text().strip()
        if not text:
            return

        screen_ctx = win_title = reason = page_title = site_name = ""
        if self.current_data:
            screen_ctx = (
                self.current_data.get('screen_context', '') or
                self.current_data.get('error_context', '')
            )
            reason     = self.current_data.get('reason', '')
            win_title  = self.current_data.get('window_title', '')
            page_title = self.current_data.get('page_title', '')
            site_name  = self.current_data.get('site_name', '')

        prompt  = _build_chip_prompt(
            text, screen_ctx, reason, win_title, page_title, site_name,
            selected_text=self.current_data.get('selected_text', '') if self.current_data else ''
        )
        display = text[:50] + "…" if len(text) > 50 else text

        self.ask_cora_clicked.emit(display, prompt)
        self.ask_input.clear()
        self.hide_bubble()

    def _on_pick_clicked(self):
        self.pick_requested.emit()

    def trigger_reading_action(self, hint: str):
        if not self.current_data:
            return

        screen_ctx = self.current_data.get('screen_context', '')
        reason     = self.current_data.get('reason', '')
        win_title  = self.current_data.get('window_title', '')
        page_title = self.current_data.get('page_title', '')
        site_name  = self.current_data.get('site_name', '')

        print(f"Chip: '{hint}' | ctx={len(screen_ctx)}ch | page='{page_title}' | site='{site_name}'")

        prompt  = _build_chip_prompt(
            hint, screen_ctx, reason, win_title, page_title, site_name,
            selected_text=self.current_data.get('selected_text', '') if self.current_data else ''
        )
        display = f"{hint}…"

        self.ask_cora_clicked.emit(display, prompt)
        self.hide_bubble()