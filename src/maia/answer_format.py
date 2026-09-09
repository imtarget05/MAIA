"""Answer post-processing: strip technical leakage from LLM text.

The retrieval context wraps chunks in
    <retrieved_document id="..." source="..."> ... </retrieved_document>
as an injection boundary (see prompt.assemble). Some LLMs echo those tags
verbatim into the answer, and both system prompts ask the model to append a
`Sources:` footer. The Streamlit UI already renders citations from the
structured `citations[]` packet, so any in-text duplication looks
unprofessional and leaks chunk_ids.

clean_answer() is display-only: grounding/citation checks MUST run on the raw
LLM output first, then clean for storage/display.
"""
from __future__ import annotations

import re

_OPEN_TAG_RE = re.compile(r"<retrieved_document\b[^>]*>", re.IGNORECASE)
_CLOSE_TAG_RE = re.compile(r"</retrieved_document\s*>", re.IGNORECASE)
# Trailing footer the prompts request, e.g.:
#   Sources: [S1], [S2]
#   Sources: [S1] HR_Policy.md, [S2] Leave_Policy.md
#   Nguồn: [S1] ...   (agent sometimes writes Vietnamese variant)
_SOURCES_FOOTER_RE = re.compile(
    r"(?im)^\s*(sources?\s*:|nguồn\s*:)\s*(\[S\d+\][^\n]*)?\s*$"
)
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def clean_answer(text: str) -> str:
    """Remove boundary tags + redundant Sources footer from LLM text."""
    if not text:
        return text
    out = _OPEN_TAG_RE.sub("", text)
    out = _CLOSE_TAG_RE.sub("", out)
    # Drop footer lines, but only if at least one inline [Sn] remains
    # elsewhere (otherwise keep the cites so evidence is not lost).
    if _SOURCES_FOOTER_RE.search(out):
        without = _SOURCES_FOOTER_RE.sub("", out).strip()
        if re.search(r"\[S\d+\]", without):
            out = without
        # else: footer is the only citation carrier -> keep it; the UI
        # badge still shows structured sources, but at least text keeps [Sn].
    out = _BLANK_RUN_RE.sub("\n\n", out).strip()
    return out


def has_technical_leak(text: str) -> bool:
    """True if raw text still contains boundary tags or a Sources footer."""
    if not text:
        return False
    return bool(
        _OPEN_TAG_RE.search(text)
        or _CLOSE_TAG_RE.search(text)
        or _SOURCES_FOOTER_RE.search(text)
    )
