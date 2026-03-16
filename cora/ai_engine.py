"""
Layer 4: AI ENGINE
Only component that calls the LLM.
Receives Context, builds prompt, returns response.
No UI logic. No window detection.
"""
import os
import threading
import json
from PyQt6.QtCore import QObject, pyqtSignal
from context_extractor import Context

class AIEngine(QObject):
    suggestion_ready = pyqtSignal(dict)   # proactive suggestion payload
    stream_chunk     = pyqtSignal(str)    # streaming chat token
    stream_done      = pyqtSignal()
    error_occurred   = pyqtSignal(str)

    def __init__(self, model_name: str = "models/gemini-2.5-flash"):
        super().__init__()
        self._model          = model_name
        self._lock           = threading.Lock()
        self._generating     = False
        self._last_call_time = 0
        self._min_call_interval = 10.0
        self._retry_after    = 0

        # Use new google-genai SDK
        try:
            from google import genai
            from google.genai import types
            api_key = os.getenv("GEMINI_API_KEY", "")
            if not api_key:
                print("WARNING: GEMINI_API_KEY not set")
                self._client = None
                self._sdk    = None
            else:
                self._client = genai.Client(api_key=api_key)
                self._types  = types
                self._sdk    = "new"
                print(f"AIEngine: Gemini Client (v2) ready ({model_name})")
        except ImportError:
            try:
                import google.generativeai as genai
                api_key = os.getenv("GEMINI_API_KEY", "")
                genai.configure(api_key=api_key)
                self._client = genai.GenerativeModel(model_name)
                self._sdk    = "old"
                print(f"AIEngine: Gemini Client (v1) ready ({model_name})")
            except Exception as e:
                print(f"AIEngine: Gemini init failed: {e}")
                self._client = None
                self._sdk    = None

    # ── Proactive suggestion ──────────────────────────────────────────────
    def generate_suggestion_async(self, ctx: Context) -> None:
        """Generate a proactive suggestion for the current context."""
        if self._generating:
            return
        threading.Thread(
            target=self._generate_suggestion,
            args=(ctx,),
            daemon=True,
        ).start()

    def _generate_suggestion(self, ctx: Context) -> None:
        import time
        now = time.time()

        # Respect retry-after from quota errors
        if now < self._retry_after:
            wait = int(self._retry_after - now)
            print(f"AIEngine: Rate limited — waiting {wait}s")
            return

        # Enforce minimum interval between calls, EXCEPT for user-initiated regions
        is_user_region = (ctx.source == 'region')
        if not is_user_region and (now - self._last_call_time) < self._min_call_interval:
            return

        with self._lock:
            self._generating = True
        try:
            self._last_call_time = now
            prompt   = self._build_suggestion_prompt(ctx)
            # Use 0.4 temperature for proactive suggestions — stable but informative
            response = self._call_llm(prompt, ctx.image, temperature=0.4)
            payload  = self._parse_suggestion(response, ctx)
            self.suggestion_ready.emit(payload)
        except Exception as e:
            print(f"AIEngine suggestion error: {e}")
            # Parse retry delay from 429 errors
            err_str = str(e)
            if '429' in err_str or 'quota' in err_str.lower():
                import re
                match = re.search(r'retry_delay\s*\{\s*seconds:\s*(\d+)', err_str)
                delay = int(match.group(1)) if match else 60
                self._retry_after = time.time() + delay
                print(f"AIEngine: Quota hit — backing off {delay}s")
            self.error_occurred.emit(str(e))
        finally:
            with self._lock:
                self._generating = False

    def _build_suggestion_prompt(self, ctx: Context) -> str:
        source_label = {
            'window':    'FULL PAGE CONTENT',
            'selection': 'USER SELECTION',
            'region':    'PICKED REGION',
            'ocr':       'SCREEN OCR'
        }.get(ctx.source, 'CONTENT')

        # Regional specificity
        img_instruction = ""
        if ctx.source == 'region' and ctx.image:
            img_instruction = "STRICT RULE: Focus on the provided IMAGE. The OCR text below is just a supplement. Tell the user what is VISUALLY interesting or actionable in that specific screenshot."

        # Anti-Hallucination rules
        strict_rules = """STRICT REALITY RULES:
1. Suggest based on the ACTUAL visible content provided below. If you don't see classes/functions, DO NOT suggest explaining them.
2. Be proactive: if the content is light (scant text), use the App/Window Title to offer helpful general actions for that application.
3. IGNORE all information from previous sessions. This is a FRESH start."""

        return f"""You are CORA, a proactive desktop AI assistant.

Context Type: {ctx.activity}
Active App: {ctx.app}
File Name / Context Title: {ctx.window_title}

{strict_rules}

{img_instruction}

What you see on screen ({source_label}):
{'='*40}
{ctx.best_text()[:3000]}
{'='*40}

Based on this exact content, what may the user need right now?
Provide EXACTLY 4 SHORT, specific, actionable suggestion chips.

CRITICAL RULE: INTEGRATE ON-SCREEN CONTENT
At least 2 out of 4 chips MUST incorporate significant keywords or topics found within the PAGE CONTENT (OCR text).
- GOOD: "Explain [Visible Term]", "Verify [Data Point]".
- BAD: "Summarize Window", "Analyze Chrome", "Explain Class".


EMPTY/LIGHT PAGES:
If the user is on a "New Tab", "Desktop", "File Explorer", or any empty page, DO NOT try to summarize. Instead:
- Set reason: "Watching [App Name]" (e.g., "Watching File Explorer").
- Include a chip: "Need any help?".
- Offer tips related to that specific app.

SPECIFIC CONTEXT RULES:
- If CODE is visible: one suggestion MUST be "Check for bugs in [filename or context]". Others must reference specific variables or functions found in the code.
- If an ERROR is visible: suggest specific debugging or fix actions for that specific error.
- If WRITING/TEXT is visible: suggest grammar fixes or summarization for the specific topic found on screen.
- If DATA/TABLES are visible: mention specific columns or headers in the suggestion.
- ALWAYS mention exact names (functions, variables, topics) found on the screen.

Respond ONLY with a raw JSON block.

STRICT SUGGESTION RULES:
1. NEVER use generic labels مانند "Help", "Analyze", "Explain", "Insight", or "Next Step" without a specific object from the screen.
2. Suggestions MUST be directly and obviously derived from the visible screen content.
3. FORMAT: "[Action] [Actual Word from Screen]" (e.g., "Summarize report.docx", "Explain calculate_sum", "Fix SyntaxError").
4. At least 2 out of 4 chips MUST follow this format.

JSON FORMAT:
{{
  "type": "specific_category",
  "reason": "Brief reason based ONLY on content",
  "reason_long": "One detailed sentence for user info.",
  "confidence": 1.0,
  "suggestions": [
     {{"label": "Specific Action 1", "hint": "Detailed prompt for chat"}},
     {{"label": "Specific Action 2", "hint": "Detailed prompt for chat"}},
     {{"label": "Specific Action 3", "hint": "Detailed prompt for chat"}},
     {{"label": "Specific Action 4", "hint": "Detailed prompt for chat"}}
  ]
}}
"""

    def _parse_suggestion(self, response: str, ctx: Context) -> dict:
        import json
        import re
        text = response.strip()
        
        # Robust JSON extraction: look for the first '{' and last '}'
        try:
            match = re.search(r'(\{.*\})', text, re.DOTALL)
            if match:
                clean_json = match.group(1)
                payload = json.loads(clean_json)
            else:
                # Try raw parsing if no braces found (unlikely but safe)
                payload = json.loads(text)
        except Exception as e:
            print(f"[AI ENGINE] Parse Error: {e}")
            # Intelligent fallback based on context
            if ctx.app == 'editor' or ctx.activity == 'coding':
                 chips = [
                     {"label": f"Analyze {ctx.window_title[:20]}", "hint": "Analyze the contents of this file."},
                     {"label": "Check for bugs/issues", "hint": "Scan the visible text/code for potential problems or errors."},
                     {"label": "Explain content", "hint": "Explain the major elements visible right now."},
                     {"label": "Need any help?", "hint": "Ask me anything about your current task."}
                 ]
            elif ctx.app in ('browser', 'chrome', 'firefox', 'edge'):
                 chips = [
                     {"label": f"Summarize {ctx.window_title[:20]}", "hint": "Provide a concise summary of this page."},
                     {"label": "Extract key points", "hint": "What are the most important takeaways from this page?"},
                     {"label": "Analyze page topic", "hint": "Perform a detailed analysis of the main subject here."},
                     {"label": "Find related info", "hint": "What other information relates to what I'm looking at?"}
                 ]
            elif ctx.activity == 'writing_document' or ctx.app == 'word':
                 chips = [
                     {"label": "Fix grammar & spelling", "hint": "Correct any linguistic errors in the visible text."},
                     {"label": "Improve document flow", "hint": "Suggest ways to make this text clearer and easier to read."},
                     {"label": "Summarize section", "hint": "Give me a quick summary of this part of the document."},
                     {"label": "Continue writing", "hint": "Help me expand on the current visible paragraph."}
                 ]
            else:
                 app_label = ctx.app or "your screen"
                 chips = [
                     {"label": "Need any help?", "hint": "I'm watching your screen and ready to assist whenever you need me."},
                     {"label": f"Analyze {app_label}", "hint": f"Give me a detailed breakdown of what's happening in {app_label}."},
                     {"label": f"Explore {ctx.window_title[:20]}...", "hint": "Explore the major elements visible right now."},
                     {"label": "Check for issues", "hint": "Look for grammar, bugs, or logical errors in the current view."}
                 ]
            
            payload = {
                "reason":      f"Watching {ctx.app or 'your screen'}",
                "reason_long": f"Cora is observing {ctx.window_title[:40]} and ready to assist.",
                "confidence":  0.3,
                "suggestions": chips,
            }

        # Ensure exactly 4 chips
        payload.setdefault('suggestions', [])
        while len(payload['suggestions']) < 4 and ctx.source != 'region':
             payload['suggestions'].append({"label": "More help...", "hint": "Ask Cora for more assistance."})
        
        if len(payload['suggestions']) > 4 and ctx.source != 'region':
             payload['suggestions'] = payload['suggestions'][:4]
             
        payload['screen_context'] = ctx.best_text()
        payload['window_title']   = ctx.window_title
        payload['app']            = ctx.app
        payload['source']         = ctx.source
        payload['activity']       = ctx.activity
        payload['file_path']      = ctx.file_path
        payload['page_title']     = ctx.page_title
        payload['url']            = ctx.url
        return payload

    # ── Chat response ─────────────────────────────────────────────────────
    def stream_chat_async(self, user_message: str, ctx: Context,
                          history: list) -> None:
        """Stream a chat response grounded in context."""
        threading.Thread(
            target=self._stream_chat,
            args=(user_message, ctx, history),
            daemon=True,
        ).start()

    def _stream_chat(self, user_message: str, ctx: Context,
                     history: list) -> None:
        import time
        now = time.time()
        if now < self._retry_after:
            wait = int(self._retry_after - now)
            self.stream_chunk.emit(f"Rate limited — please wait {wait} seconds.")
            self.stream_done.emit()
            return
        try:
            prompt   = self._build_chat_prompt(user_message, ctx)
            messages = self._build_message_history(history, prompt)
            # Use higher temperature for chat to allow creative/detailed explanations
            self._stream_llm(messages, ctx.image, temperature=0.7)
            self._last_call_time = time.time()
        except Exception as e:
            err_str = str(e)
            if '429' in err_str or 'quota' in err_str.lower():
                import re
                match = re.search(r'retry_delay\s*\{\s*seconds:\s*(\d+)', err_str)
                delay = int(match.group(1)) if match else 60
                self._retry_after = time.time() + delay
                self.stream_chunk.emit(
                    f"\n\n⏳ Rate limit hit — Gemini asks to wait {delay}s. "
                    f"Try again shortly."
                )
            else:
                self.stream_chunk.emit(f"\n\n*Error: {err_str[:200]}*")
            self.stream_done.emit()

    def _build_chat_prompt(self, user_message: str, ctx: Context) -> str:
        content = ctx.best_text()
        if not content:
            return user_message

        is_specific = ctx.source in ('selection', 'region')
        source_label = {
            'selection': 'USER SELECTED THIS TEXT',
            'region':    'USER PICKED THIS SCREEN REGION',
            'window':    'CURRENT SCREEN CONTENT',
        }.get(ctx.source, 'SCREEN CONTENT')

        if is_specific:
            # User wants to talk about THIS SPECIFIC PART. Don't drown it in full-page context.
            img_directive = ""
            if ctx.source == 'region' and ctx.image:
                img_directive = "STRICT RULE: Look at the provided IMAGE of this regional selection. The visual details in the screenshot are your primary source of truth."

            return f"""You are CORA, a desktop AI assistant. 
The user has specifically clicked an action or pointed to this content. 
{img_directive}
STRICT RULE: Focus ONLY on providing the response for the specified ACTION below using the provided content. 

{source_label}:
{'='*40}
{content[:8000]}
{'='*40}

ACTIVE APP: {ctx.app} | WINDOW: {ctx.window_title}

ACTION REQUESTED: {user_message}

CRITICAL RULES:
- Focus EXCLUSIVELY on answering the action requested.
- Use the provided context/image to give a precise, technical, or helpful response.
- If the content is specific (like a code snippet), provide a direct breakdown of THAT snippet.
- Do NOT talk about the rest of the application or generic context.
- NEVER use placeholders like "CODE_BLOCK_N". Write out all code inside triple backticks.
"""

        return f"""You are CORA, a desktop AI assistant. You have full visibility of the user's screen.
STRICT RULE: Focus ONLY on providing the response for the specified ACTION below using the provided screen content.

{source_label}:
{'='*40}
{content[:8000]}
{'='*40}

APP: {ctx.app} | WINDOW: {ctx.window_title}
ACTIVITY: {ctx.activity}

ACTION REQUESTED: {user_message}

CRITICAL RULES:
- Focus EXCLUSIVELY on answering the action requested.
- Use the provided context/image to give a precise, technical, or helpful response.
- Do NOT just restate the filename or app name.
- NEVER use placeholders like "CODE_BLOCK_N". Write out all code inside triple backticks.
"""

    def _build_message_history(self, history: list, prompt: str) -> list:
        messages = []
        for turn in history[-6:]:  # last 6 turns for context
            messages.append({'role': turn['role'], 'content': turn['content']})
        messages.append({'role': 'user', 'content': prompt})
        return messages

    # ── LLM calls ────────────────────────────────────────────────────────
    def _call_llm(self, prompt: str, image: bytes = None, temperature: float = 0.7) -> str:
        if not self._client:
            return ""
        try:
            contents = [prompt]
            if image:
                if self._sdk == "new":
                    contents.append(self._types.Part.from_bytes(data=image, mime_type="image/png"))
                else:
                    contents.append({'mime_type': 'image/png', 'data': image})

            if self._sdk == "new":
                config = self._types.GenerateContentConfig(
                    temperature       = temperature,
                    max_output_tokens = 1024,
                    safety_settings   = [
                        self._types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                        self._types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                        self._types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                        self._types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                    ]
                )
                response = self._client.models.generate_content(
                    model    = self._model,
                    contents = contents,
                    config   = config,
                )
                return response.text.strip()
            else:
                # Old SDK fallback
                safety = [
                    {'category': 'HARM_CATEGORY_HARASSMENT', 'threshold': 'BLOCK_NONE'},
                    {'category': 'HARM_CATEGORY_HATE_SPEECH', 'threshold': 'BLOCK_NONE'},
                    {'category': 'HARM_CATEGORY_SEXUALLY_EXPLICIT', 'threshold': 'BLOCK_NONE'},
                    {'category': 'HARM_CATEGORY_DANGEROUS_CONTENT', 'threshold': 'BLOCK_NONE'},
                ]
                response = self._client.generate_content(
                    contents,
                    generation_config = {'temperature': temperature, 'max_output_tokens': 1024},
                    safety_settings   = safety
                )
                return response.text.strip()
        except Exception as e:
            print(f"Gemini call error: {e}")
            return ""

    def _stream_llm(self, messages: list, image: bytes = None, temperature: float = 0.7) -> None:
        if not self._client:
            self.stream_chunk.emit("AI engine not initialized.")
            self.stream_done.emit()
            return
        try:
            # Build single prompt from history
            history_text = ""
            for msg in messages[:-1]:
                role    = "User" if msg['role'] == 'user' else "Assistant"
                content = msg.get('content', '')
                history_text += f"{role}: {content}\n\n"
            last_msg    = messages[-1].get('content', '')
            full_prompt = f"{history_text}User: {last_msg}" if history_text else last_msg

            contents = [full_prompt]
            if image:
                if self._sdk == "new":
                    contents.append(self._types.Part.from_bytes(data=image, mime_type="image/png"))
                else:
                    contents.append({'mime_type': 'image/png', 'data': image})

            if self._sdk == "new":
                for chunk in self._client.models.generate_content_stream(
                    model    = self._model,
                    contents = contents,
                    config   = self._types.GenerateContentConfig(
                        max_output_tokens = 4096,
                        temperature       = temperature,
                        safety_settings   = [
                            self._types.SafetySetting(
                                category="HARM_CATEGORY_HARASSMENT",
                                threshold="BLOCK_NONE",
                            ),
                            self._types.SafetySetting(
                                category="HARM_CATEGORY_HATE_SPEECH",
                                threshold="BLOCK_NONE",
                            ),
                            self._types.SafetySetting(
                                category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
                                threshold="BLOCK_NONE",
                            ),
                            self._types.SafetySetting(
                                category="HARM_CATEGORY_DANGEROUS_CONTENT",
                                threshold="BLOCK_NONE",
                            ),
                        ]
                    )
                ):
                    try:
                        if chunk.text:
                            self.stream_chunk.emit(chunk.text)
                    except Exception:
                        continue
            else:
                safety = [
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                ]
                response = self._client.generate_content(contents, stream=True, safety_settings=safety)
                for chunk in response:
                    try:
                        if chunk.text:
                            self.stream_chunk.emit(chunk.text)
                    except Exception:
                        continue

            self.stream_done.emit()

        except Exception as e:
            err = str(e)
            print(f"Gemini stream error: {err[:200]}")
            if '429' in err or 'quota' in err.lower():
                import re
                match = re.search(r'retry_delay.*?seconds.*?(\d+)', err)
                delay = int(match.group(1)) if match else 60
                self._retry_after = time.time() + delay
                self.stream_chunk.emit(
                    f"\n\n⏳ Rate limited — wait {delay}s and try again."
                )
            else:
                self.stream_chunk.emit(f"\n\n*Error: {err[:200]}*")
            self.stream_done.emit()
