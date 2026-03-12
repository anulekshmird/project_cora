
import sys
import os
import datetime
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QSize, QTimer, QPropertyAnimation, QEasingCurve, QPoint
from PyQt6.QtGui import QFont, QIcon, QTextCursor, QColor, QAction, QPainter, QBrush, QLinearGradient, QPalette
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QLabel, QTextEdit, QPushButton, QListWidget, QFrame, 
    QFileDialog, QMessageBox, QScrollArea, QListWidgetItem, QMenu,
    QGraphicsDropShadowEffect, QSizePolicy
)

try:
    import speech_recognition as sr
except ImportError:
    sr = None
    print("Speech Recognition not found. Voice features disabled.")

# --- Voice Worker (unchanged from original) ---
class VoiceWorker(QThread):
    text_ready = pyqtSignal(str)
    finished = pyqtSignal()
    
    def __init__(self, recognizer):
        super().__init__()
        self.recognizer = recognizer
        self.running = False

    def run(self):
        self.running = True
        print("VoiceWorker: Starting...")
        try:
            with sr.Microphone() as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=1.0)
                while self.running:
                    try:
                        audio = self.recognizer.listen(source, timeout=5, phrase_time_limit=10)
                        text = self.recognizer.recognize_google(audio)
                        if text:
                            self.text_ready.emit(text)
                    except sr.WaitTimeoutError:
                        continue
                    except sr.UnknownValueError:
                        continue
                    except sr.RequestError as e:
                         print(f"VoiceWorker: Request Error: {e}")
                         break
        except Exception as e:
            print(f"Voice Error: {e}")
        finally:
            self.finished.emit()

    def stop(self):
        self.running = False

# --- Modern Message Bubble ---
class MessageBubble(QFrame):
    def __init__(self, text, is_user=False, timestamp=None, parent=None):
        super().__init__(parent)
        self.is_user = is_user
        self.text = text
        self.timestamp = timestamp or datetime.datetime.now().strftime("%H:%M")
        
        self.setup_ui()
        self.apply_styles()
        
    def setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 5, 20, 5)
        
        # Spacer for alignment
        if self.is_user:
            layout.addStretch()
        
        # Bubble container
        self.bubble_container = QFrame()
        self.bubble_container.setObjectName("bubbleContainer")
        bubble_layout = QVBoxLayout(self.bubble_container)
        bubble_layout.setContentsMargins(12, 8, 12, 8)
        bubble_layout.setSpacing(4)
        
        # Message text
        self.msg_label = QLabel(self.text)
        self.msg_label.setWordWrap(True)
        self.msg_label.setTextFormat(Qt.TextFormat.MarkdownText) # Enable Markdown/RichText
        self.msg_label.setOpenExternalLinks(True)
        self.msg_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        
        # Timestamp
        time_label = QLabel(self.timestamp)
        time_label.setObjectName("timestamp")
        time_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        
        bubble_layout.addWidget(self.msg_label)
        bubble_layout.addWidget(time_label)
        
        # Add shadow effect
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(10)
        shadow.setColor(QColor(0, 0, 0, 30))
        shadow.setOffset(0, 2)
        self.bubble_container.setGraphicsEffect(shadow)
        
        layout.addWidget(self.bubble_container)
        
        if not self.is_user:
            layout.addStretch()
            
        # Set maximum width for bubble
        self.bubble_container.setMaximumWidth(600)
        self.bubble_container.setMinimumWidth(100)
        
    def apply_styles(self):
        if self.is_user:
            self.setStyleSheet("""
                QFrame#bubbleContainer {
                    background-color: #2563EB;
                    border-radius: 18px;
                    border-bottom-right-radius: 4px;
                }
                QLabel {
                    color: white;
                    font-size: 15px;
                    background: transparent;
                    border: none;
                }
                QLabel#timestamp {
                    color: rgba(255, 255, 255, 0.6);
                    font-size: 11px;
                    margin-top: 2px;
                }
            """)
        else:
            self.setStyleSheet("""
                QFrame#bubbleContainer {
                    background-color: #1E293B;
                    border-radius: 18px;
                    border-bottom-left-radius: 4px;
                }
                QLabel {
                    color: #E2E8F0;
                    font-size: 15px;
                    background: transparent;
                    border: none;
                }
                QLabel#timestamp {
                    color: #94A3B8;
                    font-size: 11px;
                    margin-top: 2px;
                }
            """)

# --- Modern Chat Display with Bubbles ---
class ChatDisplay(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("""
            QScrollArea {
                border: none;
                background-color: #0F172A;
            }
            QScrollBar:vertical {
                border: none;
                background: #1E293B;
                width: 10px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical {
                background: #475569;
                border-radius: 5px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: #64748B;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                border: none;
                background: none;
            }
        """)
        
        self.container = QWidget()
        self.container.setStyleSheet("background-color: #0F172A;")
        self.layout = QVBoxLayout(self.container)
        self.layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.layout.setSpacing(15)
        self.layout.setContentsMargins(20, 20, 20, 20)
        
        self.setWidget(self.container)
        
        # Welcome message
        self.add_welcome_message()
        
    def add_welcome_message(self):
        welcome_text = """
        <div style='text-align: center; margin: 50px 0;'>
            <h1 style='color: white; font-size: 32px; margin-bottom: 10px;'>👋 Hello! I'm Cora</h1>
            <p style='color: #94A3B8; font-size: 16px;'>Your AI assistant. How can I help you today?</p>
        </div>
        """
        self.welcome_label = QLabel(welcome_text)
        self.welcome_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.layout.addWidget(self.welcome_label)
        
    def add_message(self, text, is_user=False):
        # Remove welcome message if it exists
        if hasattr(self, 'welcome_label') and self.welcome_label.isVisible():
             self.welcome_label.setVisible(False) # Just hide it, don't delete to avoid layout shift issues if cleared later
        
        bubble = MessageBubble(text, is_user)
        self.layout.addWidget(bubble)
        
        # Scroll to bottom
        QTimer.singleShot(50, self.scroll_to_bottom)
    
    def get_last_bubble(self):
        # Return the last MessageBubble in the layout
        cnt = self.layout.count()
        if cnt > 0:
            item = self.layout.itemAt(cnt - 1)
            if item.widget() and isinstance(item.widget(), MessageBubble):
                return item.widget()
        return None

    def scroll_to_bottom(self):
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
        
    def clear(self):
         # Clear all widgets except welcome or just reset
         while self.layout.count():
             item = self.layout.takeAt(0)
             if item.widget():
                 item.widget().deleteLater()
         self.add_welcome_message()

# --- Modern Input Area ---
class ModernInputArea(QFrame):
    message_sent = pyqtSignal(str, object) # text, attachment
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_attachment = None
        self.setup_ui()
        self.apply_styles()
        
    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        
        # Attachment chip
        self.chip_container = QFrame()
        self.chip_container.setVisible(False)
        chip_layout = QHBoxLayout(self.chip_container)
        chip_layout.setContentsMargins(20, 0, 20, 0)
        
        chip_content = QFrame()
        chip_content.setObjectName("chipContent")
        chip_content.setFixedHeight(32)
        chip_content_layout = QHBoxLayout(chip_content)
        chip_content_layout.setContentsMargins(10, 0, 5, 0)
        
        self.chip_label = QLabel("")
        self.chip_label.setStyleSheet("color: #60A5FA; font-size: 13px;")
        
        close_chip = QPushButton("✕")
        close_chip.setFixedSize(20, 20)
        close_chip.setCursor(Qt.CursorShape.PointingHandCursor)
        close_chip.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #94A3B8;
                border: none;
                font-size: 14px;
            }
            QPushButton:hover {
                color: #EF4444;
            }
        """)
        close_chip.clicked.connect(self.remove_attachment)
        
        chip_content_layout.addWidget(self.chip_label)
        chip_content_layout.addWidget(close_chip)
        chip_layout.addWidget(chip_content)
        chip_layout.addStretch()
        
        # Input container
        input_container = QFrame()
        input_container.setObjectName("inputContainer")
        input_layout = QHBoxLayout(input_container)
        input_layout.setContentsMargins(15, 8, 15, 8)
        input_layout.setSpacing(10)
        
        # Attachment button
        self.attach_btn = QPushButton("📎")
        self.attach_btn.setFixedSize(36, 36)
        self.attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.attach_btn.clicked.connect(self.attach_file)
        
        # Text input
        self.input_field = QTextEdit()
        self.input_field.setPlaceholderText("Type your message...")
        self.input_field.setMaximumHeight(120)
        self.input_field.setMinimumHeight(50)
        self.input_field.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.input_field.installEventFilter(self) # Install filter to catch Enter
        
        # Voice button
        self.voice_btn = QPushButton("🎤")
        self.voice_btn.setFixedSize(36, 36)
        self.voice_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.voice_btn.clicked.connect(self.toggle_voice)
        
        # Send button
        self.send_btn = QPushButton("➤")
        self.send_btn.setObjectName("sendBtn")
        self.send_btn.setFixedSize(50, 40)
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.clicked.connect(self.send_message)
        
        input_layout.addWidget(self.attach_btn)
        input_layout.addWidget(self.input_field)
        input_layout.addWidget(self.voice_btn)
        input_layout.addWidget(self.send_btn)
        
        layout.addWidget(self.chip_container)
        layout.addWidget(input_container)
        
        # Apply shadow to input container
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 50))
        shadow.setOffset(0, 2)
        input_container.setGraphicsEffect(shadow)
        
    def apply_styles(self):
        self.setStyleSheet("""
            ModernInputArea {
                background: transparent;
            }
            QFrame#inputContainer {
                background-color: #1E293B;
                border-radius: 12px;
                border: 1px solid #334155;
                margin: 0px 10px 10px 10px;
            }
            QFrame#chipContent {
                background-color: #1E293B;
                border: 1px solid #334155;
                border-radius: 16px;
            }
            QTextEdit {
                background: transparent;
                border: none;
                color: #E2E8F0;
                font-size: 15px;
                selection-background-color: #2563EB;
            }
            QTextEdit:focus {
                outline: none;
            }
            QPushButton {
                background: transparent;
                border: none;
                color: #94A3B8;
                font-size: 18px;
                border-radius: 18px;
            }
            QPushButton:hover {
                background-color: #334155;
                color: white;
            }
            QPushButton#sendBtn {
                background-color: #2563EB;
                color: white;
                font-size: 18px;
                font-weight: bold;
                border-radius: 20px;
            }
            QPushButton#sendBtn:hover {
                background-color: #1D4ED8;
            }
            QPushButton#sendBtn:disabled {
                background-color: #475569;
                color: #94A3B8;
            }
        """)
        
    def attach_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Attach File")
        if path:
            self.current_attachment = path
            self.chip_label.setText(f"📎 {os.path.basename(path)}")
            self.chip_container.setVisible(True)
            
    def remove_attachment(self):
        self.current_attachment = None
        self.chip_container.setVisible(False)
        
    def toggle_voice(self):
        pass # Signal handled by main wrapper
        
    def send_message(self):
        # Check if button is in STOP mode (text is square)
        if self.send_btn.text() == "⏹":
             self.message_sent.emit("", None) # Emit empty to trigger stop logic in handle_send
             return

        text = self.input_field.toPlainText().strip()
        if text or self.current_attachment:
            self.message_sent.emit(text, self.current_attachment)
            self.input_field.clear()
            self.remove_attachment()
            
    def eventFilter(self, obj, event):
        if obj == self.input_field and event.type() == event.Type.KeyPress:
             if event.key() == Qt.Key.Key_Return and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                 self.send_message()
                 return True
        return super().eventFilter(obj, event)

# Sidebar Removed


# --- Main Window (This is the class that replaces the old ChatWindow) ---
# NOTE: Renamed to ChatWindow to match main.py expectation
class ChatWindow(QMainWindow):
    # Signals matching the old ChatWindow for compatibility with main.py
    send_message_signal = pyqtSignal(str, object)
    stop_signal = pyqtSignal()
    ai_response_signal = pyqtSignal(str)
    stream_token_signal = pyqtSignal(str)
    stream_finished_signal = pyqtSignal()
    new_chat_signal = pyqtSignal()
    switch_chat_signal = pyqtSignal(str)
    delete_session_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Cora AI")
        self.setMinimumSize(1000, 750)
        
        self.recognizer = sr.Recognizer() if sr else None
        self.voice_thread = None
        self.is_generating = False
        
        self.init_ui()
        self.apply_styles()
        
        # Connect internal signals
        self.ai_response_signal.connect(self.on_ai_response_start)
        self.stream_token_signal.connect(self.stream_response)
        self.stream_finished_signal.connect(self.finish_response)
        
    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # Sidebar Removed

        
        # Main content
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        
        # Chat display
        self.chat_display = ChatDisplay()
        
        # Input area
        self.input_area = ModernInputArea()
        self.input_area.message_sent.connect(self.handle_send)
        self.input_area.voice_btn.clicked.connect(self.toggle_voice)
        
        content_layout.addWidget(self.chat_display)
        content_layout.addWidget(self.input_area)
        
        # Add to main layout
        # Add to main layout
        # main_layout.addWidget(self.sidebar) 
        main_layout.addWidget(content_widget, 1)
        
    def apply_styles(self):
        self.setStyleSheet("""
            QMainWindow {
                background-color: #0F172A;
            }
            QWidget {
                font-family: 'Segoe UI', 'Arial', sans-serif;
            }
        """)
        
    def handle_send(self, text, attachment=None):
        # Trigger Stop if generating (InputArea sends empty text if stop clicked)
        if self.is_generating:
             print("ChatWindow: Stop Signal Emitted")
             self.stop_signal.emit()
             # Reset UI State manually if backend doesn't acknowledge quickly?
             # No, finish_response handles that.
             return

        # Regular Message
        # Only proceed if text/attachment exists (prevent empty bubbles)
        if text or attachment:
             if text:
                 self.chat_display.add_message(text, is_user=True)
                 
             if attachment:
                 self.chat_display.add_message(f"📎 Attached: {os.path.basename(attachment)}", is_user=True)
                 
             # Emit signal to main.py
             self.set_generating_state(True)
             self.send_message_signal.emit(text, attachment)
        
    def set_generating_state(self, is_generating):
        self.is_generating = is_generating
        if is_generating:
            self.input_area.send_btn.setText("⏹")
            self.input_area.send_btn.setStyleSheet("""
                QPushButton#sendBtn {
                    background-color: #EF4444;
                    color: white;
                    font-size: 18px;
                    font-weight: bold;
                    border-radius: 20px;
                }
                QPushButton#sendBtn:hover {
                    background-color: #DC2626;
                }
            """)
        else:
            self.input_area.send_btn.setText("➤")
            self.input_area.send_btn.setStyleSheet("""
                QPushButton#sendBtn {
                    background-color: #2563EB;
                    color: white;
                    font-size: 18px;
                    font-weight: bold;
                    border-radius: 20px;
                }
                QPushButton#sendBtn:hover {
                    background-color: #1D4ED8;
                }
            """)
        QApplication.processEvents()
            
    def on_ai_response_start(self, initial_text):
        # Start a new bubble for Cora
        self.chat_display.add_message(initial_text, is_user=False)
        self.current_response_bubble = self.chat_display.get_last_bubble()
        
    def stream_response(self, text):
        try:
            # Check if bubble object is still valid C++ side
            if hasattr(self, 'current_response_bubble') and self.current_response_bubble:
                # Double check with internal Qt test if easy, or rely on try/catch
                lbl = self.current_response_bubble.msg_label
                current_text = lbl.text()
                lbl.setText(current_text + text)
                self.chat_display.scroll_to_bottom()
            else:
                # Fallback: Try to get last bubble if it's not user
                last = self.chat_display.get_last_bubble()
                if last and not last.is_user:
                    self.current_response_bubble = last
                    lbl = last.msg_label
                    lbl.setText(lbl.text() + text)
                    self.chat_display.scroll_to_bottom()
        except RuntimeError:
            # Widget deleted (likely session switch), ignore stream
            self.current_response_bubble = None
        except Exception as e:
            print(f"Stream Error: {e}")
                    
    def finish_response(self):
        self.set_generating_state(False)
        self.current_response_bubble = None
        
    # --- Integration Methods for Main.py ---
    
    def start_new_chat(self):
        # We just emit the signal. Main.py will call methods to clear UI via handle_new_chat logic
        # OR main.py expects this method to signal AND clear. 
        # Based on previous fixes, main.py clears UI. But let's be safe.
        self.chat_display.clear()
        self.new_chat_signal.emit()
        
    def switch_chat(self, session_id):
        self.switch_chat_signal.emit(session_id)
        
    def delete_chat(self, session_id):
        self.delete_session_signal.emit(session_id)
        
    def toggle_voice(self):
        if not self.recognizer:
            QMessageBox.warning(self, "Voice Error", "Speech Recognition module not installed.")
            return
            
        if self.voice_thread and self.voice_thread.isRunning():
            self.voice_thread.stop()
            self.input_area.voice_btn.setStyleSheet("color: #94A3B8; border: none; background: transparent; font-size: 18px;")
        else:
            self.input_area.voice_btn.setStyleSheet("color: #EF4444; border: none; background: transparent; font-size: 18px; font-weight: bold;")
            self.voice_thread = VoiceWorker(self.recognizer)
            self.voice_thread.text_ready.connect(lambda t: self.input_area.input_field.insertPlainText(t + " "))
            self.voice_thread.finished.connect(self.on_voice_finished)
            self.voice_thread.start()
            
    def on_voice_finished(self):
        self.input_area.voice_btn.setStyleSheet("color: #94A3B8; border: none; background: transparent; font-size: 18px;")
        
    def load_sessions(self, sessions):
        pass # Sidebar removed

            
    def append_message(self, role, text, is_user=False):
        # This is called by main.py when loading history
        self.chat_display.add_message(text, is_user=is_user)
        
    # Helper to clean/prep markdown text if needed
    def clean_text(self, text):
        return text

    def add_user(self, text):
        self.chat_display.add_message(text, is_user=True)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    
    font = QFont("Segoe UI", 10)
    app.setFont(font)
    
    window = ChatWindow()
    window.show()
    
    sys.exit(app.exec())
