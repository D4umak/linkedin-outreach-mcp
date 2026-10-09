"""The language a campaign's messages are written in (outcome #2560).

One module answers three questions, and the client carries a byte-for-byte
twin of the detector (heylead-api ``app/services/language.py``) with the
same test vectors (``tests/data/language_vectors.json``):

- ``detect_language(text)``: which language a piece of text is in. Cyrillic
  is told apart by letter (і ї є ґ is Ukrainian, ы э ъ ё is Russian); Latin
  text by the share of common words of en, de, fr, es, pl, pt and it, plus
  diacritic hints. Too short or too close to call is ``und``. No dependency.
- ``message_language_for(setting, ...)``: which language the next message is
  written in. A prospect who wrote is answered in their language, whatever
  the setting; otherwise the campaign's fixed language; in ``prospect`` mode
  their LinkedIn profile's ``primary_locale``; else English. Never a name, a
  location or ``languages[]`` (decision #99).
- ``language_instruction(code)``: the one prompt line every prospect-facing
  generator carries, English included, so no prompt is ever silent on it.

The campaign setting is ``config_json["message_language"]``. Its absence means
"never chosen" (it behaves as English, but launch asks once when the brief is
in another language); an explicit ``en`` is a choice.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

SETTING_KEY = "message_language"

# The values the campaign setting accepts, in the order the UI lists them.
SUPPORTED: tuple[str, ...] = ("en", "prospect", "uk", "de", "fr", "es", "pl", "pt", "it")
# The fixed languages: every setting but ``prospect``.
FIXED: frozenset[str] = frozenset(SUPPORTED) - {"prospect"}
DEFAULT = "en"
UNDETERMINED = "und"

LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "uk": "Ukrainian",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "pl": "Polish",
    "pt": "Portuguese",
    "it": "Italian",
    "ru": "Russian",
    # Codes a LinkedIn profile's primary_locale can carry in prospect mode.
    "nl": "Dutch",
    "sv": "Swedish",
    "da": "Danish",
    "no": "Norwegian",
    "nb": "Norwegian",
    "fi": "Finnish",
    "cs": "Czech",
    "sk": "Slovak",
    "ro": "Romanian",
    "hu": "Hungarian",
    "tr": "Turkish",
    "el": "Greek",
    "ja": "Japanese",
    "zh": "Chinese",
    "ko": "Korean",
    "ar": "Arabic",
    "he": "Hebrew",
    "hi": "Hindi",
    "id": "Indonesian",
    "ms": "Malay",
    "th": "Thai",
    "vi": "Vietnamese",
}
PROSPECT_LABEL = "the prospect's language"


# ---------------------------------------------------------------------------
# Detector. Keep identical to the client's twin; the shared vectors pin both.
# ---------------------------------------------------------------------------

_MIN_LETTERS = 12
_MIN_SCORE = 0.08
_MIN_MARGIN = 0.03

_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_MENTION_RE = re.compile(r"@\w+")
_TOKEN_RE = re.compile(r"[^\W\d_]+")

_UK_MARKERS = frozenset("іїєґ")
_RU_MARKERS = frozenset("ыэъё")

_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset("""
        the and is are to of in that it for you with on this be have was at as
        not but we your i my can would will do if me so just about what how
        from our an or thanks hi they
    """.split()),
    "de": frozenset("""
        der die das und ist nicht ich sie es mit auf für den dem ein eine einen
        zu von wir ihr ihnen sich auch aber oder wie was noch nur bei haben
        sind wird kann gerne danke hallo mein uns schon
    """.split()),
    "fr": frozenset("""
        le la les et est des une un du pour pas vous nous je que qui dans sur
        avec ce cette mais ou il elle au aux sont merci bonjour votre vos mon
        très être avoir ai suis peut plus en
    """.split()),
    "es": frozenset("""
        el los las y es que de en un una por para con no se su al lo como más
        pero muy gracias hola usted nosotros estoy está tengo también ya sí mi
        del este esta son hay puede sobre
    """.split()),
    "pt": frozenset("""
        o os as e é que de do da dos das em um uma para com não se seu sua no
        na ao mais mas muito obrigado obrigada olá você nós estou está tenho
        também já sim meu isso são
    """.split()),
    "it": frozenset("""
        il lo gli le e è che di del della in un una per con non si suo sua nel
        nella al alla più ma molto grazie ciao lei noi sono ho anche già sì mio
        questo questa sei come
    """.split()),
    "pl": frozenset("""
        i w z na się nie to jest że do o jak ale po co tak za od dla jestem mam
        czy już bardzo dziękuję dzień dobry pan pani mnie może był jego tylko
        przez które który nas są tym
    """.split()),
}

_DIACRITICS: dict[str, frozenset[str]] = {
    "en": frozenset(),
    "pl": frozenset("ąęłńśźż"),
    "de": frozenset("äöüß"),
    "fr": frozenset("éèêàçù"),
    "es": frozenset("ñ¿¡áíóú"),
    "pt": frozenset("ãõç"),
    "it": frozenset("àèìòù"),
}
# Each diacritic adds this much per token, and the hint never adds more than
# the cap: a hint tips a close call, it never outweighs the words.
_DIACRITIC_WEIGHT = 0.25
_DIACRITIC_CAP = 0.10

LATIN_LANGUAGES: tuple[str, ...] = ("en", "de", "fr", "es", "pl", "pt", "it")


def _script(ch: str) -> str:
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return ""
    if name.startswith("CYRILLIC"):
        return "cyrillic"
    if name.startswith("LATIN"):
        return "latin"
    return "other"


def _strip_noise(text: str) -> str:
    text = _URL_RE.sub(" ", text)
    text = _EMAIL_RE.sub(" ", text)
    return _MENTION_RE.sub(" ", text)


def _cyrillic_language(lowered: str) -> str:
    uk = sum(1 for ch in lowered if ch in _UK_MARKERS)
    ru = sum(1 for ch in lowered if ch in _RU_MARKERS)
    if uk and not ru:
        return "uk"
    if ru and not uk:
        return "ru"
    if uk > ru:
        return "uk"
    if ru > uk:
        return "ru"
    return UNDETERMINED


def latin_scores(text: str) -> dict[str, float]:
    """Each Latin-script language's score for ``text`` (exposed for tests)."""
    lowered = _strip_noise(text or "").lower()
    tokens = _TOKEN_RE.findall(lowered)
    if not tokens:
        return dict.fromkeys(LATIN_LANGUAGES, 0.0)
    total = len(tokens)
    scores: dict[str, float] = {}
    for code in LATIN_LANGUAGES:
        words = _STOPWORDS[code]
        share = sum(1 for tok in tokens if tok in words) / total
        marks = _DIACRITICS[code]
        hint = 0.0
        if marks:
            count = sum(1 for ch in lowered if ch in marks)
            hint = min(_DIACRITIC_CAP, _DIACRITIC_WEIGHT * count / total)
        scores[code] = share + hint
    return scores


def detect_language(text: str) -> str:
    """The language ``text`` is written in: a code, ``ru`` or ``und``.

    ``und`` when the text has fewer than 12 letters once links, emails and
    @mentions are gone, when no script holds a majority, or when no language
    wins clearly. The code can be outside the setting's list (``ru``).
    """
    cleaned = _strip_noise(text or "")
    letters = [ch for ch in cleaned if ch.isalpha()]
    if len(letters) < _MIN_LETTERS:
        return UNDETERMINED
    scripts = [_script(ch) for ch in letters]
    total = len(letters)
    cyrillic = scripts.count("cyrillic")
    latin = scripts.count("latin")
    if cyrillic / total > 0.5:
        return _cyrillic_language(cleaned.lower())
    if latin / total > 0.5:
        scores = latin_scores(cleaned)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        (best, top), (_, second) = ranked[0], ranked[1]
        if top >= _MIN_SCORE and top - second >= _MIN_MARGIN:
            return best
        return UNDETERMINED
    return UNDETERMINED


# ---------------------------------------------------------------------------
# Setting, resolver, instruction.
# ---------------------------------------------------------------------------


def normalize_setting(value: Any) -> str | None:
    """The stored form of a ``message_language`` value, or None when empty.

    Raises ValueError for anything outside ``SUPPORTED``.
    """
    if value is None:
        return None
    code = str(value).strip().lower()
    if not code:
        return None
    if code not in SUPPORTED:
        raise ValueError(
            f"message_language must be one of {', '.join(SUPPORTED)}; got {value!r}"
        )
    return code


def setting_from_config(config: dict[str, Any] | None) -> str | None:
    """The campaign's chosen language, or None when it was never chosen.

    A stored value outside ``SUPPORTED`` reads as never chosen.
    """
    if not isinstance(config, dict):
        return None
    try:
        return normalize_setting(config.get(SETTING_KEY))
    except ValueError:
        return None


def _profile_locale(profile: dict[str, Any] | None) -> str:
    if not isinstance(profile, dict):
        return ""
    locale = profile.get("primary_locale")
    if isinstance(locale, dict):
        lang = locale.get("language")
    elif isinstance(locale, str):
        lang = locale
    else:
        lang = None
    if not isinstance(lang, str):
        return ""
    code = lang.strip().lower()[:2]
    return code if len(code) == 2 and code.isalpha() else ""


def message_language_for(
    setting: Any,
    *,
    prospect_last_message: str = "",
    profile: dict[str, Any] | None = None,
) -> str:
    """The language the next message to this prospect is written in.

    1. Their latest message, when its language is known, whatever the setting.
    2. A fixed setting.
    3. ``prospect``: their profile's ``primary_locale.language``, else English.
    4. Unset: English.
    """
    if prospect_last_message:
        heard = detect_language(prospect_last_message)
        if heard != UNDETERMINED:
            return heard
    code = str(setting or "").strip().lower()
    if code in FIXED:
        return code
    if code == "prospect":
        return _profile_locale(profile) or DEFAULT
    return DEFAULT


def language_name(code: str | None) -> str:
    """The English name of a language code; ``prospect`` reads as a phrase."""
    key = str(code or "").strip().lower()
    if key == "prospect":
        return PROSPECT_LABEL
    return LANGUAGE_NAMES.get(key, LANGUAGE_NAMES[DEFAULT])


def language_instruction(code: str | None) -> str:
    """The one prompt line that says which language to write in.

    English gets its line too, so every generator always carries one.
    ``und`` and unknown codes read as English.
    """
    key = str(code or "").strip().lower()
    name = LANGUAGE_NAMES.get(key, LANGUAGE_NAMES[DEFAULT])
    return f"Write this message in {name}. Keep names, company names and quoted text as they are."


def keep_draft_language_instruction() -> str:
    """The line a rewrite pass carries: it never changes the draft's language."""
    return (
        "Keep the draft's language: write the result in the same language as the draft. "
        "Keep names, company names and quoted text as they are."
    )


def brief_language(project_brief: str = "", target_description: str = "") -> str:
    """The language a campaign's brief is written in (the api's one source)."""
    return detect_language(f"{project_brief or ''}\n{target_description or ''}")


def campaign_language(campaign_context: dict[str, Any] | None) -> str:
    """The campaign setting carried on a generator's ``campaign_context``."""
    if not isinstance(campaign_context, dict):
        return ""
    value = campaign_context.get(SETTING_KEY)
    return str(value).strip().lower() if value else ""


def resolve_for_context(
    campaign_context: dict[str, Any] | None,
    *,
    prospect_last_message: str = "",
    profile: dict[str, Any] | None = None,
) -> str:
    """``message_language_for`` with the setting read off ``campaign_context``."""
    return message_language_for(
        campaign_language(campaign_context),
        prospect_last_message=prospect_last_message,
        profile=profile,
    )


# ---------------------------------------------------------------------------
# Fixed strings that reach a prospect without a model writing them.
# ---------------------------------------------------------------------------
#
# Every language in FIXED has each key. None of them opens with a greeting
# (Denys, 9 Oct 2026: "we never open conversation with hello", in any
# language): a subject or an email body starts with the substance.

_FIXED_COPY: dict[str, dict[str, str]] = {
    # The email/InMail subject that asserts nothing about the reader.
    "subject_company": {
        "en": "Quick question about {company}",
        "uk": "{company}: коротке запитання",
        "de": "Kurze Frage zu {company}",
        "fr": "Petite question sur {company}",
        "es": "Una pregunta sobre {company}",
        "pl": "Krótkie pytanie o {company}",
        "pt": "Uma pergunta sobre {company}",
        "it": "Una domanda su {company}",
    },
    "subject_plain": {
        "en": "Quick question",
        "uk": "Коротке запитання",
        "de": "Kurze Frage",
        "fr": "Petite question",
        "es": "Una pregunta rápida",
        "pl": "Krótkie pytanie",
        "pt": "Uma pergunta rápida",
        "it": "Una domanda veloce",
    },
    # The calendar event the closer books.
    "intro_call": {
        "en": "Intro call with {name}",
        "uk": "Дзвінок-знайомство: {name}",
        "de": "Kennenlerngespräch mit {name}",
        "fr": "Appel découverte avec {name}",
        "es": "Llamada de presentación con {name}",
        "pl": "Rozmowa zapoznawcza: {name}",
        "pt": "Conversa de apresentação com {name}",
        "it": "Chiamata conoscitiva con {name}",
    },
    # The closer's booking-link email.
    "booking_subject": {
        "en": "A time that works",
        "uk": "Зручний час для дзвінка",
        "de": "Ein passender Termin",
        "fr": "Un créneau qui vous convient",
        "es": "Un horario que le venga bien",
        "pl": "Dogodny termin",
        "pt": "Um horário que funcione",
        "it": "Un orario che vada bene",
    },
    "booking_link": {
        "en": "Here is a link to pick a time: {link}",
        "uk": "Оберіть, будь ласка, зручний час за посиланням: {link}",
        "de": "Hier ist ein Link, um einen Termin zu wählen: {link}",
        "fr": "Voici un lien pour choisir un créneau : {link}",
        "es": "Aquí tiene un enlace para elegir un horario: {link}",
        "pl": "Oto link do wyboru terminu: {link}",
        "pt": "Aqui está um link para escolher um horário: {link}",
        "it": "Ecco un link per scegliere un orario: {link}",
    },
    "booking_no_link": {
        "en": "Which time would work for you?",
        "uk": "Напишіть, будь ласка, коли вам зручно.",
        "de": "Welcher Termin passt Ihnen?",
        "fr": "Quel créneau vous conviendrait ?",
        "es": "¿Qué horario le vendría bien?",
        "pl": "Który termin byłby dogodny?",
        "pt": "Que horário funciona melhor para você?",
        "it": "Quale orario le andrebbe bene?",
    },
    # The one-line reply to a comment on the owner's own post; {{0}} is the
    # commenter's first name, filled in at send time.
    "comment_thanks": {
        "en": "Thanks, {{0}}!",
        "uk": "Дякую!",
        "de": "Danke, {{0}}!",
        "fr": "Merci, {{0}} !",
        "es": "¡Gracias, {{0}}!",
        "pl": "Dziękuję!",
        "pt": "Agradeço, {{0}}!",
        "it": "Grazie, {{0}}!",
    },
}


def fixed_copy(key: str, code: str | None, **values: str) -> str:
    """A fixed string in language ``code`` (English when it has none).

    ``values`` fill the template; a template without a placeholder ignores
    them. Raises KeyError for an unknown key: a typo must not send English.
    """
    table = _FIXED_COPY[key]
    template = table.get(str(code or "").strip().lower()) or table[DEFAULT]
    if not values:
        return template
    return template.format(**values)


def apply_campaign_language(context: dict[str, Any], config: dict[str, Any] | None) -> dict[str, Any]:
    """Put the campaign's chosen language on a generator context, from config.

    Called AFTER a ``**context_json`` merge: the client pushes context_json
    whole, so a stale or foreign ``message_language`` there must never stand
    in for the setting (QA F3). Unset stays unset (the key is removed).
    """
    context.pop(SETTING_KEY, None)
    chosen = setting_from_config(config)
    if chosen:
        context[SETTING_KEY] = chosen
    return context


def language_unconfirmed(
    config: dict[str, Any] | None, project_brief: str = "", target_description: str = "",
) -> str:
    """The brief's language when launch must ask first, else "".

    Launch pauses once: the setting was never chosen and the brief (or the
    target) reads as a known language other than English. Any explicit
    choice, English included, answers it for good.
    """
    if setting_from_config(config):
        return ""
    found = brief_language(project_brief, target_description)
    # Only a language the setting can take: a Russian brief has no answer
    # the user could pick, so it does not hold the launch.
    if found == DEFAULT or found not in FIXED:
        return ""
    return found


def unconfirmed_message(found: str) -> str:
    """The designer's launch question, for the api's 409 and the MCP reply."""
    name = language_name(found)
    return (
        f"Not launched yet. This campaign's brief is in {name} and its messages will go out "
        f"in English. Ask the user: \"Messages will go out in English. Write them in {name}?\" "
        f"Then edit_campaign(message_language='{found}' or 'en'), and launch again."
    )


def launched_language_properties(config: dict[str, Any] | None) -> dict[str, str]:
    """``campaign.launched``'s language properties (codes only, never text).

    ``message_language`` is the setting (en, prospect or a fixed code; a
    stored value outside the list is ``other``); ``language_set_by`` is
    ``owner`` when someone chose it and ``default`` when it was never set.
    ``written_in`` is what launch tells the user.
    """
    chosen = setting_from_config(config)
    raw = (config or {}).get(SETTING_KEY) if isinstance(config, dict) else None
    if chosen:
        return {"message_language": chosen, "language_set_by": "owner", "written_in": chosen}
    if raw:
        return {"message_language": "other", "language_set_by": "owner", "written_in": DEFAULT}
    return {"message_language": DEFAULT, "language_set_by": "default", "written_in": DEFAULT}


def written_in_phrase(code: str | None) -> str:
    """What follows "Messages are written in" for a setting (designer's copy)."""
    key = str(code or "").strip().lower()
    if key == "prospect":
        return (
            "each prospect's language: their latest message, else their LinkedIn "
            "profile's language, else English"
        )
    return language_name(key or DEFAULT)


def create_language_lines(
    config: dict[str, Any] | None, project_brief: str = "", target_description: str = "",
) -> str:
    """The create reply's language lines (designer's MCP copy, #2560)."""
    chosen = setting_from_config(config)
    lines = [
        f"Language: messages are written in {written_in_phrase(chosen or DEFAULT)}. "
        "Change it with edit_campaign(message_language='prospect') for each prospect's "
        "language, or a code: uk, de, fr, es, pl, pt, it."
    ]
    found = language_unconfirmed(config, project_brief, target_description)
    if found:
        name = language_name(found)
        lines.append(
            f"Ask the user before launch: \"Messages will go out in English. Write them in {name}?\" "
            "Change it only on their answer."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Client glue: the one place every local writer reads its language line from.
# ---------------------------------------------------------------------------


def validate_setting(value: Any) -> str:
    """An error line for an unknown ``message_language``, or "" when valid."""
    try:
        normalize_setting(value)
    except ValueError:
        return (
            f"Unknown message_language '{value}'. Use 'en', 'prospect' (the "
            "prospect's language) or a code: uk, de, fr, es, pl, pt, it."
        )
    return ""


def latest_prospect_text(
    history: list[dict[str, Any]] | None, reply_text: str | None = None,
) -> str:
    """The prospect's latest words: the reply being answered, else history's last."""
    if reply_text and str(reply_text).strip():
        return str(reply_text)
    for msg in reversed(history or []):
        if isinstance(msg, dict) and msg.get("role") == "prospect":
            text = str(msg.get("text") or "")
            if text.strip():
                return text
    return ""


def _setting_of(*sources: dict[str, Any] | None) -> str:
    for src in sources:
        value = campaign_language(src)
        if value:
            return value
    return ""


def resolve_for_prompt(
    campaign_config: dict[str, Any] | None,
    campaign_context: dict[str, Any] | None = None,
    prospect: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    reply_text: str | None = None,
) -> str:
    """The resolved code for one message, from what a local writer holds."""
    return message_language_for(
        _setting_of(campaign_config, campaign_context),
        prospect_last_message=latest_prospect_text(history, reply_text),
        profile=prospect,
    )


def language_rule_for(
    campaign_config: dict[str, Any] | None,
    campaign_context: dict[str, Any] | None = None,
    prospect: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    reply_text: str | None = None,
) -> str:
    """The language line for a local writer's prompt.

    Once the prospect has written, a second sentence keeps the reply rule:
    whatever the setting, they are answered in the language they used (the
    detector can call a short reply ``und``; the model still sees it).
    """
    code = resolve_for_prompt(campaign_config, campaign_context, prospect, history, reply_text)
    line = language_instruction(code)
    if latest_prospect_text(history, reply_text):
        line += (
            " If the prospect's latest message is in another language, reply in "
            "the language they used."
        )
    return line


def with_language_rule(system: str, rule: str) -> str:
    """``system`` carrying ``rule`` once (a rendered template may already hold it)."""
    system = system or ""
    if not rule or rule in system:
        return system
    if not system.strip():
        return f"LANGUAGE: {rule}"
    return f"{system.rstrip()}\n\nLANGUAGE: {rule}"


def language_needs_asking(
    config: dict[str, Any] | None, context: dict[str, Any] | None,
) -> str:
    """The brief's language when launch must ask first, else "".

    The api twin ``language_unconfirmed`` over this campaign's brief and
    target: asked once, while the setting was never chosen and the brief reads
    as a fixed language other than English.
    """
    return language_unconfirmed(
        config,
        str((context or {}).get("project_brief") or ""),
        str((config or {}).get("target_description") or ""),
    )


def is_language_unconfirmed(detail: Any) -> str:
    """The brief's language when the api refused with ``language_unconfirmed``.

    The api's launch answers 409 ``{"code": "language_unconfirmed",
    "brief_language": "uk"}``; the client sees it as a dict or its text.
    Returns "" for any other answer.
    """
    if isinstance(detail, dict):
        data = detail.get("detail") if isinstance(detail.get("detail"), dict) else detail
    else:
        text = str(detail or "")
        if "language_unconfirmed" not in text:
            return ""
        import json

        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            match = re.search(r"brief_language['\"]?\s*[:=]\s*['\"]?([a-z]{2})", text)
            return match.group(1) if match else UNDETERMINED
        if isinstance(data, dict) and isinstance(data.get("detail"), dict):
            data = data["detail"]
    if not isinstance(data, dict) or data.get("code") != "language_unconfirmed":
        return ""
    return str(data.get("brief_language") or UNDETERMINED).strip().lower()


def not_launched_text(brief_code: str) -> str:
    """Launch's answer while the language was never chosen (the api's words)."""
    return unconfirmed_message(brief_code)


def launched_language_line(config: dict[str, Any] | None) -> str:
    """Launch's second line: which language the messages are written in."""
    return f"Messages are written in {written_in_phrase(setting_from_config(config) or DEFAULT)}."
