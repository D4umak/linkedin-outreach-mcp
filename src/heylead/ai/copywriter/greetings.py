"""Greeting openers and sign-offs, in every message language (heylead-api#2560).

Denys, 9 Oct 2026: "we never open conversation with hello", and the house
rules hold whatever language a campaign writes in. The checks were English
only ("Hi", "Best"), so a Ukrainian draft opening "Добрий день, Олено!" or
closing "З повагою" passed every one of them.

One place for the words, Unicode-aware, used by the validator (a failed
stage sends the draft to the fixer), the polish pass (rule
``no-greeting-opener``) and the generators' last-resort strip. Languages:
en, uk, de, fr, es, pl, pt, it (and the Russian forms a model slips into).
The word lists are a superset of heylead-api copywriter/polish.py's
_GREETINGS and _SIGN_OFF_WORDS; add a word there and here together.
"""

from __future__ import annotations

import re

# Longest first inside each language, so "good morning" wins over "good".
GREETINGS: dict[str, tuple[str, ...]] = {
    "en": (
        "good morning", "good afternoon", "good evening", "greetings",
        "hello", "hiya", "hey", "dear", "hi",
    ),
    "uk": (
        "доброго ранку", "доброго вечора", "доброго дня", "добрий ранок",
        "добрий вечір", "добрий день", "шановна", "шановний", "шановні",
        "привіт", "вітаю", "вітання", "добридень", "здрастуйте",
    ),
    "ru": (
        "здравствуйте", "добрый день", "доброе утро", "добрый вечер",
        "приветствую", "привет",
    ),
    "de": (
        "guten morgen", "guten abend", "guten tag", "sehr geehrte",
        "sehr geehrter", "liebes", "liebe", "lieber", "hallo", "servus", "moin",
    ),
    "fr": ("bonjour", "bonsoir", "salut", "chère", "cher"),
    "es": (
        "buenos días", "buenos dias", "buenas tardes", "buenas noches",
        "estimada", "estimado", "hola",
    ),
    "pl": (
        "dzień dobry", "dzien dobry", "szanowna", "szanowny", "witam",
        "witaj", "cześć", "czesc", "hej",
    ),
    "pt": (
        "bom dia", "boa tarde", "boa noite", "prezada", "prezado", "olá",
        "ola", "oi",
    ),
    "it": (
        "buongiorno", "buonasera", "gentilissima", "gentilissimo", "gentile", "salve", "ciao",
    ),
}

SIGN_OFFS: dict[str, tuple[str, ...]] = {
    "en": (
        "kind regards", "warm regards", "best regards", "best wishes",
        "all the best", "thank you", "regards", "sincerely", "cordially",
        "cheers", "thanks", "best", "yours",
    ),
    "uk": (
        "з найкращими побажаннями", "з повагою", "щиро ваш", "щиро ваша",
        "дякую", "гарного дня",
    ),
    "ru": ("с уважением", "всего доброго", "спасибо"),
    "de": (
        "mit freundlichen grüßen", "mit freundlichen grüssen",
        "freundliche grüße", "viele grüße", "beste grüße", "liebe grüße",
        "herzliche grüße", "grüße", "danke", "lg",
    ),
    "fr": (
        "bien cordialement", "bien à vous", "bonne journée", "cordialement", "amicalement",
        "salutations", "merci",
    ),
    "es": (
        "un saludo", "un abrazo", "atentamente", "saludos", "gracias",
    ),
    "pl": (
        "z wyrazami szacunku", "z poważaniem", "pozdrawiam", "pozdrowienia",
        "dziękuję",
    ),
    "pt": (
        "atenciosamente", "cumprimentos", "abraços", "abraço", "obrigado",
        "obrigada",
    ),
    "it": (
        "cordiali saluti", "distinti saluti", "buona giornata", "un saluto", "saluti", "grazie",
    ),
}


def _alternation(words: dict[str, tuple[str, ...]]) -> str:
    flat = sorted({w for group in words.values() for w in group}, key=len, reverse=True)
    return "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in flat)


_WORD = r"[^\W\d_][\w'’\-]*"
_NAME_TOKEN = r"(?:\{\{\d+\}\}|" + _WORD + r")"

# A greeting at the very start, an optional address ("there", the reader's
# name in any case, or a mention token), and the punctuation that ends it.
# The look-ahead after the greeting keeps "Hiring", "Олена" or "Ciaone" out.
GREETING_OPENER = re.compile(
    r"^[\s\W]*(?:" + _alternation(GREETINGS) + r")(?![\w])"
    r"(?:\s*,?\s*" + _NAME_TOKEN + r"(?:\s+" + _WORD + r")?)?"
    r"\s*(?:[,!.:;—–\-]+\s*|\n+\s*|$)",
    re.IGNORECASE | re.UNICODE,
)

# A sign-off alone on its own last line, optionally followed by a name, or a
# trailing "- Name". Never the last word of a sentence ("suits you best"), and
# never a one-line message ("Thanks, Olena!" is a reply, not a signature).
SIGN_OFF = re.compile(
    r"\n\s*(?:" + _alternation(SIGN_OFFS) + r")(?![\w])"
    r"\s*[,.!]?\s*(?:" + _WORD + r")?\s*[.!]?\s*$"
    r"|\n\s*[-–—]+\s*" + _WORD + r"\s*\.?\s*$",
    re.IGNORECASE | re.UNICODE,
)


# The looser test the validator uses: a greeting word first, whatever follows
# ("Hi Arina saw your post" has no comma and is still a greeting).
GREETING_WORD = re.compile(
    r"^[\s\W]*(?:" + _alternation(GREETINGS) + r")(?![\w])",
    re.IGNORECASE | re.UNICODE,
)


def opens_with_greeting(text: str | None) -> bool:
    """True when the draft's first words are a greeting, in any message language."""
    return bool(GREETING_WORD.match(text or ""))


def greeting_opener(text: str | None) -> str:
    """The greeting a draft opens with ("Добрий день, Олено! "), or ""."""
    match = GREETING_OPENER.match(text or "")
    return match.group(0) if match else ""


def strip_greeting_opener(text: str | None) -> str:
    """The draft without its greeting opener; unchanged when that leaves nothing."""
    original = text or ""
    opener = greeting_opener(original)
    if not opener:
        return original
    rest = original[len(opener):].lstrip()
    if not rest:
        return original
    return rest[0].upper() + rest[1:]


def has_sign_off(text: str | None) -> bool:
    """True when the draft ends with a sign-off, in any message language."""
    return bool(SIGN_OFF.search((text or "").rstrip()))


def strip_sign_off(text: str | None) -> str:
    """The draft without a closing sign-off; unchanged when that leaves nothing."""
    original = (text or "").rstrip()
    stripped = SIGN_OFF.sub("", original).rstrip()
    return stripped or original
