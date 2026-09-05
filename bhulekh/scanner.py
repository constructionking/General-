"""Worker pool that scans villages: pin village → name search per prefix → store rows → match → hits."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn, TimeRemainingColumn

from .browser import CURRENT_FASLI, Portal, PortalDialog, PortalError, PortalServerError, Tab
from .catalog import build_catalog, ensure_districts
from .matcher import Target, all_prefixes, categorise, match_row, near_miss, targets_from_config
from .ratectl import RateController
from .store import Store, Village

console = Console()
FAMILY_TARGETS = ("T1", "T2")
# Playwright's wording when the browser/driver is gone (Ctrl-C reached it first, or it crashed): every
# further call fails instantly, so this is a reason to stop the run, not a failure of the village
_DRIVER_DEAD = ("Connection closed", "has been closed", "Target closed", "browser has been closed",
                "Browser closed", "Playwright connection closed")


def driver_dead(exc: BaseException) -> bool:
    return any(s in str(exc) for s in _DRIVER_DEAD)


class Scanner:
    def __init__(self, cfg: dict, store: Store, districts: Optional[list[str]], limit: Optional[int],
                 headless: bool = True, old_fasli: Optional[bool] = None, max_tabs: Optional[int] = None,
                 capture: bool = True, start_tabs: Optional[int] = None, affinity: bool = True):
        self.cfg = cfg
        self.capture = capture
        self.affinity = affinity
        self.store = store
        self.districts = districts
        self.limit = limit
        self.headless = headless
        self.old_fasli = cfg["old_fasli"] if old_fasli is None else old_fasli
        cc = cfg["concurrency"]
        self.rate = RateController(start_tabs or cc["start"], max_tabs or cc["max"], cc["min"], cc["ramp_every_s"], cc["backoff_factor"])
        self.village_timeout = cc["village_timeout_s"]
        self.retries = cc["retries"]
        self.targets: list[Target] = targets_from_config(cfg)
        self.prefixes = all_prefixes(self.targets)
        self.skip = set(store.get_meta("skip_districts", []) or [])
        self.adaptive = store.get_meta("adaptive", {"boost_neighbours_on_hit": True}) or {}
        self.claimed: set[str] = set()
        self.buffer: list[Village] = []
        self.done_count = 0
        self.attempted = 0          # every village attempt, success or error (this is what --limit caps)
        self.hit_count = 0
        self.active = 0
        self.stop = False
        self.max_rows_seen = 0
        self._lock: Optional[asyncio.Lock] = None   # created inside the running loop (py3.9)

    # ---- queue ---------------------------------------------------------
    async def claim_next(self, tab: Optional[Tab] = None) -> Optional[Village]:
        async with self._lock:
            if self.limit is not None and self.attempted + len(self.claimed) >= self.limit:
                return None
            # tehsil affinity: keep a tab on the tehsil it is already on (avoids district/tehsil reloads)
            if self.affinity and tab is not None and tab.district and tab.tehsil:
                for v in self.buffer:
                    if v.district == tab.district and v.tehsil == tab.tehsil:
                        self.buffer.remove(v)
                        self.claimed.add(v.code)
                        return v
                cand = self.store.next_pending(self.districts, 5, self.retries, district=tab.district, tehsil=tab.tehsil)
                for v in cand:
                    if v.code not in self.claimed and v.district not in self.skip:
                        self.claimed.add(v.code)
                        return v
            if not self.buffer:
                cand = self.store.next_pending(self.districts, 40, self.retries)
                self.buffer = [v for v in cand if v.code not in self.claimed and v.district not in self.skip]
            if not self.buffer:
                return None
            v = self.buffer.pop(0)
            self.claimed.add(v.code)
            return v

    # ---- one village ---------------------------------------------------
    async def scan_village(self, tab: Tab, v: Village) -> int:
        await tab.refresh_if_stale()
        try:
            return await self._scan_once(tab, v)
        except PortalServerError as e:
            # a 5xx, or an empty result with no dialog (dead session): the token is no longer accepted.
            # Reload the page for a fresh token and do the village once more before it counts as an error.
            self.store.event("retry", f"{v.code} {e}")
            await tab.open_search()
            return await self._scan_once(tab, v)

    async def _scan_once(self, tab: Tab, v: Village) -> int:
        await tab.set_location(v.district, v.tehsil, v.label, v.code)
        faslis = [CURRENT_FASLI]
        if self.old_fasli:
            faslis += [f for f in await tab.fasli_options() if f != CURRENT_FASLI]
        hits = 0
        for fasli in faslis:
            await tab.set_fasli(fasli)
            for prefix in self.prefixes:
                rows = await tab.search_name_complete(prefix, big=self.cfg.get("expand_above_rows", 1500))
                self.max_rows_seen = max(self.max_rows_seen, len(rows))
                if tab.timings.get("expanded"):
                    self.store.event("expand", f"{v.code} {prefix} {len(rows)} rows")
                ids = self.store.add_rows(v.code, prefix, fasli, (r.as_dict() for r in rows))
                for r, rid in zip(rows, ids):
                    m = match_row(r.khatedar, r.father, self.targets)
                    if not m:
                        nm = near_miss(r.khatedar, r.father, self.targets)
                        if nm:   # right name, wrong father: kept for the "ruled out" audit table
                            self.store.add_hit(rid, nm.target.id, nm.name_score, nm.father_score, nm.name_score,
                                               "near_miss", nm.reason)
                        continue
                    self.store.add_hit(rid, m.target.id, m.name_score, m.father_score, m.score, "pending", "")
                    hits += 1
                    self._categorise(rid, m, v)
                    console.print(f"[bold green]HIT[/bold green] {m.target.id} {v.district} › {v.tehsil} › {v.label} : "
                                  f"{r.khata} | {r.khatedar} | {r.father} | {r.area} हे०")
        return hits

    def _categorise(self, rid: int, m, v: Village):
        fam = self.store.family_hits_in_tehsil(v.district, v.tehsil, FAMILY_TARGETS)
        sib = self.store.pair_hits_in_village(v.code, m.target.id)
        cat, why = categorise(m, self.store_district_en(v.district), sib, fam)
        self.store.add_hit(rid, m.target.id, m.name_score, m.father_score, m.score, cat, why)
        if cat == "probable" and self.adaptive.get("boost_neighbours_on_hit", True):
            self.store.boost(v.district, v.tehsil, -2_000_000)
            self.store.boost(v.district, None, -1_000_000)
            self.buffer.clear()  # re-read queue with new priorities

    def store_district_en(self, label: str) -> str:
        from .store import split_label
        return split_label(label)[0]

    # ---- workers -------------------------------------------------------
    async def worker(self, i: int, portal: Portal, progress: Progress, task_id):
        tab: Optional[Tab] = None
        try:
            while not self.stop:
                if not self.rate.allows(self.active):
                    await asyncio.sleep(1.0)
                    continue
                v = await self.claim_next(tab)
                if v is None:
                    return
                self.active += 1
                t0 = time.time()
                try:
                    if tab is None:
                        tab = await portal.new_tab()   # page loads are gated inside Portal/Tab.open_search
                    self.store.mark_started(v.code)
                    hits = await asyncio.wait_for(self.scan_village(tab, v), timeout=self.village_timeout)
                    self.store.mark_done(v.code)
                    self.store.event("timing", f"{v.code} total={time.time()-t0:.2f} " +
                                     " ".join(f"{k}={val}" for k, val in tab.timings.items()))
                    self.rate.record_success()
                    self.done_count += 1
                    self.hit_count += hits
                    progress.update(task_id, advance=1)   # only finished villages move the bar
                except PortalDialog as e:
                    # the portal has no khatauni for this village (e.g. under chakbandi): record, don't retry
                    self.store.mark_skipped(v.code, str(e))
                    self.store.event("skipped", f"{v.code} {e}")
                    self.rate.record_success()
                    self.done_count += 1
                    progress.update(task_id, advance=1)
                    progress.console.print(f"[cyan]{v.district} › {v.label}: skipped — {e}[/cyan]")
                except (PortalError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
                    if driver_dead(e):
                        # the browser is gone: hand the attempt back and stop every worker; re-run resumes
                        self.store.unmark_started(v.code)
                        if not self.stop:
                            self.stop = True
                            self.store.event("stop", f"browser driver closed at {v.code}: {str(e)[:100]}")
                            progress.console.print("[red]browser driver closed — stopping; re-run the same command to resume[/red]")
                        return
                    msg = f"{type(e).__name__}: {str(e)[:160]}"
                    self.store.mark_error(v.code, msg)
                    self.store.event("error", f"{v.code} {msg}")
                    self.rate.record_error()
                    progress.console.print(f"[yellow]{v.district} › {v.label}: {msg}[/yellow]")
                    if tab is not None:
                        await tab.close()
                        tab = None
                    await asyncio.sleep(2.0)
                finally:
                    self.active -= 1
                    self.attempted += 1
                    self.claimed.discard(v.code)
                progress.update(task_id, description=f"tabs {self.active}/{self.rate.target} · hits {self.hit_count} · "
                                                     f"errors {self.rate.total_errors} · {time.time()-t0:.1f}s/village")
        finally:
            if tab is not None:
                await tab.close()

    async def run(self):
        self._lock = asyncio.Lock()
        if not self.store.districts():
            async with Portal(self.cfg["portal_url"], headless=self.headless) as portal:
                await ensure_districts(self.store, portal)
        # make sure the catalog covers the requested districts
        need = [d for d in (self.districts or self.store.districts()) if not self.store.catalog_done(d)]
        if need:
            await build_catalog(self.cfg, self.store, need, tabs=min(6, len(need)), headless=self.headless)
        pending = len(self.store.next_pending(self.districts, 10**9, self.retries))
        total = min(pending, self.limit) if self.limit else pending
        if total == 0:
            console.print("[green]nothing pending for the selected districts[/green]")
            return
        console.print(f"scanning {total} village(s), prefixes {self.prefixes}, max tabs {self.rate.max}")
        self.store.event("scan_start", f"{total} villages")
        t0 = time.time()
        async with Portal(self.cfg["portal_url"], headless=self.headless, capture=self.capture) as portal:
            with Progress(TextColumn("[progress.description]{task.description}"), BarColumn(),
                          TextColumn("{task.completed}/{task.total}"), TimeElapsedColumn(),
                          TimeRemainingColumn(), console=console) as progress:
                task_id = progress.add_task("starting…", total=total)
                workers = [asyncio.create_task(self.worker(i, portal, progress, task_id)) for i in range(self.rate.max)]
                try:
                    await asyncio.gather(*workers)
                except (KeyboardInterrupt, asyncio.CancelledError):
                    self.stop = True
                    raise
        dt = time.time() - t0
        self.store.event("scan_end", f"{self.done_count} villages in {dt:.0f}s, {self.hit_count} hits")
        console.print(f"[green]done: {self.done_count} villages in {dt/60:.1f} min "
                      f"({self.done_count/max(dt,1):.2f} villages/s), {self.hit_count} hits, "
                      f"max rows in one search {self.max_rows_seen}, errors {self.rate.total_errors}[/green]")
