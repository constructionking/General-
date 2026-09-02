"""Where does CPU go? Time (wall + process CPU incl. chromium children) for switch-only vs search-only."""
from __future__ import annotations

import asyncio
import os
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bhulekh.browser import Portal  # noqa: E402

AM = "Amroha (अमरोहा)"


def cpu_children() -> float:
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


async def main():
    async with Portal("https://upbhulekh.gov.in/", headless=True, capture=True) as portal:
        tab = await portal.new_tab()
        await tab.set_district(AM)
        await tab.set_tehsil(AM)
        villages = await tab.ng_options("villageSelect")
        small = villages[:8]
        # warm up
        await tab.set_village(small[0], small[0].rsplit("-", 1)[-1].strip())

        # (a) switch only
        c0, w0 = cpu_children(), time.time()
        for lb in small[1:7]:
            await tab.set_village(lb, lb.rsplit("-", 1)[-1].strip())
        print(f"switch x6: wall {time.time()-w0:.1f}s, cpu {cpu_children()-c0:.1f}s")

        # (b) search only (same village), small result
        c0, w0 = cpu_children(), time.time()
        for _ in range(6):
            rows = await tab.search_name("वि")
        print(f"search 'वि' x6 ({len(rows)} rows): wall {time.time()-w0:.1f}s, cpu {cpu_children()-c0:.1f}s")

        # (c) type only (keyboard clicks + waits), no request
        c0, w0 = cpu_children(), time.time()
        for _ in range(6):
            await tab.type_prefix("वि")
        print(f"type x6: wall {time.time()-w0:.1f}s, cpu {cpu_children()-c0:.1f}s")

        # (d) fasli/landtype re-render cost: select_option toggling
        c0, w0 = cpu_children(), time.time()
        for _ in range(3):
            await tab.set_fasli("1422-1427")
            await tab.set_fasli("999")
        print(f"fasli toggle x6: wall {time.time()-w0:.1f}s, cpu {cpu_children()-c0:.1f}s")

        # (e) big village search
        big = [o for o in villages if o.endswith("117939")][0]
        await tab.set_village(big, "117939")
        c0, w0 = cpu_children(), time.time()
        rows = await tab.search_name("स")
        print(f"search 'स' big ({len(rows)} rows): wall {time.time()-w0:.1f}s, cpu {cpu_children()-c0:.1f}s")


asyncio.run(main())
