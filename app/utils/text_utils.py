"""text_utils.py — Shared text cleaning utilities.

Called by: services/ai_email_parser.py, services/response_parser.py, email_service.py
Depends on: re (stdlib)
"""

import re

# Block-level tags that become line breaks; remaining tags collapse to spaces.
_BLOCK_TAG_RE = re.compile(r"<br\s*/?>|</p>|</tr>|</li>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WS_RE = re.compile(r"[^\S\n]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_DISCLAIMER_RES = [
    re.compile(pat, re.IGNORECASE | re.DOTALL)
    for pat in (
        r"this email and any attachments.*?(?=\n\n|\Z)",
        r"confidentiality notice.*?(?=\n\n|\Z)",
        r"disclaimer.*?(?=\n\n|\Z)",
    )
]


def clean_email_body(body: str) -> str:
    """Strip HTML, excessive whitespace, and email disclaimers.

    Preserves newlines so tabular data and list formatting survive intact.
    """
    if not body:
        return ""
    text = _BLOCK_TAG_RE.sub("\n", body)
    text = _TAG_RE.sub(" ", text)
    text = _INLINE_WS_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    for disclaimer_re in _DISCLAIMER_RES:
        text = disclaimer_re.sub("", text)
    return text.strip()
