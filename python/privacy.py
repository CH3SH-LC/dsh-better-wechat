"""Extract readable card text without opening links or media attachments.

Plain message text is returned verbatim. Privacy is a source/publication boundary,
not default redaction of the user's authorized runtime conversations.
"""
from __future__ import annotations

import html
import xml.etree.ElementTree as ET


def structured_text(value: str, message_type: str = "text") -> str:
    """Never emit raw XML, attachment metadata or malformed structured bodies."""
    if message_type == "unread":
        return "[非文本消息正文无法解码；未读取]"
    if message_type in {"text", "system"} and not value.lstrip().startswith("<"):
        return value
    if message_type == "text":
        return value
    if message_type in {"image", "voice", "video", "file", "emoji", "location", "contact"}:
        return "[图片、语音、视频或附件正文未读取]"
    if value.lstrip().startswith("&lt;"):
        value = html.unescape(value)
    if "<" not in value:
        return value
    if "<!DOCTYPE" in value.upper() or "<!ENTITY" in value.upper():
        return "[结构化消息未读取]"
    candidate = value[value.find("<"):].strip()
    try:
        root = ET.fromstring(candidate)
    except (ET.ParseError, ValueError):
        # HTML is intentionally not interpreted; even malformed card attributes
        # can carry encrypted media keys and should never survive as plain text.
        return "[结构化消息未读取]"
    lines = []
    for node in root.iter():
        tag = node.tag.split("}")[-1].lower()
        if tag in {"title", "des", "digest", "url"}:
            text = "".join(node.itertext()).strip()
            if text and text not in lines:
                lines.append(text)
        elif tag == "refermsg":
            quote_type = node.findtext("type", "1")
            for child in node.iter("content"):
                text = child.text or ""
                if text.strip():
                    lines.append(structured_text(text, "text" if quote_type == "1" else "card"))
    return "\n".join(lines) or "[结构化消息未读取]"
