"""User-defined search priority hierarchy → village queue order."""
from __future__ import annotations

from typing import Optional

import yaml

from .store import Store, split_label

REGIONS = {
    "west": ["Saharanpur", "Muzaffarnagar", "Shamli", "Meerut", "Baghpat", "Ghaziabad", "Hapur",
             "Gautam Buddha Nagar", "Bulandshahr", "Aligarh", "Hathras", "Mathura", "Agra", "Firozabad",
             "Mainpuri", "Etah", "Kasganj", "Bijnor", "Moradabad", "Sambhal", "Rampur", "Amroha",
             "Bareilly", "Budaun", "Pilibhit", "Shahjahanpur"],
    "central": ["Lucknow", "Unnao", "Rae Bareli", "Sitapur", "Hardoi", "Kheri", "Kanpur Nagar", "Kanpur Dehat",
                "Etawah", "Auraiya", "Farrukhabad", "Kannauj", "Ayodhya", "Ambedkar Nagar", "Bara Banki",
                "Sultanpur", "Amethi", "Fatehpur", "Kaushambi", "Pratapgarh"],
    "east": ["Prayagraj", "Varanasi", "Chandauli", "Ghazipur", "Jaunpur", "Mirzapur", "Sonbhadra", "Bhadohi",
             "Azamgarh", "Mau", "Ballia", "Gorakhpur", "Deoria", "Kushinagar", "Mahrajganj", "Basti",
             "Sant Kabir Nagar", "Siddharthnagar", "Gonda", "Balrampur", "Bahraich", "Shrawasti"],
    "bundelkhand": ["Jhansi", "Lalitpur", "Jalaun", "Hamirpur", "Mahoba", "Banda", "Chitrakoot"],
}
DISTRICT_REGION = {d.lower(): r for r, ds in REGIONS.items() for d in ds}

EXAMPLE = """# Search priority hierarchy — evaluated top-down; anything unlisted comes last in catalog order.
targets_order: [family, vijay]            # informational; both prefixes always run per village
levels:
  regions:   [west, central, east, bundelkhand]
  districts: [Amroha, Moradabad, Rampur, Bijnor, Sambhal, Lucknow]
  tehsils:   {Amroha: [Hasanpur, Amroha], Lucknow: [Sadar, Sarojini Nagar]}
  villages:  {name_contains: ["अली", "पुर"], codes_first: [117944]}
skip:        {districts: []}
adaptive:
  boost_neighbours_on_hit: true           # a Probable hit moves the rest of that tehsil, then district, to the front
time_budget_min: 0
"""


def _rank(name: str, ordered: list[str]) -> int:
    key = name.lower()
    for i, o in enumerate(ordered):
        if o.lower() == key:
            return i
    return 10_000


def load_strategy(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def apply_strategy(store: Store, strat: dict, text: Optional[str] = None) -> dict:
    """Compute an order for every village and write it as priorities. Returns a summary dict."""
    levels = strat.get("levels", {}) or {}
    regions = [r.lower() for r in levels.get("regions", []) or []]
    districts = levels.get("districts", []) or []
    tehsils = levels.get("tehsils", {}) or {}
    villages = levels.get("villages", {}) or {}
    name_contains = villages.get("name_contains", []) or []
    codes_first = [str(c) for c in villages.get("codes_first", []) or []]
    skip = strat.get("skip", {}) or {}
    skip_districts = [d for d in (skip.get("districts", []) or [])]

    rows = list(store.conn.execute("SELECT code,label,district,tehsil,name_en,name_hi,rowid FROM villages"))
    keyed = []
    skip_labels = set()
    for r in rows:
        d_en = split_label(r["district"])[0]
        t_en = split_label(r["tehsil"])[0]
        if any(d_en.lower() == s.lower() for s in skip_districts):
            skip_labels.add(r["district"])
        region = DISTRICT_REGION.get(d_en.lower(), "other")
        k = (
            0 if r["code"] in codes_first else 1,
            _rank(region, regions) if regions else 0,
            _rank(d_en, districts),
            _rank(t_en, tehsils.get(d_en, []) or tehsils.get(r["district"], []) or []),
            0 if any(s in (r["name_hi"] or "") or s.lower() in (r["name_en"] or "").lower() for s in name_contains) else 1,
            r["rowid"],
        )
        keyed.append((k, r["code"]))
    keyed.sort()
    store.set_priorities([c for _, c in keyed])
    store.set_meta("skip_districts", sorted(skip_labels))
    store.set_meta("adaptive", strat.get("adaptive", {"boost_neighbours_on_hit": True}))
    store.set_meta("strategy_yaml", yaml.safe_dump(strat, allow_unicode=True, sort_keys=False))
    if text:
        store.set_meta("strategy_text", text)
    first = [c for _, c in keyed[:5]]
    return {"villages_ordered": len(keyed), "skipped_districts": sorted(skip_labels), "first_codes": first}
