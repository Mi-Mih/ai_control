from __future__ import annotations

import html
import re

MESSAGE_LIMIT = 3800
MAX_MESSAGES = 5

_FENCE = re.compile(r"^\s*```")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def markdown_to_html(text: str) -> str:
    """Converts the Markdown subset agents use into Telegram HTML with every tag balanced.

    Args:
        text: Agent answer in Markdown.

    Returns:
        HTML safe to send with ``parse_mode=HTML``.
    """
    return "\n\n".join(_render_block(block) for block in _blocks(text))


def split_markdown(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Renders Markdown and packs it into Telegram messages on block boundaries.

    Args:
        text: Agent answer in Markdown.
        limit: Maximum length of one rendered message.

    Returns:
        Rendered HTML chunks, each no longer than ``limit``.
    """
    chunks: list[str] = []
    current = ""
    for block in _blocks(text):
        for piece in _fit_block(block, limit):
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


def _blocks(text: str) -> list[str]:
    """Splits Markdown into paragraphs, keeping each fenced code block whole."""
    blocks: list[str] = []
    current: list[str] = []
    in_code = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            if in_code:
                current.append(line)
                blocks.append("\n".join(current))
                current = []
            else:
                if current:
                    blocks.append("\n".join(current))
                current = [line]
            in_code = not in_code
        elif in_code:
            current.append(line)
        elif not line.strip():
            if current:
                blocks.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _is_code(block: str) -> bool:
    return bool(_FENCE.match(block))


def _code_body(block: str) -> list[str]:
    lines = block.split("\n")[1:]
    if lines and _FENCE.match(lines[-1]):
        lines = lines[:-1]
    return lines


def _render_block(block: str) -> str:
    if _is_code(block):
        return "<pre>" + html.escape("\n".join(_code_body(block))) + "</pre>"
    return "\n".join(_render_line(line) for line in block.split("\n"))


def _render_line(line: str) -> str:
    heading = _HEADING.match(line)
    if heading:
        return f"<b>{_render_inline(heading.group(1))}</b>"
    bullet = _BULLET.match(line)
    if bullet:
        return bullet.group(1) + "• " + _render_inline(line[bullet.end() :])
    return _render_inline(line)


def _render_inline(text: str) -> str:
    parts: list[str] = []
    position = 0
    for match in _INLINE_CODE.finditer(text):
        parts.append(_render_plain(text[position : match.start()]))
        parts.append(f"<code>{html.escape(match.group(1))}</code>")
        position = match.end()
    parts.append(_render_plain(text[position:]))
    return "".join(parts)


def _render_plain(text: str) -> str:
    parts: list[str] = []
    position = 0
    for match in _LINK.finditer(text):
        parts.append(_BOLD.sub(r"<b>\1</b>", html.escape(text[position : match.start()])))
        label = html.escape(match.group(1))
        parts.append(f'<a href="{html.escape(match.group(2), quote=True)}">{label}</a>')
        position = match.end()
    parts.append(_BOLD.sub(r"<b>\1</b>", html.escape(text[position:])))
    return "".join(parts)


def _fit_block(block: str, limit: int) -> list[str]:
    """Renders one block, splitting it by lines when it does not fit into a message."""
    rendered = _render_block(block)
    if len(rendered) <= limit:
        return [rendered]
    code = _is_code(block)
    lines = _code_body(block) if code else block.split("\n")
    pieces: list[str] = []
    current: list[str] = []

    def render(chunk: list[str]) -> str:
        if code:
            return "<pre>" + html.escape("\n".join(chunk)) + "</pre>"
        return "\n".join(_render_line(line) for line in chunk)

    for line in lines:
        if len(render([line])) > limit:
            if current:
                pieces.append(render(current))
                current = []
            pieces.extend(_hard_split(line, limit, code))
            continue
        if current and len(render([*current, line])) > limit:
            pieces.append(render(current))
            current = []
        current.append(line)
    if current:
        pieces.append(render(current))
    return pieces


def _hard_split(line: str, limit: int, code: bool) -> list[str]:
    # Escaping can at most sextuple the length ("&quot;"), so this step always fits.
    step = max(1, (limit - 11) // 6)
    pieces = [line[index : index + step] for index in range(0, len(line), step)]
    if code:
        return ["<pre>" + html.escape(piece) + "</pre>" for piece in pieces]
    return [html.escape(piece) for piece in pieces]
