"""Remove the withdrawn AI sentence from past sent text before it is read again.

From 26 Sep 15:25 to 28 Sep 15:35 London 2026 a fixed sentence saying the
message was sent with the sender's AI assistant was appended to first touches
(api #1481). Denys never agreed to it, and the feature is gone (api #1771,
client #696; outcome api #1766). The stored ``sdr`` rows of those days still
end with it, and past sent text is read back into generation: the exemplar
lane stores it as ``reply_exemplar`` knowledge, and every history builder
pastes it into a prompt. A model shown its own earlier message with that
sentence copies the sentence.

So every reader of past sent text goes through ``strip_withdrawn_sentence``:
``LLMClient._call_provider`` (every prompt a local key answers),
``llm_router.call_llm`` (the prompt handed to the backend), and
``prepare_outbound_text`` on the wire. The backend's twin
(heylead-api app/services/withdrawn_sentence.py) covers the hosted lanes.
``tests/test_withdrawn_sentence_never_reaches_a_prompt.py`` holds that list.

The pattern is assembled from parts, not written as the sentence: the semgrep
rule ``a-message-that-says-an-ai-sent-it`` refuses any string literal that
carries it, and this module must not become the one place that does.
"""

from __future__ import annotations

import re

# "my", "ai", "assistant" with any spacing and case: the phrase the guard
# regex covers (the owner, then the two words), whatever verb came first.
_PHRASE = r"\bmy\s+ai\s+assistant\b"

# The whole sentence around the phrase: from the end of the previous sentence
# (or a bracket) to its own terminator, with the blank it sat after.
_SENTENCE = re.compile(
    r"[ \t]*\(?[^.!?\n()]*" + _PHRASE + r"[^.!?\n()]*[.!?]*\)?",
    re.IGNORECASE,
)
_FIND = re.compile(_PHRASE, re.IGNORECASE)


def carries_withdrawn_sentence(text: str | None) -> bool:
    """True when ``text`` still carries the withdrawn sentence."""
    return bool(_FIND.search(text or ""))


def strip_withdrawn_sentence(text: str) -> str:
    """``text`` without the withdrawn sentence; unchanged when it has none.

    Idempotent. Only text that carried the sentence is re-spaced, so ordinary
    copy and prompts come back byte for byte.
    """
    if not text or not _FIND.search(text):
        return text
    out = _SENTENCE.sub("", text)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


__all__ = ["carries_withdrawn_sentence", "strip_withdrawn_sentence"]
