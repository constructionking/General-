"""Surname-line discipline, token-boundary given names, near-miss audit rows, and the compilation report."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhulekh.config import load_config  # noqa: E402
from bhulekh.matcher import lineage_conflict, match_row, near_miss, targets_from_config  # noqa: E402
from bhulekh.report import build_report  # noqa: E402
from bhulekh.store import Store  # noqa: E402

CFG = load_config()
TARGETS = targets_from_config(CFG)
AM = "Amroha (अमरोहा)"


def _m(k, f):
    return match_row(k, f, TARGETS)


def test_surname_line_rejects_other_families():
    # the seven Hardoi families that a fuzzy matcher accepted in an earlier sweep
    assert _m("सादिक शाह", "साबिर") is None
    assert _m("सादिक खां", "साबिर खां") is None
    assert _m("सादिक हुसैन जैदी", "साबिर हुसैन") is None
    assert _m("सादिक हुसेन", "साबिर हुसेन") is None
    assert _m("सादिक हुसैन", "साबिर खां") is None
    assert _m("सादिक अली", "साबिर खां") is None          # right name, father of another line
    assert lineage_conflict("साबिर खां", ["साबिर अली", "साबिर"]) == "खां"
    assert lineage_conflict("साबिर", ["साबिर अली"]) is None
    assert lineage_conflict("साबिर अली अंसारी", ["साबिर अली", "साबिर अली अंसारी"]) is None


def test_given_name_on_token_boundary():
    assert _m("साबिरा", "साबिर") is None                  # a woman's name caught inside 'साबिर'
    assert _m("साबिरा", "भल्लू") is None
    assert _m("साबीर अली", "भल्लू")                       # long vowel spelling still matches
    assert _m("सादिक़ अली", "साबिर अली")                   # nukta


def test_family_still_matches():
    assert _m("साबिर अली", "भल्लू").target.id == "T1"
    assert _m("साबिर", "भालू").target.id == "T1"
    assert _m("सादिक अली", "साबिर अली").target.id == "T2"
    assert _m("सादिक", "साबिर").target.id == "T2"


def test_near_miss_explains_rejection():
    nm = near_miss("सादिक अली", "रहमत अली", TARGETS)
    assert nm and nm.target.id == "T2" and "does not match" in nm.reason
    nm = near_miss("सादिक हुसैन", "साबिर खां", TARGETS)
    assert nm and "line" in nm.reason
    assert near_miss("मक्खन", "मुरली", TARGETS) is None
    assert near_miss("सादिक अली", "साबिर अली", TARGETS) is None or _m("सादिक अली", "साबिर अली")


def test_report_renders_all_sections(tmp_path):
    s = Store(str(tmp_path / "t.sqlite"))
    s.upsert_districts([AM])
    s.upsert_tehsils(AM, [AM])
    s.upsert_villages(AM, AM, ["Haryana (हरियाना) - 118073", "B (ब) - 118074"])
    s.mark_started("118073")
    ids = s.add_rows("118073", "स", "999", [
        {"khata": "906", "khatedar": "सादिक", "father": "साबिर अली", "unique_code": "1180730906000012", "area": 0.227, "raw": ""},
        {"khata": "12", "khatedar": "सादिक हुसैन", "father": "साबिर खां", "unique_code": "1180730012000012", "area": 0.5, "raw": ""},
    ])
    s.add_hit(ids[0], "T2", 100, 100, 100, "probable", "खातेदार matches; पिता matches")
    s.add_hit(ids[1], "T2", 95, 0, 95, "near_miss", "पिता is of the 'खां' line")
    s.mark_done("118073")
    md, html_path = build_report(s, str(tmp_path / "out"), cfg=CFG)
    md_text = Path(md).read_text(encoding="utf-8")
    html_text = Path(html_path).read_text(encoding="utf-8")
    for needle in ("The bottom line", "Where we searched", "Ruled out", "Reasoning per hit", "What to confirm next"):
        assert needle in md_text and needle in html_text
    assert "0.227" in html_text and "सादिक हुसैन" in html_text and "PROBABLE" in html_text
    assert "verdict-row" in html_text
