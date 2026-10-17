"""Loading an image or a video for a post.

Unipile's POST /api/v1/posts is multipart/form-data and takes files under
`attachments`. This is the one place that decides whether a path is something
we are willing to send: the clients take bytes and do not second-guess them,
and the tool turns a ValueError from here into a message the user reads.
"""

from __future__ import annotations

import os

# Our own cap, not LinkedIn's. Unipile documents a resolution limit for
# LinkedIn images (6012x6012) but no byte size; 10MB is enough for any photo
# worth posting and small enough that a mistyped path cannot stall a request.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

_MIME_BY_EXTENSION = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def load_post_image(path: str) -> tuple[str, bytes, str] | None:
    """Return (filename, bytes, mime) for *path*, or None when there is none.

    Raises ValueError with a message meant for the user.
    """
    path = (path or "").strip()
    if not path:
        return None

    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        raise ValueError(f"No image at {path}")

    extension = os.path.splitext(expanded)[1].lower()
    mime = _MIME_BY_EXTENSION.get(extension)
    if not mime:
        supported = ", ".join(sorted(_MIME_BY_EXTENSION))
        raise ValueError(
            f"{os.path.basename(path)} is not an image we can post. "
            f"Supported: {supported}"
        )

    size = os.path.getsize(expanded)
    if size > MAX_IMAGE_BYTES:
        raise ValueError(
            f"{os.path.basename(path)} is too large "
            f"({size / 1_048_576:.1f}MB, limit {MAX_IMAGE_BYTES // 1_048_576}MB)"
        )

    with open(expanded, "rb") as fh:
        return os.path.basename(expanded), fh.read(), mime


# The api's own cap for a Content post's video (heylead-api
# content_posts.VIDEO_MAX_BYTES, 25 MiB, compared with ``>``): a file of
# exactly this size is accepted there, so it is accepted here.
MAX_VIDEO_BYTES = 25 * 1024 * 1024


def load_post_video(path: str) -> tuple[str, bytes, str] | None:
    """Return (filename, bytes, "video/mp4") for an mp4 at *path*, or None
    when there is none (heylead-api#3272).

    Raises ValueError with a message meant for the user. Only the extension
    is checked: the api sniffs the file itself and answers 415 for one that
    is not an MP4.
    """
    path = (path or "").strip()
    if not path:
        return None

    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        raise ValueError(f"No video at {path}")

    name = os.path.basename(path)
    if os.path.splitext(expanded)[1].lower() != ".mp4":
        raise ValueError(f"{name} is not a video we can post. Supported: .mp4")

    size = os.path.getsize(expanded)
    if size > MAX_VIDEO_BYTES:
        raise ValueError(
            f"{name} is too large ({size / 1_048_576:.1f}MB, "
            f"limit {MAX_VIDEO_BYTES // 1_048_576}MB). Export it smaller "
            "(a 720p or 1080p MP4 is plenty) and try again."
        )

    with open(expanded, "rb") as fh:
        return os.path.basename(expanded), fh.read(), "video/mp4"
