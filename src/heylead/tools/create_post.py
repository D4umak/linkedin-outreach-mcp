"""Tool: create_post — Generate and publish voice-matched posts.

Creates LinkedIn posts in the user's voice. X posting is dormant: see
X_POSTING_AVAILABLE.
"""

from __future__ import annotations

import logging

from .. import hosted_writes
from ..db.queries import get_setting, log_action, save_published_post
from ..hosted_writes import ensure_laptop_may_write, laptop_writes_refused
from ..linkedin import get_account_id, get_linkedin_client, UnipileError
from ..db.async_bridge import run_db
from ..services.campaign_naming import cut_at_word

logger = logging.getLogger(__name__)

# X posting stays off until the hosted server holds X credentials (heylead-api#1399).
X_POSTING_AVAILABLE = False
X_NOT_AVAILABLE = "X posting isn't available in HeyLead yet; this post was not sent to X."


async def run_create_post(
    topic: str = "",
    tone: str = "professional",
    platforms: str = "linkedin",
    image: str = "",
) -> str:
    """Generate and publish a voice-matched LinkedIn post.

    Args:
        topic: What to post about.
        tone: Post tone: "professional", "casual", "thought-leader", "storytelling".
        platforms: "linkedin". A request that names X gets X_NOT_AVAILABLE and
            nothing is sent to X; LinkedIn, if also named, still posts.
        image: Optional path to a photo to attach.
    """
    platform_list = [p.strip().lower() for p in platforms.split(",")]
    wants_x = "x" in platform_list or "twitter" in platform_list
    if wants_x and not X_POSTING_AVAILABLE and "linkedin" not in platform_list:
        return X_NOT_AVAILABLE

    # ── Pre-checks ──
    setup_done = await run_db(get_setting, "setup_complete", False)
    if not setup_done:
        return (
            "Setup required before creating posts.\n\n"
            "Please run setup_profile first."
        )

    if not topic:
        return (
            "Please provide a topic for the post.\n\n"
            "Examples:\n"
            '  create_post(topic="share a tip about cold outreach")\n'
            '  create_post(topic="comment on AI in sales")\n'
            '  create_post(topic="share a lesson learned this week")'
        )

    # A hosted account's laptop posts nothing itself (heylead-api#2318): the
    # cloud drafts the post in Content, under the workspace's publish mode.
    if laptop_writes_refused():
        if "linkedin" not in platform_list:
            return X_NOT_AVAILABLE
        try:
            from ..services.post_media import load_post_image

            post_image = load_post_image(image)
        except ValueError as e:
            return f"{e}"
        answer = await _create_post_in_cloud(topic, tone, post_image)
        return f"{answer}\n\n{X_NOT_AVAILABLE}" if wants_x else answer

    # Check at least one platform has credentials.
    # Off the loop: get_account_id is a sync DB read and db.get_db refuses one
    # on the event loop thread. It caches after it succeeds once, so calling it
    # bare only failed when create_post was the first thing in a process to
    # want the account — a freshly restarted MCP server, exactly.
    account_id = await run_db(get_account_id)
    has_linkedin = "linkedin" in platform_list and bool(account_id)
    has_x = wants_x and X_POSTING_AVAILABLE

    if not has_linkedin and not has_x:
        answer = "No accounts connected. Run setup_profile first."
        return f"{answer}\n\n{X_NOT_AVAILABLE}" if wants_x else answer

    # Before generation: a rejected path should not cost an LLM call, and a
    # post that cannot carry its image should not go out without one.
    try:
        from ..services.post_media import load_post_image

        post_image = load_post_image(image)
    except ValueError as e:
        return f"{e}"

    # ── Load profile + voice ──
    profile = await run_db(get_setting, "profile", {})
    voice = await run_db(get_setting, "voice_signature", {})

    from ..ai.llm_router import call_llm
    from ..ai.voice_analyzer import voice_prompt_block
    from ..services.voice_examples import example_posts_block, select_example_posts

    name = profile.get("name", "")
    title = profile.get("title", "")
    company = profile.get("company", "")
    industry = profile.get("industry", "")
    voice_block = voice_prompt_block(voice)
    # LinkedIn only: their LinkedIn posts are the wrong shape to few-shot a
    # 280-character tweet with.
    examples = await run_db(select_example_posts, profile, 3) if has_linkedin else []

    output_parts: list[str] = []

    # ── LinkedIn ──
    if has_linkedin:
        result = await _publish_linkedin(
            call_llm, account_id, topic, tone,
            name, title, company, industry, voice_block, voice,
            example_posts_block(examples), examples, post_image,
            profile=profile,
        )
        output_parts.append(result)

    # ── X/Twitter ──
    if has_x:
        result = await _publish_x(
            call_llm, topic, tone,
            name, title, company, industry, voice_block, voice,
            profile=profile,
        )
        output_parts.append(result)
    elif wants_x:
        output_parts.append(X_NOT_AVAILABLE)

    return "\n\n---\n\n".join(output_parts)


# The client's tones, in the cloud composer's words (heylead-api
# content_posts.CONTENT_TONES); a cloud tone passes through as it is.
_CLOUD_TONES = ("thought-leader", "insight", "story", "how-to", "contrarian", "question")
_TONE_TO_CLOUD = {
    "professional": "insight",
    "casual": "story",
    "thought-leader": "thought-leader",
    "thought_leader": "thought-leader",
    "storytelling": "story",
}


def cloud_tone(tone: str) -> str:
    key = (tone or "").strip().lower()
    if key in _CLOUD_TONES:
        return key
    return _TONE_TO_CLOUD.get(key, "thought-leader")


async def _create_post_in_cloud(topic: str, tone: str, image) -> str:
    """create_post for a hosted account: the cloud drafts, attaches, publishes.

    The draft is written by POST /content/posts. A photo goes into the
    Content library and onto the draft. The post is published now only
    when the workspace's publish mode is auto; under require_approval it
    stays a draft for the person to approve (the publish route itself does
    not consult the mode, because pressing Publish IS the approval).
    """
    client = get_linkedin_client()
    status, post = await client.create_content_post(topic, cloud_tone(tone))
    if status >= 400 or not post.get("id"):
        detail = str(post.get("detail") or "").strip()
        return f"Nothing was posted. {detail}" if detail else hosted_writes.post_publish_failed()
    post_id = str(post["id"])

    photo_failed = False
    if image:
        photo_failed = True
        try:
            filename, data, mime = image
            added = await client.upload_content_photo(filename, data, mime)
            items = added.get("items") or []
            photo_id = str(items[0].get("id") or "") if items else ""
            if photo_id:
                st, _ = await client.attach_library_photo(post_id, photo_id)
                photo_failed = st >= 400
        except Exception as e:  # the post still goes, without its photo
            logger.warning("create_post: the photo could not be added: %s", e)

    st, settings = await client.get_content_settings()
    publish_mode = str(settings.get("publish_mode") or "") if st < 400 else ""
    if publish_mode != "auto":
        held = hosted_writes.post_held()
        return f"{held} {hosted_writes.post_photo_not_added()}" if photo_failed else held

    if not str(post.get("text") or "").strip():
        return hosted_writes.post_still_drafting()

    st, published = await client.publish_content_post(post_id)
    if st == 409 and "no linkedin account" in str(published.get("detail") or "").lower():
        return hosted_writes.post_no_seat()
    if st >= 400:
        return hosted_writes.post_publish_failed()
    await run_db(log_action, "post_created", details={
        "topic": topic, "tone": tone, "post_id": published.get("post_id", ""),
        "platform": "linkedin", "via": "cloud",
    })
    if photo_failed:
        return hosted_writes.post_published_without_photo()
    return hosted_writes.post_published()


def _author_sources(profile, name, title, company, industry, topic) -> tuple:
    """What the author gave, for the claims check: the stored profile (or the
    four fields a caller passed without it) and the topic."""
    given = profile or {"name": name, "title": title, "company": company, "industry": industry}
    return (given, topic)


async def _publish_linkedin(
    call_llm, account_id, topic, tone,
    name, title, company, industry, voice_block, voice=None,
    examples_block="", examples=None, image=None, profile=None,
) -> str:
    """Generate and publish a LinkedIn post."""
    from ..ai.copywriter.author_claims import known_facts_rule
    from ..ai.copywriter.polish import keep_to_sources

    try:
        prompt = f"""Write a LinkedIn post for {name} ({title} at {company}).
Industry: {industry}

HOW {name} WRITES
{voice_block or "No voice signature on file — write plainly and concretely."}

{examples_block}

Topic: {topic}
Tone requested: {tone}

Requirements:
- Write in first person, using {name}'s voice and style
- Keep it between 150-1200 characters (optimal LinkedIn engagement)
- Include a hook in the first line to stop scrolling
- Use short paragraphs (1-3 sentences each)
- End with a question or call-to-action to drive engagement
- Do NOT use hashtags unless the user's style includes them
- Do NOT use emojis unless the user's style includes them
- Be authentic and conversational, not corporate
- Share a genuine insight or perspective. A story about {name} only if this
  prompt tells it.
{known_facts_rule(name)}
- Say things the way {name} would say them out loud. No sales-methodology
  vocabulary, and no pointing at a number as though it explained itself.

Return ONLY the post text, nothing else."""

        post_text = await call_llm(prompt, max_tokens=800)
        if not post_text or len(post_text) < 30:
            return "LinkedIn: Failed to generate post content."
        from ..ai.draft_guard import guard_draft
        from ..services.voice_examples import borrows_from_examples

        # An example in the prompt is a string the model can echo — that is
        # how "[Company]" reached four real prospects on 18 Aug and how the
        # 8 Sep follow-ups invented a vendor name. These examples are the user's
        # own past posts, so a leak republishes last quarter's news.
        if borrows_from_examples(post_text, examples or []):
            return "LinkedIn: Draft copied from a past post and was not published."
        # 25 Sep 2026: "Having spoken with over 600 CTOs", for an author who
        # never said it. A figure they did not give does not go out.
        post_text = await keep_to_sources(
            post_text, channel="post", max_chars=1200,
            sources=_author_sources(profile, name, title, company, industry, topic),
        )
        # The floor the draft already had to clear: what is left after the
        # claims go must still be a post, not a stub the gate would pad out.
        if len(post_text) < 30:
            return "LinkedIn: Draft was made of claims you never gave and was not published."
        post_text = await guard_draft(post_text, voice or {}, "post", 1200)
        if not post_text:
            return "LinkedIn: Draft failed quality checks and was not published."
    except Exception as e:
        return f"LinkedIn: Failed to generate post: {e}"

    try:
        ensure_laptop_may_write("create_post")
        client = get_linkedin_client()
        # Only pass image when there is one: a text post keeps the two-argument
        # call every implementation of this method already accepts.
        if image:
            result = await client.create_post(account_id, post_text, image=image)
        else:
            result = await client.create_post(account_id, post_text)
        await client.close()

        if result.get("success"):
            post_id = result.get("post_id", "")
            await run_db(log_action, "post_created", details={
                "topic": topic, "tone": tone, "chars": len(post_text),
                "post_id": post_id, "platform": "linkedin",
                "image": image[0] if image else "",
            })
            if post_id:
                try:
                    await run_db(save_published_post, post_id=post_id, text=post_text[:500], topic=topic)
                except Exception:
                    pass
            return (
                f"LinkedIn post published!\n\n"
                f'   "{cut_at_word(post_text, 200)}"\n'
                f"   ({len(post_text)} chars)"
            )
        else:
            return f"LinkedIn: {result.get('error', 'Unknown error')}"
    except (UnipileError, Exception) as e:
        return f"LinkedIn publish failed: {e}"


async def _publish_x(
    call_llm, topic, tone,
    name, title, company, industry, voice_block, voice=None, profile=None,
) -> str:
    """Generate and publish an X/Twitter post via backend proxy."""
    from ..ai.copywriter.author_claims import known_facts_rule
    from ..ai.copywriter.polish import keep_to_sources

    try:
        prompt = f"""Write a tweet for {name} ({title} at {company}).
Industry: {industry}

HOW {name} WRITES
{voice_block or "No voice signature on file — write plainly and concretely."}

Topic: {topic}
Tone requested: {tone}

Requirements:
- Write in first person, using {name}'s voice
- MUST be under 280 characters (strict Twitter limit)
- Hook immediately — no padding or filler
- Be provocative, insightful, or actionable
- Do NOT use hashtags unless the user's style includes them
- No emojis unless the user naturally uses them
- End with a punchy statement or question
{known_facts_rule(name)}
- Say things the way {name} would say them out loud. No sales-methodology
  vocabulary, and no pointing at a number as though it explained itself.

Return ONLY the tweet text, nothing else."""

        tweet_text = await call_llm(prompt, max_tokens=400)
        if not tweet_text or len(tweet_text) < 10:
            return "X: Failed to generate tweet content."
        tweet_text = tweet_text.strip()
        tweet_text = await keep_to_sources(
            tweet_text, channel="x_post", max_chars=280,
            sources=_author_sources(profile, name, title, company, industry, topic),
        )
        if len(tweet_text) < 10:
            return "X: Draft was made of claims you never gave and was not published."
        if len(tweet_text) > 280:
            tweet_text = cut_at_word(tweet_text, 280)
        from ..ai.draft_guard import guard_draft
        tweet_text = await guard_draft(tweet_text, voice or {}, "post", 280)
        if not tweet_text:
            return "X: Draft failed quality checks and was not published."
    except Exception as e:
        return f"X: Failed to generate tweet: {e}"

    # Post via backend proxy (which handles X OAuth tokens)
    try:
        from .. import config as cfg
        if cfg.is_backend_mode():
            client = get_linkedin_client()
            # Use the multi-platform proxy endpoint
            import httpx
            url = f"{client.base_url}/api/v1/posts"
            resp = await client._client.post(
                url,
                json={"text": tweet_text, "platforms": ["x"]},
                headers=client._headers(),
            )
            data = resp.json() if resp.status_code in (200, 201) else {}
            results = data.get("results", {})
            x_result = results.get("x", data)
            if x_result.get("success"):
                await run_db(log_action, "post_created", details={
                    "topic": topic, "tone": tone, "chars": len(tweet_text),
                    "post_id": x_result.get("post_id", ""), "platform": "x",
                })
                return (
                    f"X tweet published!\n\n"
                    f'   "{tweet_text}"\n'
                    f"   ({len(tweet_text)} chars)"
                )
            else:
                return f"X: {x_result.get('error', 'Unknown error')}"
        else:
            return "X posting requires backend mode. Set up backend connection first."
    except Exception as e:
        logger.error("X publish failed: %s", e)
        return f"X publish failed: {e}"
