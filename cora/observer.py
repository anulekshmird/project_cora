import time
import mss
import ollama
import threading
from PIL import Image
import io
import os
import config
import json
import re
import context_engine
import ocr_engine
from PyQt6.QtCore import QObject, pyqtSignal

class ObserverSignal(QObject):
    suggestion_ready = pyqtSignal(object) # json payload
    prepare_capture = pyqtSignal()
    finished_capture = pyqtSignal()
    error_resolved = pyqtSignal()

class Observer:
    def __init__(self):
        self.running = False
        self.paused = False
        self.stop_flag = False
        self.signals = ObserverSignal()
        self.model = config.OLLAMA_MODEL 
        self.context_engine = context_engine.ContextEngine()
        self.last_llm_call_time = 0
        
        # Proactive context storage (for grounded suggestion execution)
        self.last_ocr_text = ""
        self.last_proactive_screenshot = None  # bytes
        
        # Session Management
        self.chats_dir = os.path.join(os.getcwd(), "chats")
        if not os.path.exists(self.chats_dir):
            os.makedirs(self.chats_dir)
            
        self.current_session_id = None
        self.chat_history = [] 
        self.create_new_session()

    def create_new_session(self):
        import uuid
        self.current_session_id = str(uuid.uuid4())[:8]
        self.chat_history = []
        print(f"Created new session: {self.current_session_id}")
        self.save_session()

    def switch_session(self, session_id):
        filepath = os.path.join(self.chats_dir, f"{session_id}.json")
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                    self.chat_history = data.get('history', [])
                self.current_session_id = session_id
                print(f"Switched to session: {session_id}")
                return True
            except Exception as e:
                print(f"Error loading session: {e}")
                return False
        return False

    def get_sessions(self):
        sessions = []
        if not os.path.exists(self.chats_dir): return []
        
        for f in os.listdir(self.chats_dir):
            if f.endswith(".json"):
                 sid = f.replace(".json", "")
                 # Load first message as title if poss?
                 title = f"Chat {sid}"
                 try:
                     with open(os.path.join(self.chats_dir, f), 'r') as file:
                         data = json.load(file)
                         
                         # Priority 1: Saved Title
                         if data.get('title'):
                             title = data['title']
                         else:
                             # Priority 2: First Message Inference (Fallback)
                             hist = data.get('history', [])
                             if hist:
                                 for msg in hist:
                                     if msg['role'] == 'user':
                                         txt = msg['content'].split("USER:")[-1].strip()[:30]
                                         title = txt if txt else title
                                         break
                 except: pass
                 sessions.append({'id': sid, 'title': title})
        return sessions

    def delete_session(self, session_id):
        filepath = os.path.join(self.chats_dir, f"{session_id}.json")
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                print(f"Deleted session: {session_id}")
                
                # If current session deleted, create new one
                if self.current_session_id == session_id:
                    self.create_new_session()
                return True
        except Exception as e:
            print(f"Error deleting session: {e}")
        return False

    def save_session(self):
        if not self.current_session_id: return
        filepath = os.path.join(self.chats_dir, f"{self.current_session_id}.json")
        try:
            # Load existing to preserve title
            # Strip image bytes from history (not JSON serializable)
            clean_history = []
            for msg in self.chat_history:
                clean_msg = {k: v for k, v in msg.items() if k != 'images'}
                clean_history.append(clean_msg)
            
            data = {'id': self.current_session_id, 'history': clean_history}
            if os.path.exists(filepath):
                 with open(filepath, 'r') as f:
                     existing = json.load(f)
                     if 'title' in existing:
                         data['title'] = existing['title']
            
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Error saving session: {e}")

    def stop_chat(self):
        self.stop_flag = True
        print("Stopping generation...")

    def clear_history(self):
        # Instead of clearing, we create a new session
        self.create_new_session()

    # ... (capture_screen, _image_to_bytes, pause, resume, analyze, read_file_content unused changes omitted)



    def capture_screen(self):
        try:
            # 0. Prevent Self-Analysis (Recursion Guard)
            win_title = self.context_engine.get_active_window_title().lower()
            if any(x in win_title for x in ["cora", "assistant", "suggestion"]):
                return None

            # 1. Hide UI (Prevent recursion)
            self.signals.prepare_capture.emit()
            time.sleep(0.3) # Give UI time to vanish
            
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                sct_img = sct.grab(monitor)
                img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                # Downscale for performance, but KEEP READABLE
                img.thumbnail((3000, 3000)) # Increased from 2048 to preserve text
                
            # 2. Restore UI
            self.signals.finished_capture.emit()
            return img
            
        except Exception as e:
            print(f"Screen Capture Error: {e}")
            self.signals.finished_capture.emit() # Always restore
            return None

    def _image_to_bytes(self, image):
        if not image: return None
        with io.BytesIO() as output:
            image.save(output, format='PNG') # PNG is lossless, better for text
            return output.getvalue()

    def pause(self):
        self.paused = True
        print("Observer Paused for Chat.")

    def resume(self):
        self.paused = False
        print("Observer Resumed.")

    def analyze(self, image_data, context_text=""):
        if self.paused or not image_data: return None
        
        # Self-analysis guard: skip if current window is Cora UI
        try:
            win_title = self.context_engine.get_active_window_title().lower()
            if any(kw in win_title for kw in ["cora", "assistant", "suggestion"]):
                return None
        except:
            pass
        
        # Convert to bytes if PIL Image
        # Convert to bytes if PIL Image
        if not isinstance(image_data, bytes):
            image_data = self._image_to_bytes(image_data)
        
        # -----------------------------------------------------------------
        # HYBRID PERCEPTION: OCR + VISION
        # -----------------------------------------------------------------
        ocr_text = ""
        try:
             # Re-convert bytes back to PIL for OCR (inefficient but safe for now)
             ocr_img = Image.open(io.BytesIO(image_data))
             ocr_text = ocr_engine.extract_text(ocr_img)
             if len(ocr_text) < 20: 
                 ocr_text = "" # Ignore noise
             else:
                 # Truncate to avoid context overflow (first 2000 chars relevant for context)
                 ocr_text = ocr_text[:2000] 
        except Exception as e:
             print(f"OCR Pipeline Error: {e}")
        
        # Store for suggestion execution pipeline
        self.last_ocr_text = ocr_text
        self.last_proactive_screenshot = image_data

        # Add Context to Prompt
        # Add Context to Prompt
        full_prompt = f"""
        You are a grounded screen assistant.

        ACTIVE APP: {context_text}

        OCR TEXT:
        {ocr_text}

        RULES:
        - Base response ONLY on OCR text.
        - If OCR empty → return confidence 0.
        - Detect errors, writing improvements, or code issues.
        - Suggest EXACT action.

        OUTPUT JSON:
        {{
         "reason": "Specific grounded observation",
         "confidence": 0-1,
         "suggestions": [{{"label": "Action", "hint": "Specific action"}}]
        }}
        """
        
        try:
            # Rate Limiting
            now = time.time()
            if now - self.last_llm_call_time < 1.5:
                return None
            self.last_llm_call_time = now

            # Use general SYSTEM_PROMPT for visual analysis (Productivity/Terminal)
            response = ollama.chat(model=self.model, messages=[
                {'role': 'system', 'content': config.SYSTEM_PROMPT},
                {'role': 'user', 'content': full_prompt, 'images': [image_data]}
            ])
            text = response['message']['content'].strip()
            print(f"DEBUG: RAW OBSERVER OUT: {text[:100]}...") # Limit log

            # Clean JSON
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            
            # Loose JSON fix
            if not text.endswith("}"): 
                 idx = text.rfind("}")
                 if idx != -1: text = text[:idx+1]

            payload = json.loads(text)
            payload["screen_context"] = ocr_text
            return payload
        except Exception as e:
            # print(f"Observer Analyze Error: {e}")
            return None
            # print(f"Ollama Analyze Error: {e}") 
            return None

    def update_session_title(self, session_id, user_text):
        if not user_text: return
        try:
            # Generate a short 3-5 word title
            prompt = f"Summarize this user query into a short 3-5 word title: '{user_text}'. Return ONLY the title, no quotes."
            response = ollama.chat(model=self.model, messages=[
                {'role': 'user', 'content': prompt}
            ])
            title = response['message']['content'].strip().replace('"', '')
            
            # Save the new title
            filepath = os.path.join(self.chats_dir, f"{session_id}.json")
            if os.path.exists(filepath):
                with open(filepath, 'r+') as f:
                    data = json.load(f)
                    data['title'] = title
                    f.seek(0)
                    json.dump(data, f, indent=2)
                    f.truncate()
            print(f"Session {session_id} renamed to: {title}")
            return title
        except Exception as e:
            print(f"Title Generation Error: {e}")
            return None

    def read_file_content(self, path):
        try:
            if not path: return None
            _, ext = os.path.splitext(path)
            ext = ext.lower()
            
            # 1. Try PDF
            if ext == '.pdf':
                try:
                    import pypdf
                    reader = pypdf.PdfReader(path)
                    text = ""
                    for page in reader.pages[:10]: # Increased page limit to 10
                         extract = page.extract_text()
                         if extract:
                             text += extract + "\n"
                    
                    # FALLBACK: If text extraction failed (Scanned PDF), use OCR
                    if len(text.strip()) < 50:
                        try:
                            # Try PDF -> Image -> OCR Strategy
                            from pdf2image import convert_from_path
                            import pytesseract
                            
                            print("PDF is likely scanned. Attempting OCR...")
                            images = convert_from_path(path, first_page=1, last_page=3)
                            ocr_text = ""
                            for img in images:
                                ocr_text += pytesseract.image_to_string(img) + "\n"
                                
                            if len(ocr_text.strip()) > 50:
                                return f"[OCR EXTRACTED FROM SCANNED PDF]:\n{ocr_text}"
                        except Exception as ocr_e:
                            print(f"OCR Fallback Failed: {ocr_e}")
                            
                        return f"[WARNING: Extracted text from PDF is very short ({len(text)} chars). The PDF might be scanned. Please open it on your screen so I can see it.]"
                        
                    print(f"PDF Parsing Success: {len(text)} chars extracted.")
                    return text
                except ImportError:
                    return f"[PDF detected at {path}. Install 'pypdf' (and optional 'pdf2image', 'pytesseract') to read content.]"
                except Exception as e:
                    return f"[Error reading PDF: {e}]"

            # 2. Text/Code
            valid_exts = ['.txt', '.py', '.md', '.json', '.html', '.css', '.js', '.csv', '.bat', '.sh', '.xml', '.yaml', '.yml', '.ini', '.log']
            if ext not in valid_exts:
                return f"[File type '{ext}' not currently supported for deep analysis, but path is: {path}]"
            
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read(50000) # Increased char limit
                return content
        except Exception as e:
            return f"[Error reading file: {e}]"

    def stream_chat_with_screen(self, user_query, attachment=None, proactive_context=None):
        self.stop_flag = False
        try:
            image_bytes = None
            current_images = []
            
            # 1. Fetch OS Context (Active Window, File)
            os_context = self.context_engine.get_context_snapshot()
            window_title = os_context.get('window_title', 'Unknown')
            mode_primary = os_context.get('mode_primary', os_context.get('mode', 'general'))
            
            print(f"Context: {window_title} ({mode_primary})")
            
            # 2. Prepare Base Content
            prompt_context = ""
            
            # ---------------------------------------------------------------
            # PROACTIVE CONTEXT INJECTION (Grounded Suggestion Execution)
            # If a proactive context is provided, use it instead of capturing
            # fresh screen. This ensures suggestion clicks stay grounded.
            # ---------------------------------------------------------------
            if proactive_context:
                print("Using stored proactive context (grounded suggestion execution).")
                pc_mode = proactive_context.get('mode_primary', mode_primary)
                pc_window = proactive_context.get('window_title', window_title)
                pc_reason = proactive_context.get('reason', '')
                pc_ocr = proactive_context.get('ocr_text', '')
                pc_screenshot = proactive_context.get('screenshot', None)
                pc_error_ctx = proactive_context.get('error_context', '')
                pc_error_file = proactive_context.get('error_file', '')
                pc_error_msg = proactive_context.get('error_message', '')
                pc_file_content = proactive_context.get('file_content', '')
                
                # Build grounded command prompt
                prompt_context = f"""\n\n[COMMAND MODE: Suggestion Execution]

ACTIVE APP: {pc_window}
MODE: {pc_mode}

DETECTED CONTEXT:
{pc_reason}
"""
                if pc_error_ctx:
                    prompt_context += f"""\nERROR DETAILS:
File: {pc_error_file}
Error: {pc_error_msg}
Code:\n{pc_error_ctx}
"""
                if pc_file_content:
                    prompt_context += f"""\n[ACTIVE FILE CONTENT]:\n{pc_file_content}\n[END FILE]
"""
                if pc_ocr:
                    prompt_context += f"""\nOCR TEXT (from screen):
{pc_ocr[:2000]}
"""
                prompt_context += """\nTASK:
Execute the suggestion on the detected screen content.
Do not say you cannot see the screen.
Act as if this context is visible.\n"""
                
                # Use stored screenshot if available
                if pc_screenshot:
                    current_images.append(pc_screenshot)
                    print("Proactive context: Using stored screenshot.")
                
                # Use mode from proactive context for system prompt selection
                mode_primary = pc_mode
            
            # 3. Handle Attachment vs OS Context
            elif attachment:
                print(f"Reading attachment: {attachment}")
                
                # Check directly for Image attachment
                _, ext = os.path.splitext(attachment)
                if ext.lower() in ['.png', '.jpg', '.jpeg', '.bmp', '.gif']:
                     print("Image attachment detected. Loading for vision context.")
                     try:
                         with open(attachment, "rb") as f:
                             image_bytes = f.read()
                             current_images.append(image_bytes)
                         prompt_context = f"\n[User has attached an image: {os.path.basename(attachment)}]\n"
                     except Exception as e:
                         prompt_context = f"\n[Error loading attached image: {e}]\n"
                         
                else:
                    # Try reading text/pdf content
                    content = self.read_file_content(attachment)
                    prompt_context = f"\n\n[PRIORITY CONTEXT - ATTACHED FILE: {os.path.basename(attachment)}]:\n{content}\n[END FILE]\n"
                    
                    if content.strip().startswith("[WARNING") or content.strip().startswith("[Error"):
                        print("Text extraction insufficient. Falling back to Screen Capture.")
                        img = self.capture_screen()
                        cap_bytes = self._image_to_bytes(img)
                        if cap_bytes: current_images.append(cap_bytes)
                    else:
                        print("STRICT PRIORITY: Using Attachment Content (Text Extracted).")
            
            elif mode_primary == 'developer' and os_context.get('file_content'):
                 # 4. Developer Mode: Use File Content provided by Context Engine
                 print(f"Developer Mode detected. Using active file: {os_context['file_path']}")
                 prompt_context = f"\n\n[OS CONTEXT - ACTIVE FILE]:\n{os_context['file_content']}\n[END FILE]\n"
                 
                 vision_keywords = ["look", "see", "screen", "visual", "watch", "view", "active window", "what is this", "screenshot"]
                 if any(k in user_query.lower() for k in vision_keywords):
                     print("Developer Mode: Vision keywords detected. Overriding strict text-only.")
                     img = self.capture_screen()
                     image_bytes = self._image_to_bytes(img)
                     if image_bytes: current_images.append(image_bytes)
                 else:
                     print("Skipping screen capture (Code Context Provided).")

            else:
                # 5. General/Chat Mode
                vision_keywords = ["look", "see", "screen", "visual", "watch", "view", "active window", "what is this", "screenshot", "observe", "check", "debug", "fix"]
                is_short_query = len(user_query.split()) < 5
                
                if any(k in user_query.lower() for k in vision_keywords) or is_short_query:
                    print("Visual keywords or short query detected. Activating Vision Mode.")
                    print("Capturing screen for visual context...")
                    img = self.capture_screen()
                    if img:
                        image_bytes = self._image_to_bytes(img)
                        if image_bytes: current_images.append(image_bytes)
                else:
                    print("Reactive Mode: Text Only (Specific Query).")

            # 6. Select System Prompt based on mode_primary
            if mode_primary == 'developer':
                system_prompt = config.DEV_SYSTEM_PROMPT
            elif mode_primary == 'writing':
                system_prompt = config.PRODUCTIVITY_SYSTEM_PROMPT
            elif mode_primary == 'reading':
                system_prompt = config.READING_SYSTEM_PROMPT
            else:
                system_prompt = config.CHAT_SYSTEM_PROMPT

            print(f"Streaming ({self.model})...")
            
            # 7. Construct History-Aware Message
            user_content = f"{prompt_context}\nUSER: {user_query}"
            
            new_message = {'role': 'user', 'content': user_content}
            
            # Ensure proper image handling for Ollama
            if current_images: 
                # Ollama expects list of base64 strings OR bytes.
                # Since _image_to_bytes returns bytes, and we read file as bytes, we are consistent.
                new_message['images'] = current_images
                
            self.chat_history.append(new_message)
            
            # Generate Title if First Message
            if len(self.chat_history) == 1:
                t = threading.Thread(target=self.update_session_title, args=(self.current_session_id, user_query), daemon=True)
                t.start()
            
            # 8. Send to LLM
            messages_payload = [{'role': 'system', 'content': system_prompt}] + self.chat_history
            stream = ollama.chat(model=self.model, messages=messages_payload, stream=True)

            full_response = ""
            for chunk in stream:
                if self.stop_flag: break
                token = chunk['message']['content']
                full_response += token
                yield token
            
            self.chat_history.append({'role': 'assistant', 'content': full_response})
            self.save_session()

        except Exception as e:
            print(f"Stream Error: {e}")
            yield f"[Error: {e}]"

    def loop(self):
        print("Observer started (Silent Mode)...")
        self.running = True
        self.last_reported_error_sig = None
        self.loop_count = 0
        
        while self.running:
            if self.paused:
                time.sleep(1)
                continue

            try:
                # ----------------------------------------------
                # 1. Proactive OS Monitoring (Lightweight)
                # ----------------------------------------------
                ctx = self.context_engine.get_context_snapshot()
                
                # Check for Syntax Errors
                if ctx.get('error'):
                    sig = ctx['error_signature']
                    if sig != self.last_reported_error_sig:
                        # NEW ERROR DETECTED!
                        print(f"🚨 New Syntax Error: {ctx['error']['message']} in {os.path.basename(ctx['error']['file'])}")
                        
                        # Generate Fix Suggestions via LLM (Silent)
                        # We use the existing analyze flow but inject the specific error context
                        error_prompt = f"""
                        SYNTAX ERROR DETECTED:
                        File: {ctx['error']['file']}
                        Line: {ctx['error']['line']}
                        Error: {ctx['error']['message']}
                        Code:
                        {ctx['error']['context']}
                        
                        Provide a brief fix explanation and the corrected code block.
                        Format as JSON: {{ "reason": "Explanation", "code": "Corrected Code", "confidence": 1.0 }}
                        """
                        
                        # Call LLM
                        response = ollama.chat(model=self.model, messages=[
                             {'role': 'system', 'content': config.DEV_SYSTEM_PROMPT},
                             {'role': 'user', 'content': error_prompt}
                        ])
                        
                        # Parse
                        text = response['message']['content'].strip()
                         # Clean JSON
                        if "```json" in text:
                            text = text.split("```json")[1].split("```")[0].strip()
                        elif "```" in text:
                            text = text.split("```")[1].split("```")[0].strip()
                        
                        try:
                            payload = json.loads(text)
                            payload['type'] = 'syntax_error' # Mark for UI
                            payload.setdefault('screen_context', '')
                            payload.setdefault('error_context', '')
                            payload.setdefault('suggestions', [])
                            self.signals.suggestion_ready.emit(payload)
                            self.last_reported_error_sig = sig # Mark handled
                        except:
                            pass
                            
                # 2. Visual Monitoring (Fallback)
                # Now inclusive of 'developer' mode for unsaved changes/logic errors
                # But throttled to avoid excessive LLM calls
                check_visual = False
                
                if ctx.get('mode_primary', ctx.get('mode')) in ['terminal', 'general']:
                    check_visual = True
                elif ctx.get('mode_primary', ctx.get('mode')) == 'developer':
                    check_visual = False

                if check_visual:
                     # Only check visual if we haven't seen a file error recently
                     # And if we haven't reported a visual suggestion recently
                     img = self.capture_screen()
                     payload = self.analyze(img) 
                     
                     if payload:
                         reason = payload.get('reason', '')
                         confidence = payload.get('confidence', 0.0)

                         # FILTER 1: Self-Reflection Prevention
                         if "Cora" in reason or "AI" in reason or "Ui" in reason:
                             pass # Skip
                         
                         # FILTER 2: Low Confidence Prevention
                         elif confidence < config.PROACTIVE_THRESHOLD:
                             pass # Skip low confidence
                         else:
                             # FILTER 3: De-Duplication
                             visual_sig = f"{reason}:{payload.get('suggestions', [])}"
                             
                             if visual_sig != self.last_reported_error_sig:
                                 print(f"✨ Visual Suggestion: {reason}")
                                 self.signals.suggestion_ready.emit(payload)
                                 self.last_reported_error_sig = visual_sig
                
                self.loop_count += 1
            except Exception as e:
                print(f"Observer Loop Error: {e}")
            
            # Wait for next cycle
            time.sleep(config.CHECK_INTERVAL)

    def stop(self):
        self.running = False
