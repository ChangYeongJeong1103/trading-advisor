"""
alerts/renderer/inline_assets.py — Email body inline `cid:` attachment
helper (P11(a).3 — same mechanism for both brand logos and per-alert plots).

────────────────────────────────────────────────────────────────────────
Role:
  HTML email bodies reference inline images via the `cid:` scheme, e.g.
  `<img src="cid:logo_cme">`. In the actual MIME, a MIMEImage with the
  same Content-ID must be attached inside multipart/related — only then
  do mail clients display the image.

  This module handles two things at once:
    1) channel name → brand logo file path mapping (assets/anomaly/channel_logos/).
    2) Helper that takes a list of `(cid, file_path)` and builds `MIMEImage` objects.

  channel_email.py imports this to:
    - Embed `<img src="cid:{cid}">` tags into the HTML
    - Attach the result of build_inline_image_parts() to MIMEMultipart("related").

────────────────────────────────────────────────────────────────────────
Usage:

    from anomaly_detection.alerts.renderer.inline_assets import (
        channel_logo_path, build_inline_image_parts,
    )

    cid_logo = "logo_cme"
    cid_plot1 = "plot_1h"

    parts = build_inline_image_parts([
        (cid_logo,  channel_logo_path("cme")),
        (cid_plot1, Path("/tmp/alert_1h.png")),
    ])
    for part in parts:
        msg_related.attach(part)

────────────────────────────────────────────────────────────────────────
Design decisions:
  · All logo PNGs are locked under `assets/anomaly/channel_logos/`.
  · All 4 channels are guaranteed to have PNGs (CME also user-supplied) → no missing files.
  · If a file is missing, build_inline_image_parts silently skips + WARN logs.
    The email itself still goes out (header falls back to no-logo).
  · cid naming: `logo_<channel>` / `plot_1h` / `plot_6h`.
"""

from __future__ import annotations

import logging
from email.mime.image import MIMEImage
from pathlib import Path

from ...core.schemas import (
    CHANNEL_CME,
    CHANNEL_HYPERLIQUID,
    CHANNEL_POLYMARKET,
    CHANNEL_TRUTH_SOCIAL,
    CHANNEL_X,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Logo file location — relative to the repo root.
#   src/anomaly/alerts/renderer/inline_assets.py
#   parents[4] = repo root.
# ─────────────────────────────────────────────────────────────────────
_REPO_ROOT: Path = Path(__file__).resolve().parents[4]
_CHANNEL_LOGO_DIR: Path = _REPO_ROOT / "assets" / "anomaly" / "channel_logos"

# channel name → absolute path. Five user-supplied PNGs.
# Truth Social — user final decision (2026-05-16): instead of a purple T box,
# use only Trump's avatar PNG as the channel identifier → set the logo path
# itself to the avatar PNG.
CHANNEL_LOGO_PATH: dict[str, Path] = {
    CHANNEL_CME:           _CHANNEL_LOGO_DIR / "cme.png",
    CHANNEL_POLYMARKET:    _CHANNEL_LOGO_DIR / "polymarket.png",
    CHANNEL_HYPERLIQUID:   _CHANNEL_LOGO_DIR / "hyperliquid.png",
    CHANNEL_X:             _CHANNEL_LOGO_DIR / "x.png",
    CHANNEL_TRUTH_SOCIAL:  _CHANNEL_LOGO_DIR / "truth_social_avatar.png",
}

# Optional — only filled when a channel needs an inline image beyond the logo.
# Currently no channel uses it (we previously embedded both the truth_social
# purple T and the Trump face, but per user decision we use only the Trump
# face — consolidated into the logo path).
CHANNEL_AVATAR_PATH: dict[str, Path] = {}


def channel_logo_path(channel: str) -> Path | None:
    """Channel name → absolute path of the logo PNG. None if not registered."""
    return CHANNEL_LOGO_PATH.get(channel)


def channel_avatar_path(channel: str) -> Path | None:
    """Channel name → optional avatar PNG (currently only the Trump face for truth_social).

    None means the channel has no avatar — the renderer simply skips it.
    """
    return CHANNEL_AVATAR_PATH.get(channel)


def channel_avatar_cid(channel: str) -> str:
    """Channel name → avatar inline cid string (used in HTML as cid:avatar_<channel>)."""
    return f"avatar_{channel}"


def channel_logo_cid(channel: str) -> str:
    """Channel name → cid string used inside the email body.

    On the HTML side, reference as `<img src="cid:logo_<channel>">`.
    Don't put angle brackets in the cid itself (the renderer wraps it in
    `<...>` automatically when building the Content-ID header).
    """
    return f"logo_{channel}"


def plot_cid(window_minutes: int) -> str:
    """Window in minutes → plot inline cid (e.g. plot_60m, plot_360m)."""
    return f"plot_{window_minutes}m"


# ─────────────────────────────────────────────────────────────────────
# MIMEImage builder — used by channel_email.
# ─────────────────────────────────────────────────────────────────────
def build_inline_image_parts(
    items: list[tuple[str, Path]],
) -> list[MIMEImage]:
    """List of `(cid, file_path)` → list of MIMEImages with matching cids.

    Args:
        items: List of (cid_string_without_brackets, png_file_path) tuples.
            file_path may be absolute or relative. Missing files are silently skipped.

    Returns:
        `MIMEImage` list in the same order. Missing files are dropped from
        the result. The renderer just attaches this list to a
        `MIMEMultipart("related")`.

    Raises:
        Does not raise — missing files are only logged and skipped (the
        email itself still goes out).
    """
    parts: list[MIMEImage] = []
    for cid, file_path in items:
        try:
            data = file_path.read_bytes()
        except FileNotFoundError:
            logger.warning(
                "inline_assets: logo/plot file missing → cid=%s path=%s (skip)",
                cid, file_path,
            )
            continue
        except OSError as exc:
            logger.warning(
                "inline_assets: read failed cid=%s path=%s — %s (skip)",
                cid, file_path, exc,
            )
            continue

        # Assume PNG (currently every logo + every plot is a PNG).
        # Content-ID must be wrapped in angle brackets (RFC 2045/2392).
        img = MIMEImage(data, _subtype="png")
        img.add_header("Content-ID", f"<{cid}>")
        # Disposition = inline → in Gmail etc., the image does not show in
        # the attachment list and is rendered only in the body.
        img.add_header(
            "Content-Disposition", "inline", filename=file_path.name,
        )
        parts.append(img)

    return parts


__all__ = [
    "CHANNEL_LOGO_PATH",
    "CHANNEL_AVATAR_PATH",
    "channel_logo_path",
    "channel_logo_cid",
    "channel_avatar_path",
    "channel_avatar_cid",
    "plot_cid",
    "build_inline_image_parts",
]
