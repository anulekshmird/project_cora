import sys
import os
import threading
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PyQt6.QtGui import QIcon, QAction
from PyQt6.QtCore import QObject, pyqtSignal, QTimer
import observer
import ui_overlay
import chat_window
from ocr_engine import extract_text_for_window
from system_observer import SystemObserver, SystemEvent
from context_extractor import ContextExtractor
from context_manager import ContextManager
from ai_engine import AIEngine

from dotenv import load_dotenv
load_dotenv()

# Try importing keyboard, fallback if missing
try:
    import keyboard
except ImportError:
    print("Keyboard library not found. Hotkeys disabled.")
    keyboard = None

class ShortcutListener(QObject):
    activated = pyqtSignal()
    exit_triggered = pyqtSignal()
    pick_triggered = pyqtSignal()
    
    def __init__(self):
        super().__init__()
        self._last_ctrl_time = 0
        self._tap_count = 0
        
    def start(self):
        if keyboard:
            try:
                # Register Ctrl+Shift+Q to toggle chat
                keyboard.add_hotkey('ctrl+shift+q', self.on_hotkey)
                # Register Ctrl+Shift+E to exit app
                keyboard.add_hotkey('ctrl+shift+e', self.on_exit_hotkey)
                
                # Use on_press for double-tap detection (more reliable than hook on some systems)
                keyboard.on_press(self._on_key_event)
                
                print("Global Shortcuts Registered: Double-tap Ctrl (Pick), Ctrl+Shift+Q (Chat), Ctrl+Shift+E (Exit)")
                print("--- Keyboard Listener Active ---")
            except Exception as e:
                print(f"Failed to register hotkey: {e}")

    def _on_key_event(self, event):
        
        if 'ctrl' in event.name.lower():
            import time
            now = time.time()
            
            # Avoid repeat-bounce (if user holds key)
            if (now - self._last_ctrl_time) < 0.1:
                return

            if (now - self._last_ctrl_time) < 0.5: # 500ms window
                self._tap_count += 1
            else:
                self._tap_count = 1
            
            self._last_ctrl_time = now
            print(f"DEBUG: Ctrl detected (Count={self._tap_count})")
            
            if self._tap_count >= 2:
                print("[HOTKEY] DOUBLE-TAP CTRL TRIGGERED!")
                self._tap_count = 0
                self.pick_triggered.emit()
        else:
            # If any other key is pressed between taps, reset
            self._tap_count = 0
                
    def on_hotkey(self):
        self.activated.emit()

    def on_exit_hotkey(self):
        self.exit_triggered.emit()

    def on_pick_hotkey(self):
        self.pick_triggered.emit()

class CoraApp(QObject):
    _suggestion_ready_signal = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        
        # Load Icon (Support both png formats just in case)
        self.icon = QIcon("icon.png")
        
        # UI Bubble (Proactive)
        self.bubble = ui_overlay.ProactiveBubble()
        self._picker_instance = None  # keep strong reference to prevent GC
        
        # UI Chat Window (Reactive)
        self.chat_win = chat_window.ChatWindow()
        self.chat_win.setWindowIcon(self.icon)
        
        self.is_chat_active = False
        
        # Shortcut Handler
        self.shortcut = ShortcutListener()
        self.shortcut.activated.connect(self.toggle_chat_thread_safe)
        self.shortcut.exit_triggered.connect(self.quit_app)
        self.shortcut.start()
        
        # Group Picker Interactions
        self.shortcut.pick_triggered.connect(self.start_pick_to_ask)
        
        # Layer 1 — System Observer
        self.sys_observer = SystemObserver()

        # Layer 2 — Context Extractor
        self.ctx_extractor = ContextExtractor(
            ocr_engine=extract_text_for_window
        )

        # Layer 3 — Context Manager
        self.ctx_manager = ContextManager()

        # Layer 4 — AI Engine
        self.ai_engine = AIEngine(model_name="models/gemini-2.5-flash")

        # Layer 5 — UI (existing bubble and chat_win)

        # ── Layer wiring ──────────────────────────────────────────────────────
        # Layer 1 → Layer 2
        self.sys_observer.event_emitted.connect(self._on_system_event)

        # Layer 4 → Layer 5
        self.ai_engine.suggestion_ready.connect(self._on_suggestion_ready)
        self.ai_engine.stream_chunk.connect(self.chat_win.append_stream_chunk)
        self.ai_engine.stream_done.connect(self.chat_win.on_stream_done)

        # Layer 3 → Layer 5 (Reactive UI)
        self.ctx_manager.context_updated.connect(self._on_context_updated)
        self.ctx_manager.context_updated.connect(lambda ctx: self.bubble.set_context_status(ctx))

        # UI actions → pipeline
        self.bubble.dismissed.connect(self._on_dismissed)
        self.bubble.ask_cora_clicked.connect(self._on_chip_clicked)
        self.bubble.pick_requested.connect(self.start_pick_to_ask)
        # self.bubble.refresh_requested.connect(self.handle_manual_refresh)

        # Chat window → AI engine
        self.chat_win.send_message_signal.connect(self._on_chat_message_sent)
        self.chat_win.stop_signal.connect(self._on_stop_requested)
        self.chat_win.closed_signal.connect(self._on_chat_closed)

        print("CORA: All signals wired.")
        self._suggestion_ready_signal.connect(self._generate_suggestion_for_ctx)

        # Observer Thread (Legacy kept for now but not used in wiring)
        self.observer = observer.Observer()
        
        # Bridge Server (Legacy kept for context engine access)
        import bridge_server
        self.bridge_server = bridge_server.BridgeServer(self.observer.context_engine)
        self.bridge_server.start()

        # Start observer
        self.sys_observer.start()
        print("CORA: Event-driven pipeline started.")

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
        
        # Heartbeat state
        self._last_suggestion_window = ""
        self._suggestion_cooldown    = 2.0  # seconds between background suggestions
        self._last_context_hash      = None
        self._last_suggestion_time   = 0
        
        # 1. STARTUP: Enter Idle Mode
        # Ensure the bubble is visible as a passive observer
        QTimer.singleShot(1000, lambda: self.bubble.enter_idle_mode())

        # Heartbeat timer for periodic suggestions
        self._obs_timer = QTimer(self.app)
        self._obs_timer.setInterval(5000)  # 5 seconds
        self._obs_timer.timeout.connect(self._observe_tick)
        
        # Delay start until event loop is running (FIX 1)
        QTimer.singleShot(3000, self._start_observation)
        
        # Load initial history
        self.refresh_sessions()
        self.is_chat_active = False

    def _start_observation(self):
        """Called 3s after launch — starts heartbeat on main thread."""
        self._obs_timer.start()
        print("[OBSERVE] Heartbeat started — interval=10s")
        # Fire first tick immediately
        self._observe_tick()

    def start(self):
        # Observer thread is replaced by CopilotController (already started)
        self.app.exec()

    def on_tray_activate(self, reason):
        self.open_chat()

    def toggle_chat_thread_safe(self):
        self.open_chat()

    def open_chat(self):
        if self.chat_win.isVisible():
            self.chat_win.hide()
            self._on_chat_closed()
        else:
            self.is_chat_active = True
            if hasattr(self, 'sys_observer'):
                self.sys_observer.stop() # Pause proactive
            
            self.chat_win.show()
            self.chat_win.activateWindow()
            self.chat_win.raise_()
            
            # Update Mode Indicator
            ctx = self.ctx_manager.get()
            self.chat_win.update_mode_indicator(ctx.mode)

    def handle_manual_refresh(self):
        print("[PIPELINE] Manual refresh requested.")
        # Reset cooldowns and clear last window to force immediate tick
        self._last_suggestion_time = 0
        self._last_suggestion_window = ""
        # Trigger immediate observe tick
        self._observe_tick()

    def _on_chat_closed(self):
        print("Chat closed — resuming suggestions.")
        # Reset cooldown so next tick fires immediately
        self._last_suggestion_time   = 0
        self._last_suggestion_window = ""
        # Fire observation tick after short delay
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(2000, self._observe_tick)

    def _on_chat_sent(self, text, attachment=None):
        """User sent a manual message."""
        ctx = self.ctx_manager.get()
        self.chat_win.set_generating_state(True)
        self.chat_win.on_ai_response_start("…") # Initialize bubble
        self.ai_engine.stream_chat_async(text, ctx, self.chat_win.get_history())

    def _on_stop(self):
        """Stop requested."""
        print("Stop requested.")
        # self.ai_engine needs stop logic if supported

    def _on_context_updated(self, ctx):
        """Handle real-time context changes by updating UI elements immediately."""
        # Update Chat Window header
        if hasattr(self.chat_win, 'set_context'):
            self.chat_win.set_context(ctx)
        
        # update_mode_indicator now takes activity as keyword arg
        self.chat_win.update_mode_indicator(ctx.app, activity=ctx.activity)
        
        # Update Proactive Bubble idle status
        if hasattr(self, 'bubble') and hasattr(self.bubble, 'set_context_status'):
            self.bubble.set_context_status(ctx)
            
            # Ensure bubble stays visible
            if not self.bubble.isVisible():
                self.bubble.show()
                self.bubble.raise_()

            # ONLY enter idle mode if we are NOT currently showing a suggestion or regional info
            is_region = (ctx.source == 'region')
            if self.bubble.orb_state == self.bubble.STATE_IDLE and not self.bubble.is_expanded and not is_region:
                 # Check if we should actually re-enter idle mode (layout might need refresh)
                 pass 
        
        # Force label for idle state
        if ctx.activity == 'idle':
             # Maybe show a special chip or just the idle status
             pass

        # Log to terminal for verification
        print(f"[REACTIVE UI] Activity: {ctx.activity} | App: {ctx.app}")

    def _on_system_event(self, event_type: str, event_data: dict):
        from system_observer import SystemEvent
        import time

        if event_type == SystemEvent.WINDOW_CHANGED:
            title  = event_data.get('window_title', '').strip()
            if not title or len(title) < 2:
                return

            tl = title.lower()
            skip = [
                'cora picker', 'snipping tool', 'task switching',
                'task manager', 'new notification', 'system tray',
                'cora suggestion', 'cora ai',
            ]
            if any(k in tl for k in skip):
                return

            if title == self._last_suggestion_window:
                return

            print(f"[SWITCH] → {title[:60]}")
            self._last_suggestion_window = title
            self._last_suggestion_time   = 0  # reset cooldown

            # IMMEDIATE: Perform a shallow update so chips work right away
            # This doesn't wait for OCR
            shallow_enrich = self.ctx_extractor._classify_and_enrich(title)
            from context_extractor import Context
            shallow_ctx = Context(
                app          = shallow_enrich['app'],
                mode         = shallow_enrich['mode'],
                window_title = title,
                page_title   = shallow_enrich['page_title'],
                file_path    = shallow_enrich['file_path'],
                url          = shallow_enrich['url'],
                activity     = self.ctx_extractor.infer_user_activity({
                    "app": shallow_enrich['app'], 
                    "window_title": title,
                    "page_title": shallow_enrich['page_title']
                }),
                source       = 'window',
                timestamp    = time.time()
            )
            self.ctx_manager.update(shallow_ctx)

            # Show instant app-specific chips immediately
            self._show_instant_chips(title)

            # Then extract full context and update with AI suggestion
            self.ctx_extractor.extract_async(
                'WINDOW_CHANGED',
                {'window_title': title, 'timestamp': time.time()},
                self._on_context_ready_for_suggestion,
            )
            return

        # Region/selection events
        self.ctx_extractor.extract_async(
            event_type,
            event_data,
            self._on_context_ready_for_suggestion,
        )

    def _show_instant_chips(self, title: str):
        """Show instant chips for the new window while AI processes."""
        from PyQt6.QtCore import QTimer
        import re
        tl = title.lower()
        
        app_name = "Unknown App"
        doing    = "Observing activity..."
        chips    = []

        if any(k in tl for k in ['word', '.docx', 'document', 'writer']):
            app_name = "Microsoft Word"
            match = re.search(r'([a-zA-Z0-9_\-\s]+\.docx?)', title)
            fname = match.group(1) if match else "document"
            doing = f"Writing {fname}"
        elif any(k in tl for k in ['chrome', 'edge', 'firefox', 'opera', 'brave']):
            app_name = "Web Browser"
            if "youtube" in tl: app_name = "YouTube"
            elif "github" in tl: app_name = "GitHub"
            elif "whatsapp" in tl: app_name = "WhatsApp"
            
            # Use smarter parsing for page title
            parts = re.split(r'\s*[-—|]\s*', title)
            clean_page = parts[0] if parts else ""
            
            # If it's a search
            if "google search" in tl:
                doing = f"Searching: {clean_page}"
            elif "youtube" in tl:
                if len(parts) > 1 and "youtube" in parts[-1].lower():
                    doing = f"Watching: {' - '.join(parts[:-1])}"
                else:
                    doing = "Watching video"
            elif "github" in tl:
                doing = f"Browsing: {clean_page}"
            else:
                skip_titles = ['google chrome', 'new tab', 'untitled', 'google search', 'youtube', 'microsoft edge', 'mozilla firefox', 'brave', 'opera']
                if not clean_page or any(s in clean_page.lower() for s in skip_titles):
                    doing = "Browsing web content"
                else:
                    doing = f"Reading: {clean_page}"
        elif any(k in tl for k in ['code', 'vscode', 'pycharm', '.py', '.js', '.ts', '.html', '.css', '.txt', '.md', '.json', '.yaml', '.yml', '.rs', '.go', '.cpp', '.h', '.requirements']):
            app_name = "VS Code" if ("code" in tl or "vscode" in tl) else "Code Editor"
            # Smarter regex to find filename with extension
            match = re.search(r'([a-zA-Z0-9_\-\.\+]+\.[a-zA-Z0-9]{1,10})', title)
            fname = match.group(1) if match else "document"
            doing = f"Editing {fname}"
        elif '.pdf' in tl:
            app_name = "PDF Reader"
            match = re.search(r'([a-zA-Z0-9_\-\s]+\.pdf)', title)
            fname = match.group(1) if match else "document"
            doing = f"Reading {fname}"
        elif app_name == "idle" or not title or any(k in tl for k in ['taskbar', 'system tray', 'desktop', 'program manager']):
            app_name = "Desktop"
            doing    = "Resting"
            chips    = [
                {"label": "Need any help?", "hint": "Ask Cora for assistance"},
                {"label": "Check Reminders", "hint": "See if you have tasks"},
                {"label": "What can you do?", "hint": "Learn about Cora's features"},
            ]
        else:
            return  # No instant chips for unknown apps

        payload = {
            "type":        "general",
            "reason":      f"<b>You’re in:</b> {app_name}<br><b>Looks like:</b> {doing}",
            "reason_long": f"Cora noticed you're {doing.lower()} and is ready to help.",
            "confidence":  0.7,
            "suggestions": chips,
        }
        # Force show bubble instantly
        self.bubble.show()
        QTimer.singleShot(0, lambda: self.bubble.show_suggestion(payload))


    def _observe_tick(self):
        import time
        import pygetwindow as gw

        try:
            win            = gw.getActiveWindow()
            current_window = win.title.strip() if win else ""
        except Exception as e:
            print(f"[OBSERVE] Error: {e}")
            return

        print(f"[OBSERVE] Tick: '{current_window[:60]}'")

        if not current_window or len(current_window.strip()) < 2:
            print("[OBSERVE] Empty window title — skipping")
            return

        win_lower = current_window.lower()

        # Hard skip list
        skip = [
            'cora picker',
            'snipping tool',
            'task switching',
            'task manager', 
            'new notification',
            'system tray overflow',
            'cora suggestion',
            'cora ai',
        ]
        if any(k in win_lower for k in skip):
            print(f"[OBSERVE] Skipping: {current_window[:40]}")
            return

        # Skip if chat is open
        if self.chat_win.isVisible():
            print("[OBSERVE] Chat open — skipping")
            return

        now             = time.time()
        window_changed  = (current_window != self._last_suggestion_window)
        cooldown_ok     = (now - self._last_suggestion_time) > self._suggestion_cooldown

        if not window_changed:
            # ONLY trigger periodic updates if the window changed.
            # This prevents Cora from constantly refreshing on the same page.
            return

        # Window CHANGED: Reset stability hash to ensure we get a fresh analysis for the new page
        self._last_context_hash = None

        if not cooldown_ok:
            return

        # Don't trigger background suggestions if user is looking at a current suggestion or picked region
        if self.bubble.orb_state != self.bubble.STATE_IDLE:
            return

        print(f"[OBSERVE] → Extracting context for: {current_window[:50]}")
        self._last_suggestion_window = current_window
        self._last_suggestion_time   = now

        self.ctx_extractor.extract_async(
            'WINDOW_CHANGED',
            {
                'window_title': current_window,
                'timestamp':    now,
                'use_window_capture': True,  # hint to use window-only capture
            },
            self._on_context_ready_for_suggestion,
        )

    def _on_context_ready_for_suggestion(self, ctx):
        """Called from background thread — use signal to reach main thread."""
        print(f"[PIPELINE] Context ready: app={ctx.app} text={len(ctx.best_text())}ch")
        self._suggestion_ready_signal.emit(ctx)

    def _generate_suggestion_for_ctx(self, ctx):
        # Silence verbose background extraction logs
        # print(f"[PIPELINE] Generating: app={ctx.app} text={len(ctx.best_text())}ch")

        if self.chat_win.isVisible():
            return

        hard_skip = ('antigravity',)
        if ctx.app in hard_skip and not ctx.best_text():
            return

        best = ctx.best_text()
        if not best and ctx.window_title:
            from context_extractor import Context
            ctx.visible_text = f"Active window: {ctx.window_title}"
            best = ctx.visible_text

        # Synchronously sync to ContextManager so it can purge stale state (like old regions)
        # We do this BEFORE any skips so the State of Cora is always accurate to the Screen.
        self.ctx_manager.update(ctx)

        # Content Stability Check
        curr_hash = ctx.identity_hash()
        is_region = (ctx.source == 'region')
        
        if curr_hash == self._last_context_hash and not is_region:
            print(f"[SUGGEST] Skipping — content unchanged (hash={curr_hash})")
            return
            
        if not best:
            if ctx.source == 'region':
                print("[PIPELINE] No text but regional selection — proceeding with image")
                best = ""
            else:
                print("[PIPELINE] No content — skipping")
                return

        # If we are currently showing a specific regional suggestion, 
        # DO NOT let background window heartbeats overwrite it.
        # BUT if THIS call is regional (user just picked something), proceed ALWAYS.
        is_background = (ctx.source == 'window')
        current_bubble_win = self.bubble.current_data.get('window_title') if self.bubble.current_data else ""
        is_showing_region = (self.bubble.current_data and self.bubble.current_data.get('type') == 'picked_suggestion')
        
        # If showing a region, ONLY skip background heartbeats if it's the SAME window.
        # If the user switched windows, we MUST proceed to show the new window's context.
        if is_background and is_showing_region and ctx.window_title == current_bubble_win:
            # Update window title metadata without changing the actual content
            return

        print(f"[SUGGEST] ✓ Calling AI for app={ctx.app} text={len(best)}ch (hash={curr_hash})")
        self._last_context_hash = curr_hash
        self.ai_engine.generate_suggestion_async(ctx)

    def _on_suggestion_ready(self, payload: dict):
        """Layer 4 → Layer 5: Show suggestion in UI."""
        print(f"[UI] Suggestion ready: {payload.get('reason','')[:50]}")
        self.bubble.show() # Force pop up
        self.bubble.show_suggestion(payload)

    def _on_dismissed(self):
        """User dismissed — clear region context."""
        if hasattr(self, 'ctx_manager'):
            self.ctx_manager.clear_region()

    def _on_chip_clicked(self, label: str, hint: str):
        ctx = self.ctx_manager.get()
        self.chat_win.show()
        if hasattr(self.chat_win, 'set_context'):
            self.chat_win.set_context(ctx)
        # Show only the label as user message — not the full hint/prompt
        self.chat_win.chat_display.add_message(label, is_user=True)
        self.chat_win.set_generating_state(True)
        self.chat_win.ai_response_signal.emit("…")
        history = self.chat_win.get_history()
        # Send hint as the actual instruction to AI but show label in UI
        self.ai_engine.stream_chat_async(hint, ctx, history)


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
            if self.observer.current_session_id == session_id:
                self.chat_win.start_new_chat()
            self.refresh_sessions()

    def refresh_sessions(self):
        sessions = self.observer.get_sessions()
        self.chat_win.load_sessions(sessions)


    def show_last_hint(self):
        self.bubble.show_message(self.last_title, self.last_details)


    def hide_ui_for_capture(self):
        # Store state
        self.was_chat_visible = self.chat_win.isVisible()
        # In new UI, bubble itself is the widget
        self.was_bubble_visible = self.bubble.isVisible()
        
        # Hide logic
        # if self.was_chat_visible:
        #    self.chat_win.hide()  <-- CAUSING BLINKING. User prefers it content visible.
        if self.was_bubble_visible:
            # self.bubble.hide() # UNCOMMENT if user reports capture issues, but preferring visibility
            pass
            
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

    def start_pick_to_ask(self):
        from screen_picker import ScreenPicker
        print("Pick to Ask: Launching...")
        # Hide CORA UI so it doesn't get picked
        self.bubble.hide()
        if self.chat_win.isVisible():
            self.chat_win.hide()
            
        def _launch_picker_delayed():
            self._picker_instance = ScreenPicker(None)
            self._picker_instance.region_selected.connect(self.on_region_picked)
            self._picker_instance.cancelled.connect(self.on_pick_cancelled)
            self._picker_instance.showFullScreen()

        QTimer.singleShot(200, _launch_picker_delayed)

    def on_region_picked(self, x, y, image_bytes, ocr_text):
        from system_observer import SystemEvent
        from context_extractor import Context
        import time
        # show_suggestion handles visibility internally
        
        # IMMEDIATE: Sync regional context to ContextManager so chips work right away
        base = self.ctx_manager.get()
        reg_ctx = Context(
            app           = base.app,
            mode          = base.mode,
            window_title  = base.window_title,
            page_title    = base.page_title,
            file_path     = base.file_path,
            url           = base.url,
            selected_text = ocr_text,
            image         = image_bytes,
            source        = 'region',
            timestamp     = time.time()
        )
        self.ctx_manager.update(reg_ctx)
        
        # Directly trigger AI analysis with this enriched context
        # Skip going back through sys_observer to avoid a redundant/weaker loop
        self._generate_suggestion_for_ctx(reg_ctx)

        # Show immediate "Analyzing..." state
        clean_text = " ".join(ocr_text.split())
        display_text = clean_text[:60] + "..." if len(clean_text) > 60 else clean_text
        if not display_text:
            display_text = "Analyzing visual element..."
        else:
            display_text = f"Analyzing: {display_text}"

        payload = {
            "type":           "picked_suggestion",
            "app":            self.ctx_manager.get().app,
            "activity":       "analyzing_region",
            "reason":         f"Analyzing: {display_text}",
            "reason_long":    "Cora is analyzing the selected region to provide specific suggestions.",
            "confidence":     1.0,
            "suggestions":    [{"label": "Analyzing...", "hint": "Please wait while Cora thinks..."}],
            "window_title":   self.ctx_manager.get().window_title,
            "source":         "region",
        }
        
        QTimer.singleShot(0, lambda: self.bubble.show_suggestion(payload))

    def on_pick_cancelled(self):
        print("Pick to Ask: Cancelled.")
        self.bubble.show()


    def _on_chat_message_sent(self, text: str, attachment):
        if not text.strip() and not attachment:
            return

        ctx     = self.ctx_manager.get()
        history = self.chat_win.get_history()
        self.chat_win.ai_response_signal.emit("…")
        self.chat_win.set_generating_state(True)

        # Handle attachment
        if attachment:
            import threading
            def _send_with_attachment():
                enriched_text = self._read_attachment(attachment, text)
                self.ai_engine.stream_chat_async(enriched_text, ctx, history)
            threading.Thread(target=_send_with_attachment, daemon=True).start()
        else:
            self.ai_engine.stream_chat_async(text, ctx, history)

    def _read_attachment(self, file_path: str, user_message: str) -> str:
        """Read attachment content and prepend to user message."""
        import os
        ext = os.path.splitext(file_path)[1].lower()
        content = ""

        try:
            if ext == '.pdf':
                try:
                    import fitz  # PyMuPDF
                    doc  = fitz.open(file_path)
                    text_content = ""
                    for i in range(min(10, len(doc))):
                        text_content += doc[i].get_text()
                    content = text_content[:6000]
                    print(f"[ATTACH] PDF read: {len(content)}ch from {os.path.basename(file_path)}")
                except ImportError:
                    # Fallback — read as bytes and use OCR
                    print("[ATTACH] PyMuPDF not installed — trying OCR")
                    content = f"[PDF file attached: {os.path.basename(file_path)}]"

            elif ext in ('.txt', '.md', '.py', '.js', '.ts', '.html', '.css', '.json'):
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()[:6000]
                print(f"[ATTACH] Text file read: {len(content)}ch")

            elif ext in ('.docx',):
                try:
                    import docx
                    doc     = docx.Document(file_path)
                    content = '\n'.join([p.text for p in doc.paragraphs])[:6000]
                    print(f"[ATTACH] DOCX read: {len(content)}ch")
                except ImportError:
                    content = f"[Word document attached: {os.path.basename(file_path)}]"

            elif ext in ('.png', '.jpg', '.jpeg', '.webp'):
                # Image — handled via vision if passed to ai_engine
                # For now, just pass the message — vision logic exists in AIEngine._stream_llm
                return user_message

        except Exception as e:
            print(f"[ATTACH] Error reading {file_path}: {e}")
            content = f"[Could not read file: {os.path.basename(file_path)}]"

        if content:
            filename = os.path.basename(file_path)
            return (
                f"FILE: {filename}\n"
                f"{'='*50}\n"
                f"{content}\n"
                f"{'='*50}\n\n"
                f"USER REQUEST: {user_message or 'Please analyze this file.'}"
            )
        return user_message or "Please analyze the attached file."

    def _on_stop_requested(self):
        # Signal AI engine to stop (implement if needed)
        self.chat_win.finish_response()

if __name__ == "__main__":
    cora = CoraApp()
    cora.start()
