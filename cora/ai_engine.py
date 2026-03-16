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
1. Suggest based on the ACTUAL visible content provided below.
2. Be proactive: if the content is light (scant text), use the App/Window Title to offer helpful general actions for that application.
3. IGNORE all information from previous sessions. This is a FRESH start.
4. If you see code, analyze the code shown. If you see a website, analyze that website."""

        return f"""You are CORA, a proactive desktop AI assistant.
Current Activity: {ctx.activity}
Active App: {ctx.app} | Window: {ctx.window_title}

{strict_rules}

{img_instruction}

{source_label}:
{'='*40}
{ctx.best_text()[:3000]}
{'='*40}

TASK: "What are the most useful actions for the user right now?"
Provide EXACTLY 4 SHORT, specific, actionable suggestion chips that are OBVIOUSLY derived from the visible content.

SPECIFIC CONTEXT RULES:
- If CODE is visible: suggest specific optimizations, explanations, or implementation steps (e.g. "Optimize loop in main.py").
- MANDATORY CODE RULE: If you see code, one suggestion MUST have the label "Check for bugs" and a hint to check for common errors.
- If an ERROR is visible: suggest specific debugging or fix actions (e.g. "Fix SyntaxError in line 42").
- If WRITING/TEXT is visible: suggest grammar fixes, rewriting, or summarization (e.g. "Fix spelling in intro paragraph").
- If DATA/TABLES are visible: suggest analysis or data extraction (e.g. "Calculate total from column B").
- ALWAYS mention exact names (functions, variables, files, titles) found on the screen in the labels.

Respond ONLY with a raw JSON block.

STRICT SUGGESTION RULES:
1. NEVER use generic labels like "Help", "Analyze", "Explain", "Insight", or "Next Step" without a specific object.
2. Suggestions MUST be directly and obviously derived from the visible content.

JSON FORMAT:
{{
  "type": "specific_category",
  "reason": "Brief reason based ONLY on content",
  "reason_long": "One detailed sentence for user info.",
  "confidence": 1.0,
  "suggestions": [
     {{"label": "Specific Action 1", "hint": "Detailed prompt for chat"}},
     ... (exactly 4 items)
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
            if ctx.app == 'editor' or ctx.activity == 'coding' or ctx.file_path:
                 name = os.path.basename(ctx.file_path) if ctx.file_path else "code"
                 chips = [
                     {"label": f"Optimize {name}", "hint": f"How can I optimize the code visible in {name}?"},
                     {"label": "Check for bugs", "hint": "Check the visible code for potential issues or bugs."},
                     {"label": "Explain Logic", "hint": "Explain what this part of the code does."},
                     {"label": "Refactor Code", "hint": "Suggest ways to refactor this code for better readability."}
                 ]
            elif ctx.app in ('browser', 'chrome', 'firefox', 'edge'):
                 target = ctx.page_title or "this page"
                 chips = [
                     {"label": f"Summarize {target}", "hint": f"Give me a summary of {target}."},
                     {"label": "Key Takeaways", "hint": "What are the most important points here?"},
                     {"label": "Research Topic", "hint": "Find more information related to what's on screen."},
                     {"label": "Analyze Page", "hint": "Provide a detailed analysis of this page."}
                 ]
            elif ctx.activity == 'writing_document' or ctx.app == 'word':
                 chips = [
                     {"label": "Fix Grammar", "hint": "Correct any grammatical errors in the visible text."},
                     {"label": "Improve Wording", "hint": "Suggest ways to improve the clarity and flow."},
                     {"label": "Continue Writing", "hint": "Help me continue writing this section."},
                     {"label": "Check Structure", "hint": "Analyze the structure of this document."}
                 ]
            else:
                 chips = [
                     {"label": "How can I help?", "hint": "Suggest some things you can do for me here."},
                     {"label": "Analyze Screen", "hint": "Explain what I'm looking at right now."},
                     {"label": "Summarize Text", "hint": "Summarize the text currently on screen."},
                     {"label": "Next Steps", "hint": "What should I do next based on this screen?"}
                 ]
            
            payload = {
                "reason":      f"Watching {ctx.app or 'your screen'}",
                "reason_long": f"Cora is observing {ctx.window_title[:40]} and ready to assist.",
                "confidence":  0.3,
                "suggestions": chips,
            }

        # Ensure essential fields exist
        payload.setdefault('suggestions', [])
        payload.setdefault('type', 'general')

        # Enforce "Check for bugs" for coding context
        is_coding = (ctx.app == 'editor' or ctx.activity == 'coding' or ctx.file_path)
        if is_coding:
             if payload['type'] == 'general':
                 payload['type'] = 'developer_suggestion'
             
             # Check if "Check for bugs" is already there (case-insensitive)
             has_bug_check = any("check for bugs" in s.get('label', '').lower() for s in payload['suggestions'])
             if not has_bug_check:
                 bug_chip = {"label": "Check for bugs", "hint": "Check the visible code for potential issues or bugs."}
                 if len(payload['suggestions']) < 4:
                     payload['suggestions'].append(bug_chip)
                 else:
                     # Replace the last one if already at 4
                     payload['suggestions'][3] = bug_chip

        # Ensure exactly 4 suggestions
        target = 4
        while len(payload['suggestions']) > target:
             payload['suggestions'].pop()
        while len(payload['suggestions']) < target:
             payload['suggestions'].append({"label": "Analyze Screen", "hint": "Explain what I'm looking at right now."})
             
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
