"""Devanagari name normalisation for fuzzy matching.

Two forms are produced for every string:
  clean(s)    -> NFC, nukta stripped, digits ASCII, honorifics/guardian notes removed, spaces collapsed
  skeleton(s) -> clean(s) with vowel-length collapsed (ी→ि, ू→ु, ै→े, ौ→ो, ँ→ं) so that
                 साबिर / सबीर, भल्लू / भल्लु, शुक्ल / शुक्ला score high against each other.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# precomposed nukta letters -> base letter
_NUKTA_MAP = {
    "क़": "क", "ख़": "ख", "ग़": "ग", "ज़": "ज", "ड़": "ड", "ढ़": "ढ", "फ़": "फ", "य़": "य",
    "ऩ": "न", "ऱ": "र", "ळ": "ल",
}
_NUKTA_COMBINING = "़"
_DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# tokens that are not part of a person's name in khatauni text
_HONORIFICS = {
    "श्री", "श्रीमती", "श्रीमति", "कुमारी", "कु0", "कु.", "सुश्री", "मो0", "मो.", "मोहम्मद", "मो",
    "पुत्र", "पुत्री", "पत्नी", "विधवा", "वल्द", "व0", "उर्फ", "s/o", "d/o", "w/o",
    "नावा0", "नावा", "नाबालिग", "नाबा0", "अवयस्क", "साकिन", "सा0", "निवासी", "नि0",
}
# guardian / minor annotations: "नावा० आयु 13 वर्ष सं० मुनेश देवी माता सगी"
_GUARDIAN_RE = re.compile(r"(सं0|संरक्षक|संरक्षिका|सरंक्षक|माता|पिता|सगी|सगे|सगा)\b.*$")
_AGE_RE = re.compile(r"आयु\s*\d+\s*(वर्ष|साल)?")
_PUNCT_RE = re.compile(r"[\.\-_/,;:()\[\]{}'\"“”‘’!?|]+")
_SPACE_RE = re.compile(r"\s+")

_VOWEL_LEN = str.maketrans({
    "ी": "ि", "ू": "ु", "ै": "े", "ौ": "ो", "ँ": "ं", "ॉ": "ो", "ऐ": "ए", "औ": "ओ",
    "ई": "इ", "ऊ": "उ",
    "व": "ब",   # व/ब are used interchangeably in UP revenue records (साविर = साबिर)
})


def strip_nukta(s: str) -> str:
    s = unicodedata.normalize("NFC", s)
    for k, v in _NUKTA_MAP.items():
        s = s.replace(k, v)
    return s.replace(_NUKTA_COMBINING, "")


@lru_cache(maxsize=65536)
def clean(s: str) -> str:
    """Normalise a khatauni name field to a comparable string.

    Cached: matching compares every row against the same handful of target spellings, so the same
    strings are normalised millions of times over a full rematch. The cache is bounded, and LRU keeps
    the target spellings resident while row values churn through it."""
    if not s:
        return ""
    s = strip_nukta(s).translate(_DEV_DIGITS)
    s = _AGE_RE.sub(" ", s)
    s = _GUARDIAN_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    toks = [t for t in _SPACE_RE.split(s.strip()) if t and t not in _HONORIFICS]
    return " ".join(toks)


@lru_cache(maxsize=65536)
def skeleton(s: str) -> str:
    return clean(s).translate(_VOWEL_LEN)


def aliases(s: str) -> list[str]:
    """Split 'X उर्फ Y' into both names; always returns at least the whole string."""
    parts = [p.strip() for p in re.split(r"\s+उर्फ\s+", strip_nukta(s)) if p.strip()]
    return parts or [s]


def tokens(s: str) -> list[str]:
    return clean(s).split()
