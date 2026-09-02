"""Smoke test: compare render mode vs capture (fast) mode on the same villages; time each step."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bhulekh.browser import Portal  # noqa: E402

AM = "Amroha (अमरोहा)"
V1 = ("Akbarpur Sakinya (अकबरपुर सकैनिया) - 117944", "117944")
V2 = ("Pattiraipur Kalan (पटटी रायपुर कलाँ) - 117925", "117925")     # whitespace-mismatch case


async def run(capture: bool):
    t0 = time.time()
    out = {}
    async with Portal("https://upbhulekh.gov.in/", headless=True, capture=capture) as portal:
        tab = await portal.new_tab()
        await tab.set_district(AM)
        await tab.set_tehsil(AM)
        for label, code in (V1, V2):
            t1 = time.time()
            await tab.set_village(label, code)
            ts = time.time() - t1
            for prefix in ["स", "वि", "क्ष"]:
                t2 = time.time()
                rows = await tab.search_name(prefix)
                out[(code, prefix)] = sorted((r.unique_code, r.khatedar, r.father, r.khata) for r in rows)
                print(f"  {'fast' if capture else 'render'} {code} {prefix!r}: {len(rows)} rows, switch {ts:.1f}s, search {time.time()-t2:.1f}s")
                ts = 0.0
        big = [o for o in await tab.ng_options("villageSelect") if o.endswith("117939")][0]
        t1 = time.time()
        await tab.set_village(big, "117939")
        rows = await tab.search_name("स")
        out[("117939", "स")] = sorted((r.unique_code, r.khatedar, r.father, r.khata) for r in rows)
        print(f"  {'fast' if capture else 'render'} 117939 'स': {len(rows)} rows in {time.time()-t1:.1f}s (switch+search)")
    print(f"{'fast' if capture else 'render'} total {time.time()-t0:.1f}s")
    return out


async def main():
    a = await run(False)
    b = await run(True)
    for k in a:
        same = a[k] == b.get(k)
        print(k, "identical" if same else f"DIFFERENT ({len(a[k])} vs {len(b.get(k, []))})")
        if not same:
            print("   render-only:", [x for x in a[k] if x not in b.get(k, [])][:3])
            print("   fast-only:", [x for x in b.get(k, []) if x not in a[k]][:3])


asyncio.run(main())
