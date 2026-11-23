"""Deterministic channel-language detection for the visibility filter.

Only letter characters are counted: digits, punctuation, whitespace, URLs and
emoji contribute nothing, so an emoji-only channel is ``unknown``.

Documented thresholds:

* At least ``MIN_SAMPLE`` weighted characters are needed; below that the
  sample is too small and the result is ``unknown``.
* One CJK character carries roughly one word, so CJK counts ``CJK_WEIGHT``
  times. This keeps a title such as ``TG 频道`` on the CJK side instead of
  splitting it evenly with the two Latin letters.
* A sample is ``cjk`` as soon as it carries ``CJK_MIN_CHARS`` raw CJK
  characters or a weighted CJK share of ``CJK_SHARE``, even when Latin or
  another script dominates by count. A channel that links 20+ ideographs is
  CJK content with a Latin wrapper, not a Latin channel.
* Otherwise the leading script must hold at least ``DOMINANT_SHARE`` of the
  weighted sample. Anything below that (and no tie) is ``other``: a genuinely
  mixed channel is not assigned to one language.
* A tie for the highest count is ``other`` as well, because neither script is
  the majority.

Codes are lowercase: ``cyrillic``, ``latin``, ``cjk``, ``other``, ``unknown``.
Language is a visibility filter only; it never changes similarity, size_fit,
or score.
"""

from __future__ import annotations

CYRILLIC = "cyrillic"
LATIN = "latin"
CJK = "cjk"
OTHER = "other"
UNKNOWN = "unknown"
LANGUAGES = (CYRILLIC, LATIN, CJK, OTHER, UNKNOWN)

MIN_SAMPLE = 3.0
DOMINANT_SHARE = 0.6
CJK_WEIGHT = 2.0
CJK_MIN_CHARS = 20
CJK_SHARE = 0.15


def _is_cyrillic(char: str) -> bool:
    return "\u0400" <= char <= "\u052f"


def _is_cjk(char: str) -> bool:
    return (
        "\u3040" <= char <= "\u30ff"  # Hiragana, Katakana
        or "\u3400" <= char <= "\u4dbf"  # CJK Unified Ideographs Extension A
        or "\u4e00" <= char <= "\u9fff"  # CJK Unified Ideographs
        or "\uf900" <= char <= "\ufaff"  # CJK Compatibility Ideographs
        or "\uac00" <= char <= "\ud7af"  # Hangul syllables
    )


def _is_latin(char: str) -> bool:
    return (
        "A" <= char <= "Z"
        or "a" <= char <= "z"
        or "\u00c0" <= char <= "\u00d6"
        or "\u00d8" <= char <= "\u00f6"
        or "\u00f8" <= char <= "\u024f"
        or "\u1e00" <= char <= "\u1eff"
    )


def detect_language(*texts: str | None) -> str:
    """Return the dominant script of the given texts (see the module docstring)."""
    counts = {CYRILLIC: 0.0, LATIN: 0.0, CJK: 0.0, OTHER: 0.0}
    raw_cjk = 0
    for text in texts:
        if not text:
            continue
        for char in text:
            if _is_cyrillic(char):
                counts[CYRILLIC] += 1.0
            elif _is_cjk(char):
                counts[CJK] += CJK_WEIGHT
                raw_cjk += 1
            elif _is_latin(char):
                counts[LATIN] += 1.0
            elif char.isalpha():
                counts[OTHER] += 1.0
    total = sum(counts.values())
    if total < MIN_SAMPLE:
        return UNKNOWN
    if raw_cjk >= CJK_MIN_CHARS or counts[CJK] / total >= CJK_SHARE:
        return CJK
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    leading, leading_count = ranked[0]
    if leading_count == ranked[1][1]:
        return OTHER
    if leading_count / total < DOMINANT_SHARE:
        return OTHER
    return leading
