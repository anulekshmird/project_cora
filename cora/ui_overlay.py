
import sys
import json
from PyQt6.QtCore import Qt, pyqtSignal, QPropertyAnimation, QPoint, QEasingCurve, QRect, QSize, QTimer
from PyQt6.QtGui import QIcon, QPainter, QColor, QBrush, QPainterPath
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
    QLabel, QPushButton, QFrame, QGraphicsOpacityEffect,
    QLineEdit
)

class ProactiveBubble(QWidget):
    ask_cora_clicked = pyqtSignal(str, str) # display, prompt
    dismissed = pyqtSignal()
    snoozed = pyqtSignal(int) # minutes
    
    # Orb States
    STATE_IDLE = "idle"
    STATE_THINKING = "thinking"
    STATE_ERROR = "error"
    STATE_SUGGESTION = "suggestion"
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Cora Suggestion")
        
        # State
        self.current_data = None
        self.is_expanded = False
        self.orb_state = self.STATE_IDLE
        
        # Determine Screen Position (Bottom Right)
        self.screen_geo = QApplication.primaryScreen().availableGeometry()
        self.bubble_size = 70
        self.panel_width = 300
        self.panel_height = 220
        self.margin = 20
        
        # Initial Geometry (Hidden)
        self.setGeometry(0, 0, 0, 0)
        
        # Main Layout: [Panel] [Bubble]
        self.main_layout = QHBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(15)
        self.main_layout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        
        # =====================================================================
        # 1. EXPANDABLE PANEL
        # =====================================================================
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
            QLabel#header { font-weight: bold; font-size: 14px; color: white; }
            QLabel#content { font-size: 13px; line-height: 1.4; color: #cbd5e1; }
        """)
        
        # Panel Content
        self.panel_layout = QVBoxLayout(self.panel)
        self.panel_layout.setContentsMargins(20, 15, 20, 15)
        self.panel_layout.setSpacing(8)
        
        self.header_label = QLabel("Suggestion")
        self.header_label.setObjectName("header")
        
        self.content_label = QLabel("Content...")
        self.content_label.setObjectName("content")
        self.content_label.setWordWrap(True)
        self.content_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.content_label.setMaximumHeight(80)
        
        # Dynamic Action Buttons Container
        self.dynamic_btns_layout = QHBoxLayout()
        self.dynamic_btns_layout.setSpacing(8)
        
        # Permanent Buttons (detached/reattached — never deleted)
        self.dismiss_btn = QPushButton("Dismiss")
        self.dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dismiss_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: 1px solid #475569;
                color: #94a3b8;
                border-radius: 6px;
                padding: 6px 12px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 0.05);
                color: #cbd5e1;
                border-color: #64748b;
            }
        """)
        self.dismiss_btn.clicked.connect(self.on_dismiss)
        
        self.action_btn = QPushButton("Action")
        self.action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action_btn.setStyleSheet("""
            QPushButton {
                background-color: #3b82f6;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: bold;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #2563eb; }
        """)
        self.action_btn.clicked.connect(self.on_action)
        
        # =====================================================================
        # "ASK ABOUT THIS" INPUT BOX (Spec §5)
        # =====================================================================
        self.ask_input = QLineEdit()
        self.ask_input.setPlaceholderText("Ask about this...")
        self.ask_input.setClearButtonEnabled(True)
        self.ask_input.setStyleSheet("""
            QLineEdit {
                background-color: #1e293b;
                border: 1px solid #334155;
                color: #e2e8f0;
                border-radius: 8px;
                padding: 8px 12px;
                font-size: 12px;
                font-family: 'Segoe UI', sans-serif;
            }
            QLineEdit:focus {
                border-color: #3b82f6;
            }
        """)
        self.ask_input.returnPressed.connect(self.on_ask_input_submit)
        
        # Assemble Panel Layout
        self.panel_layout.addWidget(self.header_label)
        self.panel_layout.addWidget(self.content_label, 1)
        self.panel_layout.addLayout(self.dynamic_btns_layout)
        self.panel_layout.addWidget(self.ask_input)

        # =====================================================================
        # 2. CIRCULAR ORB BUTTON
        # =====================================================================
        self.bubble_btn = QPushButton()
        self.bubble_btn.setFixedSize(self.bubble_size, self.bubble_size)
        self.bubble_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bubble_btn.clicked.connect(self.toggle_expand)
        
        self.main_layout.addWidget(self.panel)
        self.main_layout.addWidget(self.bubble_btn)
        
        self.panel.hide()  # Start hidden
        
        # Animation
        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.opacity_effect)
        self.anim = QPropertyAnimation(self.opacity_effect, b"opacity")
        self.anim.setDuration(300)
        
        # Pulse animation timer for orb states
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._pulse_tick)
        self._pulse_phase = 0

    # =====================================================================
    # DRAG SUPPORT
    # =====================================================================
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
    
    # =====================================================================
    # ORB STATE MACHINE
    # =====================================================================
    def _set_orb_state(self, state):
        """Update orb visual state: idle, thinking, error, suggestion."""
        self.orb_state = state
        self._pulse_timer.stop()
        
        if state == self.STATE_IDLE:
            self.bubble_btn.setText("")
            self.bubble_btn.setIcon(QIcon())
            self.bubble_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: rgba(15, 23, 42, 0.7);
                    border: 2px solid rgba(99, 133, 180, 0.4);
                    border-radius: {self.bubble_size//2}px;
                    border-image: url(icon.png) 0 0 0 0 stretch;
                }}
                QPushButton:hover {{
                    background-color: rgba(15, 23, 42, 0.95);
                    border-color: #60a5fa;
                }}
            """)
            # Gentle breathing glow to show orb is available
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
                    background-color: #991b1b;
                    border-color: #fca5a5;
                }}
            """)
            # Start red pulse
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
                    background-color: #334155;
                    border-color: #fbbf24;
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
                QPushButton:hover {{
                    border: 2px solid #60a5fa;
                }}
            """)

    def _pulse_tick(self):
        """Animate orb border glow via opacity toggle."""
        try:
            self._pulse_phase = (self._pulse_phase + 1) % 2
            
            if self.orb_state == self.STATE_IDLE:
                # Subtle breathing glow — dim ↔ bright border
                border_color = "rgba(99, 133, 180, 0.6)" if self._pulse_phase == 0 else "rgba(99, 133, 180, 0.2)"
                self.bubble_btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: rgba(15, 23, 42, 0.7);
                        border: 2px solid {border_color};
                        border-radius: {self.bubble_size//2}px;
                        border-image: url(icon.png) 0 0 0 0 stretch;
                    }}
                    QPushButton:hover {{
                        border-color: #60a5fa;
                    }}
                """)
            elif self.orb_state == self.STATE_ERROR:
                base_color = "#7f1d1d" if self._pulse_phase == 0 else "#991b1b"
                border_color = "#ef4444" if self._pulse_phase == 0 else "#fca5a5"
                self.bubble_btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {base_color};
                        border: 2px solid {border_color};
                        border-radius: {self.bubble_size//2}px;
                    }}
                """)
            elif self.orb_state == self.STATE_THINKING:
                border_color = "#f59e0b" if self._pulse_phase == 0 else "#fbbf24"
                self.bubble_btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: #1e293b;
                        border: 2px solid {border_color};
                        border-radius: {self.bubble_size//2}px;
                    }}
                """)
        except RuntimeError:
            self._pulse_timer.stop()
    
    # =====================================================================
    # LAYOUT POSITIONING
    # =====================================================================
    def update_layout_pos(self):
        self.screen_geo = QApplication.primaryScreen().availableGeometry()
        total_w = self.bubble_size + self.margin
        if self.is_expanded:
            total_w += self.panel_width + 15
        total_h = max(self.bubble_size, self.panel_height) + self.margin
        x = self.screen_geo.x() + self.screen_geo.width() - total_w
        y = self.screen_geo.y() + self.screen_geo.height() - total_h
        self.setGeometry(x, y, total_w, total_h)

    # =====================================================================
    # SHOW SUGGESTION (Main Entry Point)
    # =====================================================================
    def show_suggestion(self, data):
        try:
            self._show_suggestion_inner(data)
        except RuntimeError as e:
            print(f"UI Safety: Qt object deleted during show_suggestion: {e}")
        except Exception as e:
            print(f"UI Error in show_suggestion: {e}")
    
    def _show_suggestion_inner(self, data):
        is_already_visible = self.isVisible() and self.opacity_effect.opacity() > 0.9
        
        if not is_already_visible:
            self.anim.stop()
            self.opacity_effect.setOpacity(1.0)
            try:
                self.anim.finished.disconnect()
            except: pass
        
        # Guarantee valid payload structure
        if not isinstance(data, dict):
            data = {"type": "fallback", "reason": str(data), "suggestions": []}
        if "type" not in data:
            data["type"] = "general"
        if "reason" not in data:
            data["reason"] = "Suggestion"
        if "suggestions" not in data:
            data["suggestions"] = []
        if "screen_context" not in data:
            data["screen_context"] = ""
        if "error_context" not in data:
            data["error_context"] = ""
        
        self.current_data = data
        
        # AUTO-EXPAND for critical errors, otherwise maintain state or collapse
        suggestion_type = data.get('type', 'general')
        if suggestion_type == 'syntax_error' and not ("Analyzing" in data.get('reason', '')):
            self.is_expanded = True
            self.panel.show()
        elif not is_already_visible:
            self.is_expanded = False
            self.panel.hide()
        
        reason = data.get('reason', 'No details')
        
        # Update Content
        self.content_label.setText(reason)
        
        # Clear ask input
        self.ask_input.clear()
        
        # ----------------------------------------------------------------
        # SET ORB STATE + HEADER based on type
        # ----------------------------------------------------------------
        if suggestion_type == 'syntax_error':
            self.header_label.setText("⚠️ Syntax Error Detected")
            self.action_btn.setText("Fix Error")
            if "Analyzing" in reason or "Fetching" in reason:
                self._set_orb_state(self.STATE_THINKING)
            else:
                self._set_orb_state(self.STATE_ERROR)
            
        elif suggestion_type == 'writing_suggestion':
            self.header_label.setText("✍️ Writing Tip")
            self.action_btn.setText("Improve")
            self._set_orb_state(self.STATE_SUGGESTION)
            # Purple accent for writing
            self.bubble_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: #4c1d95;
                    border: 2px solid #8b5cf6;
                    border-radius: {self.bubble_size//2}px;
                }}
                QPushButton:hover {{
                    background-color: #5b21b6;
                    border-color: #a78bfa;
                }}
            """)
            self.bubble_btn.setText("✍️")
            self.bubble_btn.setIcon(QIcon())

        elif suggestion_type == 'reading_suggestion':
            self.header_label.setText("📖 Reading Assistant")
            self.action_btn.setText("Ask")
            self._set_orb_state(self.STATE_SUGGESTION)
            self.bubble_btn.setText("📖")
            self.bubble_btn.setIcon(QIcon())

        else:
            self.header_label.setText("✨ Cora Suggestion")
            self.action_btn.setText("View")
            self._set_orb_state(self.STATE_SUGGESTION)

        # ----------------------------------------------------------------
        # UPDATE ACTION BUTTONS (Dynamic)
        # ----------------------------------------------------------------
        # Detach permanent buttons FIRST to prevent deleteLater() destroying them
        if self.dismiss_btn.parent():
            self.dismiss_btn.setParent(None)
        if self.action_btn.parent():
            self.action_btn.setParent(None)
        
        # Clear dynamic buttons (FIX 8: Qt safety guard with parent check)
        while self.dynamic_btns_layout.count():
             item = self.dynamic_btns_layout.takeAt(0)
             w = item.widget()
             if w:
                 try:
                     if w.parent():
                         w.deleteLater()
                 except RuntimeError:
                     pass  # Already deleted C++ object
        
        # Build mode-specific action chips
        if suggestion_type == 'syntax_error':
            self._add_chip_buttons([
                {"label": "Fix Error", "action": "fix_error"},
                {"label": "Explain", "action": "explain_error"},
                {"label": "Show Code", "action": "show_code"},
            ])
            
        elif suggestion_type == 'writing_suggestion':
            suggestions = data.get('suggestions', [])
            if not suggestions:
                suggestions = [
                    {"label": "Summarize", "hint": "Summarize this content"},
                    {"label": "Fix Grammar", "hint": "Fix grammar issues"},
                    {"label": "Improve Clarity", "hint": "Improve text clarity"},
                    {"label": "Bullets", "hint": "Convert to bullet points"},
                ]
            self._add_suggestion_chips(suggestions)
            
        elif suggestion_type == 'reading_suggestion':
            suggestions = data.get('suggestions', [])
            if not suggestions:
                suggestions = [
                    {"label": "Summarize Page", "hint": "Summarize this visible page"},
                    {"label": "Explain Concepts", "hint": "Explain key concepts on this page"},
                    {"label": "Key Points", "hint": "Extract bullet points"},
                ]
            self._add_suggestion_chips(suggestions)
            
        else:
            # Default: Dismiss + Main Action
            self.dynamic_btns_layout.addWidget(self.dismiss_btn)
            self.dynamic_btns_layout.addWidget(self.action_btn)
            self.dismiss_btn.show()
            self.action_btn.show()

        print("DEBUG: Showing Suggestion Bubble!")
        self.update_layout_pos()
        self.show()
        self.raise_()
        QApplication.processEvents()
        
        # Fade In only if not already visible
        if not is_already_visible:
            self.opacity_effect.setOpacity(0)
            self.anim.setStartValue(0.0)
            self.anim.setEndValue(1.0)
            self.anim.start()

    # =====================================================================
    # ACTION CHIP BUILDERS
    # =====================================================================
    def _make_chip(self, label, tooltip=""):
        """Create a styled chip button."""
        btn = QPushButton(label)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(tooltip)
        btn.setStyleSheet("""
            QPushButton {
                background-color: #0f172a;
                border: 1px solid #3b82f6;
                color: #60a5fa;
                border-radius: 6px;
                padding: 5px 10px;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #1e3a8a;
                color: white;
            }
        """)
        return btn

    def _add_chip_buttons(self, actions):
        """Add error-mode chip buttons (Fix, Explain, Show Code)."""
        for action_info in actions:
            label = action_info["label"]
            action_type = action_info["action"]
            btn = self._make_chip(label)
            
            def make_cb(at):
                return lambda: self._handle_error_chip(at)
            btn.clicked.connect(make_cb(action_type))
            self.dynamic_btns_layout.addWidget(btn)
        
        # Always add dismiss at end
        self.dynamic_btns_layout.addWidget(self.dismiss_btn)
        self.dismiss_btn.show()

    def _add_suggestion_chips(self, suggestions):
        """Add reading/writing mode suggestion chips."""
        for sugg in suggestions:
            label = sugg.get('label', 'Action')
            hint = sugg.get('hint', '')
            btn = self._make_chip(label, hint)
            
            def make_cb(h):
                return lambda: self.trigger_reading_action(h)
            btn.clicked.connect(make_cb(hint))
            self.dynamic_btns_layout.addWidget(btn)
        
        # Add dismiss at end
        self.dynamic_btns_layout.addWidget(self.dismiss_btn)
        self.dismiss_btn.show()

    def _handle_error_chip(self, action_type):
        """Handle error-mode chip clicks."""
        if not self.current_data:
            return
        
        error_file = self.current_data.get('error_file', 'Unknown')
        error_line = self.current_data.get('error_line', '?')
        error_msg = self.current_data.get('error_message', '')
        error_context = self.current_data.get('error_context', '')
        code = self.current_data.get('code', '')
        reason = self.current_data.get('reason', '')
        if isinstance(code, dict): code = json.dumps(code)
        
        if action_type == "fix_error":
            prompt = (f"SYNTAX ERROR DETECTED\n"
                      f"FILE: {error_file}\n"
                      f"LINE: {error_line}\n"
                      f"ERROR: {error_msg}\n\n"
                      f"CODE:\n{error_context}\n\n"
                      f"SYSTEM FIX:\n{reason}\n{code}\n\n"
                      f"TASK:\nExplain the fix briefly and provide final corrected code.")
            display = "Fixing Syntax Error..."
            
        elif action_type == "explain_error":
            prompt = (f"SYNTAX ERROR DETECTED\n"
                      f"FILE: {error_file}\n"
                      f"LINE: {error_line}\n"
                      f"ERROR: {error_msg}\n\n"
                      f"CODE:\n{error_context}\n\n"
                      f"TASK:\nExplain what went wrong and why this error occurs.")
            display = "Explaining Error..."
            
        elif action_type == "show_code":
            prompt = (f"SYNTAX ERROR DETECTED\n"
                      f"FILE: {error_file}\n"
                      f"ERROR: {error_msg}\n\n"
                      f"CODE:\n{error_context}\n\n"
                      f"TASK:\nProvide only the corrected code block, no explanation.")
            display = "Showing Corrected Code..."
        else:
            return
        
        self.ask_cora_clicked.emit(display, prompt)
        self.hide_bubble()

    # =====================================================================
    # PANEL INTERACTION
    # =====================================================================
    def show_message(self, title, message):
        self._show_suggestion_inner({'reason': f"{message}", 'type': 'message'})
        self.header_label.setText(title)

    def toggle_expand(self):
        # If no suggestion data loaded yet, show a default "Ready" state
        if not hasattr(self, 'current_data') or self.current_data is None:
            self._show_suggestion_inner({
                "type": "general",
                "reason": "I'm observing your activity. Ask me anything about your current work below!",
                "suggestions": []
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
        self.update_layout_pos()

    def on_dismiss(self):
        self.dismissed.emit()
        self.hide_bubble()

    def enter_idle_mode(self):
        """Collapses the bubble to a passive, non-intrusive state."""
        self.anim.stop()
        self.opacity_effect.setOpacity(1.0)
        self.is_expanded = False
        self.panel.hide()
        self._set_orb_state(self.STATE_IDLE)
        self.update_layout_pos()
        self.show()
        
    def hide_bubble(self):
        # FIX 9: Proper state transition on hide
        self.enter_idle_mode()

    # =====================================================================
    # ACTION HANDLERS
    # =====================================================================
    def on_action(self):
        if self.current_data:
            display = "Fixing issue..."
            prompt = ""
            
            if self.current_data.get('type') == 'syntax_error':
                 self._handle_error_chip("fix_error")
                 return
            elif self.current_data.get('type') == 'writing_suggestion':
                 reason = self.current_data.get('reason', '')
                 prompt = f"The system suggested a writing improvement: {reason}. Please apply it."
                 display = "Applying Writing Fix..."
            else:
                 reason = self.current_data.get('reason', '')
                 screen_ctx = self.current_data.get('screen_context', '')
                 file_ctx = self.current_data.get('error_context', '')

                 prompt = f"""
COMMAND: Follow Suggestion

SYSTEM OBSERVATION:
{reason}

VISIBLE SCREEN CONTENT:
{screen_ctx}

CODE CONTEXT:
{file_ctx}

INSTRUCTION:
Act directly using the provided context.
"""
                 display = "Viewing Suggestion..."

            self.ask_cora_clicked.emit(display, prompt)
            self.hide_bubble()

    def on_ask_input_submit(self):
        """Handle Enter press on the 'Ask about this' input box."""
        text = self.ask_input.text().strip()
        if not text:
            return
        
        # Build context from current suggestion data
        context = ""
        if self.current_data:
            reason = self.current_data.get('reason', '')
            error_ctx = self.current_data.get('error_context', '')
            if error_ctx:
                context = f"\n\nContext (current error):\n{error_ctx}"
            elif reason:
                context = f"\n\nContext: {reason}"
        
        prompt = f"{text}{context}"
        display = text[:50] + "..." if len(text) > 50 else text
        
        self.ask_cora_clicked.emit(display, prompt)
        self.ask_input.clear()
        self.hide_bubble()

    def trigger_reading_action(self, hint):
         print(f"Reading Action Triggered: {hint}")
         display = f"Reading: {hint}..."
         prompt = (f"COMMAND: Reading Task\n"
                   f"TASK: {hint}\n"
                   f"CONTEXT: The user is reading a document. Use the screen content or active document to answer.\n"
                   f"INSTRUCTION: Provide a clear, concise answer.")
         self.ask_cora_clicked.emit(display, prompt)
         self.hide_bubble()
