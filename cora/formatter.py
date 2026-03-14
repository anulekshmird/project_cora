import re
import base64
import html


class ResponseFormatter:
    """
    Converts LLM output (markdown + Cora section headers) into clean HTML
    for rendering inside QLabel (RichText mode).

    Pipeline:
      1. Sanitize raw text (strip stray JSON, hallucinated placeholders)
      2. Extract fenced code blocks (protect them from further processing)
      3. Apply Cora section headers  (⚠ Error / Cause / Fix / Commands / Notes)
      4. Apply inline markdown       (bold, italic, inline code)
      5. Apply block markdown        (headings, bullet lists, numbered lists, blockquotes)
      6. Restore code blocks         (styled with lang label + COPY link)
      7. Convert remaining newlines  (only outside tags)
    """

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    @staticmethod
    def format(text: str) -> str:
        if not text or not text.strip():
            return ""

        text = ResponseFormatter._sanitize(text)
        if not text.strip():
            return ""

        text, code_blocks = ResponseFormatter._extract_code_blocks(text)
        text = ResponseFormatter._apply_section_headers(text)
        text = ResponseFormatter._apply_inline_markdown(text)
        text = ResponseFormatter._apply_block_markdown(text)
        text = ResponseFormatter._newlines_to_br(text)
        text = ResponseFormatter._restore_code_blocks(text, code_blocks)
        return text.strip()

    # ------------------------------------------------------------------
    # Step 1 — Sanitize
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize(text: str) -> str:
        # ── Strip hallucinated CODEBLOCK placeholder text ──────────────
        # llava or Gemini sometimes write "CODE_BLOCK_0" or "CODE BLOCK 1"
        # instead of actual fenced code. Remove these so they never render.
        text = re.sub(r'\bCODE[_\s]*BLOCK[_\s]*\d+\b', '', text, flags=re.IGNORECASE)

        # ── Drop bare JSON-only payloads (internal LLM artifacts) ──────
        # Only drop it if the ENTIRE response (after stripping) is a JSON
        # object — not if it happens to start with { mid-sentence.
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                import json
                json.loads(stripped)
                return ""   # pure JSON blob — discard entirely
            except Exception:
                pass        # not valid JSON — keep going

        # ── Remove ```json { ... } ``` fences ──────────────────────────
        text = re.sub(r"```json\s*\{.*?\}\s*```", "", text, flags=re.DOTALL)

        # ── Collapse runs of 4+ blank lines ────────────────────────────
        text = re.sub(r'\n{4,}', '\n\n\n', text)

        return text

    # ------------------------------------------------------------------
    # Step 2 — Extract fenced code blocks
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_code_blocks(text: str):
        """
        Replace ```lang\\ncode``` with __CODE_BLOCK_N__ placeholders.
        Returns (modified_text, list_of_(lang, code) tuples).
        """
        blocks = []

        def replacer(m):
            lang = (m.group(1) or "").strip() or "code"
            code = m.group(2)
            code_escaped = html.escape(code)
            blocks.append((lang, code_escaped))
            return f"<<CB_{len(blocks) - 1}>>"

        text = re.sub(
            r"```([a-zA-Z0-9_+-]*)[ \t]*\n?(.*?)```",
            replacer,
            text,
            flags=re.DOTALL,
        )
        return text, blocks

    # ------------------------------------------------------------------
    # Step 3 — Cora section headers
    # ------------------------------------------------------------------

    _SECTIONS = [
        (r"⚠\s*Error",   "⚠",  "#f87171", "#3b0f0f", "#ef4444"),
        (r"(?i)cause",    "🔍", "#93c5fd", "#0f1e35", "#3b82f6"),
        (r"(?i)fix",      "🛠", "#6ee7b7", "#0b2218", "#34d399"),
        (r"(?i)commands", "⌨", "#fcd34d", "#1c1407", "#f59e0b"),
        (r"(?i)notes?",   "📝", "#c4b5fd", "#160d2b", "#8b5cf6"),
        (r"(?i)summary",  "📋", "#94a3b8", "#0f172a", "#475569"),
        (r"(?i)output",   "📤", "#67e8f9", "#041e24", "#06b6d4"),
    ]

    @staticmethod
    def _apply_section_headers(text: str) -> str:
        lines  = text.split("\n")
        result = []
        i      = 0

        while i < len(lines):
            line            = lines[i]
            matched_section = None

            for pattern, icon, color, bg, border in ResponseFormatter._SECTIONS:
                if re.fullmatch(pattern, line.strip()):
                    matched_section = (icon, color, bg, border)
                    break

            if matched_section:
                icon, color, bg, border = matched_section
                i += 1
                body_lines = []
                while i < len(lines):
                    next_line = lines[i]
                    is_next_header = any(
                        re.fullmatch(p, next_line.strip())
                        for p, *_ in ResponseFormatter._SECTIONS
                    )
                    if is_next_header:
                        break
                    body_lines.append(next_line)
                    i += 1

                body = "\n".join(body_lines).strip()
                result.append(
                    f'<div style="'
                    f'background-color:{bg};'
                    f'border-left:4px solid {border};'
                    f'border-radius:6px;'
                    f'padding:10px 14px;'
                    f'margin:8px 0;'
                    f'">'
                    f'<div style="color:{color};font-weight:bold;font-size:14px;margin-bottom:6px;">'
                    f'{icon} {line.strip().split()[-1].capitalize()}'
                    f'</div>'
                    f'<div style="color:#e2e8f0;">{body}</div>'
                    f'</div>'
                )
            else:
                result.append(line)
                i += 1

        return "\n".join(result)

    # ------------------------------------------------------------------
    # Step 4 — Inline markdown
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_inline_markdown(text: str) -> str:
        # Bold+italic
        text = re.sub(r"\*{3}(.+?)\*{3}", r'<b><i>\1</i></b>', text)
        # Bold
        text = re.sub(r"\*{2}(.+?)\*{2}", r'<b>\1</b>', text)
        text = re.sub(r"__(.+?)__",        r'<b>\1</b>', text)
        # Italic
        text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r'<i>\1</i>', text)
        text = re.sub(r"(?<!\w)_(.+?)_(?!\w)",    r'<i>\1</i>', text)
        # Strikethrough
        text = re.sub(r"~~(.+?)~~", r'<s>\1</s>', text)
        # Inline code
        text = re.sub(
            r"`([^`\n]+)`",
            r'<code style="'
            r'background-color:#1e293b;'
            r'color:#7dd3fc;'
            r'padding:1px 5px;'
            r'border-radius:4px;'
            r'font-family:Consolas,monospace;'
            r'font-size:13px;'
            r'">\1</code>',
            text,
        )
        return text

    # ------------------------------------------------------------------
    # Step 5 — Block markdown
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_block_markdown(text: str) -> str:
        lines  = text.split("\n")
        result = []
        in_ul  = False
        in_ol  = False

        def close_lists():
            nonlocal in_ul, in_ol
            if in_ul:
                result.append("</ul>")
                in_ul = False
            if in_ol:
                result.append("</ol>")
                in_ol = False

        for line in lines:
            h_match = re.match(r"^(#{1,4})\s+(.*)", line)
            if h_match:
                close_lists()
                level = len(h_match.group(1))
                sizes = {1: "14px", 2: "13px", 3: "13px", 4: "13px"}
                result.append(
                    f'<div style="color:#e2e8f0;font-size:{sizes.get(level,"13px")};'
                    f'font-weight:bold;margin:6px 0 2px 0;">'
                    f'{h_match.group(2)}</div>'
                )
                continue

            bq_match = re.match(r"^>\s?(.*)", line)
            if bq_match:
                close_lists()
                result.append(
                    f'<div style="border-left:3px solid #475569;'
                    f'padding-left:10px;color:#94a3b8;margin:4px 0;">'
                    f'{bq_match.group(1)}</div>'
                )
                continue

            ul_match = re.match(r"^\s*[-*+]\s+(.*)", line)
            if ul_match:
                if not in_ul:
                    close_lists()
                    result.append(
                        '<ul style="margin:4px 0 4px 16px;padding:0;'
                        'color:#cbd5e1;list-style-type:disc;">'
                    )
                    in_ul = True
                result.append(f"<li>{ul_match.group(1)}</li>")
                continue

            ol_match = re.match(r"^\s*(\d+)\.\s+(.*)", line)
            if ol_match:
                if not in_ol:
                    close_lists()
                    result.append(
                        '<ol style="margin:4px 0 4px 16px;padding:0;color:#cbd5e1;">'
                    )
                    in_ol = True
                result.append(f"<li>{ol_match.group(2)}</li>")
                continue

            if re.match(r"^(-{3,}|\*{3,}|_{3,})$", line.strip()):
                close_lists()
                result.append(
                    '<hr style="border:none;border-top:1px solid #334155;margin:10px 0;">'
                )
                continue

            close_lists()
            result.append(line)

        close_lists()
        return "\n".join(result)

    # ------------------------------------------------------------------
    # Step 6 — Restore code blocks
    # ------------------------------------------------------------------

    @staticmethod
    def _restore_code_blocks(text: str, blocks: list) -> str:
        for i, (lang, code_escaped) in enumerate(blocks):
            b64 = base64.b64encode(
                html.unescape(code_escaped).encode()
            ).decode()

            styled = (
                f'<div style="'
                f'background-color:#0d1117;'
                f'border:1px solid #30363d;'
                f'border-radius:8px;'
                f'padding:0;'
                f'margin:8px 0;'
                f'overflow:hidden;'
                f'">'
                f'<table width="100%" style="'
                f'background-color:#161b22;'
                f'padding:6px 12px;'
                f'border-bottom:1px solid #30363d;'
                f'border-radius:8px 8px 0 0;'
                f'">'
                f'<tr>'
                f'<td style="color:#8b949e;font-size:11px;'
                f'text-transform:uppercase;font-family:Consolas,monospace;">'
                f'{lang}</td>'
                f'<td align="right">'
                f'<a href="copy:{b64}" style="'
                f'color:#58a6ff;font-size:11px;font-weight:bold;'
                f'text-decoration:none;background:#1f2937;'
                f'padding:2px 8px;border-radius:4px;'
                f'">COPY</a>'
                f'</td>'
                f'</tr>'
                f'</table>'
                f'<pre style="'
                f'color:#e6edf3;'
                f'font-family:Consolas,\'Courier New\',monospace;'
                f'font-size:13px;margin:0;padding:12px 14px;'
                f'white-space:pre-wrap;word-break:break-word;'
                f'">{code_escaped}</pre>'
                f'</div>'
            )
            text = text.replace(f"<<CB_{i}>>", styled)
        return text

    # ------------------------------------------------------------------
    # Step 7 — Newlines → <br>  (only outside HTML tags)
    # ------------------------------------------------------------------

    @staticmethod
    def _newlines_to_br(text: str) -> str:
        """
        Replace \\n with <br> only outside HTML tags.
        __CODE_BLOCK_N__ placeholders are shielded first so the
        character scan never inserts <br> inside them.
        """
        placeholder_map: dict = {}

        def _shield(m: re.Match) -> str:
            original = m.group(0)
            token    = f"<CBLOCK{m.group(1)}/>"
            placeholder_map[token] = original
            return token

        text = re.sub(r"<<CB_(\d+)>>", _shield, text)

        result = []
        in_tag = False
        for ch in text:
            if ch == "<":
                in_tag = True
                result.append(ch)
            elif ch == ">":
                in_tag = False
                result.append(ch)
            elif ch == "\n" and not in_tag:
                result.append("<br>")
            else:
                result.append(ch)
        text = "".join(result)

        for token, original in placeholder_map.items():
            text = text.replace(token, original)

        return text