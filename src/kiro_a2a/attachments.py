"""Turn the parts of an A2A message into ACP prompt blocks.

  text part                  -> text block
  data part                  -> text block with the JSON
  file part, text-like bytes -> embedded resource (or inline text if the agent can't take resources)
  file part, image bytes     -> image block (if the agent accepts images)
  file part with a URL       -> resource_link

Large content is cut to stay within the limits below, and marked as cut.
"""

from __future__ import annotations

import base64
import binascii
import json

PART_LIMIT = 512 * 1024          # characters of text per part
MESSAGE_LIMIT = 2 * 1024 * 1024  # characters of text per message
IMAGE_LIMIT = 5 * 1024 * 1024    # bytes per image
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/yaml", "application/x-yaml",
              "application/javascript", "application/toml", "application/sql")


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... cut: {len(text) - limit} more characters not sent]"


def _is_text(mime: str, raw: bytes) -> bool:
    if any(mime.startswith(t) for t in TEXT_TYPES):
        return True
    if mime and mime != "application/octet-stream":
        return False
    # Unknown type (common for .md, .ts, .go, ... on some systems): look at the bytes.
    sample = raw[:8192]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as exc:
        return exc.start > len(sample) - 4  # only a character cut off at the end of the sample
    return True


def to_prompt_blocks(message: dict, capabilities: dict | None = None) -> list[dict]:
    capabilities = capabilities or {}
    blocks: list[dict] = []
    budget = MESSAGE_LIMIT

    def add_text(text: str) -> None:
        nonlocal budget
        if text and budget > 0:
            text = _clip(_clip(text, PART_LIMIT), budget)
            budget -= len(text)
            blocks.append({"type": "text", "text": text})

    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("kind") or ("text" if "text" in part else "data" if "data" in part else "file")
        if kind == "text":
            add_text(part.get("text") or "")
        elif kind == "data":
            add_text("```json\n" + json.dumps(part.get("data"), indent=2, ensure_ascii=False) + "\n```")
        elif kind == "file":
            file = part.get("file") or {}
            name = file.get("name") or "attachment"
            mime = (file.get("mimeType") or "").lower()
            if file.get("uri"):
                blocks.append({"type": "resource_link", "uri": file["uri"], "name": name,
                               **({"mimeType": mime} if mime else {})})
                continue
            try:
                raw = base64.b64decode(file.get("bytes") or "", validate=True)
            except (binascii.Error, ValueError):
                add_text(f"[attachment {name}: could not decode it]")
                continue
            if mime.startswith("image/"):
                if not capabilities.get("image"):
                    add_text(f"[image {name} not sent: this agent does not accept images]")
                elif len(raw) > IMAGE_LIMIT:
                    add_text(f"[image {name} not sent: larger than {IMAGE_LIMIT // (1024 * 1024)} MB]")
                else:
                    blocks.append({"type": "image", "mimeType": mime, "data": base64.b64encode(raw).decode()})
            elif _is_text(mime, raw):
                text = _clip(raw.decode("utf-8", errors="replace"), min(PART_LIMIT, max(budget, 0)))
                budget -= len(text)
                if capabilities.get("embeddedContext"):
                    blocks.append({"type": "resource", "resource": {
                        "uri": f"attachment:///{name}", "mimeType": mime or "text/plain", "text": text}})
                else:
                    blocks.append({"type": "text", "text": f"File {name}:\n```\n{text}\n```"})
            else:
                add_text(f"[attachment {name} ({mime or 'unknown type'}) not sent: only text and images are supported]")
    return blocks
