"""Where a message came from: the prompt, its version, the model and the variant.

Twin of heylead-api's app/services/copywriter/provenance.py (outcome
heylead-api#1210). Until 24 Sep 2026 intent.select_prompt promised that the
resolved prompt name "is logged by callers into the message audit trail",
and no caller did: ``messages`` had no column for it, and the A/B variant was
a sentence appended to the prompt, untraceable after the send.

How it travels, without changing what any generator returns:

* ``prompt_loader.render_prompt`` is the one door every templated generator
  goes through. Rendering a template in ``MESSAGE_TEMPLATES`` starts the
  task's current draft: its resolved name (``outreach_invitation_buy`` when
  the buy variant exists) and version.
* ``LLMClient`` notes the model that answered; the first answer after the
  render is the draft's model.
* ``generate_send`` notes the A/B arm when a running test's instruction was
  applied to this prospect.
* ``queries.save_message`` takes the draft for the outbound row it writes
  (``take`` clears it, so a draft is stamped on one row only) unless the
  caller names the provenance itself, as the cloud pull does.

The prompt version is ``<declared>-<hash>``: the template's own ``version``
field, then eight hex digits of its content, so a reworded template moves
even when nobody bumps the number.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import weakref
from dataclasses import asdict, dataclass, replace
from typing import Any

FIELDS = ("prompt_name", "prompt_version", "model", "variant")

# Templates whose rendered text becomes a message a person receives, by base
# name; an intent suffix (``_buy``) resolves to the same base. Every other
# template is listed in NOT_A_MESSAGE, and a test refuses a template file in
# neither set, so a new message template cannot ship unattributed.
MESSAGE_TEMPLATES = frozenset({
    "outreach_invitation", "outreach_inmail", "outreach_job_search",
    "followup_message", "outreach_response",
})
NOT_A_MESSAGE = frozenset({
    # The system prompt and the passes that judge or rewrite a draft someone
    # else wrote: the draft keeps the name of the template that wrote it.
    "outreach_system", "outreach_validate", "followup_validate",
    "message_fix", "followup_fix", "message_improve",
    "followup_reasoning", "followup_news",
    # Not messages at all.
    "comment_main", "filter_candidates", "prospect_analysis", "voice_analysis",
})


@dataclass(frozen=True)
class Provenance:
    prompt_name: str = ""
    prompt_version: str = ""
    model: str = ""
    variant: str = ""

    def as_row(self) -> dict[str, str]:
        return {k: str(v or "") for k, v in asdict(self).items()}

    @classmethod
    def from_row(cls, row: dict[str, Any] | None) -> "Provenance":
        """The four fields off any row or payload; a missing or NULL one is ""."""
        row = row or {}
        return cls(**{f: str(row.get(f) or "") for f in FIELDS})


EMPTY = Provenance()
# An outbound row with no words in it (an invitation sent without a note).
NO_COPY = Provenance(prompt_name="none", prompt_version="none")


def _owner() -> Any:
    """The running task, weakly; None off the event loop (the DB thread)."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return None
    return weakref.ref(task) if task is not None else None


class _Scope:
    """One task's current draft. A child task gets its own (see _owner), so
    two prospects written concurrently never trade drafts."""

    __slots__ = ("draft", "awaiting_model", "variant", "owner")

    def __init__(self) -> None:
        self.draft: Provenance = EMPTY
        self.awaiting_model = False
        self.variant = ""
        self.owner = _owner()


_scope: contextvars.ContextVar[_Scope | None] = contextvars.ContextVar(
    "copy_provenance_scope", default=None,
)


def _current_scope() -> _Scope:
    """The scope a writer in this task writes to, made on first use.

    A task inherits its parent's context, and with it the parent's scope
    object; writing into that would hand a sibling's draft to whichever
    task saves first. So a scope belongs to the task that made it, and any
    other task starts its own.
    """
    scope = _scope.get()
    owner = _owner()
    if scope is None or (owner is not None and (
        scope.owner is None or scope.owner() is not owner()
    )):
        scope = _Scope()
        _scope.set(scope)
    return scope


def begin() -> None:
    """Start a fresh scope: nothing drafted before this point is current."""
    _scope.set(_Scope())


def message_base(name: str) -> str:
    """The message template *name* resolves from, or "" when it is not one."""
    for base in MESSAGE_TEMPLATES:
        if name == base or name.startswith(base + "_"):
            return base
    return ""


def prompt_version(data: dict[str, Any]) -> str:
    declared = str(data.get("version") or "0").strip()
    if not declared.lower().startswith("v"):
        declared = f"v{declared}"
    digest = hashlib.sha256(str(data.get("content") or "").encode("utf-8")).hexdigest()[:8]
    return f"{declared}-{digest}"


def note_template(name: str, data: dict[str, Any]) -> None:
    """A template was rendered. Only a message template starts a draft."""
    if not message_base(name):
        return
    scope = _current_scope()
    scope.draft = Provenance(
        prompt_name=name, prompt_version=prompt_version(data), variant=scope.variant,
    )
    scope.awaiting_model = True


def note_model(model: str) -> None:
    """A model answered. The first answer after a message template is its model."""
    scope = _scope.get()
    if scope is None or not scope.awaiting_model:
        return
    scope.draft = replace(scope.draft, model=str(model or ""))
    scope.awaiting_model = False


def note_variant(variant: str) -> None:
    """The A/B arm whose instruction the next draft carries ("" for none)."""
    _current_scope().variant = str(variant or "")


def current() -> Provenance:
    scope = _scope.get()
    return scope.draft if scope else EMPTY


def take() -> Provenance:
    """The current draft's provenance, cleared so it is stamped on one row only."""
    scope = _scope.get()
    if scope is None:
        return EMPTY
    draft = scope.draft
    scope.draft, scope.awaiting_model, scope.variant = EMPTY, False, ""
    return draft


def for_outbound(text: str, explicit: Provenance | None) -> Provenance:
    """What an outbound row records: the caller's, else the draft's."""
    if not str(text or "").strip():
        if explicit is None:
            take()
        return NO_COPY
    if explicit is not None:
        return explicit
    return take()
