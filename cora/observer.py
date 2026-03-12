import time
import mss
import threading

# ollama imported lazily to avoid blocking on startup
def _get_ollama():
    import ollama
    return ollama
from PIL import Image
import io
import os
import config
import json
import re
import context_engine
import ocr_engine
from PyQt6.QtCore import QObject, pyqtSignal
import base64
import hashlib
from datetime import datetime
import docx
from pptx import Presentation


# ---------------------------------------------------------------------------
# Template / placeholder guards
# ---------------------------------------------------------------------------
_TEMPLATE_PATTERNS = [
    r"<condition>",
    r"<your_",
    r"# commands",
    r"# your code",
    r"pass\s*#",
    r"\.\.\.",
]

def _is_template_code(code: str) -> bool:
    for p in _TEMPLATE_PATTERNS:
        if re.search(p, code, re.IGNORECASE):
            return True
    return False


class ObserverSignal(QObject):
    suggestion_ready = pyqtSignal(object)
    prepare_capture  = pyqtSignal()
    finished_capture = pyqtSignal()
    error_resolved   = pyqtSignal()


class Observer:
    def __init__(self):
        self.running   = False
        self.paused    = False
        self.stop_flag = False
        self.signals   = ObserverSignal()
        self.model       = config.OLLAMA_MODEL
        self.text_model  = config.OLLAMA_TEXT_MODEL
        self.context_engine   = context_engine.ContextEngine()
        self.last_llm_call_time = 0

        self.last_ocr_text             = ""
        self.last_proactive_screenshot = None
        self.last_frame_hash           = None
        self.last_screen_hash          = None
        self.proactive_pause           = False
        self.last_reported_error_sig   = None

        self.chats_dir = os.path.join(os.getcwd(), "chats")
        if not os.path.exists(self.chats_dir):
            os.makedirs(self.chats_dir)

        self.current_session_id = None
        self.chat_history       = []
        self.create_new_session()

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

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

    def get_sessions(self):
        sessions = []
        if not os.path.exists(self.chats_dir):
            return []
        for f in os.listdir(self.chats_dir):
            if not f.endswith(".json"):
                continue
            sid   = f.replace(".json", "")
            title = f"Chat {sid}"
            try:
                with open(os.path.join(self.chats_dir, f), 'r') as fh:
                    data = json.load(fh)
                    if data.get('title'):
                        title = data['title']
                    else:
                        for msg in data.get('history', []):
                            if msg['role'] == 'user':
                                txt = msg['content'].split("USER:")[-1].strip()[:30]
                                title = txt if txt else title
                                break
            except Exception:
                pass
            sessions.append({'id': sid, 'title': title})
        return sessions

    def delete_session(self, session_id):
        filepath = os.path.join(self.chats_dir, f"{session_id}.json")
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                print(f"Deleted session: {session_id}")
                if self.current_session_id == session_id:
                    self.create_new_session()
                return True
        except Exception as e:
            print(f"Error deleting session: {e}")
        return False

    def save_session(self):
        if not self.current_session_id:
            return
        filepath = os.path.join(self.chats_dir, f"{self.current_session_id}.json")
        try:
            clean_history = [
                {k: v for k, v in msg.items() if k != 'images'}
                for msg in self.chat_history
            ]
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
        self.create_new_session()

    # ------------------------------------------------------------------
    # Screen capture
    # ------------------------------------------------------------------

    def capture_screen(self, force=False, hide_ui=True):
        try:
            if not force:
                win_title = self.context_engine.get_active_window_title().lower()
                if any(x in win_title for x in ["cora", "assistant", "suggestion", "overlay"]):
                    return None

            if hide_ui:
                self.signals.prepare_capture.emit()
                time.sleep(0.1)

            mode = self.context_engine.get_context_snapshot().get('mode_primary', 'general')

            with mss.mss() as sct:
                monitor = sct.monitors[1]
                region  = monitor

                if mode == "developer":
                    try:
                        import pygetwindow as gw
                        win = gw.getActiveWindow()
                        if win and win.width > 0 and win.height > 0:
                            left   = max(win.left, monitor["left"])
                            top    = max(win.top,  monitor["top"])
                            right  = min(win.left + win.width,  monitor["left"] + monitor["width"])
                            bottom = min(win.top  + win.height, monitor["top"]  + monitor["height"])
                            region = {
                                "top":    int(top),
                                "left":   int(left),
                                "width":  int(right - left),
                                "height": int(bottom - top),
                            }
                    except Exception as e:
                        print(f"Window capture fallback: {e}")

                sct_img = sct.grab(region)
                img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                img.thumbnail((3000, 3000))

            if hide_ui:
                self.signals.finished_capture.emit()
            return img

        except Exception as e:
            print(f"Screen Capture Error: {e}")
            if hide_ui:
                self.signals.finished_capture.emit()
            return None

    def _image_to_bytes(self, image):
        if not image:
            return None
        with io.BytesIO() as output:
            image.save(output, format='PNG')
            return output.getvalue()

    def hash_and_encode_screen(self, image):
        """Encodes image to PNG once, returns (hash, bytes). Never encodes twice."""
        buf = io.BytesIO()
        image.save(buf, format='PNG')
        data = buf.getvalue()
        thumb = image.resize((320, 180))
        tbuf = io.BytesIO()
        thumb.save(tbuf, format='PNG')
        h = hashlib.md5(tbuf.getvalue()).hexdigest()
        return h, data

    # ------------------------------------------------------------------
    # OCR
    # ------------------------------------------------------------------

    def extract_text_from_screen(self, image, file_path: str = "") -> str:
        from ocr_engine import extract_text_for_window
        snapshot     = self.context_engine.get_context_snapshot()
        win_title    = snapshot.get("window_title", "")
        mode_primary = snapshot.get("mode_primary", "general")
        text = extract_text_for_window(
            image        = image,
            window_title = win_title,
            file_path    = file_path,
            mode_primary = mode_primary,
        )
        self.last_ocr_text = text
        return text

    # ------------------------------------------------------------------
    # Pause / Resume
    # ------------------------------------------------------------------

    def pause(self):
        self.paused = True
        print("Observer Paused for Chat.")

    def resume(self):
        self.paused = False
        self.last_frame_hash  = None
        self.last_screen_hash = None
        self.last_ocr_text    = ""
        print("Observer Resumed.")

    # ------------------------------------------------------------------
    # Analysis  ← stronger OCR priority, ignore UI chrome
    # ------------------------------------------------------------------

    def analyze(self, image, context_text="", snapshot=None, precomputed_ocr=None, image_bytes=None):
        if self.paused or not image:
            return None

        try:
            win_title_lower = self.context_engine.get_active_window_title().lower()
            system_titles   = [
                "task switching", "task view", "start", "search",
                "notification center", "action center", "new notification",
                "cortana", "volume control", "system tray",
                "windows shell", "microsoft shell",
            ]
            if (
                any(kw in win_title_lower for kw in ["cora", "assistant", "suggestion", "overlay"])
                or any(t == win_title_lower or t in win_title_lower for t in system_titles)
                or not win_title_lower.strip()
                or win_title_lower == "window"
            ):
                return None
        except Exception:
            pass

        if image_bytes is None:
            image_bytes = self._image_to_bytes(image)
        if image_bytes is None:
            return None

        current_hash = hashlib.md5(image_bytes).hexdigest()
        if current_hash == self.last_frame_hash:
            print("Observer: Screen hash match. Skipping.")
            return None
        self.last_frame_hash = current_hash

        if snapshot is None:
            snapshot = self.context_engine.get_context_snapshot()

        mode_primary = snapshot.get('mode_primary', 'general')
        win_title    = snapshot.get('window_title', '')
        page_title   = snapshot.get('page_title', '')
        site_name    = snapshot.get('site_name', '')
        browser_name = snapshot.get('browser_name', '')

        # ── Expanded OCR — all meaningful modes ───────────────────────
        high_text_apps = [
            "word", "pdf", "docs", "notepad", "editor", ".pdf",
            "powerpoint", "slides", "keynote", "prezi",
        ]
        need_ocr = (
            mode_primary in [
                "developer", "writing", "reading", "document",
                "browser", "youtube", "video", "spreadsheet",
            ]
            or any(a in win_title.lower() for a in high_text_apps)
        )

        ocr_text = ""
        if precomputed_ocr is not None:
            ocr_text = precomputed_ocr
        elif need_ocr:
            try:
                from ocr_engine import extract_text_for_window
                ocr_img  = Image.open(io.BytesIO(image_bytes))
                ocr_text = extract_text_for_window(
                    image        = ocr_img,
                    window_title = win_title,
                    mode_primary = mode_primary,
                )
                if len(ocr_text.strip()) < 15:
                    ocr_text = ""
                else:
                    ocr_text = ocr_text[:3000]
                print(f"Observer OCR: {len(ocr_text)} chars for mode={mode_primary}")
            except Exception as e:
                print(f"OCR Pipeline Error: {e}")
        else:
            print(f"Observer: Skipping OCR for {mode_primary} mode.")

        self.last_ocr_text             = ocr_text
        self.last_proactive_screenshot = image_bytes

        # ── Build rich context hint from parsed title fields ──────────
        context_hint_parts = []
        if page_title:
            context_hint_parts.append(f"PAGE/CONTENT TITLE: {page_title}")
        if site_name:
            context_hint_parts.append(f"SITE: {site_name}")
        if browser_name:
            context_hint_parts.append(f"BROWSER: {browser_name}")
        if not context_hint_parts and win_title:
            context_hint_parts.append(f"WINDOW: {win_title}")

        rich_context_hint = "\n".join(context_hint_parts) if context_hint_parts else context_text

        # ── Determine document type label for prompt ──────────────────
        doc_type_hint = ""
        wl = win_title.lower()
        if any(x in wl for x in ["word", ".docx", "docs", "writer"]):
            doc_type_hint = "Microsoft Word document"
        elif any(x in wl for x in [".pdf", "acrobat", "foxit"]):
            doc_type_hint = "PDF document"
        elif any(x in wl for x in ["powerpoint", ".pptx", "slides"]):
            doc_type_hint = "PowerPoint presentation"
        elif any(x in wl for x in ["excel", ".xlsx", "sheets"]):
            doc_type_hint = "spreadsheet"
        elif any(x in wl for x in ["youtube"]) or site_name.lower() == "youtube":
            doc_type_hint = "YouTube video"
        elif browser_name:
            doc_type_hint = f"web page in {browser_name}"

        # ── Build prompt with strong OCR priority ─────────────────────
        ocr_section = (
            f"DOCUMENT/PAGE TEXT (extracted via OCR — HIGHEST PRIORITY):\n"
            f"{'=' * 60}\n"
            f"{ocr_text}\n"
            f"{'=' * 60}"
            if ocr_text else
            "(No text extracted — rely on window info and image)"
        )

        full_prompt = f"""You are a subtle screen observer assistant.

WINDOW INFO:
{rich_context_hint}
{f'CONTENT TYPE: {doc_type_hint}' if doc_type_hint else ''}

{ocr_section}

TASK:
Based on the OCR text and window info above, describe what the user is
working on and suggest the most helpful action.

STRICT RULES:
1. OCR TEXT IS THE GROUND TRUTH. If OCR text is present, base your
   entire response on it. Do NOT describe the application UI.
2. NEVER mention: toolbars, ribbons, text input fields, scroll bars,
   title bars, buttons, menus, or any application chrome/interface.
3. For Word/document: describe the DOCUMENT CONTENT (topic, what is
   written), not the Word application.
4. For YouTube: use PAGE/CONTENT TITLE as the video name. Suggest
   video-specific actions (explain topic, key points, summarize).
5. For browsers: describe the WEB PAGE content, not the browser UI.
6. REASON must be specific — mention actual content (≤12 words).
   BAD:  "Text input field with typing error"
   BAD:  "Viewing content in a browser"
   GOOD: "Writing acknowledgement section for biology project"
   GOOD: "Watching tutorial on Python decorators"
7. CONFIDENCE: be conservative — 0.7+ only if OCR clearly shows content.
8. Do not mention Cora, AI, or yourself.

OUTPUT JSON ONLY — no prose, no markdown wrapper:
{{
  "reason": "Specific content description (≤12 words)",
  "reason_long": "1-2 sentence detail about what the user is doing",
  "confidence": 0.0-1.0,
  "suggestions": [{{"label": "Action label", "hint": "Specific action"}}]
}}"""

        try:
            now = time.time()
            if now - self.last_llm_call_time < 1.5:
                return None
            self.last_llm_call_time = now

            system_prompt = config.SYSTEM_PROMPT
            if mode_primary == 'developer':
                system_prompt = config.DEV_SYSTEM_PROMPT
            elif mode_primary in ('writing', 'document'):
                system_prompt = config.PRODUCTIVITY_SYSTEM_PROMPT
            elif mode_primary == 'reading':
                system_prompt = config.READING_SYSTEM_PROMPT
            elif mode_primary in ('video', 'youtube'):
                system_prompt = config.VIDEO_SYSTEM_PROMPT
            elif mode_primary == 'browser':
                system_prompt = config.READING_SYSTEM_PROMPT

            response = _get_ollama().chat(model=self.model, messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user',   'content': full_prompt, 'images': [image_bytes]},
            ])
            text = response['message']['content'].strip()
            print(f"Observer RAW: {text[:150]}…")

            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            if not text.endswith("}"):
                idx = text.rfind("}")
                if idx != -1:
                    text = text[:idx + 1]

            payload = json.loads(text)
            payload["screen_context"] = ocr_text
            payload["page_title"]     = page_title
            payload["site_name"]      = site_name
            payload["window_title"]   = win_title
            return payload

        except Exception as e:
            print(f"Observer Analyze Error: {e}")
            return None

    def analyze_picked_region(self, image_bytes: bytes, ocr_text: str, x: int, y: int) -> dict:
        """
        Analyze a user-picked screen region and return a rich suggestion payload.
        Called when user uses Pick to Ask feature.
        """
        try:
            snapshot = self.context_engine.get_context_snapshot()
            win_title    = snapshot.get('window_title', '')
            page_title   = snapshot.get('page_title', '')
            site_name    = snapshot.get('site_name', '')
            mode_primary = snapshot.get('mode_primary', 'general')

            # Build a focused prompt based on the picked region content
            ocr_section = (
                f"PICKED ELEMENT TEXT (user explicitly selected this):\n"
                f"{'=' * 50}\n{ocr_text}\n{'=' * 50}"
                if ocr_text
                else "(No text in selected region — visual element only)"
            )

            context_label = page_title or site_name or win_title or "screen"

            full_prompt = f"""The user explicitly picked/clicked a screen element to ask about it.

ACTIVE WINDOW: {win_title}
CONTENT: {context_label}

{ocr_section}

TASK: Based on the picked element content above, suggest the 3 most useful
actions the user might want to do with this specific content.

RULES:
- Base suggestions entirely on the PICKED ELEMENT TEXT
- Be very specific — mention actual content from the text
- reason must describe what was picked in ≤10 words
- If it looks like code: suggest explain, fix, optimize
- If it looks like text/writing: suggest improve, summarize, rewrite  
- If it looks like data/numbers: suggest analyze, explain, calculate
- If it looks like an error message: suggest fix, explain cause
- Do NOT describe UI elements or application chrome

OUTPUT JSON ONLY:
{{
  "reason": "Picked: [specific description ≤10 words]",
  "reason_long": "What this element contains and what the user likely wants",
  "confidence": 0.95,
  "type": "picked_suggestion",
  "suggestions": [
    {{"label": "Action 1", "hint": "Specific action based on content"}},
    {{"label": "Action 2", "hint": "Specific action based on content"}},
    {{"label": "Action 3", "hint": "Specific action based on content"}}
  ]
}}"""

            import config
            response = _get_ollama().chat(
                model=self.text_model,
                messages=[
                    {'role': 'system', 'content': config.SYSTEM_PROMPT},
                    {'role': 'user',   'content': full_prompt,
                     'images': [image_bytes]},
                ]
            )
            text = response['message']['content'].strip()

            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()

            import json
            payload = json.loads(text)
            payload["screen_context"] = ocr_text
            payload["window_title"]   = win_title
            payload["page_title"]     = page_title
            payload["site_name"]      = site_name
            payload["picked_image"]   = image_bytes
            payload["type"]           = "picked_suggestion"
            return payload

        except Exception as e:
            print(f"analyze_picked_region error: {e}")
            # Fallback payload based purely on OCR — no LLM needed
            import json
            has_code  = any(k in ocr_text for k in ["def ", "import ", "class ", "=>", "{}"])
            has_error = any(k in ocr_text.lower() for k in ["error", "exception", "traceback", "failed"])

            if has_error:
                suggestions = [
                    {"label": "Fix Error",    "hint": f"Fix this error: {ocr_text[:100]}"},
                    {"label": "Explain",      "hint": "Explain what caused this error"},
                    {"label": "Find Solution","hint": "Find the solution to this error"},
                ]
                reason = "Error message detected"
            elif has_code:
                suggestions = [
                    {"label": "Explain Code", "hint": f"Explain this code: {ocr_text[:100]}"},
                    {"label": "Find Issues",  "hint": "Find bugs or issues in this code"},
                    {"label": "Optimize",     "hint": "Suggest improvements for this code"},
                ]
                reason = "Code snippet selected"
            else:
                suggestions = [
                    {"label": "Explain",      "hint": f"Explain: {ocr_text[:100]}"},
                    {"label": "Summarize",    "hint": "Summarize this content"},
                    {"label": "Ask Question", "hint": "I have a question about this"},
                ]
                reason = f"Selected: {ocr_text[:40]}" if ocr_text else "Region selected"

            return {
                "type":           "picked_suggestion",
                "reason":         reason,
                "reason_long":    ocr_text[:120] if ocr_text else "Visual element selected",
                "confidence":     0.9,
                "suggestions":    suggestions,
                "screen_context": ocr_text,
                "window_title":   snapshot.get('window_title', ''),
                "page_title":     snapshot.get('page_title', ''),
                "site_name":      snapshot.get('site_name', ''),
                "picked_image":   image_bytes,
            }

    # ------------------------------------------------------------------
    # Session title generation
    # ------------------------------------------------------------------

    def update_session_title(self, session_id, user_text):
        if not user_text:
            return
        try:
            response = _get_ollama().chat(model=self.text_model, messages=[{
                'role': 'user',
                'content': (
                    f"Summarize this user query into a short 3-5 word title: '{user_text}'. "
                    "Return ONLY the title, no quotes."
                ),
            }])
            title    = response['message']['content'].strip().replace('"', '')
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

    # ------------------------------------------------------------------
    # File readers
    # ------------------------------------------------------------------

    def read_pdf(self, path):
        from ocr_engine import extract_from_file
        result = extract_from_file(path)
        if not result or len(result.strip()) < 50:
            return "[WARNING: PDF appears to be scanned images or empty.]"
        return result

    def read_docx(self, path):
        from ocr_engine import extract_from_file
        return extract_from_file(path) or "[Error reading DOCX]"

    def read_pptx(self, path):
        from ocr_engine import extract_from_file
        return extract_from_file(path) or "[Error reading PPTX]"

    def read_file_content(self, path):
        try:
            if not path:
                return None
            ext = os.path.splitext(path)[1].lower()
            if ext in ('.pdf', '.docx', '.pptx'):
                from ocr_engine import extract_from_file
                return extract_from_file(path)
            valid_exts = [
                '.txt', '.py', '.md', '.json', '.html', '.css', '.js',
                '.csv', '.bat', '.sh', '.xml', '.yaml', '.yml', '.ini', '.log',
            ]
            if ext not in valid_exts:
                return f"[File type '{ext}' not supported. Path: {path}]"
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read(100_000)
        except Exception as e:
            return f"[Error reading file: {e}]"

    # ------------------------------------------------------------------
    # Streaming chat
    # ------------------------------------------------------------------

    def stream_chat_with_screen(self, user_query, attachment=None, proactive_context=None):
        self.stop_flag = False
        mode_primary   = "general"

        try:
            current_images = []
            prompt_context = ""
            system_prompt  = config.CHAT_SYSTEM_PROMPT

            # ── Attachment ────────────────────────────────────────────
            if attachment:
                print(f"Attachment: {attachment}")
                content        = self.read_file_content(attachment)
                fname          = os.path.basename(attachment)
                prompt_context = (
                    f"\n\n[PRIORITY ATTACHMENT: {fname}]\n\n{content}\n\n[END ATTACHMENT]\n"
                )
                mode_primary  = "general"
                system_prompt = config.CHAT_SYSTEM_PROMPT

            # ── Proactive suggestion execution ────────────────────────
            elif proactive_context:
                print("Using proactive context.")
                pc_mode   = proactive_context.get('mode_primary', 'general')
                pc_window = proactive_context.get('window_title', 'Unknown')
                pc_page   = proactive_context.get('page_title', '')
                pc_site   = proactive_context.get('site_name', '')
                has_error = bool(proactive_context.get('error_message'))

                # Use the most descriptive label available
                context_label = pc_page or pc_site or pc_window

                # Include OCR text in proactive context for chip responses
                pc_ocr = proactive_context.get('screen_context', '')
                prompt_context = (
                    f"\n\n[COMMAND MODE: Suggestion Execution]\n"
                    f"ACTIVE CONTENT: {context_label}\n"
                    f"APP/SITE: {pc_site or pc_window}\n"
                    f"MODE: {pc_mode}\n"
                )
                if pc_ocr:
                    prompt_context += f"\nSCREEN TEXT (full page content — use this as primary source):\n{pc_ocr[:4000]}\n"

                # Never send image if OCR text is available — llava describes instead of acting on text
                pc_has_text = bool(proactive_context.get('screen_context', '').strip())
                if proactive_context.get('screenshot') and not pc_has_text:
                    current_images.append(proactive_context['screenshot'])
                    print("Streaming: Sending image (no OCR text available)")
                else:
                    print("Streaming: Text-only mode — skipping image to improve response quality")

                mode_primary  = pc_mode
                system_prompt = (
                    config.DEV_SYSTEM_PROMPT
                    if (pc_mode == 'developer' or has_error)
                    else config.CHAT_SYSTEM_PROMPT
                )

            # ── Reactive chat ─────────────────────────────────────────
            else:
                print("Reactive Mode: Capturing context.")
                vision_keywords = [
                    "look", "see", "screen", "visual", "watch",
                    "what is this", "screenshot", "observe", "check", "debug", "fix",
                ]
                is_vision_request = any(k in user_query.lower() for k in vision_keywords)

                if is_vision_request:
                    img = self.capture_screen(force=True, hide_ui=True)
                    if img:
                        image_bytes = self._image_to_bytes(img)
                        ocr_text    = self.extract_text_from_screen(img)
                        if ocr_text:
                            prompt_context = f"\n\n[SCREEN CONTEXT (OCR) — treat as ground truth of page content]:\n{ocr_text[:4000]}\n"
                        current_images.append(image_bytes)
                else:
                    if self.last_ocr_text:
                        prompt_context = f"\n\n[SCREEN CONTEXT (OCR) — treat as ground truth of page content]:\n{self.last_ocr_text[:4000]}\n"

                snap         = self.context_engine.get_context_snapshot()
                window_title = snap.get('window_title', 'Unknown')
                page_title   = snap.get('page_title', '')
                site_name    = snap.get('site_name', '')
                mode_primary = snap.get('mode_primary', 'general')

                context_label = page_title or site_name or window_title
                prompt_context += (
                    f"\n[ACTIVE CONTENT]: {context_label}"
                    f"\n[SITE/APP]: {site_name or window_title}"
                    f"\n[MODE]: {mode_primary}\n"
                )

                prompt_map = {
                    'developer': config.DEV_SYSTEM_PROMPT,
                    'writing':   config.PRODUCTIVITY_SYSTEM_PROMPT,
                    'document':  config.DOCUMENT_SYSTEM_PROMPT,
                    'reading':   config.READING_SYSTEM_PROMPT,
                    'video':     config.VIDEO_SYSTEM_PROMPT,
                    'youtube':   config.VIDEO_SYSTEM_PROMPT,
                    'browser':   config.CHAT_SYSTEM_PROMPT,
                }
                system_prompt = prompt_map.get(mode_primary, config.CHAT_SYSTEM_PROMPT)

            print(f"Streaming mode={mode_primary}")

            user_content = f"{prompt_context}\nUSER: {user_query}"
            new_message  = {'role': 'user', 'content': user_content}
            if current_images:
                new_message['images'] = current_images

            self.chat_history.append(new_message)

            if len(self.chat_history) == 1:
                threading.Thread(
                    target=self.update_session_title,
                    args=(self.current_session_id, user_query),
                    daemon=True,
                ).start()

            # Use vision model if images present, text model otherwise
            chat_model = self.model if current_images else self.text_model
            messages_payload = [{'role': 'system', 'content': system_prompt}] + self.chat_history
            stream = _get_ollama().chat(model=chat_model, messages=messages_payload, stream=True)

            full_response = ""
            for chunk in stream:
                if self.stop_flag:
                    break
                token          = chunk['message']['content']
                full_response += token
                yield token

            self.chat_history.append({'role': 'assistant', 'content': full_response})
            self.save_session()

        except Exception as e:
            print(f"Stream Error: {e}")
            yield f"[Error: {e}]"

    # ------------------------------------------------------------------
    # Syntax error checking
    # ------------------------------------------------------------------

    def _check_syntax_errors(self, ctx=None):
        if ctx is None:
            ctx = self.context_engine.get_context_snapshot()

        if not ctx.get('error'):
            return

        sig = ctx['error_signature']
        if sig == self.last_reported_error_sig:
            return

        error = ctx['error']

        error_context_code = error.get('context', '')
        if _is_template_code(error_context_code):
            print(f"Copilot: Skipping template placeholder error.")
            return

        print(f"🚨 New Syntax Error: {error['message']} in {os.path.basename(error['file'])}")

        clean_code = re.sub(r'\n{4,}', '\n\n', error_context_code.strip())

        error_prompt = (
            f"SYNTAX ERROR DETECTED\n"
            f"FILE:  {error['file']}\n"
            f"LINE:  {error['line']}\n"
            f"ERROR: {error['message']}\n\n"
            f"CODE:\n{clean_code}\n\n"
            f"Respond with ONLY valid JSON, no prose outside the JSON:\n"
            f'{{"reason": "Brief fix explanation", "code": "corrected code here", "confidence": 1.0}}'
        )

        try:
            response = _get_ollama().chat(model=self.text_model, messages=[
                {'role': 'system', 'content': config.DEV_SYSTEM_PROMPT},
                {'role': 'user',   'content': error_prompt},
            ])
            text = response['message']['content'].strip()
            print(f"Copilot: LLM Response (Raw): {text[:80]}…")

            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()

            payload = json.loads(text)
            payload['type']          = 'syntax_error'
            payload['error_file']    = error['file']
            payload['error_line']    = error['line']
            payload['error_message'] = error['message']
            payload['error_context'] = clean_code
            payload.setdefault('screen_context', '')
            payload.setdefault('suggestions', [])

            print("Copilot: Payload created (JSON parsed)")
            self.signals.suggestion_ready.emit(payload)
            self.last_reported_error_sig = sig

        except Exception as e:
            print(f"Syntax error LLM parse failed: {e}")
            # Surface error to overlay even without LLM fix
            self.signals.suggestion_ready.emit({
                'type':           'syntax_error',
                'reason':         error['message'],
                'error_file':     error['file'],
                'error_line':     error['line'],
                'error_message':  error['message'],
                'error_context':  clean_code,
                'confidence':     1.0,
                'screen_context': '',
                'suggestions':    [],
            })
            self.last_reported_error_sig = sig

    # ------------------------------------------------------------------
    # Proactive observer loop
    # ------------------------------------------------------------------

    def loop(self):
        # LEGACY: This loop is superseded by CopilotController (copilot_controller.py).
        # CopilotController calls capture_screen() and analyze() directly.
        # This method is retained for reference but is NOT called by main.py.
        print("Observer started (Silent Mode)…")
        self.running             = True
        self.last_reported_error_sig = None
        self.last_screen_hash    = None
        self.proactive_pause     = False

        while self.running:
            if self.paused or self.proactive_pause:
                time.sleep(1)
                continue

            try:
                screenshot = self.capture_screen()
                if not screenshot:
                    time.sleep(1)
                    continue

                current_hash, image_bytes = self.hash_and_encode_screen(screenshot)
                if current_hash == self.last_screen_hash:
                    self._check_syntax_errors()
                    time.sleep(config.CHECK_INTERVAL)
                    continue

                self.last_screen_hash = current_hash

                # Fetch snapshot once — reuse for OCR + analyze
                ctx          = self.context_engine.get_context_snapshot()
                win_title    = ctx.get('window_title', '')
                mode_primary = ctx.get('mode_primary', 'general')
                page_title   = ctx.get('page_title', '')
                site_name    = ctx.get('site_name', '')

                from ocr_engine import extract_text_for_window
                ocr_text = extract_text_for_window(
                    image        = screenshot,
                    window_title = win_title,
                    mode_primary = mode_primary,
                )
                self.last_ocr_text = ocr_text

                if page_title or site_name:
                    print(f"Observer: {site_name or 'App'} — \"{page_title or win_title}\"")

                self._check_syntax_errors(ctx)

                check_visual = not (mode_primary == 'developer' and ctx.get('error'))

                if check_visual:
                    payload = self.analyze(screenshot, snapshot=ctx, precomputed_ocr=ocr_text, image_bytes=image_bytes)

                    if payload:
                        reason     = payload.get('reason', '')
                        confidence = payload.get('confidence', 0.0)

                        if any(kw in reason for kw in ["Cora", " AI", " Ui"]):
                            pass
                        elif confidence < config.PROACTIVE_THRESHOLD:
                            pass
                        else:
                            self.signals.suggestion_ready.emit(payload)

            except Exception as e:
                print(f"Observer Loop Error: {e}")

            time.sleep(config.CHECK_INTERVAL)

    def stop(self):
        self.running = False