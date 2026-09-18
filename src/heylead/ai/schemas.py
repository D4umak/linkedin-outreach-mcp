"""JSON schemas for LLM structured output.

Every provider enforces these server-side (see LLMClient.generate_json), which
is what removed the old parse-then-repair-then-hope pipeline.

Two rules keep a schema portable across Gemini, Anthropic and OpenAI:

* every property listed in ``required``
* ``additionalProperties: false`` on every object

OpenAI's strict mode demands both, and the other two accept them. Free-form
nested objects cannot be expressed under those rules — where output genuinely
needs one (Pydantic-validated reasoning, the brand plan), the caller asks for
plain text and parses defensively instead.
"""

from __future__ import annotations

from typing import Any


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


_STR = {"type": "string"}


# An outreach invitation, follow-up DM or reply. `reasoning` is a short plain
# explanation kept for the audit trail — it used to be a free-form object,
# which no strict schema can express.
MESSAGE = _obj({"message": _STR, "reasoning": _STR})

# A comment on a prospect's post.
COMMENT = _obj({"comment": _STR, "style": _STR, "reasoning": _STR})

# message_validator's LLM second opinion.
VALIDATION = _obj({
    "valid": {"type": "boolean"},
    "issues": {"type": "array", "items": _STR},
    "reasoning": _STR,
})

# What the last exchange with a prospect used, for anti-repetition.
OUTREACH_MEMORY = _obj({"cta_used": _STR, "pain_used": _STR, "hook_angle": _STR})

# Which LinkedIn search parameter codes to keep.
SEARCH_PARAM_CODES = _obj({"param_codes": {"type": "array", "items": _STR}})

# Reply sentiment.
SENTIMENT = _obj({
    "sentiment": {
        "type": "string",
        "enum": [
            "positive", "negative", "question", "neutral",
            "engaged", "out_of_office", "opt_out", "calendar",
        ],
    },
})

# One turn of the short-loop agent kernel (JSON tools, not native tool_choice).
AGENT_STEP = _obj({
    "action": {
        "type": "string",
        "enum": [
            "read_thread", "read_timeline", "read_icp", "read_fit",
            "read_commons", "write_commons", "decide",
        ],
    },
    "decision": {
        "type": "string",
        "enum": ["hold", "skip", "reply", "book", "none"],
    },
    "reason": _STR,
    "done": {"type": "boolean"},
    "note": _STR,
})

# ICP research vertical — extra strings are a flat filter patch (empty = no change).
ICP_AGENT_STEP = _obj({
    "action": {
        "type": "string",
        "enum": [
            "read_icp", "preview_search", "read_filters",
            "read_commons", "write_commons", "decide",
        ],
    },
    "decision": {
        "type": "string",
        "enum": ["keep", "revise", "hold", "none"],
    },
    "reason": _STR,
    "done": {"type": "boolean"},
    "note": _STR,
    "titles_include": _STR,
    "titles_exclude": _STR,
    "locations_include": _STR,
    "locations_exclude": _STR,
    "industries_include": _STR,
    "industries_exclude": _STR,
})

# Strategist replan — remaining_json is leftover planned_actions (empty = skip_today).
REPLAN_AGENT_STEP = _obj({
    "action": {
        "type": "string",
        "enum": [
            "read_plan", "read_signals", "read_thread", "read_status",
            "read_commons", "write_commons", "decide",
        ],
    },
    "decision": {
        "type": "string",
        "enum": ["keep", "revise", "hold", "none"],
    },
    "reason": _STR,
    "done": {"type": "boolean"},
    "note": _STR,
    "remaining_json": _STR,
})

# Hot-lead closer — book only when email + ISO start are grounded in evidence.
CLOSER_AGENT_STEP = _obj({
    "action": {
        "type": "string",
        "enum": [
            "read_thread", "read_contact", "read_calendar_target",
            "read_commons", "write_commons", "decide",
        ],
    },
    "decision": {
        "type": "string",
        "enum": ["book", "hold", "none"],
    },
    "reason": _STR,
    "done": {"type": "boolean"},
    "note": _STR,
    "attendee_email": _STR,
    "start_iso": _STR,
    "duration_minutes": _STR,
})

# Coordinator — digest is sidecar-authored; the loop may note or hold.
COORDINATOR_AGENT_STEP = _obj({
    "action": {
        "type": "string",
        "enum": ["read_commons", "read_digest", "write_commons", "decide"],
    },
    "decision": {
        "type": "string",
        "enum": ["none", "note", "hold"],
    },
    "reason": _STR,
    "done": {"type": "boolean"},
    "note": _STR,
})

# Product / code agent — local git checkout only. Extra strings carry the patch.
PRODUCT_AGENT_STEP = _obj({
    "action": {
        "type": "string",
        "enum": [
            "read_file", "search_repo", "git_status",
            "read_commons", "write_commons", "decide",
        ],
    },
    "decision": {
        "type": "string",
        "enum": ["draft", "apply", "pr", "hold", "none"],
    },
    "reason": _STR,
    "done": {"type": "boolean"},
    "note": _STR,
    "path": _STR,
    "query": _STR,
    "patch": _STR,
    "paths": _STR,
    "pr_title": _STR,
    "pr_body": _STR,
})
