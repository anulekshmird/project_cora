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
        self._min_call_interval = 15.0
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
            response = self._call_llm(prompt, ctx.image)
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
        content = ctx.best_text()
        
        if ctx.source == 'region':
            return f"""You are CORA, a desktop assistant.
Identify what is in the selected region and provide specific, helpful suggestions.

SELECTED REGION TEXT:
{ctx.selected_text}

ACTIVE APP: {ctx.app}
FILE/PAGE: {ctx.page_title or ctx.file_path}

Task:
1. Determine exactly what is in the selected region (e.g., a specific code function, a paragraph about a topic, a YouTube comment, etc.).
2. Return a 'reason' that is specific and grounded (e.g., "Reviewing Python logic in [file]" or "Analyzing article about [topic]").
3. Return a 'type' like 'code_analysis', 'text_summary', 'video_help', or 'error_fix'.
4. Return 3 targeted suggestions.

Respond ONLY in this JSON format:
{{
  "type": "specific_category",
  "reason": "Specific summary of what you see",
  "reason_long": "One detailed sentence explaining why these suggestions help",
  "confidence": 0.95,
  "suggestions": [
    {{"label": "Action", "hint": "Specific detail-oriented instruction"}},
    {{"label": "Action", "hint": "Specific detail-oriented instruction"}},
    {{"label": "Action", "hint": "Specific detail-oriented instruction"}}
  ]
}}"""

        app_hints = {
            'word':    'User is writing a Word document.',
            'editor':  'User is writing code.',
            'browser': 'User is browsing the web.',
            'youtube': 'User is watching a YouTube video.',
            'pdf':     'User is reading a PDF document.',
            'general': 'User is working on their computer.',
        }
        app_hint = app_hints.get(ctx.app, 'User is working on their computer.')

        return f"""You are CORA, a smart desktop AI assistant.
USER ACTIVITY: {ctx.activity}
LIKELY NEEDS: {', '.join(ctx.needs)}

ACTIVE WINDOW: {ctx.window_title}
CONTENT:
{'='*40}
{content[:3000]}
{'='*40}

Suggest the 3 most useful actions for this exact content and activity.
Be SPECIFIC — mention actual words, topics, or code from the content above.
Do NOT give generic suggestions like "summarize" without context.

Respond ONLY in this exact JSON format, nothing else:
{{
  "reason": "You’re in: [App Name] | Looks like: [Specific title, topic, or content description]",
  "reason_long": "One specific sentence about what would help most for the detected task",
  "confidence": 0.95,
  "suggestions": [
    {{"label": "Short Action", "hint": "Specific instruction using actual content"}},
    {{"label": "Short Action", "hint": "Specific instruction using actual content"}},
    {{"label": "Short Action", "hint": "Specific instruction using actual content"}}
  ]
}}"""

    def _parse_suggestion(self, response: str, ctx: Context) -> dict:
        import json
        text = response.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        try:
            payload = json.loads(text)
        except Exception:
            payload = {
                "reason":      ctx.window_title[:50] or "Active screen",
                "reason_long": "I'm observing your current activity.",
                "confidence":  0.5,
                "suggestions": [
                    {"label": "Ask Anything", "hint": "Ask me about what's on your screen"},
                ],
            }
        payload['screen_context'] = ctx.best_text()
        payload['window_title']   = ctx.window_title
        payload['app']            = ctx.app
        payload['source']         = ctx.source
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
            self._stream_llm(messages, ctx.image)
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

        source_label = {
            'selection': 'USER SELECTED THIS TEXT',
            'region':    'USER PICKED THIS SCREEN REGION',
            'window':    'CURRENT SCREEN CONTENT',
        }.get(ctx.source, 'SCREEN CONTENT')

        return f"""You are CORA, a desktop AI assistant. You have full visibility of the user's screen.

{source_label}:
{'='*40}
{content[:8000]}
{'='*40}

APP: {ctx.app} | WINDOW: {ctx.window_title}
ACTIVITY: {ctx.activity} | NEEDS: {', '.join(ctx.needs)}

USER REQUEST: {user_message}

CRITICAL RULES:
- The content above IS what the user is looking at. Analyze it deeply in the context of their activity: {ctx.activity}.
- Do NOT just restate the filename or app name.
- If the user asks for an explanation, explain the ACTUAL LOGIC or MEANING of the content provided.
- Be extremely specific. Mention variable names, specific sentences, or values found in the content.
- If asked to fix or rewrite, provide the complete corrected version.
- NEVER use placeholders like "CODE_BLOCK_N" or "CODE_BLOCK".
- ALWAYS write out the full code inside the response using triple backticks.
- Tailor your response to the user's current need: {', '.join(ctx.needs)}."""

    def _build_message_history(self, history: list, prompt: str) -> list:
        messages = []
        for turn in history[-6:]:  # last 6 turns for context
            messages.append({'role': turn['role'], 'content': turn['content']})
        messages.append({'role': 'user', 'content': prompt})
        return messages

    # ── LLM calls ────────────────────────────────────────────────────────
    def _call_llm(self, prompt: str, image: bytes = None) -> str:
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
                response = self._client.models.generate_content(
                    model    = self._model,
                    contents = contents,
                )
                return response.text.strip()
            else:
                response = self._client.generate_content(contents)
                return response.text.strip()
        except Exception as e:
            print(f"Gemini call error: {e}")
            return ""

    def _stream_llm(self, messages: list, image: bytes = None) -> None:
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
                        temperature       = 0.7,
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
