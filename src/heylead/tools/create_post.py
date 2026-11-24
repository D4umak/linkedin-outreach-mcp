"""Tool: create_post — Generate and publish voice-matched posts.

Creates LinkedIn posts in the user's voice. X posting is dormant: see
X_POSTING_AVAILABLE.
"""

from __future__ import annotations

import logging
import httpx

from .. import hosted_writes
from ..hosted_writes import laptop_writes_refused
from ..linkedin import get_linkedin_client, UnipileError
from ..linkedin.unipile import UnipileAuthError
from ..linkedin.backend_client import VIDEO_PUBLISH_TIMEOUT
from ..db.async_bridge import run_db
from ..db.queries import get_setting, log_action
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
    text: str = "",
    video: str = "",
) -> str:
    """Generate and publish a voice-matched LinkedIn post.

    Args:
        topic: What to post about.
        tone: Post tone: "professional", "casual", "thought-leader", "storytelling".
        platforms: "linkedin". A request that names X gets X_NOT_AVAILABLE and
            nothing is sent to X; LinkedIn, if also named, still posts.
        image: Optional path to a photo to attach.
        text: The post's own words, already approved. Published as given
            (only blank space around it is trimmed): no drafting, and on a
            hosted account no library photo; the draft is read back and
            must match before it is published (heylead-api#3160).
        video: Optional path to an mp4 (25 MiB at most) to attach instead of
            a photo. Needs ``text`` and a hosted account; the video's bytes
            are read back too before Publish (heylead-api#3272).
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

    text = (text or "").strip()
    video = (video or "").strip()
    # Before anything else can run: a wrong video call must never fall
    # through to a post without its video (heylead-api#3272).
    if video:
        if (image or "").strip():
            return hosted_writes.post_photo_and_video()
        if not text:
            return hosted_writes.post_video_needs_text()
        if not laptop_writes_refused():
            return hosted_writes.post_video_needs_cloud()
    if text:
        refused = _refuse_text(text)
        if refused:
            return refused
        topic = (topic or "").strip() or _topic_from(text)

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
            from ..services.post_media import load_post_image, load_post_video

            post_image = load_post_image(image)
        except ValueError as e:
            return f"{e}"
        try:
            post_video = load_post_video(video)
        except ValueError as e:
            return f"Nothing was posted. {e}"
        if post_video:
            answer = await _publish_exact_in_cloud(text, topic, tone, None, post_video)
        elif text:
            answer = await _publish_exact_in_cloud(text, topic, tone, post_image)
        else:
            answer = await _create_post_in_cloud(topic, tone, post_image)
        return f"{answer}\n\n{X_NOT_AVAILABLE}" if wants_x else answer

    return hosted_writes.generic_refusal()


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


# The api's limits (content_posts.TEXT_MIN_TO_PUBLISH and TEXT_MAX): text
# outside them would be saved and then refused at Publish.
TEXT_MIN = 30
TEXT_MAX = 3000


def _refuse_text(text: str) -> str:
    if len(text) < TEXT_MIN:
        return f"Nothing was posted. A LinkedIn post needs at least {TEXT_MIN} characters."
    if len(text) > TEXT_MAX:
        return (
            f"Nothing was posted. A LinkedIn post can be at most {TEXT_MAX:,} characters; "
            f"this one is {len(text):,}."
        )
    return ""


def _topic_from(text: str) -> str:
    """The draft's topic label in Content: the post's first line."""
    first = next((line for line in text.splitlines() if line.strip()), text)
    return cut_at_word(first, 100)


async def _publish_exact_in_cloud(text: str, topic: str, tone: str, image, video=None) -> str:
    """create_post(text=...) for a hosted account: these words, this photo
    or this video.

    The words go with the create (heylead-api#3231): the cloud stores them,
    writes no draft and attaches no library photo. An older api drafts from
    the topic and reserves a library photo instead, so the text is PATCHed
    and the picture removed whenever the answer shows that happened. The
    draft is created unscheduled either way (a default slot could publish
    it before it is checked), then read back: only a draft that holds
    exactly *text* and exactly *image* (or no picture) is published.

    With *video* (heylead-api#3272) the video is uploaded in place of the
    photo step (the api releases any picture the draft held), and the
    read-back also requires no picture and exactly the video's bytes.
    """
    client = get_linkedin_client()
    status, post = await client.create_content_post(
        topic, cloud_tone(tone), scheduled_at=0, text=text,
    )
    if status >= 400 or not post.get("id"):
        detail = str(post.get("detail") or "").strip()
        return f"Nothing was posted. {detail}" if detail else hosted_writes.post_publish_failed()
    post_id = str(post["id"])

    if str(post.get("text") or "") != text:
        st, saved = await client.update_content_post_text(post_id, text)
        if st >= 400:
            detail = str(saved.get("detail") or "").strip()
            return f"Nothing was posted. {detail}" if detail else hosted_writes.post_text_not_kept()

    if video:
        refused = await _put_video(client, post_id, video)
        if refused:
            return refused
        return await _check_and_publish_video(client, post_id, text, topic, tone, video)

    try:
        st = 200
        if image:
            filename, data, mime = image
            st, _ = await client.put_content_post_image(post_id, filename, data, mime)
        elif post.get("has_image"):
            st, _ = await client.delete_content_post_image(post_id)
    except Exception as e:
        logger.warning("create_post: the photo could not be set on %s: %s", post_id, e)
        st = 500
    if st >= 400:
        return hosted_writes.post_photo_not_kept()

    # Read back what Publish will send.
    st, current = await client.get_content_post(post_id)
    if st >= 400 or str(current.get("text") or "") != text:
        return hosted_writes.post_text_not_kept()
    img_status, img_bytes = await client.get_content_post_image(post_id)
    has_picture = bool(current.get("has_image")) or (img_status < 400 and bool(img_bytes))
    if image:
        if img_status >= 400 or img_bytes != image[1]:
            return hosted_writes.post_photo_not_kept()
    elif has_picture:
        return hosted_writes.post_photo_not_kept()

    st, settings = await client.get_content_settings()
    publish_mode = str(settings.get("publish_mode") or "") if st < 400 else ""
    if publish_mode != "auto":
        return hosted_writes.post_held()

    published, refused = await _publish_once(client, post_id)
    if refused:
        return refused
    await _log_published({
        "topic": topic, "tone": tone, "post_id": published.get("post_id", ""),
        "platform": "linkedin", "via": "cloud", "exact_text": True,
        "chars": len(text), "image": image[0] if image else "",
    })
    return hosted_writes.post_published()


async def _log_published(details: dict) -> None:
    """The local action log for a post the cloud published. Best effort: the
    post is on LinkedIn, and a failure here must not read as one there."""
    try:
        await run_db(log_action, "post_created", details=details)
    except Exception as e:
        logger.warning("create_post: published, but the local log failed: %s", e)


async def _publish_once(client, post_id: str, *, timeout=None) -> tuple[dict, str]:
    """Publish *post_id* with one call, never retried: (body, refusal or "").

    The api may still be handing the post to LinkedIn when this side gives
    up (a read timeout), or fail after the send went out (it reloads the row
    after publishing; a gateway in front can time out), so an error or any
    5xx answers "may already be on LinkedIn" and never "try again": a
    second call would post it twice (heylead-api#3272). ``timeout`` None
    keeps the client's shared window.
    """
    try:
        if timeout is None:
            st, published = await client.publish_content_post(post_id)
        else:
            st, published = await client.publish_content_post(post_id, timeout=timeout)
    except (UnipileError, httpx.HTTPError) as e:
        logger.warning("create_post: the publish of %s did not answer: %s", post_id, e)
        return {}, hosted_writes.post_may_be_live()
    if st >= 500:
        return published, hosted_writes.post_may_be_live()
    if st == 409 and "no linkedin account" in str(published.get("detail") or "").lower():
        return published, hosted_writes.post_no_seat()
    if st >= 400:
        return published, hosted_writes.post_publish_failed()
    return published, ""


async def _put_video(client, post_id: str, video) -> str:
    """Upload *video* onto the draft; the refusal to answer, or ""."""
    filename, data, mime = video
    try:
        st, _ = await client.put_content_post_video(post_id, filename, data, mime)
    except UnipileAuthError:
        raise  # an expired sign-in, not a video the draft lost
    except Exception as e:  # a timeout or a dropped connection: nothing published
        logger.warning("create_post: the video could not be put on %s: %s", post_id, e)
        return hosted_writes.post_video_not_kept()
    if st == 413:
        return hosted_writes.post_video_too_large()
    if st == 415:
        return hosted_writes.post_video_wrong_type()
    if st >= 400:
        return hosted_writes.post_video_not_kept()
    return ""


async def _check_and_publish_video(client, post_id: str, text: str, topic: str, tone: str, video) -> str:
    """Read back the words, no picture and the video's bytes; then publish once."""
    filename, data, _mime = video
    st, current = await client.get_content_post(post_id)
    if st >= 400 or str(current.get("text") or "") != text:
        return hosted_writes.post_text_not_kept()
    try:
        img_status, img_bytes = await client.get_content_post_image(post_id)
        vid_status, vid_bytes = await client.get_content_post_video(post_id)
    except UnipileAuthError:
        raise
    except Exception as e:
        logger.warning("create_post: the video on %s could not be read back: %s", post_id, e)
        return hosted_writes.post_video_not_kept()
    has_picture = bool(current.get("has_image")) or (img_status < 400 and bool(img_bytes))
    if has_picture or vid_status >= 400 or vid_bytes != data:
        return hosted_writes.post_video_not_kept()

    st, settings = await client.get_content_settings()
    publish_mode = str(settings.get("publish_mode") or "") if st < 400 else ""
    if publish_mode != "auto":
        return hosted_writes.post_held_with_video()

    published, refused = await _publish_once(client, post_id, timeout=VIDEO_PUBLISH_TIMEOUT)
    if refused:
        return refused
    await _log_published({
        "topic": topic, "tone": tone, "post_id": published.get("post_id", ""),
        "platform": "linkedin", "via": "cloud", "exact_text": True,
        "chars": len(text), "image": "", "video": True, "video_name": filename,
    })
    return hosted_writes.post_published_with_video()


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

    published, refused = await _publish_once(client, post_id)
    if refused:
        return refused
    await _log_published({
        "topic": topic, "tone": tone, "post_id": published.get("post_id", ""),
        "platform": "linkedin", "via": "cloud",
    })
    if photo_failed:
        return hosted_writes.post_published_without_photo()
    return hosted_writes.post_published()
