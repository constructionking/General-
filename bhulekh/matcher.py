"""Target matching and hit categorisation.

A row is a hit only when BOTH khatedar and father clear the threshold for one target.
Every hit carries the two scores, the rule that fired and a human-readable reasoning.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from rapidfuzz import fuzz

from .normalize import aliases, clean, skeleton, tokens

HIT_MIN = 80          # both names must reach this
PROBABLE_MIN = 90     # both names must reach this AND no conflicting tokens
FIRST_TOKEN_MIN = 85  # the given name itself must match: विनय is not विजय even if the surname matches

# Surname / lineage tokens. A row that carries one of these which appears in NONE of the target's spellings
# belongs to a different family, however similar the given names: सादिक हुसैन s/o साबिर खां shares no
# lineage with सादिक अली s/o साबिर अली. Skeleton form (vowel length folded, व→ब) so spellings agree.
LINEAGE_TOKENS = {
    "अली", "खां", "खान", "खाँ", "खा", "शाह", "हुसैन", "हुसेन", "हुसैनी", "जैदी", "रिजवी", "नकवी", "सिद्दीकी",
    "अंसारी", "कुरैशी", "शेख", "मलिक", "अहमद", "मोहम्मद", "सिंह", "यादव", "वर्मा", "शर्मा", "गुप्ता", "शुक्ला",
    "शुक्ल", "मिश्रा", "मिश्र", "तिवारी", "पांडेय", "पाण्डेय", "दुबे", "द्विवेदी", "त्रिपाठी", "चौधरी", "कुमार",
    "देवी", "प्रसाद", "लाल", "राम", "बेगम", "खातून", "बानो",
}


@dataclass
class Target:
    id: str
    label: str
    khatedar: list[str]                    # accepted spellings
    father: list[str] = field(default_factory=list)
    father_regex: Optional[str] = None     # used when father is only known by initials
    father_loose_regex: Optional[str] = None
    district_hint: Optional[str] = None    # English district name where a hit is expected
    prefixes: list[str] = field(default_factory=list)   # on-screen keys to type


@dataclass
class Match:
    target: Target
    name_score: float
    father_score: float
    name_variant: str
    father_variant: str
    conflicts: list[str]

    @property
    def score(self) -> float:
        return min(self.name_score, self.father_score)

    @property
    def is_hit(self) -> bool:
        return self.name_score >= HIT_MIN and self.father_score >= HIT_MIN


_LINEAGE_SK = {skeleton(t) for t in LINEAGE_TOKENS}


def _lineage(value: str) -> set[str]:
    """Lineage/surname tokens present in a name (skeleton form)."""
    return {t for t in skeleton(value).split() if t in _LINEAGE_SK}


def lineage_conflict(value: str, variants: list[str]) -> Optional[str]:
    """The first lineage token in `value` that none of the target spellings carries, else None.
    'साबिर' (no surname) never conflicts; 'साबिर खां' conflicts with ['साबिर अली', 'साबिर']."""
    allowed = set()
    for v in variants:
        allowed |= _lineage(v)
    for t in skeleton(value).split():
        if t in _LINEAGE_SK and t not in allowed:
            return t
    return None


def _first_token_ok(first: str, variant_first: str) -> bool:
    """The given name must match on a token boundary: identical after normalisation, or a near-identical
    spelling that is not merely the other with letters appended. Rejects the feminine साबिरा against साबिर
    (a suffix) and सदाकत against सादिक (too different), accepts भालू against भल्लू (a conjunct)."""
    if first == variant_first:
        return True
    if first.startswith(variant_first) or variant_first.startswith(first):
        return False
    return fuzz.ratio(first, variant_first) >= FIRST_TOKEN_MIN


def _given_name_matches(value: str, variants: list[str]) -> bool:
    """Does the given name (first token) of value match the given name of any variant?"""
    for alias in aliases(value):
        sk = skeleton(alias)
        if sk and any(_first_token_ok(sk.split()[0], skeleton(v).split()[0]) for v in variants if skeleton(v)):
            return True
    return False


def _best(value: str, variants: list[str]) -> tuple[float, str]:
    """Best skeleton ratio of any alias of value against any variant."""
    best, best_v = 0.0, ""
    for alias in aliases(value):
        sk = skeleton(alias)
        if not sk:
            continue
        first = sk.split()[0]
        for v in variants:
            skv = skeleton(v)
            if not _first_token_ok(first, skv.split()[0]):
                continue
            s = fuzz.ratio(sk, skv)
            if s > best:
                best, best_v = s, v
    return best, best_v


def _conflicts(value: str, variant: str) -> list[str]:
    """Tokens present in the row that the target spelling does not have (e.g. 'खान', 'सिंह')."""
    have = set(tokens(variant))
    extra = []
    for t in tokens(value):
        # a token is extra when it fuzzy-matches nothing in the variant
        if all(fuzz.ratio(skeleton(t), skeleton(h)) < 75 for h in have):
            extra.append(t)
    return extra


def _father_regex_score(father: str, tgt: Target) -> tuple[float, str]:
    sk = skeleton(father)
    if tgt.father_regex and re.search(tgt.father_regex, sk):
        return 100.0, f"regex {tgt.father_regex}"
    if tgt.father_loose_regex and re.search(tgt.father_loose_regex, sk):
        return 82.0, f"loose regex {tgt.father_loose_regex}"
    return 0.0, ""


def match_row(khatedar: str, father: str, targets: list[Target]) -> Optional[Match]:
    """Return the best Match among targets, or None when no target is a hit.

    The father is the anchor: the person's own name may vary in spelling, the father must match. A surname
    line the target never uses (खां, शाह, हुसैन, जैदी, सिंह …) on either name rejects the row outright."""
    best: Optional[Match] = None
    for tgt in targets:
        if lineage_conflict(khatedar, tgt.khatedar):
            continue
        if tgt.father and lineage_conflict(father, tgt.father):
            continue
        ns, nv = _best(khatedar, tgt.khatedar)
        if tgt.father:
            fs, fv = _best(father, tgt.father)
        else:
            fs, fv = _father_regex_score(father, tgt)
        m = Match(tgt, ns, fs, nv, fv, [])
        if not m.is_hit:
            continue
        m.conflicts = _conflicts(khatedar, nv) + (_conflicts(father, fv) if tgt.father else [])
        if best is None or m.score > best.score:
            best = m
    return best


@dataclass
class NearMiss:
    """Right name, wrong father (or a foreign surname line): logged for audit, never counted as family land."""
    target: Target
    name_score: float
    father_score: float
    name_variant: str
    reason: str


def near_miss(khatedar: str, father: str, targets: list[Target]) -> Optional[NearMiss]:
    """When match_row returned None: does the khatedar alone match a target? Explains why the row failed."""
    best: Optional[NearMiss] = None
    for tgt in targets:
        if not _given_name_matches(khatedar, tgt.khatedar):
            continue
        ns, nv = _best(khatedar, tgt.khatedar)
        nv = nv or tgt.khatedar[0]
        why = []
        lc = lineage_conflict(khatedar, tgt.khatedar)
        if lc:
            why.append(f"खातेदार carries the surname line '{lc}', which the target never uses")
        if tgt.father:
            fs, _ = _best(father, tgt.father)
            flc = lineage_conflict(father, tgt.father)
            if flc:
                why.append(f"पिता '{father}' is of the '{flc}' line, not '{tgt.father[0]}'")
            elif fs < HIT_MIN:
                why.append(f"पिता '{father}' does not match '{tgt.father[0]}' ({fs:.0f}%)")
        else:
            fs, _ = _father_regex_score(father, tgt)
            if fs < HIT_MIN:
                why.append(f"पिता '{father}' does not fit the initials pattern")
        if not why:
            continue
        nm = NearMiss(tgt, ns, fs, nv, f"खातेदार matches '{nv}' ({ns:.0f}%) but " + "; ".join(why))
        if best is None or nm.name_score > best.name_score:
            best = nm
    return best


def categorise(m: Match, district_en: str, sibling_hits_in_village: int, family_hits_in_tehsil: int) -> tuple[str, str]:
    """Return (category, reasoning) for a hit.

    category in {"probable", "less_probable"}.
    """
    t = m.target
    reasons = []
    probable = m.name_score >= PROBABLE_MIN and m.father_score >= PROBABLE_MIN and not m.conflicts

    if m.name_score >= PROBABLE_MIN:
        reasons.append(f"खातेदार matches '{m.name_variant}' ({m.name_score:.0f}%)")
    else:
        reasons.append(f"खातेदार is a spelling variant of '{m.name_variant}' ({m.name_score:.0f}%)")
    if t.father:
        if m.father_score >= PROBABLE_MIN:
            reasons.append(f"पिता matches '{m.father_variant}' ({m.father_score:.0f}%)")
        else:
            reasons.append(f"पिता is a spelling variant of '{m.father_variant}' ({m.father_score:.0f}%)")
    else:
        if m.father_score >= PROBABLE_MIN:
            reasons.append("पिता fits the R. S. initials pattern (first token र…, second token स/श…)")
        else:
            reasons.append("पिता fits the initials only loosely")
            probable = False
    if m.conflicts:
        reasons.append("extra/conflicting tokens in the record: " + ", ".join(m.conflicts))

    # context signals
    if t.district_hint:
        if district_en.lower() == t.district_hint.lower():
            reasons.append(f"village is in the expected district ({t.district_hint})")
        else:
            reasons.append(f"name match outside the expected district ({t.district_hint}); treat cautiously")
            probable = False
    if family_hits_in_tehsil > 1:
        reasons.append(f"{family_hits_in_tehsil} family hits in the same tehsil (grandfather/father cluster)")
    if sibling_hits_in_village > 1:
        reasons.append(f"{sibling_hits_in_village} khatas for this pair in the same village")
    if not probable and family_hits_in_tehsil <= 1 and sibling_hits_in_village <= 1:
        reasons.append("isolated single hit with no other family hit nearby")

    return ("probable" if probable else "less_probable"), "; ".join(reasons)


def targets_from_config(cfg: dict) -> list[Target]:
    out = []
    for t in cfg.get("targets", []):
        out.append(Target(
            id=t["id"], label=t.get("label", t["id"]),
            khatedar=list(t["khatedar"]), father=list(t.get("father", [])),
            father_regex=t.get("father_regex"), father_loose_regex=t.get("father_loose_regex"),
            district_hint=t.get("district_hint"), prefixes=list(t.get("prefixes", [])),
        ))
    return out


def all_prefixes(targets: list[Target]) -> list[str]:
    seen, out = set(), []
    for t in targets:
        for p in t.prefixes:
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


__all__ = ["Target", "Match", "NearMiss", "match_row", "near_miss", "lineage_conflict", "categorise",
           "targets_from_config", "all_prefixes", "clean"]
