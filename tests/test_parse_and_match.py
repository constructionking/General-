import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhulekh.browser import parse_row  # noqa: E402
from bhulekh.config import load_config  # noqa: E402
from bhulekh.matcher import categorise, match_row, targets_from_config  # noqa: E402
from bhulekh.normalize import clean, skeleton  # noqa: E402

TARGETS = targets_from_config(load_config())


def test_parse_rows():
    r = parse_row("61 : मक्खन : मुरली : 1179440061000012 : (2.6750 हे०)")
    assert r and r.khata == "61" and r.khatedar == "मक्खन" and r.father == "मुरली"
    assert r.unique_code == "1179440061000012" and r.area == 2.675
    r = parse_row("8/1 : मदन सिंह : ओमकार सिंह : 1179440008100112 : (0.6630 हे०)")
    assert r and r.khata == "8/1"
    r = parse_row("113मि : महेन्द्र सिंह : खचेडू सिंह : 1179441130002612 : (0.1250 हे०)")
    assert r and r.khata == "113मि"
    r = parse_row("5 : मयंक नावा0 आयु 13 वर्ष : अजब सिंह सं0 मुनेश देवी माता सगी : 1179440005000012 : (1.1490 हे०)")
    assert r and r.khatedar.startswith("मयंक")


def test_normalize():
    assert clean("श्री साबिर अली पुत्र भल्लू") == "साबिर अली भल्लू"
    assert clean("मयंक नावा0 आयु 13 वर्ष") == "मयंक"
    assert clean("अजब सिंह सं0 मुनेश देवी माता सगी") == "अजब सिंह"
    assert skeleton("सबीर") == skeleton("सबिर")
    assert clean("सादिक़ अली") == "सादिक अली"


def _m(k, f):
    return match_row(k, f, TARGETS)


def test_family_hits():
    m = _m("साबिर अली", "भल्लू")
    assert m and m.target.id == "T1" and m.score >= 90
    m = _m("सादिक अली", "साबिर अली")
    assert m and m.target.id == "T2" and m.score >= 90
    m = _m("सबीर अली", "भल्लु")            # spelling variants
    assert m and m.target.id == "T1"
    cat, why = categorise(m, "Amroha", 1, 1)
    assert cat in ("probable", "less_probable") and "variant" in why or cat == "probable"


def test_non_hits():
    assert _m("साबिर अली", "रहमत अली") is None
    assert _m("मक्खन", "मुरली") is None
    assert _m("विजय शुक्ला", "कमलेश शुक्ला") is None
    assert _m("संजय सिंह", "रामसरन सिंह") is None


def test_conflict_is_less_probable():
    m = _m("साबिर अली", "भल्लू सिंह")
    if m:  # may still clear the threshold, but must not be probable
        cat, why = categorise(m, "Amroha", 1, 1)
        assert cat == "less_probable" and "conflicting" in why


def test_vijay():
    m = _m("विजय शुक्ला", "राम शंकर शुक्ला")
    assert m and m.target.id == "T3" and m.father_score == 100
    cat, why = categorise(m, "Lucknow", 1, 1)
    assert cat == "probable"
    cat, why = categorise(m, "Agra", 1, 1)
    assert cat == "less_probable" and "outside" in why
    m = _m("विजय कुमार शुक्ल", "रवि शंकर")
    assert m and m.target.id == "T3"
    m = _m("विजय शुक्ला", "रामेश्वर प्रसाद शुक्ला")   # loose: र… … शुक्ला
    assert m and m.father_score < 90
