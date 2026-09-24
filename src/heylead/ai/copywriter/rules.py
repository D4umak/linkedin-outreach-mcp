"""The house copy rules, as data. One table, bound to channels.

This file is pure data and the standard library, so the client can carry it
byte for byte and a digest test on each side can prove the two have not
drifted. Anything that needs an import from this app belongs in render.py or
polish.py, not here.

Where a rule existed in both repos with different wording, the stricter
wording is the one recorded. Where it existed in only one, it is here now:
the 21 Sep 2026 inventory found fifteen families that only this repo enforced
and fifteen that only the client enforced.

A rule earns its place by having been wrong in production. The `note` field
says when, so a future reader can tell a scar from a preference.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Rule:
    id: str
    family: str
    text: str
    channels: frozenset[str]
    note: str = ""
    except_channels: frozenset[str] = field(default_factory=frozenset)
    # The same rule in one line, for channels whose whole message is shorter
    # than this rule's full wording. Rendering the long form into a
    # 200-character invite grew that prompt by a third (measured 22 Sep 2026)
    # to repeat what four other lines had already said.
    short: str = ""

    def binds(self, channel: str) -> bool:
        return channel in self.channels and channel not in self.except_channels

    def wording(self, channel: str) -> str:
        return self.short if (channel in TIGHT_CHANNELS and self.short) else self.text


# Every channel a person's words leave through.
CHANNELS: tuple[str, ...] = (
    "invite", "dm", "followup", "inmail", "email", "reply", "check_in",
    "discovery_dm", "counter_pitch", "comment", "comment_reply",
    "post", "x_post", "x_thread", "headline", "about",
)

ALL = frozenset(CHANNELS)

# Copy addressed to one named person, in a thread or under their post. These
# are the channels where a formula reads as a machine.
CONVERSATIONAL = frozenset({
    "invite", "dm", "followup", "inmail", "email", "reply", "check_in",
    "discovery_dm", "counter_pitch", "comment", "comment_reply",
})

# Copy the sender publishes under their own name, addressed to nobody.
AUTHORED = frozenset({"post", "x_post", "x_thread", "headline", "about"})

# Where the sender is writing to a stranger about work, and every claim about
# the reader has to be grounded in what the prompt actually knows.
# Channels whose finished message is measured in hundreds of characters. The
# rules bind them exactly as hard; they are simply said in fewer words.
TIGHT_CHANNELS = frozenset({"invite", "comment", "comment_reply", "x_post"})

OUTREACH = frozenset({
    "invite", "dm", "followup", "inmail", "email", "reply", "check_in",
    "discovery_dm", "counter_pitch",
})


RULES: tuple[Rule, ...] = (
    # ── typography ──
    Rule(
        id="no-dashes",
        family="typography",
        text=(
            "Never use em dashes (—) or en dashes (–). Use a comma, a colon "
            "or a full stop. Plain hyphens are fine in ranges and compounds, "
            "like 2-4 weeks or co-founder."
        ),
        channels=ALL,
        note="The oldest AI tell in this product; message_guardrails.normalize_dashes is the hard guarantee.",
        short=(
            "PUNCTUATION RULE: No em dashes (\u2014) or en dashes (\u2013). Use a comma, colon or full stop."
        ),
    ),
    Rule(
        id="no-decoration",
        family="typography",
        text=(
            "No emojis and no hashtags unless the sender's own voice uses "
            "them."
        ),
        channels=ALL,
    ),
    Rule(
        id="no-exclamation",
        family="typography",
        text=(
            "Do not end a sentence with an exclamation mark unless the "
            "sender's voice uses them."
        ),
        channels=CONVERSATIONAL,
    ),
    # ── names ──
    Rule(
        id="no-the-before-a-brand",
        family="names",
        text=(
            "ARTICLE RULE: Never put \"the\" in front of a company, product, or "
            "brand name. Write the name bare, as a person would: \"expanding\" "
            "followed by the company name, or \"the team at\" followed by the "
            "company name, never \"the\" directly before it. Use only company "
            "names that appear in this prompt."
        ),
        channels=ALL,
        short=(
            "ARTICLE RULE: Never put \"the\" in front of a company, product or brand name, and use only names that appear in this prompt."
        ),
    ),
    Rule(
        id="names-are-grounded",
        family="names",
        text=(
            "NAMES RULE: Every company, product, person or place you name must "
            "appear in this prompt (the reader's facts, the sender's details, "
            "the conversation, the capability line, or a GROUNDING EVIDENCE "
            "line that is not tagged [reply_exemplar]). Never introduce one "
            "from memory or from an example."
        ),
        channels=ALL,
    ),
    Rule(
        id="no-placeholders",
        family="names",
        text=(
            "Never write a placeholder token such as [Company], [Name] or "
            "[Topic]. If you do not have the word, write the sentence without "
            "it."
        ),
        channels=ALL,
        # {{0}} is the mention token a comment reply is required to write;
        # Unipile renders it as the @mention. Banning brackets there would ban
        # the one token that path depends on.
        except_channels=frozenset({"comment_reply"}),
        note="18 Aug 2026: four real prospects received a message containing [Company].",
        short=(
            "Never write a placeholder such as [Company] or [Name]."
        ),
    ),
    Rule(
        id="no-pointing-at-their-words",
        family="names",
        text=(
            "Do not use \"the\" or \"that\" to point back at the reader's exact "
            "wording. Say the thing in your own words."
        ),
        channels=CONVERSATIONAL,
    ),
    # ── openers and closers ──
    Rule(
        id="no-template-openers",
        family="openers",
        text=(
            "Never open with a formula. Not \"Spot on\", \"Agreed\", \"Love "
            "this\", \"So true\", \"Great post\", \"This resonates\", "
            "\"Nothing beats\", \"Congrats on the\". Open with the substance "
            "of what you have to say."
        ),
        channels=CONVERSATIONAL,
        note="22 Sep 2026: every comment reply for a week opened \"Spot on,\" or \"Agreed,\".",
        short=(
            "Never open with a formula: \"Spot on\", \"Agreed\", \"Love this\", \"So true\", \"Great post\". Open with the substance."
        ),
    ),
    Rule(
        id="no-name-opener",
        family="openers",
        text=(
            "Never open with the reader's first name. Start with the "
            "substance; the name buys nothing and costs characters."
        ),
        channels=CONVERSATIONAL,
    ),
    Rule(
        id="no-sign-off",
        family="openers",
        text=(
            "Never sign off. No \"Best\", no \"Cheers\", no \"Regards\", no "
            "\"- Name\" at the end. A LinkedIn message carries no signature, "
            "and one reads as a bot."
        ),
        channels=CONVERSATIONAL,
        # Email is a letter: it is the one channel where a plain sign-off with
        # the sender's name is what a person would write.
        except_channels=frozenset({"email"}),
        short=(
            "Never sign off. No \"Best\", no \"Cheers\", no \"- Name\": a LinkedIn message carries no signature."
        ),
    ),
    Rule(
        id="no-rhetorical-question",
        family="openers",
        text=(
            "Never ask a vague rhetorical question such as \"Do you still make "
            "time for new ideas?\". Ask one concrete question about the work in "
            "front of them, or ask nothing at all."
        ),
        channels=CONVERSATIONAL,
        short=(
            "Never ask a vague rhetorical question. Ask one concrete question about their work, or none at all."
        ),
    ),
    # ── vocabulary ──
    Rule(
        id="plain-words",
        family="vocabulary",
        text=(
            "No buzzy adjectives (\"impressive\", \"massive\", \"serious "
            "move\", \"game-changing\", \"killing it\"), no flattery (\"cool to "
            "see someone who\"), no cliches (\"a lifesaver\", \"running the "
            "whole show\"), no sales jargon (\"leverage\", \"synergy\", \"circle "
            "back\", \"touch base\"), and no LinkedIn-SDR filler (\"hands-on\", "
            "\"in the weeds\", \"hits differently\")."
        ),
        channels=ALL,
        short=(
            "No buzzy adjectives, no flattery, no cliches, no sales jargon, no LinkedIn-SDR filler (\"hands-on\", \"in the weeds\")."
        ),
    ),
    Rule(
        id="ask-about-their-work",
        family="vocabulary",
        text=(
            "Ask about what they actually do in a week. Never ask about "
            "industry trends, about AI in general, or anything a stranger "
            "could have asked them."
        ),
        channels=CONVERSATIONAL,
        short=(
            "Ask about their own week, never about industry trends or AI in "
            "general."
        ),
        note="From the client's voice_rules fragment, where it applied to ten prompts and no others.",
    ),
    Rule(
        id="not-the-same-observation-for-everyone",
        family="vocabulary",
        text=(
            "Do not reach for the same observation you would make about "
            "anyone in their role. Pick something from this person's own "
            "activity, or say nothing about them at all."
        ),
        channels=CONVERSATIONAL,
        short=(
            "Do not make the observation you would make about anyone in "
            "their role."
        ),
        note="From the client's voice_rules fragment; every note had begun to say the same thing.",
    ),
    Rule(
        id="no-labels",
        family="vocabulary",
        text=(
            "Never label the reader. Not \"as a builder\", not \"as an "
            "operator\", not \"people like you\"."
        ),
        channels=CONVERSATIONAL,
    ),
    Rule(
        id="no-number-as-an-argument",
        family="vocabulary",
        text=(
            "Never quote a number back as though it explained itself. Say what "
            "it means in plain words, or leave it out."
        ),
        channels=ALL,
        note="24 Aug 2026: \"that 21x gap\" went out as though it were an insight.",
    ),
    # ── grounding ──
    Rule(
        id="no-invention",
        family="grounding",
        text=(
            "Never invent a fact about the reader, their company, or the "
            "sender's own work. If it is not in this prompt, you do not know "
            "it."
        ),
        channels=ALL,
        short=(
            "Never invent a fact about the reader, their company or the sender's own work."
        ),
    ),
    Rule(
        id="paraphrase-never-mirror",
        family="grounding",
        text=(
            "Paraphrase loosely. Never mirror an exact phrase from their post, "
            "their profile or an earlier message, and never put their words in "
            "quotation marks."
        ),
        channels=CONVERSATIONAL,
        short=(
            "Paraphrase loosely. Never mirror an exact phrase of theirs, and never quote them."
        ),
    ),
    Rule(
        id="no-audience-claim",
        family="grounding",
        text=(
            "AUDIENCE RULE: Never describe who the sender helps. Do not write "
            "\"I help X founders\", \"we work with X teams\", \"I partner with "
            "X\". Say only what the sender DOES. Every attribute you state "
            "about the reader \u2014 nationality, country, role, seniority, "
            "company stage, industry \u2014 must appear in RECIPIENT FACTS "
            "below. If it is not listed there, you do not know it: leave it "
            "out."
        ),
        channels=OUTREACH,
        note="The audience-descriptor leak: nationality and role were asserted from nothing.",
        short=(
            "AUDIENCE RULE: Never describe who the sender helps (\"I help X founders\"). Say what the sender does. Every attribute you state about the reader must appear in RECIPIENT FACTS."
        ),
    ),
    # ── voice ──
    Rule(
        id="write-in-their-voice",
        family="voice",
        text=(
            "Write the way the sender writes, as their voice describes it: "
            "their sentence length, the way they open and close, the words "
            "they reach for. Where the voice says short standalone lines, "
            "write short standalone lines."
        ),
        channels=ALL,
        note="21 Sep 2026: the signature reached the prompt and the model still wrote to an average.",
        short=(
            "Write the way the sender writes: their sentence length, their openings, the words they reach for."
        ),
    ),
)
