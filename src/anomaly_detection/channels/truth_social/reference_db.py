"""
truth_social/reference_db.py — Reference DB loader + hybrid retrieval (Step 3).

────────────────────────────────────────────────────────────────────────
Responsibilities:

  When TruthSocialLLMScorer asks the LLM about market-impact of a new Trump
  post, this module retrieves references (few-shot examples) showing
  "how similar past posts moved the market".

  Reference DB = 42 events × 1,165 raw posts built in Step 2.

────────────────────────────────────────────────────────────────────────
2-stage hybrid retrieval (user-selected option C):

  Stage 1 — Keyword overlap
    · Fast and zero-dependency
    · Extract 6+ char tokens from the new post, score by token intersection with reference posts
    · Narrow to top-N (e.g. 30) candidates → reduces load on the embedding stage

  Stage 2 — Embedding cosine similarity (OpenAI text-embedding-3-small)
    · Strong semantic matching ("tariff" vs "duties", "rare earth" vs "magnets")
    · Embed DB once into cache → only 1 embedding per query afterwards (~$0.00001)
    · Pick top-K (e.g. 5) from Stage 1's 30 candidates

  → Only top-K references go into the LLM prompt (saves token budget)

────────────────────────────────────────────────────────────────────────
Reference structure (`ReferenceEvent`):

  · event_id      : "2025-04-09_buy_then_pause"
  · category      : "tariff" | "iran" | "china" | "fed" | ...
  · sp500_pct     : float (S&P 500 change %)
  · market_score  : int (user curated, 0-10)
  · posted_at_utc : datetime
  · posts         : list[ReferencePost] — verbatim text + post_id
  · narrative     : str (short narrative summary from v1.md)

────────────────────────────────────────────────────────────────────────
Data sources:

  · `data/anomaly/truthsocial/TruthSocial_events_v1.md`
        → YAML frontmatter (category, market_impact_score, sp500_pct, posted_at_*, post_ids)
        → narrative (first paragraph after h3 / "What he posted" sections etc.)

  · `data/anomaly/truthsocial/raw_trumpstruth/{event_id}/raw.jsonl`
        → verbatim text by truth_social_id

────────────────────────────────────────────────────────────────────────
Embedding cache:

  · Path : `data/anomaly/truthsocial/.cache/embeddings.json`
  · Format: {sha256(text)[:16] → [float×1536]}
  · Stage 2 can be disabled when no OpenAI API key is available or for cost savings
    (constructor arg `enable_embedding=False`).

────────────────────────────────────────────────────────────────────────
Plan: Step 3 — Reference DB seed
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# ====================================================================
# Constants — default paths anchored at the project root
# ====================================================================
_HERE = Path(__file__).resolve()
_PROJECT_ROOT = _HERE.parents[4]
_DEFAULT_V1_MD = _PROJECT_ROOT / "data" / "anomaly" / "truthsocial" / "TruthSocial_events_v1.md"
_DEFAULT_RAW_ROOT = _PROJECT_ROOT / "data" / "anomaly" / "truthsocial" / "raw_trumpstruth"
_DEFAULT_CACHE_DIR = _PROJECT_ROOT / "data" / "anomaly" / "truthsocial" / ".cache"

_EMBEDDING_MODEL = "text-embedding-3-small"
_EMBEDDING_DIM = 1536  # text-embedding-3-small dimension

# Stage 1 keyword retrieval — ignore tokens shorter than 6 chars (stop-word effect)
_MIN_TOKEN_LEN = 6


# ====================================================================
# Data classes
# ====================================================================
@dataclass(frozen=True)
class ReferencePost:
    """A single verbatim Trump post (read from raw.jsonl)."""

    post_id: str            # Mastodon snowflake ID
    text: str               # verbatim body
    posted_at_utc: datetime # ISO timestamp (posted_at_utc from raw.jsonl)
    is_retruth: bool = False


@dataclass
class ReferenceEvent:
    """A single historical event (1 v1.md entry + all verbatim posts within it)."""

    event_id: str
    posts: list[ReferencePost] = field(default_factory=list)

    # Values pulled directly from the v1.md frontmatter
    category: str = ""              # "tariff" / "iran" / "china" / "fed" / ...
    posted_at_utc: datetime | None = None
    market_impact_score: int = 0    # 0-10 (market_impact_score in v1.md)
    sp500_pct: float = 0.0          # S&P 500 change % (next-session)
    direction: str = "neutral"      # bull / bear / neutral
    verification_level: str = ""    # triple_verified / double_verified / ...

    # Auto-generated for search
    combined_text: str = ""         # concatenated text of all posts (for keyword matching)
    narrative: str = ""             # body of the corresponding event section in v1.md (includes Market reaction)
    embedding: list[float] | None = None  # embedding of combined_text (lazy)

    @property
    def is_high_impact(self) -> bool:
        """Whether this is a "high quality" reference suitable for LLM few-shot."""
        return self.market_impact_score >= 7 and self.verification_level.startswith(("triple", "double"))


@dataclass(frozen=True)
class RetrievedReference:
    """One retrieval result."""

    event: ReferenceEvent
    score_keyword: float  # 0~1 (token overlap ratio)
    score_embedding: float  # 0~1 (cosine similarity) — 0.0 when embedding disabled
    score_final: float    # weighted combined


# ====================================================================
# Text helpers
# ====================================================================
_PUNCT_RE = re.compile(r"[^\w\s]")
_SPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Normalize for fuzzy match / token extraction."""
    s = text.lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _SPACE_RE.sub(" ", s).strip()
    return s


def _tokenize(text: str, min_len: int = _MIN_TOKEN_LEN) -> set[str]:
    """Return a set of 6+ char tokens. set dedupes for free."""
    return {tok for tok in _normalize(text).split() if len(tok) >= min_len}


# ====================================================================
# v1.md parser
# ====================================================================
_YAML_BLOCK_RE = re.compile(r"```yaml\n(.*?)\n```", flags=re.DOTALL)
# Section narrative: from the end of one yaml block (```) until the next yaml
# block (```yaml), the next ## heading, or EOF. Splits the body per event.
_YAML_AND_NARRATIVE_RE = re.compile(
    r"```yaml\n(?P<yaml>.*?)\n```\n(?P<body>.*?)(?=\n```yaml\n|\n## |\Z)",
    flags=re.DOTALL,
)


def _parse_v1_md(v1_path: Path) -> list[tuple[dict[str, Any], str]]:
    """Extract v1.md into a list of (yaml_dict, narrative_text) tuples.

    narrative_text = markdown body for that event from right after the yaml
    block until the next event's yaml block (or ## heading). Includes the
    "Market reaction" table.
    """
    if not v1_path.exists():
        logger.warning("reference_db: v1.md not found at %s", v1_path)
        return []
    text = v1_path.read_text(encoding="utf-8")
    out: list[tuple[dict[str, Any], str]] = []
    for m in _YAML_AND_NARRATIVE_RE.finditer(text):
        try:
            data = yaml.safe_load(m.group("yaml"))
        except yaml.YAMLError as e:
            logger.warning("reference_db: yaml parse failed (skipped): %s", e)
            continue
        if not isinstance(data, dict):
            continue
        eid = data.get("event_id")
        if not eid or not isinstance(eid, str):
            continue
        if eid.endswith("slug") or "_slug" in eid:  # placeholder
            continue
        narrative = m.group("body").strip()
        out.append((data, narrative))
    return out


def _parse_utc(value: Any) -> datetime | None:
    """Convert the `posted_at_utc` field into a datetime (accepts several formats)."""
    if value is None or value == "TBD":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if not isinstance(value, str):
        return None
    s = value.strip()
    # Python 3.11+ fromisoformat handles nearly all ISO 8601 variants.
    # "Z" suffix is also natively supported from 3.11.
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    # fallback — date-only ("2025-04-09"), etc.
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(s.rstrip("Z"), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _load_raw_jsonl(event_id: str, raw_root: Path) -> dict[str, dict[str, Any]]:
    """raw.jsonl → {truth_social_id: row} dict."""
    p = raw_root / event_id / "raw.jsonl"
    out: dict[str, dict[str, Any]] = {}
    if not p.exists():
        return out
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            tsid = row.get("truth_social_id")
            if tsid:
                out[tsid] = row
    return out


# ====================================================================
# Main class
# ====================================================================
class TruthSocialReferenceDB:
    """v1.md + raw.jsonl loading + hybrid retrieval (keyword + embedding).

    Args:
        v1_md_path: path to v1.md. None → default.
        raw_root: raw_trumpstruth/ directory. None → default.
        cache_dir: embedding cache directory. None → default.
        enable_embedding: True → also run Stage 2 (embedding).
            False → keyword only (zero deps, zero OpenAI cost).
        openai_api_key: OpenAI key. None → env OPENAI_API_KEY.
            Required when enable_embedding=True.
    """

    def __init__(
        self,
        *,
        v1_md_path: Path | None = None,
        raw_root: Path | None = None,
        cache_dir: Path | None = None,
        enable_embedding: bool = True,
        openai_api_key: str | None = None,
    ) -> None:
        self._v1_path = Path(v1_md_path) if v1_md_path else _DEFAULT_V1_MD
        self._raw_root = Path(raw_root) if raw_root else _DEFAULT_RAW_ROOT
        self._cache_dir = Path(cache_dir) if cache_dir else _DEFAULT_CACHE_DIR

        self._enable_embedding = enable_embedding
        self._openai_api_key = openai_api_key or os.getenv("OPENAI_API_KEY", "")

        # ── State ──
        # event_id → ReferenceEvent
        self._events: dict[str, ReferenceEvent] = {}
        self._loaded = False

        # embedding cache: hash → vector. file-backed.
        self._embedding_cache: dict[str, list[float]] = {}
        self._embedding_cache_path = self._cache_dir / "embeddings.json"

        # lazy OpenAI client
        self._openai_client: Any = None

    # ────────────────────────────────────────────────────────────────
    # Loading
    # ────────────────────────────────────────────────────────────────
    def load(self) -> None:
        """Load v1.md + raw.jsonl files into memory. Once only."""
        if self._loaded:
            return
        rows = _parse_v1_md(self._v1_path)
        for row, narrative in rows:
            ev = self._build_event(row, narrative=narrative)
            if ev is not None:
                self._events[ev.event_id] = ev
        self._load_embedding_cache()
        self._loaded = True
        logger.info(
            "TruthSocialReferenceDB: loaded %d events, %d total posts, "
            "embedding_cache=%d entries",
            len(self._events),
            sum(len(e.posts) for e in self._events.values()),
            len(self._embedding_cache),
        )

    def _build_event(
        self,
        row: dict[str, Any],
        *,
        narrative: str = "",
    ) -> ReferenceEvent | None:
        """v1.md row → ReferenceEvent (also populates posts)."""
        event_id = row["event_id"]
        post_ids = row.get("post_ids") or []
        if not isinstance(post_ids, list):
            post_ids = []
        # v1.md may have "TBD" strings instead of post_ids
        post_ids = [pid for pid in post_ids if isinstance(pid, str) and pid.isdigit()]

        raw_map = _load_raw_jsonl(event_id, self._raw_root)

        posts: list[ReferencePost] = []
        for pid in post_ids:
            raw = raw_map.get(pid)
            if not raw:
                continue
            text = (raw.get("text") or "").strip()
            if not text:
                continue
            ts = _parse_utc(raw.get("posted_at_utc"))
            if ts is None:
                continue
            posts.append(
                ReferencePost(
                    post_id=pid,
                    text=text,
                    posted_at_utc=ts,
                    is_retruth=bool(raw.get("is_retruth")),
                ),
            )

        # Short self-built — verbatim body is more useful for LLM matching than the narrative.
        combined_text = "\n\n".join(p.text for p in posts).strip()

        # Metadata pulled directly from v1.md frontmatter
        # category is the first element of topic_tags as the representative value
        # (e.g. ["tariff", "mexico"] → "tariff").
        topic_tags = row.get("topic_tags") or []
        if isinstance(topic_tags, list) and topic_tags:
            category = str(topic_tags[0]).strip().lower()
        else:
            category = ""
        # The sp500 change % lives in the narrative table, not the frontmatter. Keep a
        # fallback that automatically catches a future `sp500_pct` field if v1.md adds it.
        sp500 = float(row.get("sp500_pct_30min") or row.get("sp500_pct") or 0.0)
        score = int(row.get("market_impact_score") or row.get("impact_score") or 0)
        # direction is not in frontmatter either — default neutral (the LLM infers a new post's direction itself).
        direction = (row.get("direction") or "neutral").strip().lower()
        verify = (row.get("verification_level") or "").strip()

        return ReferenceEvent(
            event_id=event_id,
            posts=posts,
            category=category,
            posted_at_utc=_parse_utc(row.get("posted_at_utc")),
            market_impact_score=score,
            sp500_pct=sp500,
            direction=direction,
            verification_level=verify,
            combined_text=combined_text,
            narrative=narrative,
        )

    # ────────────────────────────────────────────────────────────────
    # Embedding cache (file-backed)
    # ────────────────────────────────────────────────────────────────
    def _load_embedding_cache(self) -> None:
        if not self._embedding_cache_path.exists():
            return
        try:
            self._embedding_cache = json.loads(
                self._embedding_cache_path.read_text(encoding="utf-8"),
            )
        except json.JSONDecodeError as e:
            logger.warning("reference_db: embedding cache parse failed: %s", e)
            self._embedding_cache = {}

    def _save_embedding_cache(self) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._embedding_cache_path.write_text(
                json.dumps(self._embedding_cache),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("reference_db: embedding cache save failed: %s", e)

    @staticmethod
    def _hash_text(text: str) -> str:
        """text → cache key (16-char hex)."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    # ────────────────────────────────────────────────────────────────
    # Embedding API
    # ────────────────────────────────────────────────────────────────
    async def _ensure_openai(self) -> Any:
        """Lazy AsyncOpenAI client."""
        if self._openai_client is not None:
            return self._openai_client
        if not self._openai_api_key:
            raise RuntimeError(
                "TruthSocialReferenceDB: enable_embedding=True but "
                "OPENAI_API_KEY is missing.",
            )
        try:
            from openai import AsyncOpenAI  # type: ignore[import-not-found]
        except ModuleNotFoundError as e:
            raise RuntimeError("openai package not installed") from e
        self._openai_client = AsyncOpenAI(api_key=self._openai_api_key)
        return self._openai_client

    async def _embed_one(self, text: str) -> list[float]:
        """Embed a single text (cache hit first)."""
        key = self._hash_text(text)
        if key in self._embedding_cache:
            return self._embedding_cache[key]
        client = await self._ensure_openai()
        resp = await client.embeddings.create(
            model=_EMBEDDING_MODEL,
            input=text[:8000],  # token-safe truncation (8K chars ≈ 2K tokens headroom)
        )
        vec = list(resp.data[0].embedding)
        self._embedding_cache[key] = vec
        return vec

    async def precompute_embeddings(self) -> None:
        """Precompute combined_text embeddings for all events (warms cache).

        Recommended to call once right after daemon startup — afterwards only one
        embedding per query is needed.
        """
        if not self._loaded:
            self.load()
        if not self._enable_embedding:
            logger.info("reference_db: embedding disabled — skip precompute")
            return
        n_new = 0
        for ev in self._events.values():
            if not ev.combined_text:
                continue
            if ev.embedding is not None:
                continue
            key = self._hash_text(ev.combined_text)
            if key in self._embedding_cache:
                ev.embedding = self._embedding_cache[key]
                continue
            try:
                ev.embedding = await self._embed_one(ev.combined_text)
                n_new += 1
            except Exception as e:  # noqa: BLE001 — best-effort precompute
                logger.warning(
                    "reference_db: embed failed for %s: %s", ev.event_id, e,
                )
        if n_new:
            self._save_embedding_cache()
        logger.info(
            "reference_db: precompute_embeddings — %d new, cache=%d total",
            n_new, len(self._embedding_cache),
        )

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        """Simple cosine similarity (avoids the numpy dependency)."""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = 0.0
        na = 0.0
        nb = 0.0
        for x, y in zip(a, b, strict=True):
            dot += x * y
            na += x * x
            nb += y * y
        if na == 0.0 or nb == 0.0:
            return 0.0
        return dot / ((na ** 0.5) * (nb ** 0.5))

    # ────────────────────────────────────────────────────────────────
    # Retrieval (Stage 1 + Stage 2)
    # ────────────────────────────────────────────────────────────────
    async def retrieve(
        self,
        query_text: str,
        *,
        top_k: int = 5,
        stage1_pool: int = 30,
        require_high_impact: bool = False,
    ) -> list[RetrievedReference]:
        """Return the top-K reference events most similar to a new Trump post.

        Args:
            query_text: verbatim body of the new post.
            top_k: number of final results (few-shot count entering the LLM prompt).
            stage1_pool: candidate pool size after Stage 1 (keyword).
                stage1_pool=0 forwards all events to Stage 2.
            require_high_impact: True → only events with market_impact_score ≥ 7.

        Returns:
            RetrievedReference list, descending by score_final.
        """
        if not self._loaded:
            self.load()
        if not self._events:
            return []

        candidates: list[ReferenceEvent] = list(self._events.values())
        if require_high_impact:
            candidates = [c for c in candidates if c.is_high_impact]
            if not candidates:
                return []

        # ── Stage 1: keyword overlap ─────────────────────────────────
        q_tokens = _tokenize(query_text)
        keyword_scored: list[tuple[ReferenceEvent, float]] = []
        for ev in candidates:
            ev_tokens = _tokenize(ev.combined_text)
            if not ev_tokens or not q_tokens:
                keyword_scored.append((ev, 0.0))
                continue
            overlap = len(q_tokens & ev_tokens)
            denom = max(len(q_tokens), 1)  # query-based ratio
            keyword_scored.append((ev, overlap / denom))

        keyword_scored.sort(key=lambda x: x[1], reverse=True)

        # Narrow the Stage 1 candidate pool — saves embedding cost
        if stage1_pool > 0:
            stage1 = keyword_scored[:stage1_pool]
        else:
            stage1 = keyword_scored

        # ── Stage 2: embedding cosine ────────────────────────────────
        embedding_score_by_id: dict[str, float] = {}
        if self._enable_embedding and stage1:
            try:
                q_vec = await self._embed_one(query_text)
                self._save_embedding_cache()
            except Exception as e:  # noqa: BLE001 — fallback to keyword only
                logger.warning("reference_db: query embed failed: %s", e)
                q_vec = []

            if q_vec:
                for ev, _ in stage1:
                    if ev.embedding is None:
                        # Lazy when precompute_embeddings was not invoked
                        try:
                            ev.embedding = await self._embed_one(ev.combined_text)
                        except Exception as e:  # noqa: BLE001
                            logger.warning(
                                "reference_db: lazy embed failed for %s: %s",
                                ev.event_id, e,
                            )
                            continue
                    embedding_score_by_id[ev.event_id] = self._cosine(q_vec, ev.embedding)
                self._save_embedding_cache()

        # ── Score combination (weighted) ─────────────────────────────
        results: list[RetrievedReference] = []
        for ev, kw_score in stage1:
            emb_score = embedding_score_by_id.get(ev.event_id, 0.0)
            # weight choice — embedding-dominant: 0.7, keyword: 0.3
            # (keyword only when embedding is disabled)
            if self._enable_embedding and embedding_score_by_id:
                final = 0.3 * kw_score + 0.7 * emb_score
            else:
                final = kw_score
            results.append(
                RetrievedReference(
                    event=ev,
                    score_keyword=kw_score,
                    score_embedding=emb_score,
                    score_final=final,
                ),
            )

        results.sort(key=lambda r: r.score_final, reverse=True)
        return results[:top_k]

    # ────────────────────────────────────────────────────────────────
    # Convenience
    # ────────────────────────────────────────────────────────────────
    @property
    def events(self) -> dict[str, ReferenceEvent]:
        """Memoized: return all events (test/debug use)."""
        if not self._loaded:
            self.load()
        return self._events

    def __len__(self) -> int:
        if not self._loaded:
            self.load()
        return len(self._events)


# ====================================================================
# Module-level test runner — sanity check when run directly
# ====================================================================
def _main() -> None:  # pragma: no cover
    """Run `python -m anomaly.channels.truth_social.reference_db` for a sanity check."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    db = TruthSocialReferenceDB(enable_embedding=False)
    db.load()
    print(f"loaded {len(db)} events")
    for eid, ev in list(db.events.items())[:5]:
        print(f"  {eid}: posts={len(ev.posts)} cat={ev.category} score={ev.market_impact_score}")

    async def _query() -> None:
        results = await db.retrieve(
            "I am imposing 50% tariffs on China rare earth exports!",
            top_k=3,
        )
        for r in results:
            print(f"  {r.event.event_id}: kw={r.score_keyword:.3f} final={r.score_final:.3f}")
    asyncio.run(_query())


if __name__ == "__main__":
    _main()
