
import time
import json
import os
from PyQt6.QtCore import QThread, pyqtSignal

import config

class CopilotController(QThread):
    def __init__(self, context_engine, observer, overlay):
        super().__init__()
        self.context_engine = context_engine
        self.observer = observer
        self.overlay = overlay
        self.running = False
        self.last_error_signature = None
        self.last_visual_sig = None
        self.loop_count = 0
        self.last_llm_call_time = 0
        
        # Intelligent Dismiss State
        self.dismissed_signatures = set()
        self.snoozed_until = 0.0

        # Connect UI Signals
        self.overlay.dismissed.connect(self.on_user_dismissed)
        self.overlay.snoozed.connect(self.on_user_snoozed)
        
        # State Tracking
        self.last_active_window = None
        self.last_writing_check_time = 0
        
        # Proactive context storage (for grounded suggestion execution)
        self.last_proactive_context = None


    def on_user_dismissed(self):
        # Add current error/visual sig to dismissed
        if self.last_error_signature:
            self.dismissed_signatures.add(self.last_error_signature)
            print(f"Copilot: Dismissed error signature: {self.last_error_signature}")
        
        if self.last_visual_sig:
            self.dismissed_signatures.add(self.last_visual_sig)

    def on_user_snoozed(self, mins):
        self.snoozed_until = time.time() + (mins * 60)
        print(f"Copilot: Snoozed for {mins} minutes.")

    # ... (Start loop remains same) ...

    def process_visual_payload(self, payload):
        reason = payload.get('reason', '')
        confidence = payload.get('confidence', 0.0)
        
        # Filters
        if "Cora" in reason or "AI" in reason: return
        if confidence < config.PROACTIVE_THRESHOLD: return
        
        # Deduplication (Strict)
        sig = f"{reason}:{payload.get('suggestions', [])}"
        
        # Check Dismissed
        if sig in self.dismissed_signatures:
             return

        if sig != self.last_visual_sig:
             self.last_visual_sig = sig
             # Emit only if new
             self.observer.signals.suggestion_ready.emit(payload)

    def pause(self):
        self.paused = True
        print("Copilot Controller: Paused.")

    def resume(self):
        self.paused = False
        print("Copilot Controller: Resumed.")

    def run(self):
        self.start_proactive_loop()

    def start_proactive_loop(self):
        self.running = True
        self.paused = False
        print("Copilot Controller: Proactive Loop Started.")
        
        while self.running:
            try:
                # 0. Check Pause and Snooze
                if self.paused:
                    time.sleep(0.5)
                    continue

                if time.time() < self.snoozed_until:
                    time.sleep(2)
                    continue

                # 1. Get OS/Context Snapshot
                snapshot = self.context_engine.get_context_snapshot()
                current_window = snapshot.get('window_title', '')
                current_mode = snapshot.get('mode', 'unknown') # Backwards compat
                mode_primary = snapshot.get('mode_primary', current_mode)
                mode_secondary = snapshot.get('mode_secondary', 'unknown')
                
                # FIX 2: Skip Cora's own UI (internal mode)
                if mode_primary == "internal":
                    time.sleep(0.2)
                    continue
                
                # Skip Cora suggestion window (not internal, but nothing to analyze)
                cw_lower = (current_window or "").lower()
                if "cora suggestion" in cw_lower:
                    time.sleep(0.5)
                    continue
                
                idle_time = self.context_engine.get_idle_time()
                
                # DEBUG: Pulse Check
                if self.loop_count % 3 == 0:
                    print(f"Copilot Pulse: Mode=[{mode_primary}/{mode_secondary}] Idle=[{idle_time:.1f}s] Window=[{current_window}]")

                # ---------------------------------------------------------
                # A. APP SWITCH PRESENCE MODE
                # ---------------------------------------------------------
                if current_window != self.last_active_window:
                    print(f"Copilot: 🔄 App Switch Detected -> {current_window}")
                    self.last_active_window = current_window
                    
                    # Skip reset if switching TO Cora's own windows
                    cw_lower = current_window.lower() if current_window else ""
                    if cw_lower in ["cora ai", "cora suggestion"]:
                        time.sleep(0.5)
                        continue
                    
                    # Reset visual suggestion state for new window
                    self.observer.signals.error_resolved.emit() # Collapse to idle orb
                    self.last_visual_sig = None
                    # NOTE: Do NOT reset last_error_signature here.
                    # The error signature includes the code text, so it will
                    # naturally update when the user actually fixes the code.
                    # Resetting it here causes re-triggering on every app switch.
                    
                    # Short grace period to let UI settle
                    time.sleep(1.0) 
                    continue

                # ---------------------------------------------------------
                # B. PRIORITY: Check for Errors (Syntax/Runtime)
                # ---------------------------------------------------------
                if snapshot.get("error"):
                    err_sig = snapshot.get("error_signature")
                    
                    # Only trigger if this is a NEW error signature
                    if err_sig != self.last_error_signature:
                        self.last_error_signature = err_sig
                        
                        # Check if this specific error was dismissed
                        if err_sig in self.dismissed_signatures:
                            print(f"Copilot: Skipping dismissed error: {err_sig}")
                        else:
                            self.handle_new_error(snapshot)
                
                # ---------------------------------------------------------
                # C. WRITING MODE (Productivity Suggestion Mode)
                # ---------------------------------------------------------
                elif mode_primary == 'writing':
                    # Ensure we don't stick in "error" state from previous mode
                    if self.last_error_signature:
                         print("Copilot: Mode switched to WRITING. Clearing error state.")
                         self.observer.signals.error_resolved.emit()
                         self.last_error_signature = None
                         self.dismissed_signatures.clear() # Optional: clear dismissed history for fresh start

                    if idle_time > 1.5:
                        # IDLE: Check for suggestions
                        if time.time() - self.last_writing_check_time > 3.0: # Check more frequently (every 3s vs 5s)
                             self.handle_writing_assistance(snapshot)
                             self.last_writing_check_time = time.time()
                    else:
                        # ACTIVE: User is typing
                        # If we have a lingering suggestion, clear it explicitly
                        if self.last_visual_sig is not None:
                             print("Copilot: ⌨️ User resumed typing. Claring suggestion.")
                             self.observer.signals.error_resolved.emit()
                             self.last_visual_sig = None

                # ---------------------------------------------------------
                # D. READING MODE (PDF/E-Book Mode)
                # ---------------------------------------------------------
                elif mode_primary == 'reading':
                    # Similar to Writing, but focused on summaries/explanation
                    # Clear errors
                    if self.last_error_signature:
                         self.observer.signals.error_resolved.emit()
                         self.last_error_signature = None
                    
                    if idle_time > 2.0: # Slightly longer pause for reading
                        # Check less frequently to avoid spamming while user reads
                         if time.time() - self.last_writing_check_time > 10.0: 
                             self.handle_reading_assistance(snapshot)
                             self.last_writing_check_time = time.time()

                # ---------------------------------------------------------
                # ---------------------------------------------------------
                # E. FALLBACK: Visual / Maintenance
                # ---------------------------------------------------------
                else:
                    # No error found currently.
                    
                    # CRITICAL FIX 3: Conservative Hiding
                    should_hide = False
                    if mode_primary == 'developer':
                        should_hide = True # We are in code, but no error found -> Fixed!
                    elif mode_primary == 'general':
                        should_hide = True # Switched context completely
                    
                    if not should_hide:
                        pass # Maintain state
                        
                    # Check if we just resolved an error
                    elif self.last_error_signature:
                        print(f"Copilot: Resolving error. Mode={mode_primary} Title='{current_window}'")
                        self.last_error_signature = None
                        self.dismissed_signatures.clear()
                        self.last_visual_sig = None
                        self.handle_resolution()
                    
                    # Visual Fallback (Only if NOT writing mode, to avoid conflict)
                    # Use Secondary Mode to allow Browser checks but block productive writing
                    elif mode_primary not in ['writing', 'reading']:
                        self.handle_visual_fallback(snapshot)

                # Loop Frequency (Faster: 0.1s for immediate reaction)
                time.sleep(0.1)
                self.loop_count += 1
                
            except Exception as e:
                print(f"Copilot Loop Exception: {e}")
                time.sleep(1) # Prevent busy loop on crash

    def stop(self):
        self.running = False
        self.wait()

    def _build_error_payload(self, error, reason="", code="", payload_type="syntax_error"):
        """Build a guaranteed-valid error payload with all required fields."""
        return {
            "type": payload_type,
            "reason": reason or f"Error: {error.get('message', 'Unknown')}",
            "code": code,
            "suggestions": [{"label": "Fix Error", "hint": "Show corrected code"}],
            "confidence": 1.0,
            "screen_context": "",
            "error_file": error.get('file', ''),
            "error_line": error.get('line', ''),
            "error_message": error.get('message', ''),
            "error_context": error.get('context', '')
        }

    def handle_new_error(self, snapshot):
        error = snapshot['error']
        print(f"Copilot: 🚨 New Error Detected: {error['message']}")
        
        # PHASE 1: Immediate Visual Feedback (includes full error context)
        temp_payload = self._build_error_payload(
            error, 
            reason=f"Analyzing: {error['message']}...",
            code="# Fetching fix..."
        )
        self.observer.signals.suggestion_ready.emit(temp_payload)
        
        # Store proactive context for grounded suggestion execution
        self.last_proactive_context = {
            'mode_primary': snapshot.get('mode_primary', snapshot.get('mode', 'general')),
            'window_title': snapshot.get('window_title', ''),
            'reason': f"Error: {error.get('message', '')}",
            'ocr_text': self.observer.last_ocr_text,
            'screenshot': self.observer.last_proactive_screenshot,
            'error_file': error.get('file', ''),
            'error_line': error.get('line', ''),
            'error_message': error.get('message', ''),
            'error_context': error.get('context', ''),
            'file_content': snapshot.get('file_content', ''),
        }
        
        # Construct Prompt — JSON ONLY, no markdown
        error_prompt = f"""You are a strict debugging assistant.

LANGUAGE: Python

ERROR:
File: {error['file']}
Line: {error['line']}
Message: {error['message']}

CODE:
{error.get('context', '')}

TASK:
1. Identify exact syntax mistake
2. Provide corrected code
3. Keep explanation MAX 1 sentence
4. Do NOT give teaching paragraphs

OUTPUT JSON ONLY:
{{"reason": "short explanation", "code": "corrected code"}}"""

        # DEBUG LOGGING
        print("--- DEBUG PROMPT START ---")
        print(f"Proactive Suggestion: Analyzing: {error['message']}...")
        print(f"Error Context: {error.get('context', '')}")
        print("--- DEBUG PROMPT END ---")

        try:
            import ollama
            
            # Rate Limiting (≥1.5s between calls)
            now = time.time()
            if now - self.last_llm_call_time < 1.5:
                print("Copilot: Rate limit hit. Skipping LLM call.")
                return

            self.last_llm_call_time = now
            print("Copilot: Asking LLM for error fix...")
            response = ollama.chat(
                model=self.observer.model,
                messages=[
                    {'role': 'system', 'content': config.DEV_SYSTEM_PROMPT},
                    {'role': 'user', 'content': error_prompt}
                ]
            )
            text = response['message']['content'].strip()
            print(f"Copilot: LLM Response (Raw): {text[:80]}...")
            
            # Parse JSON
            payload = self._clean_json(text)
            if payload:
                # Merge with guaranteed structure
                final = self._build_error_payload(
                    error,
                    reason=payload.get('reason', error['message']),
                    code=payload.get('code', '')
                )
                print(f"Copilot: Payload created (JSON parsed)")
            else:
                # FALLBACK: JSON parsing failed — use raw text
                print("Copilot: JSON parse failed. Using fallback payload.")
                final = self._build_error_payload(
                    error,
                    reason=f"Fix for: {error['message']}",
                    code=text  # Raw LLM output as code
                )
                final['type'] = 'syntax_error'
            
            # Always emit a valid payload
            self.observer.signals.suggestion_ready.emit(final)
            print("Copilot: Signal emitted: suggestion_ready")
                
        except Exception as e:
            print(f"Copilot LLM Error: {e}")
            # RECOVERY: Emit fallback so UI doesn't freeze
            fallback = self._build_error_payload(
                error,
                reason=f"Error detected: {error['message']}",
                code=f"# LLM call failed: {e}"
            )
            self.observer.signals.suggestion_ready.emit(fallback)

    def handle_resolution(self):
        # Emit signal to hide bubble/overlay
        print("Copilot: Resolving error state via Signal.")
        self.observer.signals.error_resolved.emit()

    def handle_visual_fallback(self, snapshot):
        # Visual check logic (migrated from Observer)
        # Check if mode is appropriate
        mode_primary = snapshot.get('mode_primary', 'general')
        mode_secondary = snapshot.get('mode_secondary', 'unknown')
        should_check = False
        
        # Check Strategy based on Secondary Mode
        if mode_secondary in ['terminal', 'browser', 'unknown']:
            should_check = True
        elif mode_primary == 'general':
            should_check = True
        elif mode_primary in ['developer', 'chat', 'writing', 'reading']:
            # STRICT MODE: Disable visual fallback in clear productive modes
            should_check = False
                
        if should_check:
             # Rate Limiting (shared 1.5s cooldown)
             now = time.time()
             if now - self.last_llm_call_time < 1.5:
                 return

             # Capture via Observer
             img = self.observer.capture_screen()
             win_title = snapshot.get('window_title', 'Unknown').lower()
             
             # Double Check: If active window is Cora UI, ABORT
             cora_keywords = ["cora", "assistant", "suggestion"]
             if any(kw in win_title for kw in cora_keywords):
                 return

             # Analyze
             payload = self.observer.analyze(img, context_text=f"Active Window: {win_title}")
             if payload:
                 # Store proactive context for grounded suggestion execution
                 self.last_proactive_context = {
                     'mode_primary': snapshot.get('mode_primary', snapshot.get('mode', 'general')),
                     'window_title': win_title,
                     'reason': payload.get('reason', ''),
                     'ocr_text': self.observer.last_ocr_text,
                     'screenshot': self.observer.last_proactive_screenshot,
                     'error_file': '', 'error_line': '', 'error_message': '', 'error_context': '',
                     'file_content': '',
                 }
                 self.process_visual_payload(payload)

    def handle_writing_assistance(self, snapshot):
        print("Copilot: ✍️ Writing Pause Detected. Analyzing...")
        try:
             # Rate Limiting (shared 1.5s cooldown)
             now = time.time()
             if now - self.last_llm_call_time < 1.5:
                 return

             # 1. Capture Screen (Productivity App)
             img = self.observer.capture_screen()
             win_title = snapshot.get('window_title', 'Unknown Application')
             
             # 2. Re-use Observer.analyze for robust OCR + Vision + JSON
             print(f"Copilot: Analyzing Writing Context in '{win_title}'...")
             payload = self.observer.analyze(img, context_text=f"User is writing in {win_title}")
             
             # 3. Process
             if payload:
                 print(f"WRITING PAYLOAD: {payload}")
                 confidence = payload.get('confidence', 0.0)
                 
                 # 4. Check Thresholds (Lower for writing)
                 if confidence > config.WRITING_THRESHOLD:
                     payload['type'] = 'writing_suggestion'
                     
                     # Enforce Structure
                     if 'suggestions' not in payload or not payload['suggestions']:
                         payload['suggestions'] = [
                             {"label": "Explain", "hint": "Explain this content"},
                             {"label": "Summarize", "hint": "Summarize this content"}
                         ]

                     # Store proactive context for grounded suggestion execution
                     self.last_proactive_context = {
                         'mode_primary': 'writing',
                         'window_title': win_title,
                         'reason': payload.get('reason', ''),
                         'ocr_text': self.observer.last_ocr_text,
                         'screenshot': self.observer.last_proactive_screenshot,
                         'error_file': '', 'error_line': '', 'error_message': '', 'error_context': '',
                         'file_content': '',
                     }

                     # 5. Deduplicate
                     reason = payload.get('reason', '')
                     sig = f"{reason}"
                     
                     if sig != self.last_visual_sig and sig not in self.dismissed_signatures:
                         self.last_visual_sig = sig
                         print(f"✨ Writing Suggestion: {reason}")
                         self.observer.signals.suggestion_ready.emit(payload)
                 else:
                     print(f"Copilot: Low confidence ({confidence}) writing suggestion.")
                     
        except Exception as e:
            print(f"Copilot Writing Handler Error: {e}")

        
    def _clean_json(self, text):
        """Extract JSON from LLM response. Returns dict or None."""
        try:
            # Strategy 1: Direct parse
            return json.loads(text)
        except:
            pass
        
        try:
            # Strategy 2: Extract from markdown code block
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                # Could be ```python or other — try extracting JSON from first block
                block = text.split("```")[1]
                # If block starts with a language tag, skip it
                if block and block.split('\n')[0].strip().isalpha():
                    block = '\n'.join(block.split('\n')[1:])
                text = block.split("```")[0].strip()
            
            # Strategy 3: Find JSON object boundaries
            start = text.find('{')
            end = text.rfind('}')
            if start != -1 and end != -1 and end > start:
                text = text[start:end+1]
            
            return json.loads(text)
        except:
            return None
    def handle_reading_assistance(self, snapshot):
        print("Copilot: 📖 Reading Pause Detected. Analyzing...")
        try:
             # Rate Limiting (shared 1.5s cooldown)
             now = time.time()
             if now - self.last_llm_call_time < 1.5:
                 return

             # 1. Capture Screen 
             img = self.observer.capture_screen()
             win_title = snapshot.get('window_title', 'Unknown Document')

             # 2. Re-use Observer.analyze for robust OCR + Vision + JSON
             print(f"Copilot: Analyzing Reading Context in '{win_title}'...")
             payload = self.observer.analyze(img, context_text=f"User is reading document: {win_title}")
             
             if payload:
                 print(f"READING PAYLOAD: {payload}")
                 confidence = payload.get('confidence', 0.0)
                 
                 if confidence > 0.6: 
                     payload['type'] = 'reading_suggestion'
                     
                     # Ensure we have robust suggestions list
                     if 'suggestions' not in payload or not payload['suggestions']:
                         payload['suggestions'] = [
                             {"label": "Summarize Page", "hint": "Summarize this visible page"},
                             {"label": "Explain Concepts", "hint": "Explain key concepts on this page"},
                             {"label": "Key Points", "hint": "Extract bullet points"}
                         ]
                     
                     # Store proactive context for grounded suggestion execution
                     self.last_proactive_context = {
                         'mode_primary': 'reading',
                         'window_title': win_title,
                         'reason': payload.get('reason', ''),
                         'ocr_text': self.observer.last_ocr_text,
                         'screenshot': self.observer.last_proactive_screenshot,
                         'error_file': '', 'error_line': '', 'error_message': '', 'error_context': '',
                         'file_content': '',
                     }

                     reason = payload.get('reason', '')
                     sig = f"{reason}"
                     
                     if sig != self.last_visual_sig and sig not in self.dismissed_signatures:
                         self.last_visual_sig = sig
                         print(f"✨ Reading Suggestion: {reason}")
                         self.observer.signals.suggestion_ready.emit(payload)
                 else:
                     print(f"Copilot: Low confidence ({confidence}) reading suggestion.")
                     
        except Exception as e:
            print(f"Copilot Reading Handler Error: {e}")
