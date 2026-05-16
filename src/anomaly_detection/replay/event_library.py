"""
replay/event_library.py — Load data/anomaly/historical_events/*.md as HistoricalEvent.

────────────────────────────────────────────────────────────────────────
What it does:

  1. Scan every .md file under data/anomaly/historical_events/ (or a user-specified path).
  2. Parse each file's YAML frontmatter (between --- ... ---) with PyYAML.
  3. Convert frontmatter dict + file path + markdown body into HistoricalEvent (Pydantic).
  4. Store all events in dict[event_id, HistoricalEvent] — fast lookup.

────────────────────────────────────────────────────────────────────────
Design decisions:

  · README.md is automatically skipped (no frontmatter → naturally excluded at parsing).
  · Frontmatter parsing failure raises EventLibraryError — never silently skipped.
    Reason: replay is only meaningful when all 6 events are present. Missing
    even one = partial results → dangerous.

  · We do not use external libs like `python-frontmatter` — to minimize
    dependencies, we split it ourselves. PyYAML (already in requirements) is enough.

  · If the same event_id appears in two files → EventLibraryError (user copy-paste mistake).

  · YAML datetime parsing — PyYAML auto-converts ISO 8601 into datetime.
    A string without timezone ("2025-04-09T17:18:00") becomes naive datetime →
    schemas.py's _ensure_utc rejects it. Frontmatter must include "Z" or "+00:00".

────────────────────────────────────────────────────────────────────────
Usage:

    >>> lib = EventLibrary.from_default_dir()
    >>> lib.event_ids
    ['2025-04-09_liberation_day', '2025-10-10_china_tariff_100', ...]
    >>> evt = lib.get('2025-04-09_liberation_day')
    >>> evt.window_start
    datetime.datetime(2025, 4, 9, 16, 18, tzinfo=datetime.timezone.utc)

Reference: docs/p10-replay-framework.md §3.1, §7 #1
"""

from __future__ import annotations

# --- standard library ---
import re
from pathlib import Path
from typing import Any

# --- third-party ---
import yaml
from pydantic import ValidationError

# --- local ---
from .schemas import HistoricalEvent, InsiderLikelihood


# ─────────────────────────────────────────────────────────────────────
# Path constants — default events directory. Callers may override.
# Estimate repo root as src/'s parent.parent.parent.
# ─────────────────────────────────────────────────────────────────────
# This file: src/anomaly/replay/event_library.py
# repo root: ../../../  (replay → anomaly → src → repo)
_REPO_ROOT: Path = Path(__file__).resolve().parents[3]
DEFAULT_EVENTS_DIR: Path = _REPO_ROOT / "data" / "anomaly" / "historical_events"


# ─────────────────────────────────────────────────────────────────────
# Exception class — clean trace on parsing/validation failure.
# ─────────────────────────────────────────────────────────────────────
class EventLibraryError(Exception):
    """Raised on frontmatter parse failure or HistoricalEvent validation failure.

    Carries the offending file path + a short reason. Callers (CLI / tests)
    print it in a human-readable form.
    """

    def __init__(self, path: Path, reason: str, *, cause: Exception | None = None) -> None:
        # path is PosixPath — stringify before embedding in the message.
        self.path = path
        self.reason = reason
        # Chain via "raise ... from cause" if cause is set.
        msg = f"EventLibraryError: {path} — {reason}"
        super().__init__(msg)


# ─────────────────────────────────────────────────────────────────────
# Frontmatter splitter — regex. Starts with the first "---" and ends at the second "---".
# (?s) DOTALL so newlines inside the frontmatter also match.
# ─────────────────────────────────────────────────────────────────────
_FRONTMATTER_RE: re.Pattern[str] = re.compile(
    r"\A---\s*\n(?P<fm>.*?)\n---\s*\n(?P<body>.*)\Z",
    re.DOTALL,
)


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """Extract (frontmatter_yaml, narrative_md) from the file text.

    Returns:
        tuple[str, str]: (frontmatter, body) — when a frontmatter is found.
        None: when the file does not start with a frontmatter (e.g. README.md).
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    return m.group("fm"), m.group("body").lstrip("\n")


# ─────────────────────────────────────────────────────────────────────
# Frontmatter dict → HistoricalEvent kwargs normalization.
#
# Frontmatter contains PyYAML-auto-converted datetimes / ints / lists. We
# (1) convert insider_likelihood to an enum, (2) strip unknown keys
# (the user may have added doc-only notes).
# ─────────────────────────────────────────────────────────────────────
_KNOWN_FRONTMATTER_KEYS: frozenset[str] = frozenset(
    {
        "event_id",
        "announcement_ts",
        "announcement_source",
        "primary_channel",
        "primary_symbols",
        "secondary_channels",
        "insider_likelihood",
        "pre_event_window_minutes",
        "peak_signal_offset_minutes",
        "profit_estimate_usd",
        "position_size_usd",
        "position_type",
        "related_events",
        "related_x_status_ids",
        "notable_pattern",
    }
)


def _normalize_frontmatter(raw: dict[str, Any], path: Path) -> dict[str, Any]:
    """Tidy the PyYAML result dict into kwargs HistoricalEvent can accept.

    What it does:
      · Pick only known keys (silently drop extras — allow user notes).
      · Convert insider_likelihood to an InsiderLikelihood enum.
      · Drop empty list fields so defaults take over.

    Returns:
        dict: shape ready to pass to HistoricalEvent(**dict).
    """
    out: dict[str, Any] = {}

    # Keep only known keys. Unknown keys are ignored (notes allowed).
    # extra="forbid" would raise ValidationError if we forwarded them.
    for key in _KNOWN_FRONTMATTER_KEYS:
        if key in raw and raw[key] is not None:
            out[key] = raw[key]

    # insider_likelihood: string → enum.
    if "insider_likelihood" in out:
        out["insider_likelihood"] = InsiderLikelihood.parse(out["insider_likelihood"])

    return out


# ─────────────────────────────────────────────────────────────────────
# Single file → HistoricalEvent.
# ─────────────────────────────────────────────────────────────────────
def _load_event_file(path: Path) -> HistoricalEvent | None:
    """Read one .md file and return a HistoricalEvent. None if no frontmatter.

    Raises:
        EventLibraryError: frontmatter exists but YAML parsing or schema validation fails.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        # File-read failure — permission / network-drive hiccup / etc.
        raise EventLibraryError(path, f"file read failed: {e}", cause=e) from e

    split = _split_frontmatter(text)
    if split is None:
        # Files without frontmatter (e.g. README.md) — silent skip.
        return None

    fm_yaml, body_md = split

    # PyYAML safe_load → dict[str, Any]. Bad YAML → yaml.YAMLError.
    try:
        fm_dict = yaml.safe_load(fm_yaml) or {}
    except yaml.YAMLError as e:
        raise EventLibraryError(path, f"YAML parse error: {e}", cause=e) from e

    if not isinstance(fm_dict, dict):
        raise EventLibraryError(
            path,
            f"frontmatter root must be a YAML mapping, got {type(fm_dict).__name__}",
        )

    # Fall back to filename if frontmatter is missing event_id.
    # (The frontmatter event_id wins — and we enforce a match below.)
    fm_dict.setdefault("event_id", path.stem)

    # source_path / narrative are not given defaults by schemas.py, so fill them here.
    kwargs = _normalize_frontmatter(fm_dict, path)
    kwargs["source_path"] = str(path)
    kwargs["narrative_md"] = body_md

    try:
        evt = HistoricalEvent(**kwargs)
    except ValidationError as e:
        raise EventLibraryError(path, f"schema validation failed:\n{e}", cause=e) from e

    # Enforce filename ↔ frontmatter event_id match — prevent name/content confusion.
    if evt.event_id != path.stem:
        raise EventLibraryError(
            path,
            f"event_id mismatch: frontmatter='{evt.event_id}' but filename='{path.stem}'. "
            "Both must be identical (rename the file or edit the frontmatter).",
        )

    return evt


# ─────────────────────────────────────────────────────────────────────
# EventLibrary — per-directory collection.
# ─────────────────────────────────────────────────────────────────────
class EventLibrary:
    """A collection of historical events — wraps one directory.

    Public API:
        EventLibrary.from_default_dir()
        EventLibrary.from_dir(path)
        lib.event_ids                   → sorted list[str]
        lib.events                      → list[HistoricalEvent]
        lib.get(event_id)               → HistoricalEvent (KeyError if missing)
        lib.has(event_id)               → bool
        len(lib)                        → int
        for evt in lib                  → iterate in sorted order
    """

    def __init__(self, events: dict[str, HistoricalEvent], source_dir: Path) -> None:
        # Prefer from_dir / from_default_dir over direct construction externally.
        # In-memory construction for tests is allowed (private contract).
        self._events: dict[str, HistoricalEvent] = events
        self._source_dir: Path = source_dir

    # ── factory methods ────────────────────────────────────────────────
    @classmethod
    def from_default_dir(cls) -> "EventLibrary":
        """Load from the default location (data/anomaly/historical_events/)."""
        return cls.from_dir(DEFAULT_EVENTS_DIR)

    @classmethod
    def from_dir(cls, dir_path: Path | str) -> "EventLibrary":
        """Read every *.md in the given directory and build an EventLibrary.

        Args:
            dir_path: directory containing event md files.

        Raises:
            FileNotFoundError: the directory itself is missing.
            EventLibraryError: any single file fails frontmatter parse / schema.
        """
        # Accept str too — CLI args are often strings.
        dir_path = Path(dir_path)
        if not dir_path.exists():
            raise FileNotFoundError(f"events directory not found: {dir_path}")
        if not dir_path.is_dir():
            raise NotADirectoryError(f"events path is not a directory: {dir_path}")

        events: dict[str, HistoricalEvent] = {}
        # sorted() — deterministic order (directory entry order is OS-dependent).
        for md_path in sorted(dir_path.glob("*.md")):
            evt = _load_event_file(md_path)
            if evt is None:
                # Files without frontmatter (README etc.) — skip.
                continue
            if evt.event_id in events:
                # Same event_id in two files — user copy-paste mistake.
                prev_path = events[evt.event_id].source_path
                raise EventLibraryError(
                    md_path,
                    f"duplicate event_id '{evt.event_id}' (already loaded from {prev_path})",
                )
            events[evt.event_id] = evt

        return cls(events=events, source_dir=dir_path)

    # ── lookup ────────────────────────────────────────────────────────
    @property
    def event_ids(self) -> list[str]:
        """Sorted list of event_ids. Used directly in CLI list output."""
        return sorted(self._events.keys())

    @property
    def events(self) -> list[HistoricalEvent]:
        """HistoricalEvent list in sorted event_id order."""
        return [self._events[eid] for eid in self.event_ids]

    @property
    def source_dir(self) -> Path:
        """The source directory path (for audit / report display)."""
        return self._source_dir

    def get(self, event_id: str) -> HistoricalEvent:
        """Lookup by event_id. KeyError with candidate hints if missing."""
        if event_id not in self._events:
            # Readable error — likely a typo.
            raise KeyError(
                f"event_id '{event_id}' not found. "
                f"available: {self.event_ids}"
            )
        return self._events[event_id]

    def has(self, event_id: str) -> bool:
        """Existence check only (no KeyError)."""
        return event_id in self._events

    # ── convenience dunder methods ────────────────────────────────────
    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self):
        # Iterate in sorted order.
        return iter(self.events)

    def __contains__(self, event_id: object) -> bool:
        return isinstance(event_id, str) and event_id in self._events

    def __repr__(self) -> str:
        return (
            f"<EventLibrary n={len(self)} "
            f"source_dir={self._source_dir} "
            f"ids={self.event_ids}>"
        )
