"""Measure idle CPU of one tab on the search screen and list the page's timers."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bhulekh.browser import Portal  # noqa: E402

TIMER_HOOK = """
(() => {
  window.__timers = [];
  const oi = window.setInterval, ot = window.setTimeout, raf = window.requestAnimationFrame;
  window.setInterval = function (fn, ms, ...a) { window.__timers.push({k: 'interval', ms, src: String(fn).slice(0, 160)}); return oi.call(window, fn, ms, ...a); };
  let toCount = 0, rafCount = 0;
  window.setTimeout = function (fn, ms, ...a) { toCount++; window.__toCount = toCount; return ot.call(window, fn, ms, ...a); };
  window.requestAnimationFrame = function (fn) { rafCount++; window.__rafCount = rafCount; return raf.call(window, fn); };
})();
"""


def cpu() -> float:
    out = subprocess.run(["ps", "-A", "-o", "%cpu,comm"], capture_output=True, text=True).stdout
    return sum(float(l.split()[0]) for l in out.splitlines()[1:] if "chrom" in l.lower())


async def main():
    async with Portal("https://upbhulekh.gov.in/", headless=True, capture=True) as portal:
        await portal.context.add_init_script(TIMER_HOOK)
        tab = await portal.new_tab()
        await asyncio.sleep(5)
        c0 = cpu()
        t0 = await tab.page.evaluate("() => ({to: window.__toCount || 0, raf: window.__rafCount || 0})")
        await asyncio.sleep(10)
        c1 = cpu()
        t1 = await tab.page.evaluate("() => ({to: window.__toCount || 0, raf: window.__rafCount || 0})")
        print(f"idle chromium CPU%: {c0:.0f} -> {c1:.0f}; setTimeout calls in 10s: {t1['to']-t0['to']}, rAF: {t1['raf']-t0['raf']}")
        timers = await tab.page.evaluate("() => window.__timers")
        for t in timers:
            print("  ", t["k"], t["ms"], t["src"].replace("\n", " ")[:160])
        # now with a village selected
        await tab.set_location("Amroha (अमरोहा)", "Amroha (अमरोहा)", "Akbarpur Sakinya (अकबरपुर सकैनिया) - 117944", "117944")
        await asyncio.sleep(3)
        c0 = cpu()
        t0 = await tab.page.evaluate("() => ({to: window.__toCount || 0, raf: window.__rafCount || 0})")
        await asyncio.sleep(10)
        c1 = cpu()
        t1 = await tab.page.evaluate("() => ({to: window.__toCount || 0, raf: window.__rafCount || 0})")
        print(f"after village select idle CPU%: {c0:.0f} -> {c1:.0f}; setTimeout calls in 10s: {t1['to']-t0['to']}, rAF: {t1['raf']-t0['raf']}")
        timers = await tab.page.evaluate("() => window.__timers")
        print("intervals now:", len(timers))


asyncio.run(main())
