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
            if fuzz.ratio(first, skv.split()[0]) < FIRST_TOKEN_MIN:
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
    """Return the best Match among targets, or None when no target is a hit."""
    best: Optional[Match] = None
    for tgt in targets:
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


__all__ = ["Target", "Match", "match_row", "categorise", "targets_from_config", "all_prefixes", "clean"]
