"""Niche profile, TF-IDF similarity, size fit, and score persisted on channels."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit

from tg_collab.config import get_float
from tg_collab.language import UNKNOWN, detect_language
from tg_collab.models import ChannelPost, Profile
from tg_collab.niche import load_niche
from tg_collab.profile import TargetRange, derive_range
from tg_collab.store import get_posts, profile_scope

TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]{3,}")
HASHTAG_RE = re.compile(r"#([A-Za-zА-Яа-яЁё0-9_]{2,})")
URL_RE = re.compile(r"https?://[^\s<>\"']+|(?<![\w/])t\.me/[^\s<>\"']+", re.IGNORECASE)
HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{3,})")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
TITLE_WEIGHT = 2.0
BIGRAM_WEIGHT = 0.5
HASHTAG_MATCH_BONUS = 0.05
DOMAIN_MATCH_BONUS = 0.05
#: A stored (agent or manual) niche term that appears in a candidate adds a
#: small match bonus; the whole bonus is capped so it cannot beat the cosine.
NICHE_TERM_MATCH_BONUS = 0.02
NICHE_TERM_BONUS_CAP = 0.10
MAX_POSTS_JSON_BYTES = 200_000
#: How many cleaned deterministic terms make a usable list before the repeat
#: rule applies; a thinner corpus falls back to score order.
MIN_QUALIFIED_TERMS = 4
#: Document frequency says little about a corpus this small, so the repeat rule
#: only kicks in for a profile with a real posting history.
MIN_DOCS_FOR_REPEAT = 5

#: Domain fragments and schemes that are never niche terms.
TLD_NOISE = frozenset(
    {"com", "ru", "org", "net", "io", "tv", "www", "http", "https", "tme", "me", "co", "uk"}
)
PLATFORM_NOISE = frozenset(
    {
        "tiktok",
        "youtube",
        "youtu",
        "instagram",
        "facebook",
        "twitter",
        "linkedin",
        "vk",
        "dzen",
        "telegram",
        "телеграм",
    }
)
GENERIC_NOISE = frozenset({"работает", "работают", "works", "working"})
#: Generic Russian filler that survives TF-IDF in a small personal-channel corpus.
#: Used only when choosing display tags, never in the similarity formula.
TAG_FILLER = frozenset(
    {"пусть", "вас", "меня", "хочу", "нравится", "пока", "вот", "время", "всем", "привет"}
)
#: Styled Telegram text uses small-caps lookalikes; the target script follows the word.
FANCY_LATIN = str.maketrans(
    {
        "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f", "ɢ": "g",
        "ʜ": "h", "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n",
        "ᴏ": "o", "ᴩ": "p", "ʀ": "r", "ꜱ": "s", "ᴛ": "t", "ᴜ": "u", "ᴠ": "v",
        "ᴡ": "w", "ʏ": "y", "ᴢ": "z",
    }
)
FANCY_CYRILLIC = str.maketrans(
    {
        "ᴀ": "а", "ʙ": "б", "ᴄ": "с", "ᴅ": "д", "ᴇ": "е", "ꜰ": "ф", "ɢ": "г",
        "ʜ": "н", "ɪ": "и", "ᴊ": "й", "ᴋ": "к", "ʟ": "л", "ᴍ": "м", "ɴ": "н",
        "ᴏ": "о", "ᴩ": "р", "ʀ": "р", "ꜱ": "с", "ᴛ": "т", "ᴜ": "у", "ᴠ": "в",
        "ᴡ": "в", "ʏ": "у", "ᴢ": "з",
    }
)
FANCY_CYRILLIC_ONLY = str.maketrans({"ʍ": "м", "ᴦ": "г", "ᴧ": "л", "ᴨ": "п"})
SCRIPT_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
CYRILLIC_CHAR_RE = re.compile(r"[А-Яа-яЁё]")
MAX_TOKEN_CHARS = 30
UNDERSCORE_RUN_RE = re.compile(r"_+")
#: Russian suffixes folded for display, longest first; the stem keeps >= 4 chars.
RU_SUFFIXES = (
    "ами", "ями", "ии", "ий", "ов", "ев", "ей", "ые", "ая", "ое", "ие",
    "ам", "ям", "ах", "ях", "ой", "ом", "ем", "а", "я", "ы", "и", "е", "у", "ю", "о",
)
#: Latin endings that must not lose a trailing "s" during folding.
LATIN_KEEP_ENDINGS = ("ss", "us", "is", "ws")


STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "your", "you", "about",
        "our", "their", "there", "here", "what", "when", "where", "which", "will",
        "have", "has", "are", "was", "were", "not", "but", "all", "any", "can",
        "how", "who", "why", "its", "it's", "into", "than", "then", "them", "they",
        "these", "those", "just", "also", "more", "most", "some", "such", "only",
        "over", "under", "after", "before", "every", "each", "other", "very",
        "канал", "канала", "канале", "каналы", "каналов", "про", "для", "что", "как",
        "это", "или", "так", "все", "его", "она", "они", "мы", "вы", "мне", "тебя",
        "если", "уже", "ещё", "еще", "был", "была", "было", "быть", "есть", "нет",
        "при", "над", "под", "без", "через", "может", "можно", "нужно", "очень",
        "свой", "свои", "своих", "этот", "эта", "эти", "того", "тоже", "также",
        "который", "которая", "которые", "чтобы", "где", "когда", "почему",
    }
)


def _fold_styled_word(word: str) -> str:
    table = FANCY_CYRILLIC if CYRILLIC_CHAR_RE.search(word) else FANCY_LATIN
    return word.translate(table)


def normalize_text(text: str | None) -> str:
    """Fold styled Telegram text to plain letters and split ``_`` runs."""
    if not text:
        return ""
    plain = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text))
    plain = "".join(char for char in plain if not unicodedata.combining(char))
    plain = unicodedata.normalize("NFC", plain).translate(FANCY_CYRILLIC_ONLY)
    plain = UNDERSCORE_RUN_RE.sub(" ", plain)
    return SCRIPT_WORD_RE.sub(lambda match: _fold_styled_word(match.group(0)), plain)


def tokenize(text: str | None) -> list[str]:
    if not text:
        return []
    return [
        token
        for token in (match.group(0).lower() for match in TOKEN_RE.finditer(normalize_text(text)))
        if token not in STOPWORDS and not token.isdigit() and len(token) <= MAX_TOKEN_CHARS
    ]


def hashtags(texts: Iterable[str | None]) -> set[str]:
    found: set[str] = set()
    for text in texts:
        if text:
            found.update(
                match.group(1).lower()
                for match in HASHTAG_RE.finditer(normalize_text(text))
            )
    return found


def domains(texts: Iterable[str | None]) -> set[str]:
    found: set[str] = set()
    for text in texts:
        if not text:
            continue
        for match in URL_RE.finditer(normalize_text(text)):
            raw = match.group(0)
            if raw.startswith("t.me/"):
                continue
            host = (urlsplit(raw).hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
            if host and host not in {"t.me", "telegram.me"}:
                found.add(host)
    return found


@dataclass(frozen=True)
class NicheProfile:
    idf: dict[str, float]
    vector: dict[str, float]
    norm: float
    hashtags: frozenset[str]
    domains: frozenset[str]
    terms: tuple[str, ...]
    agent_terms: frozenset[str] = frozenset()


def _doc_terms(text: str | None) -> dict[str, float]:
    tokens = tokenize(text)
    counts: dict[str, float] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0.0) + 1.0
    for left, right in zip(tokens, tokens[1:]):
        bigram = f"{left} {right}"
        counts[bigram] = counts.get(bigram, 0.0) + BIGRAM_WEIGHT
    return counts


def _profile_docs(profile: Profile | None) -> list[tuple[str, float]]:
    if profile is None:
        return []
    header = " ".join(part for part in (profile.title, profile.description) if part)
    posts = profile_posts(profile)
    docs: list[tuple[str, float]] = [(header, TITLE_WEIGHT)]
    pinned = (profile.pinned_text or "").strip()
    if pinned:
        docs.append((pinned, TITLE_WEIGHT))
    for item in posts:
        docs.append((str(item.get("text") or ""), 1.0))
    tag_text = " ".join(f"#{tag}" for tag in hashtags(item.get("text") for item in posts))
    if tag_text:
        docs.append((tag_text, TITLE_WEIGHT))
    return [(text, weight) for text, weight in docs if text]


def profile_posts(profile: Profile | None) -> list[dict[str, Any]]:
    if profile is None:
        return []
    try:
        value = json.loads(profile.posts_json or "[]")
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def owner_language(profile: Profile | None) -> str:
    """The owner's language, detected from title, description, and newest posts.

    Lives here rather than in ``language`` so the detector stays a pure text
    helper while this one may read the profile corpus.
    """
    if profile is None:
        return UNKNOWN
    texts: list[str | None] = [profile.title, profile.description]
    texts.extend(str(item.get("text") or "") for item in profile_posts(profile)[:30])
    return detect_language(*texts)


def build_niche_profile(profile: Profile | None) -> NicheProfile:
    docs = _profile_docs(profile)
    document_frequency: dict[str, int] = {}
    doc_terms = [_doc_terms(text) for text, _ in docs]
    for terms in doc_terms:
        for term in terms:
            document_frequency[term] = document_frequency.get(term, 0) + 1
    total = max(len(docs), 1)
    idf = {
        term: math.log((1.0 + total) / (1.0 + frequency)) + 1.0
        for term, frequency in document_frequency.items()
    }
    vector: dict[str, float] = {}
    for terms, (_, weight) in zip(doc_terms, docs):
        for term, count in terms.items():
            vector[term] = vector.get(term, 0.0) + count * weight * idf[term]
    norm = math.sqrt(sum(value * value for value in vector.values()))
    texts = [text for text, _ in docs]
    terms_sorted = tuple(
        term for term, _ in sorted(vector.items(), key=lambda item: (-item[1], item[0]))
    )
    stored = load_niche(profile)
    return NicheProfile(
        idf=idf,
        vector=vector,
        norm=norm,
        hashtags=frozenset(hashtags(texts)),
        domains=frozenset(domains(texts)),
        terms=terms_sorted,
        agent_terms=frozenset(stored.terms) if stored is not None else frozenset(),
    )


def top_terms(profile: Profile | None, limit: int = 5) -> list[str]:
    return list(build_niche_profile(profile).terms[: max(limit, 0)])


NICHE_TERM_LIMIT = 12
NICHE_TAG_LIMIT = 5


def _clean_text(text: str | None) -> str:
    """Drop URLs and @handles so their fragments never become niche terms."""
    without_urls = URL_RE.sub(" ", normalize_text(text))
    return HANDLE_RE.sub(" ", without_urls)


def _owner_noise(profile: Profile | None, texts: Iterable[str | None]) -> frozenset[str]:
    names: set[str] = set()
    if profile is not None and profile.username:
        names.add(profile.username.lower())
    for text in texts:
        names.update(match.lower() for match in HANDLE_RE.findall(text or ""))
    return frozenset(names)


def _fold(term: str) -> str:
    """A grouping key so simple word forms share one chip."""
    if " " in term:
        return term
    if CYRILLIC_RE.search(term):
        for suffix in RU_SUFFIXES:
            stem = term[: -len(suffix)]
            if term.endswith(suffix) and len(stem) >= 4:
                return stem
        return term
    if (
        len(term) >= 5
        and term.endswith("s")
        and not term.endswith(LATIN_KEEP_ENDINGS)
    ):
        return term[:-1]
    return term


def _is_noise(term: str, owner: frozenset[str]) -> bool:
    if term in owner or term in TLD_NOISE or term in PLATFORM_NOISE or term in GENERIC_NOISE:
        return True
    if len(term) > MAX_TOKEN_CHARS:
        return True
    if " " in term:
        return any(
            part in owner
            or part in TLD_NOISE
            or part in PLATFORM_NOISE
            or part in TAG_FILLER
            or len(part) > MAX_TOKEN_CHARS
            for part in term.split()
        )
    return term in TAG_FILLER or term.count("_") > 1


def _merge_forms(terms: Iterable[str]) -> list[str]:
    """Collapse word forms, keeping the highest-weighted (first) spelling."""
    order: list[str] = []
    display: dict[str, str] = {}
    for term in terms:
        key = _fold(term)
        if key in display:
            continue
        display[key] = term
        order.append(key)
    return [display[key] for key in order][:NICHE_TERM_LIMIT]


def _merge_terms(terms: list[str]) -> list[str]:
    """The stored or deterministic terms, deduplicated and capped."""
    merged: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(term)
    return merged[:NICHE_TERM_LIMIT]


def deterministic_tags(profile: Profile | None) -> tuple[list[str], list[str], list[str]]:
    """Cleaned deterministic terms, hashtags and link domains.

    The TF-IDF vector still ranks the terms; this only filters noise and merges
    simple word forms. A term survives when it repeats across documents or
    appears in the title/description. A corpus smaller than
    ``MIN_DOCS_FOR_REPEAT`` cannot make document frequency meaningful, so it
    keeps its ranked order instead of emptying.
    """
    niche = build_niche_profile(profile)
    docs = _profile_docs(profile)
    texts = [text for text, _ in docs]
    title = profile.title if profile is not None else None
    description = profile.description if profile is not None else None
    owner = _owner_noise(profile, [title, description, *texts])
    header_clean = _clean_text(" ".join(part for part in (title, description) if part))
    header_tokens = set(tokenize(header_clean))
    doc_tokens = [set(tokenize(_clean_text(text))) for text in texts]
    doc_texts = [_clean_text(text) for text in texts]

    def repeated(term: str) -> bool:
        if " " in term:
            return term in header_clean or sum(1 for text in doc_texts if term in text) >= 2
        return term in header_tokens or sum(1 for tokens in doc_tokens if term in tokens) >= 2

    filtered = [term for term in niche.terms if not _is_noise(term, owner)]
    qualified = [term for term in filtered if repeated(term)]
    chosen = (
        qualified
        if len(qualified) >= MIN_QUALIFIED_TERMS and len(docs) >= MIN_DOCS_FOR_REPEAT
        else filtered
    )
    return (
        _merge_terms(_merge_forms(chosen)),
        sorted(niche.hashtags)[:NICHE_TAG_LIMIT],
        sorted(niche.domains)[:NICHE_TAG_LIMIT],
    )


def niche_tags(profile: Profile | None) -> tuple[list[str], list[str], list[str]]:
    """Effective terms, hashtags and link domains for the UI, queries and prompts.

    A stored (agent or manual) niche wins; otherwise the cleaned deterministic
    tags are used. The dashboard tag editor writes that stored niche.
    """
    stored = load_niche(profile)
    if stored is not None:
        return (
            _merge_terms(list(stored.terms[:NICHE_TERM_LIMIT])),
            list(stored.hashtags[:NICHE_TAG_LIMIT]),
            list(stored.domains[:NICHE_TAG_LIMIT]),
        )
    return deterministic_tags(profile)


def cosine(niche: NicheProfile, docs: list[tuple[str, float]]) -> float:
    if niche.norm <= 0 or not docs:
        return 0.0
    vector: dict[str, float] = {}
    for text, weight in docs:
        for term, count in _doc_terms(text).items():
            if term in niche.idf:
                vector[term] = vector.get(term, 0.0) + count * weight * niche.idf[term]
    norm = math.sqrt(sum(value * value for value in vector.values()))
    if norm <= 0:
        return 0.0
    dot = sum(value * niche.vector.get(term, 0.0) for term, value in vector.items())
    return max(0.0, min(1.0, dot / (niche.norm * norm)))


def _matched_niche_terms(
    terms: frozenset[str], title: str | None, description: str | None, texts: list[str | None]
) -> int:
    haystack = " ".join(
        [title or "", description or "", *(text or "" for text in texts)]
    ).lower()
    tokens = set(tokenize(haystack))
    return sum(1 for term in terms if (term in haystack if " " in term else term in tokens))


def similarity_score(
    niche: NicheProfile,
    title: str | None,
    description: str | None,
    posts: Iterable[ChannelPost],
) -> int:
    texts = [post.text for post in posts]
    docs = [(" ".join(part for part in (title, description) if part), TITLE_WEIGHT)]
    docs.extend((text or "", 1.0) for text in texts)
    base = cosine(niche, [(text, weight) for text, weight in docs if text])
    matched_tags = len(niche.hashtags & hashtags(texts))
    matched_domains = len(niche.domains & domains(texts))
    bonus = matched_tags * HASHTAG_MATCH_BONUS + matched_domains * DOMAIN_MATCH_BONUS
    if niche.agent_terms:
        matched = _matched_niche_terms(niche.agent_terms, title, description, texts)
        bonus += min(matched * NICHE_TERM_MATCH_BONUS, NICHE_TERM_BONUS_CAP)
    return max(0, min(100, int(round(100 * min(1.0, base + bonus)))))


def size_fit(subscribers: int | None, basis: int, target: TargetRange) -> float:
    if subscribers is None:
        return 0.3
    if target.low <= subscribers <= target.high:
        return 1.0
    if basis > 0 and (
        0.1 * basis <= subscribers < target.low or target.high < subscribers <= 10 * basis
    ):
        return 0.5
    return 0.1


def _target(profile: Profile, settings: dict[str, str]) -> TargetRange:
    min_mult = get_float(settings, "min_mult")
    max_mult = get_float(settings, "max_mult")
    return derive_range(
        profile,
        min_mult if min_mult is not None else 0.3,
        max_mult if max_mult is not None else 3.0,
    )


def score_channel(
    niche: NicheProfile,
    profile: Profile | None,
    settings: dict[str, str],
    subscribers: int | None,
    title: str | None,
    description: str | None,
    posts: Iterable[ChannelPost],
) -> tuple[int, float, float]:
    if profile is None:
        return 0, 0.3, 0.0
    target = _target(profile, settings)
    basis = profile.subscribers or profile.avg_views or 0
    similarity = similarity_score(niche, title, description, posts)
    fit = size_fit(subscribers, basis, target)
    return similarity, fit, round(similarity / 100 * fit, 4)


def _score_row(
    conn: sqlite3.Connection,
    niche: NicheProfile,
    profile: Profile,
    settings: dict[str, str],
    row: sqlite3.Row,
) -> tuple[int, float, float]:
    posts = get_posts(conn, row["id"], limit=30)
    return score_channel(
        niche,
        profile,
        settings,
        row["subscribers"],
        row["title"],
        row["description"],
        posts,
    )


def rescore_channels(
    conn: sqlite3.Connection,
    profile: Profile,
    settings: dict[str, str],
    profile_id: int | None = None,
) -> int:
    niche = build_niche_profile(profile)
    pid = profile_scope(conn, profile_id)
    rows = conn.execute(
        "SELECT id, username, title, description, subscribers FROM channels WHERE profile_id=?",
        (pid,),
    ).fetchall()
    with conn:
        for row in rows:
            similarity, fit, score = _score_row(conn, niche, profile, settings, row)
            conn.execute(
                "UPDATE channels SET similarity=?, size_fit=?, score=? WHERE id=?",
                (similarity, fit, score, row["id"]),
            )
    return len(rows)


def rescore_channel(
    conn: sqlite3.Connection,
    profile: Profile,
    settings: dict[str, str],
    username: str,
    niche: NicheProfile | None = None,
    profile_id: int | None = None,
) -> bool:
    pid = profile_scope(conn, profile_id)
    row = conn.execute(
        "SELECT id, username, title, description, subscribers FROM channels "
        "WHERE username=? COLLATE NOCASE AND profile_id=?",
        (username.lstrip("@"), pid),
    ).fetchone()
    if row is None:
        return False
    profile_niche = niche if niche is not None else build_niche_profile(profile)
    similarity, fit, score = _score_row(conn, profile_niche, profile, settings, row)
    with conn:
        conn.execute(
            "UPDATE channels SET similarity=?, size_fit=?, score=? WHERE id=?",
            (similarity, fit, score, row["id"]),
        )
    return True


def posts_to_json(posts: Iterable[ChannelPost], limit: int = 30) -> str:
    items = [
        {
            "text": post.text or "",
            "date": post.posted_at,
            "hashtags": sorted(hashtags([post.text])),
        }
        for post in list(posts)[: max(limit, 0)]
    ]
    while True:
        encoded = json.dumps(items, ensure_ascii=False)
        if len(encoded.encode("utf-8")) <= MAX_POSTS_JSON_BYTES or not items:
            return encoded
        items.pop()
