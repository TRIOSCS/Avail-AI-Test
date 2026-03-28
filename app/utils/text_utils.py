"""text_utils.py — Shared text cleaning utilities.

Called by: services/ai_email_parser.py, services/response_parser.py, email_service.py
Depends on: re (stdlib)
"""

import re


def clean_email_body(body: str) -> str:
    """Strip HTML, excessive whitespace, and email disclaimers.

    Preserves newlines so tabular data and list formatting survive intact.
    """
    if not body:
        return ""
    text = re.sub(r"<br\s*/?>|</p>|</tr>|</li>", "\n", body, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    disclaimer_patterns = [
        r"(?i)this email and any attachments.*?(?=\n\n|\Z)",
        r"(?i)confidentiality notice.*?(?=\n\n|\Z)",
        r"(?i)DISCLAIMER.*?(?=\n\n|\Z)",
    ]
    for pat in disclaimer_patterns:
        text = re.sub(pat, "", text, flags=re.DOTALL)
    return text.strip()
