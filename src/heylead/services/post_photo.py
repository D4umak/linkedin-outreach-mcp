"""Choosing a photo for a brand post from the user's own photo library.

The library is a folder the user curated: subfolders by kind, files named
``NNN - what the photo shows.jpeg``. The file name is the only description we
have, so it is what the model chooses from. Anything that looks private (a
personal, family or children folder, or those words in a file name) is never
offered: those photos show people who did not agree to be in a post that
goes out with nobody watching.

A photo from a library the user provided is attached whenever one is still
unused. The model only chooses *which* unused photo, never whether to skip
it: skipping was how a curated folder still produced text-only posts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass

from . import post_media
from .post_media import _MIME_BY_EXTENSION

logger = logging.getLogger(__name__)

# Local only. The cloud push sends a fixed list of settings, so this is
# neither uploaded nor overwritten by it; and a cloud-owned seat never runs
# the local brand post job that reads it (planner skips JOB_BRAND_POST).
PHOTO_LIBRARY_SETTING = "brand_photo_library"
USED_KEYS_SETTING = "brand_photo_used_keys"

# Key in the brand_post_published action details that records the photo.
IMAGE_PATH_DETAIL = "image_path"

# Ask for more while a couple of unused photos remain, so the next posts
# do not go out as text while a refill is on its way.
LOW_WATERMARK = 2

# A library under an iCloud-synced folder can block on read indefinitely when
# the file was offloaded. Past this, the post goes out text only.
LIBRARY_IO_TIMEOUT_SECONDS = 20.0

_NUMBER_PREFIX = re.compile(r"^\s*\d+\s*-\s*")
# Matched as substrings of the casefolded name. Errs toward excluding: a
# "Personal branding" folder is skipped too, and set_photo_library says so.
_PRIVATE_WORDS = (
    "personal", "family", "kids", "children", "private",
    "семья", "сім'я", "сімя", "родина", "дети", "діти",
)
# A picture of a screen is not a photo of the author. Same match as
# private: folder, file name, or a link that resolves into one.
_SCREENSHOT_WORDS = (
    "screenshot", "screen shot", "screen-shot", "screencap",
    "screengrab", "screen grab", "screen capture", "скриншот",
)


@dataclass(frozen=True)
class LibraryPhoto:
    path: str
    description: str
    group: str


@dataclass(frozen=True)
class ChosenPhoto:
    path: str
    description: str
    image: tuple[str, bytes, str]


def _is_skipped_name(name: str) -> bool:
    folded = name.casefold()
    return any(word in folded for word in _PRIVATE_WORDS + _SCREENSHOT_WORDS)


def _is_private_name(name: str) -> bool:
    """Back-compat name: private *or* a screenshot. Autopilot must skip both."""
    return _is_skipped_name(name)


def _file_key(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def private_subfolders(folder: str) -> list[str]:
    """Top-level subfolders of *folder* that are never offered to autopilot."""
    root = os.path.expanduser((folder or "").strip())
    if not root or not os.path.isdir(root):
        return []
    return sorted(
        d for d in os.listdir(root)
        if os.path.isdir(os.path.join(root, d)) and _is_private_name(d)
    )


def list_library_photos(folder: str) -> list[LibraryPhoto]:
    """Every postable photo under *folder*, minus anything that looks private.

    Links are resolved before judging a file, so a link inside a public folder
    that points into a private one, or out of the library, is not offered.
    """
    root = os.path.expanduser((folder or "").strip())
    if not root or not os.path.isdir(root):
        return []
    root = os.path.abspath(root)
    real_root = os.path.realpath(root)
    if _is_private_name(os.path.basename(real_root)):
        return []

    photos: list[LibraryPhoto] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and not _is_private_name(d)
        )
        group = os.path.relpath(dirpath, root)
        for filename in sorted(filenames):
            stem, extension = os.path.splitext(filename)
            # "_" marks the library's own files (a contact sheet, say), not
            # photos of the user.
            if filename.startswith((".", "_")) or extension.lower() not in _MIME_BY_EXTENSION:
                continue
            description = _NUMBER_PREFIX.sub("", stem).strip() or stem
            if _is_private_name(description):
                continue

            path = os.path.join(dirpath, filename)
            real = os.path.realpath(path)
            if os.path.commonpath([real, real_root]) != real_root:
                continue
            if any(_is_private_name(part) for part in os.path.relpath(real, real_root).split(os.sep)):
                continue

            photos.append(LibraryPhoto(
                path=path,
                description=description,
                group="" if group == "." else group,
            ))
    return photos


def _selection_prompt(topic: str, post_text: str, photos: list[LibraryPhoto]) -> str:
    listing = "\n".join(
        f"{i}. {p.description}" + (f" ({p.group})" if p.group else "")
        for i, p in enumerate(photos, start=1)
    )
    return f"""A LinkedIn post is about to be published. Pick the one photo of its
author that fits it best. A photo will be attached either way: if none is a
perfect match, pick the closest.

Topic: {topic}

Post:
{post_text}

Photos (described by their file names):
{listing}

Return JSON: {{"photo": <number from the list>}}."""


def _chosen_index(raw: str, count: int) -> int | None:
    try:
        value = json.loads(raw or "{}").get("photo")
    except (json.JSONDecodeError, AttributeError):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= count else None


async def _off_loop(func, *args):
    return await asyncio.wait_for(
        asyncio.to_thread(func, *args), timeout=LIBRARY_IO_TIMEOUT_SECONDS,
    )


def unused_library_photos(folder: str, used_keys: set[str]) -> list[LibraryPhoto]:
    """Postable photos in *folder* that have not been spent on a brand post."""
    return [p for p in list_library_photos(folder) if _file_key(p.path) not in used_keys]


def used_photo_keys(stored: list[str] | None, action_details: list[dict]) -> set[str]:
    """Every photo already attached to a published brand post.

    The setting is the durable list. The action log fills gaps from posts
    that went out before that list existed. Either path is enough; together
    they stop a photo being used twice.
    """
    keys: set[str] = set()
    for raw in stored or []:
        if raw:
            try:
                keys.add(_file_key(str(raw)))
            except OSError:
                keys.add(str(raw))
    for details in action_details:
        path = str(details.get(IMAGE_PATH_DETAIL) or "")
        if not path:
            continue
        try:
            keys.add(_file_key(path))
        except OSError:
            keys.add(path)
    return keys


def record_used_photo_keys(stored: list[str] | None, path: str) -> list[str]:
    """Append *path* to the spent list, preserving earlier keys."""
    keys = [str(k) for k in (stored or []) if k]
    try:
        key = _file_key(path)
    except OSError:
        key = path
    if key not in keys:
        keys.append(key)
    return keys


async def load_used_photo_keys() -> set[str]:
    from ..db.async_bridge import run_db
    from ..db.queries import get_setting, list_action_details

    stored = await run_db(get_setting, USED_KEYS_SETTING, [])
    details = await run_db(list_action_details, "brand_post_published")
    return used_photo_keys(stored if isinstance(stored, list) else [], details)


async def mark_photo_used(path: str) -> None:
    from ..db.async_bridge import run_db
    from ..db.queries import get_setting, save_setting

    stored = await run_db(get_setting, USED_KEYS_SETTING, [])
    if not isinstance(stored, list):
        stored = []
    await run_db(save_setting, USED_KEYS_SETTING, record_used_photo_keys(stored, path))


async def library_remaining() -> int:
    """Unused photos still in the configured folder, or 0 if none is set."""
    from ..db.async_bridge import run_db
    from ..db.queries import get_setting

    folder = await run_db(get_setting, PHOTO_LIBRARY_SETTING, "")
    if not folder:
        return 0
    used = await load_used_photo_keys()
    photos = await _off_loop(unused_library_photos, folder, used)
    return len(photos)


async def ingest_library_to_cloud(photos: list[LibraryPhoto]) -> int:
    """Copy unused local photos into the hosted Content library.

    The cloud drafter cannot read a folder on this machine. Uploading the
    files is what lets drafts already carry a picture for review. Soft:
    a host that does not yet have the route, or a file that will not load,
    is skipped rather than failing the local setting.
    """
    from ..config import is_backend_mode
    from ..linkedin import get_linkedin_client

    if not photos or not is_backend_mode():
        return 0
    client = get_linkedin_client()
    upload = getattr(client, "upload_content_photo", None)
    if upload is None:
        return 0
    uploaded = 0
    used = await load_used_photo_keys()
    for photo in photos:
        if _file_key(photo.path) in used:
            continue
        image = await _off_loop(post_media.load_post_image, photo.path)
        if image is None:
            continue
        filename, data, mime = image
        try:
            await upload(filename, data, mime)
        except Exception as e:
            logger.warning("Photo library ingest skipped %s: %s", photo.path, e)
            continue
        uploaded += 1
    return uploaded


async def choose_brand_post_image(topic: str, post_text: str) -> ChosenPhoto | None:
    """The next unused library photo, or None only when the library is empty."""
    from ..db.async_bridge import run_db
    from ..db.queries import get_setting

    try:
        folder = await run_db(get_setting, PHOTO_LIBRARY_SETTING, "")
        if not folder:
            return None

        used = await load_used_photo_keys()
        photos = await _off_loop(unused_library_photos, folder, used)
        if not photos:
            return None

        from ..ai import llm_router

        try:
            raw = await llm_router.call_llm(
                _selection_prompt(topic, post_text, photos),
                temperature=0.2,
                max_tokens=50,
                json_mode=True,
            )
            index = _chosen_index(raw, len(photos))
        except Exception as e:
            logger.warning("Brand post photo pick failed, using first unused: %s", e)
            index = None
        # A usable photo is never dropped because the model passed: the
        # library is the user's instruction to attach one.
        photo = photos[index - 1] if index is not None else photos[0]
        image = await _off_loop(post_media.load_post_image, photo.path)
        if image is None:
            # The chosen file could not be read; try the others before
            # giving up on a library the user already provided.
            for fallback in photos:
                if fallback.path == photo.path:
                    continue
                image = await _off_loop(post_media.load_post_image, fallback.path)
                if image is not None:
                    photo = fallback
                    break
        if image is None:
            return None
        return ChosenPhoto(path=photo.path, description=photo.description, image=image)
    except Exception as e:  # noqa: BLE001 — a photo must never stop the post
        logger.warning("Brand post photo skipped: %s", e)
        return None
