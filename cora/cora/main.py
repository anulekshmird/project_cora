import sys
import threading
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PyQt6.QtGui import QIcon, QAction
from PyQt6.QtCore import QObject, pyqtSignal, QTimer
import observer
import ui_overlay
import chat_window

# Try importing keyboard, fallback if missing
try:
    import keyboard
except ImportError:
    print("Keyboard library not found. Hotkeys disabled.")
    keyboard = None

class ShortcutListener(QObject):
    activated = pyqtSignal()
    exit_triggered = pyqtSignal()
    
    def __init__(self):
        super().__init__()
        
    def start(self):
        if keyboard:
            try:
                # Register Ctrl+Shift+Q to toggle chat
                keyboard.add_hotkey('ctrl+shift+q', self.on_hotkey)
                # Register Ctrl+Shift+E to exit app
                keyboard.add_hotkey('ctrl+shift+e', self.on_exit_hotkey)
                print("Global Shortcuts Registered: Ctrl+Shift+Q (Toggle), Ctrl+Shift+E (Exit)")
            except Exception as e:
                print(f"Failed to register hotkey: {e}")
                
    def on_hotkey(self):
        self.activated.emit()

    def on_exit_hotkey(self):
        self.exit_triggered.emit()

class CoraApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        
        # Load Icon (Support both png formats just in case)
        self.icon = QIcon("icon.png")
        
        # UI Bubble (Proactive)
        self.bubble = ui_overlay.ProactiveBubble()
        self.bubble.ask_cora_clicked.connect(self.handle_overlay_action)
        
        # UI Chat Window (Reactive)
        self.chat_win = chat_window.ChatWindow()
        self.chat_win.send_message_signal.connect(self.handle_chat_message)
        self.chat_win.stop_signal.connect(self.handle_stop)
        self.chat_win.new_chat_signal.connect(self.handle_new_chat)
        self.chat_win.switch_chat_signal.connect(self.handle_switch_session)
        self.chat_win.delete_session_signal.connect(self.handle_delete_session)
        self.chat_win.setWindowIcon(self.icon)
        
        self.is_chat_active = False
        
        # Shortcut Handler
        self.shortcut = ShortcutListener()
        self.shortcut.activated.connect(self.toggle_chat_thread_safe)
        self.shortcut.exit_triggered.connect(self.quit_app)
        self.shortcut.start()
        
        # Observer Thread
        self.observer = observer.Observer()
        self.observer.signals.suggestion_ready.connect(self.on_suggestion)
        self.observer.signals.prepare_capture.connect(self.hide_ui_for_capture)
        self.observer.signals.finished_capture.connect(self.restore_ui_after_capture)
        self.observer.signals.error_resolved.connect(self.bubble.hide_bubble)
        
        # Bridge Server (VS Code Integration)
        import bridge_server
        self.bridge_server = bridge_server.BridgeServer(self.observer.context_engine)
        self.bridge_server.start()

        # Copilot Controller (Proactive Loop)
        from copilot_controller import CopilotController
        self.copilot = CopilotController(
            self.observer.context_engine,
            self.observer,
            self.bubble
        )
        self.copilot.start() # Starts QThread monitoring loop

        # State for capture
        self.was_chat_visible = False
        self.was_bubble_visible = False
        
        # System Tray
        self.tray_icon = QSystemTrayIcon(self.icon, self.app)
        self.tray_icon.setToolTip("Cora")
        
        # Tray Interactions
        self.tray_icon.activated.connect(self.on_tray_activate)
        
        # Tray Menu
        self.tray_menu = QMenu()
        
        self.chat_action = QAction("Open Chat", self.app)
        self.chat_action.triggered.connect(self.open_chat)
        self.tray_menu.addAction(self.chat_action)
        
        self.show_hint_action = QAction("Show Last Hint", self.app)
        self.show_hint_action.triggered.connect(self.show_last_hint)
        self.tray_menu.addAction(self.show_hint_action)
        
        self.quit_action = QAction("Exit", self.app)
        self.quit_action.triggered.connect(self.quit_app)
        self.tray_menu.addAction(self.quit_action)
        
        self.tray_icon.setContextMenu(self.tray_menu)
        self.tray_icon.show()

        self.last_title = "Welcome"
        self.last_details = "Cora is running silently."
        
        # 1. STARTUP: Enter Idle Mode
        # Ensure the bubble is visible as a passive observer
        QTimer.singleShot(1000, lambda: self.bubble.enter_idle_mode())
        
        # Optional: Notification
        QTimer.singleShot(1500, lambda: self.tray_icon.showMessage(
             "Cora", 
             "Observer Active. I'm waiting in the corner.", 
             QSystemTrayIcon.MessageIcon.Information, 
             3000
        ))
        
        # Load initial history
        self.refresh_sessions()
        self.is_chat_active = False

    def start(self):
        # Observer thread is replaced by CopilotController (already started)
        sys.exit(self.app.exec())

    def on_tray_activate(self, reason):
        self.open_chat()

    def toggle_chat_thread_safe(self):
        self.open_chat()

    def open_chat(self):
        if self.chat_win.isVisible():
            self.chat_win.hide()
            self.is_chat_active = False
            self.observer.resume()
            self.copilot.resume()
        else:
            self.is_chat_active = True
            self.observer.pause() # Pause proactive
            self.copilot.pause()
            
            self.chat_win.show()
            self.chat_win.activateWindow()
            self.chat_win.raise_()
            
            # Welcome Message Logic (Handled internally by ChatDisplay)
            pass

    def handle_chat_message(self, text, attachment=None):
        print(f"User sent: {text} | File: {attachment}")
        t = threading.Thread(target=self._process_chat, args=(text, attachment))
        t.start()
        
    def handle_stop(self):
        print("Stop requested.")
        self.observer.stop_chat()

    def _process_chat(self, text, attachment=None, proactive_context=None):
        print("Processing chat in background (Streaming)...")
        
        # 1. Create empty AI bubble
        self.chat_win.ai_response_signal.emit("") 
        
        # 2. Stream tokens
        full_response = ""
        for token in self.observer.stream_chat_with_screen(text, attachment, proactive_context=proactive_context):
            full_response += token
            # Update UI incrementally
            self.chat_win.stream_token_signal.emit(token)
            
        print(f"AI Response Complete: {len(full_response)} chars")
        self.chat_win.stream_finished_signal.emit()
        
        # Refresh sidebar to show new chat title if it was new
        QTimer.singleShot(0, self.refresh_sessions)

    # reset_chat removed (replaced by handle_new_chat)

    def handle_new_chat(self):
        print("Creating new session...")
        self.observer.create_new_session()
        # Clear UI without re-emitting signal
        self.chat_win.chat_display.clear()
        self.refresh_sessions()

    def handle_switch_session(self, session_id):
        print(f"Switching session: {session_id}")
        if self.observer.switch_session(session_id):
            # Reload UI
            self.chat_win.chat_display.clear()
            
            # Replay History
            for msg in self.observer.chat_history:
                role = "Cora" if msg['role'] == 'assistant' else "You"
                is_user = (role == "You")
                content = msg.get('content', '')
                if is_user:
                     if "USER:" in content:
                          content = content.split("USER:")[-1].strip()
                self.chat_win.append_message(role, content, is_user)
            self.refresh_sessions()

    def handle_delete_session(self, session_id):
        print(f"Deleting session: {session_id}")
        if self.observer.delete_session(session_id):
            # If current deleted, UI is cleared by observer -> create_new logic roughly, 
            # but we need to ensure UI reflects empty state if current was deleted.
            if self.observer.current_session_id != session_id:
                 pass
            else:
                 self.chat_win.start_new_chat()
            self.refresh_sessions()

    def refresh_sessions(self):
        sessions = self.observer.get_sessions()
        self.chat_win.load_sessions(sessions)

    def on_suggestion(self, payload):
        print(f"Proactive Suggestion: {payload.get('reason')}")

        # Pass the full payload to the bubble to render
        QTimer.singleShot(0, lambda: self.bubble.show_suggestion(payload))

    def show_last_hint(self):
        self.bubble.show_message(self.last_title, self.last_details)

    def handle_overlay_action(self, user_text, internal_prompt):
        print(f"Overlay Action: {user_text}")
        
        # 1. Force Open Chat Window First (Avoid toggling closed if already open)
        if not self.chat_win.isVisible():
             self.open_chat()
        else:
             self.chat_win.activateWindow()
             self.chat_win.raise_()

        # Special Case: Welcome
        # If the ID was "welcome" or prompt is empty, we stop here (Open Chat is done)
        if internal_prompt == "welcome" or internal_prompt == "":
            return

        # 2. Add clean USER FRIENDLY message to UI (hide prompt details)
        self.chat_win.add_user(user_text)
        
        # 3. Grab stored proactive context for grounded chat (FIX 7)
        proactive_ctx = None
        if hasattr(self, 'copilot') and self.copilot.last_proactive_context:
            proactive_ctx = self.copilot.last_proactive_context
            print(f"Grounding chat with proactive context: mode={proactive_ctx.get('mode_primary')}")
        
        # 4. Process the INTERNAL PROMPT in background
        # FORCE BUTTON UPDATE
        self.chat_win.set_generating_state(True)
        t = threading.Thread(target=self._process_chat, args=(internal_prompt,), kwargs={'proactive_context': proactive_ctx})
        t.start()

    def hide_ui_for_capture(self):
        # Store state
        self.was_chat_visible = self.chat_win.isVisible()
        # In new UI, bubble itself is the widget
        self.was_bubble_visible = self.bubble.isVisible()
        
        # Hide logic
        # Hide logic
        # if self.was_chat_visible:
        #    self.chat_win.hide()  <-- CAUSING BLINKING. User prefers it content visible.
        if self.was_bubble_visible:
            self.bubble.hide()
            
    def restore_ui_after_capture(self):
        # Restore state
        if self.was_chat_visible:
            # self.chat_win.show()
            pass
        if self.was_bubble_visible:
            self.bubble.show()

    def quit_app(self):
        self.observer.stop()
        self.app.quit()

if __name__ == "__main__":
    cora = CoraApp()
    cora.start()
