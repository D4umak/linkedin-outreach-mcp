"""A prospect who calls our LinkedIn messages AI is held for the owner (heylead-api #7762).

The phrase floor only: a copy of heylead-api app/services/ai_callout.py (rules and
normalisation), with no model call. A bare "AI" / "ШІ" / "бот" never fires: every rule needs
an asking or accusing frame around the term. tests/test_ai_callout_7762.py pins this copy
to the api's held-out fixtures.

The laptop client refuses an autonomous reply while a call-out sits in their turns since our
last sdr row (reply_to_prospect and the scheduler's _execute_auto_reply), and parks the
thread for the owner with reason_code ai_callout. An explicit stop in the same message
still opts them out: the opt-out paths run first. Regex, not textutil.contains_term: the
rules are framed phrases with inflected Cyrillic and Polish endings, not single terms.
"""
from __future__ import annotations

import re

_F = re.IGNORECASE | re.UNICODE
E = r"(?!\w)"
# Ends a short question/accusation: punctuation, end, or a following "or"-word.
T = r"(?=\s*(?:[.!?,;:)\]]|$)|\s+(?:or|чи|или|czy|oder|chy|chi|ili|lol|haha|right|too|writing)\b)"
AI = (r"(?:an?\s+|the\s+|some\s+)?(?:ai|a\.i\.?|artificial intelligence|chat ?gpt|gpt(?:-?\d\w*)?"
      r"|llm|bot|robot|machine|automation|script)")
SL_AI = (r"(?:ш[іi]|ии|штучн\w* інтелект\w*|искусственн\w* интеллект\w*|нейронк\w*|нейромереж\w*"
         r"|нейросет\w*|нейросітк\w*|чат ?гпт\w*|гпт\w*|chat ?gpt\w*|gpt\w*|бот\w*|робот\w*|ai)")
TR_AI = (r"(?:ai|ii|shi|ki|sztuczn\w* inteligencj\w*|chat ?gpt\w*|gpt\w*|bota|botom|botem|robotem"
         r"|ne[iy]ronk\w*|ne[iy]roset\w*|maschine|roboter|einer ki|der ki|einem bot)")
MSG = (r"(?:messages?|messaging|msg|text|dms?|note|e-?mail|pitch|outreach|spam|garbage|slop|nonsense|crap"
       r"|reply|response|nachricht\w*|wiadomo\w*|tekst\w*)")
LIKE_T = (r"(?=\s*(?:[.!?,;:)]|$)|\s+(?:wrote|written|generated|made|text|message|dm|copy|slop|output"
          r"|to me|написав|написал\w*|писав|писал\w*|написано|згенерова\w*|сгенерирова\w*"
          r"|napisał\w*|geschrieben|generiert|текст\w*|повідомлен\w*|сообщени\w*)\b)")
HUM = (r"(?:an?\s+|the\s+)?(?:real\s+|actual\s+|live\s+)?(?:person|human(?: being)?|guy|людин\w*"
       r"|жив\w* людин\w*|человек\w*|жив\w* человек\w*|człowiek\w*|osob\w*|mensch\w*|echter mensch"
       r"|liudyn\w*|lyudyn\w*|ludyn\w*|chelovek\w*)")
MAC = (r"(?:an?\s+|the\s+|ein\w*\s+)?(?:bot(?:s|a|em|om|y)?|robot(?:s|a|em|om|y)?|ai|a\.i\.?|chat ?gpt|gpt|machine|automation"
       r"|chatbot|бот\w*|робот\w*|ш[іi]|ии|машин\w*|нейронк\w*|нейросет\w*|ki|maschine|shi|ii)")
OR = r"\s*,?\s*(?:or|чи|или|czy|oder|chy|chi|ili)\s+"

_SPEC = {
    "prompt_left": [
        r"\b(?:nice|good|great|cool|lovely|neat|fajny|dobry|niezły|netter|guter|schöner|toller"
        r"|гарний|хороший|класний|непоганий|klasnyi|khoroshyi|horoshiy)\s+(?:prompt|промпт)\w*",
        r"\b(?:left|forgot|forget|leave|leaving|забули|забув|забула|забыли|забыл|забыла|zapomniał\w*)"
        r"\s+(?:\S+\s+){0,3}?(?:the\s+|your\s+|ваш\w*\s+|свій\s+)?(?:prompt|промпт)\w*",
        r"\b(?:prompt|промпт)\w*\s+(?:\S+\s+){0,2}?(?:vergessen|leaked|is showing|showing|visible)",
        r"\bas an ai(?: language)? model\b",
    ],
    "human_or_machine": [
        rf"\b{HUM}{OR}{MAC}{T}", rf"\b{MAC}{OR}{HUM}{T}",
        rf"\b(?:are|r) (?:you|u) (?:an?\s+)?(?:real\s+|actual\s+|live\s+)?(?:person|human){T}",
        r"\b(?:is (?:this|there)|am i (?:talking|speaking|chatting|writing) (?:to|with)) (?:an?\s+)?"
        r"(?:real|actual|live) (?:person|human)",
        r"\b(?:ти|ви|ты|вы)\s+(?:жив\w*|справжн\w*|настоящ\w*|реальн\w*)\s+(?:людин\w*|человек\w*)",
        rf"\b(?:jesteś|bist du|sind sie) (?:prawdziw\w* |ein |eine )?(?:echter )?(?:człowiekiem|mensch){T}",
    ],
    "written_by": [
        rf"\b(?:written|generated|created|made|composed|produced|crafted|drafted) (?:by|with|using|via"
        rf"|through|in) {AI}{E}",
        r"\b(?:ai|chat ?gpt|gpt(?:-?\d\w*)?|a bot|the bot|robot|machine) (?:wrote|generated|composed) (?:this|that|it)\b",
        r"\bdid (?:an? |the )?(?:ai|chat ?gpt|gpt|bot|robot) (?:write|generate|draft|compose)\b",
        rf"\bdid (?:you|u) use (?:an? )?(?:ai|chat ?gpt|gpt|bot|llm)(?:\s+(?:to (?:write|draft|generate|craft)"
        rf"|for (?:this|that|it))|{T})",
        rf"\b(?:write|wrote|generate|generated|draft|drafted) (?:this|that|it) (?:with|using|via|through|in|by) {AI}{E}",
        # compound: "AI-written", "KI generiert", "gpt-generated" with a message noun, an end, or a judging word
        rf"(?:\b(?:is|was|'s|so|clearly|obviously|totally|pretty|very|such an?|this|that|your|ist|war|klingt"
        rf"|wirkt|sehr|komplett|diese|deine|jest|ta|twoja)\s+)?\b(?:ai|a\.i\.?|ki|chat ?gpt|gpt|bot|machine"
        rf"|maschinen?|llm|robot)[- ]?(?:written|generated|crafted|drafted|generiert\w*|geschrieben\w*"
        rf"|erstellt\w*)(?:\s+{MSG}|{T})",
        rf"\b(?:написан|згенерован|сгенерирован|створен|создан|зроблен|сделан)(?:о|а|ий|ый|ая|е|ні|і|ы)?{E}"
        rf"(?:\s+\S+){{0,2}}?\s+(?:(?:з|с|за допомогою|с помощью|через|у|в)\s+)?{SL_AI}{E}",
        r"\b(?:ш[іi]|ии|чат ?гпт\w*|chat ?gpt\w*(?:'\w+)?|gpt|гпт|нейронк\w*|нейросет\w*|нейромереж\w*|бот|робот)"
        r"\s+(?:\S+\s+)?(?:написав|написал|писав|писал|згенерував|сгенерировал|склав|составил)\w*",
        r"\b(?:написав|написал|згенерував|сгенерировал)\w*\s+(?:(?:це|это|тобі|тебе|вам|за вас|за тебя)\s+)?"
        r"(?:ш[іi]|ии|чат ?гпт\w*|chat ?gpt\w*|gpt|гпт|нейронк\w*|нейросет\w*|бот)(?!\w)",
        rf"\b(?:napysan|napisan|zgenerovan|sgenerirovan|wygenerowan|stworzon|sozdan|stvoren)\w*\s+(?:\S+\s+)?"
        rf"(?:(?:przez|z|za pomocą|przy pomocy|s|cherez|z dopomohoyu|by|with)\s+)?{TR_AI}{E}",
        r"\b(?:chat ?gpt|gpt|ai|sztuczna inteligencja|bot)\s+(?:to\s+)?(?:napisał|wygenerował)\w*",
        r"\bnapisał\w*\s+(?:to\s+)?(?:z|w|przez|przy pomocy)\s+(?:chat ?gpt\w*|gpt|ai)\b",
        r"\b(?:von|mit|durch|per) (?:einer |der |einem |dem )?(?:ki|ai|chat ?gpt|gpt|bot|roboter|maschine)\s+"
        r"(?:\w+\s+)?(?:geschrieben|generiert|erstellt|verfasst|erzeugt)",
    ],
    "gpt_like": [
        rf"\b(?:sounds?|reads?|looks?|smells?|feels?|seems?) (?:like|as if|as though) {AI}(?:[- ](?:generated|written))?{LIKE_T}",
        r"\b(?:copy[- ]?past(?:e|ed)?|kopiuj[- ]wklej|копі?паст\w*|копипаст\w*)\s+(?:(?:straight|right)\s+)?"
        r"(?:from|of|out of|z|з|с|из|із)\s+(?:an? )?(?:chat ?gpt|gpt|ai|ші|ии|чат ?гпт|bot)\w*",
        r"\b(?:straight|right|fresh) (?:out of|from) chat ?gpt\b",
        r"\b(?:thanks|thank you|nice try|ok|hi|hello),? (?:chat ?gpt|gpt|bot|robot|mr\.? bot)\b",
        r"\b(?:chat ?gpt|gpt|ai) (?:vibes|energy|slop|alert)\b",
        rf"\b(?:схоже|похоже|звучить як|звучит как|нагадує|напоминает|відгонить|отдает|пахне|пахнет|brzmi jak"
        rf"|wygląda jak|jak z|klingt (?:nach|wie)|wie von|zvuchit kak|pohozhe na|skhozhe na)\s+(?:на\s+)?"
        rf"(?:ein\w*\s+)?(?:{SL_AI}|{TR_AI}|bot(?:a|om|em)?){LIKE_T}",
    ],
    "automated_message": [
        rf"\b(?:is (?:this|that|it)|this is|that's|thats|it's|its|this was|your|another|just|such an?|what an?"
        rf"|was (?:this|that|it))\s+(?:an?\s+|just\s+an?\s+)?(?:automated|automatic|auto-?generated|auto-?sent"
        rf"|automatically (?:generated|sent|written))\s+(?:linkedin\s+)?(?:{MSG}|sequence)",
        r"\b(?:this|that|your|these)\s+(?:\w+\s+)?(?:messages?|dms?|text|outreach|note|reply|pitch|e-?mail)"
        r"\s+(?:is|are|was|were|looks?|seems?|sounds?|feels?|reads?)\s+(?:\w+\s+)?(?:automated|auto-?generated"
        r"|generated)\b",
        rf"\b(?:is|was) (?:this|that) (?:\w+ )?(?:auto-?generated|automated|generated){T}",
        rf"(?:^|[.!?]\s*)auto-?generated{T}",
        r"\bавтоматичн(?:а|е)\s+(?:розсилка|повідомлення)\b|\bавтоматическ(?:ая|ое)\s+(?:рассылка|сообщение)\b",
        r"\b(?:це|это|ваша|ваше|твоя|твоє|твое|ваш)\s+(?:\w+\s+)?автомат\w*\s+(?:розсилк|рассылк|повідомл|сообщен"
        r"|відпов|ответ)\w*",
        r"\b(?:to|czy to)?\s*automatyczn\w*\s+wiadomoś\w*|\bwiadomoś\w* automatyczn\w*",
        r"\b(?:automatisiert|automatisch|generiert)e\s+nachricht\b",
        r"\bavtomat\w*\s+(?:rassylk|rozsylk|soobshch|povidoml)\w*",
    ],
    "bot_question": [
        rf"\b(?:are|r) (?:you|u) (?:even\s+|just\s+)?(?:an?\s+)?(?:bot|robot|ai|chat ?gpt|gpt|chatbot|ai bot|ai agent){T}",
        rf"\b(?:you are|you're|youre|ur|u r) (?:just\s+|clearly\s+|obviously\s+)?(?:an?\s+)?(?:bot|robot|ai|chat ?gpt"
        rf"|chatbot|ai bot){T}",
        rf"\b(?:is|was) (?:this|that|it) (?:even\s+|just\s+)?(?:an?\s+)?(?:bot|robot|ai|chat ?gpt|gpt|chatbot|ai bot){T}",
        rf"\b(?:this is|that's|thats|it's|that is) (?:just\s+|clearly\s+|obviously\s+|definitely\s+)?an?\s+"
        rf"(?:bot|robot|chatbot|ai bot){T}",
        rf"\b(?:am i|i'm|im|i am) (?:talking|chatting|speaking|writing|messaging) (?:to|with) {AI}{E}",
        rf"(?:^|(?<=\W))(?:ти|ви|ты|вы|це|это|ty|ti|vy|tse|eto|jesteś|czy to|bist du|sind sie|ist das|du bist"
        rf"|sie sind)\s+(?:(?:просто|якийсь|какой-то|jakiś|chyba|ein|eine|einem)\s+)?(?:бот\w*|робот\w*|ш[іi]"
        rf"|ии|чат ?гпт|bot(?:em|om|a)?|robot(?:em|om)?|ai|ki|chat ?gpt){T}",
        r"\b(?:спілкуюсь|спілкуюся|спілкуємо?сь|спілкуємося|общаюсь|общаемся|переписуюсь|переписуємось|переписываюсь|переписываемся|розмовляю|разговариваю|говорю|rozmawiam"
        r"|piszę|schreibe|chatte|spreche|rede)\s+(?:\S+\s+){0,2}?(?:з|с|z|mit)\s+(?:einem\s+)?(?:ботом|роботом"
        r"|ш[іi]|ии|botem|robotem|bot|botom)(?!\w)",
        r"\b(?:пише|пишет|пишуть|пишут|відповідає|отвечает)\s+(?:мені\s+|мне\s+)?(?:бот|робот|ш[іi]|ии|нейронка"
        r"|нейросеть|чатгпт)(?!\w)",
        r"\b(?:ти|ты|ви|вы)\s+(?:\S+\s+)?(?:ботом|роботом)(?!\w)",
    ],
}
# Shapes added after the first held-out run (33/49, 0 false) and the production replay (1 of 3
# strict call-outs). Each is a general shape; its cases live in tests/fixtures/ai_callout_tuning.py.
_W = r"(?:на)?писа\w*|згенер\w*|сгенер\w*|napysa\w*|napisa\w*|pisa[łl]\w*|wygener\w*"
_BY = (r"ш[іi]|ии|чат ?гпт\w*|гпт|chat ?gpt\w*(?:'\w+)?|gpt|ai|shi|нейронк\w*|нейросет\w*|нейромереж\w*"
       r"|штучним інтелектом|искусственным интеллектом|ботом|роботом|ботами")
_SPEC["written_by"] += [
    rf"\b(?:{_BY}|бот|робот|sztuczn\w* inteligencj\w*)\s+(?:(?!(?:про|о|об|o|about|on|na|für|über|для)\s)\S+\s+){{0,2}}?(?:{_W})",
    rf"\b(?:{_W})\s+(?:(?!(?:про|о|об|o|about|on|na|für|über|для)\s)\S+\s+){{0,2}}?(?:{_BY})(?!\w)",
    r"\bdid (?:a|an|the) (?:human|person|real person) write (?:this|that|it)\b",
]
_SPEC["human_or_machine"] += [
    rf"\b{HUM}\s+\S+{OR}(?:{MAC}|sztuczn\w* inteligencj\w*){T}",
    rf"\b{HUM}{OR}sztuczn\w* inteligencj\w*{T}",
]
_SPEC["gpt_like"] += [
    r"\b(?:is|was) (?:it|this|that) (?:an? |just |some |more )?(?:ai|chat ?gpt|gpt|bot)[- ](?:\w+\s+)?"
    r"(?:talk|pitch|spam|messages?|text|outreach|dms?|copy|sales\w*|template|nonsense|slop|garbage)\b",
    r"\b(?:klingt|brzmi|звучить|звучит|sounds|reads|looks)\s+(?:(?:sehr|so|total|very|so much|bardzo|дуже|очень)\s+){1,2}"
    r"(?:nach|wie|jak|як|как|like)\s+(?:chat ?gpt|gpt|ki|ai|bot|ш[іi]|чат ?гпт)\b",
    r"\b(?:як|как|like|jak|wie)\s+(?:из|з|із|from|z|aus)\s+(?:czata? ?gpt|chat ?gpt|чат ?гпт|gpt|гпт)\b",
    r"\bz czata? ?gpt\b",
    r"\b(?:remove|delete|forgot|left|прибрати|убрать|usunąć)\b.{0,30}\b(?:chat ?gpt|gpt|чат ?гпт)\b",
    r"\b(?:листи|повідомлення|письма|сообщения|messages|emails|wiadomości)\s+(?:від|от|from|od)\s+"
    r"(?:ботів|ботов|bots|botów)\b",
]
_SPEC["automated_message"] += [
    rf"\b(?:is (?:this|that|it)|this is|it's|its) (?:all\s+|just\s+|all just\s+)(?:an?\s+)?(?:automated|automatic"
    rf"|auto-?generated)\s+(?:linkedin\s+)?(?:{MSG}|sequence)",
]
_SPEC["bot_question"] += [
    r"\b(?:your|ur|ваш\w*|твій|твоя|твой|twój|twoj\w*|dein\w*|ihr\w*)\s+(?:\w+\s+)?(?:bot|бот|robot|робот"
    r"|chatbot)(?!\w)",
    r"\b(?:це|это|тут|здесь)\s+(?:\S+\s+)?(?:бот|робот|ш[іi]|нейронка|нейросеть|чатгпт)\s+(?:пише|пишет|пишуть"
    r"|пишут|відповідає|отвечает)",
]
RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (code, re.compile(p, _F)) for code, ps in _SPEC.items() for p in ps
)

_QUOTES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "`": "'", "“": '"',
                         "”": '"', "‑": "-", "‐": "-", "–": "-", "ё": "е"})


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").casefold().translate(_QUOTES)).strip()


def rule_code(text: str) -> str:
    """The first rule that matches the reply, or "" when it is not an AI call-out."""
    norm = _norm(text)
    return next((code for code, rx in RULES if rx.search(norm)), "")


AI_CALLOUT = "ai_callout"
# Denys's recorded yes, heylead-api #7762 comment 7114133346 (hold_reasons.ai_callout.owner_text).
AI_CALLOUT_OWNER_TEXT = (
    "They suggested your messages were written by AI. HeyLead stopped writing to them. "
    "Reply yourself if you want."
)


def turns_since_ours(messages: list[dict]) -> list[str]:
    """The text of their turns after our last sdr row, oldest first."""
    theirs: list[str] = []
    for m in reversed(messages or []):
        if (m.get("role") or "") == "sdr":
            break
        if (m.get("role") or "") == "prospect":
            theirs.append(str(m.get("text") or ""))
    return list(reversed(theirs))


def callout_in(texts: list[str]) -> str:
    """The rule code of the first call-out among texts, or ""."""
    return next((code for code in map(rule_code, texts) if code), "")
