"""Worker pool that scans villages: pin village → name search per prefix → store rows → match → hits."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn, TimeRemainingColumn

from .browser import CURRENT_FASLI, Portal, PortalError, Tab
from .catalog import build_catalog, ensure_districts
from .matcher import Target, all_prefixes, categorise, match_row, targets_from_config
from .ratectl import RateController
from .store import Store, Village

console = Console()
FAMILY_TARGETS = ("T1", "T2")


class Scanner:
    def __init__(self, cfg: dict, store: Store, districts: Optional[list[str]], limit: Optional[int],
                 headless: bool = True, old_fasli: Optional[bool] = None, max_tabs: Optional[int] = None,
                 capture: bool = True, start_tabs: Optional[int] = None):
        self.cfg = cfg
        self.capture = capture
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
        self.hit_count = 0
        self.active = 0
        self.stop = False
        self.max_rows_seen = 0
        self._lock: Optional[asyncio.Lock] = None   # created inside the running loop (py3.9)

    # ---- queue ---------------------------------------------------------
    async def claim_next(self) -> Optional[Village]:
        async with self._lock:
            if self.limit is not None and self.done_count + len(self.claimed) >= self.limit:
                return None
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
        await tab.set_location(v.district, v.tehsil, v.label, v.code)
        faslis = [CURRENT_FASLI]
        if self.old_fasli:
            faslis += [f for f in await tab.fasli_options() if f != CURRENT_FASLI]
        hits = 0
        for fasli in faslis:
            await tab.set_fasli(fasli)
            for prefix in self.prefixes:
                rows = await tab.search_name_complete(prefix, big=self.cfg.get("expand_above_rows", 1000))
                self.max_rows_seen = max(self.max_rows_seen, len(rows))
                ids = self.store.add_rows(v.code, prefix, fasli, (r.as_dict() for r in rows))
                for r, rid in zip(rows, ids):
                    m = match_row(r.khatedar, r.father, self.targets)
                    if not m:
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
                v = await self.claim_next()
                if v is None:
                    return
                self.active += 1
                t0 = time.time()
                try:
                    if tab is None:
                        tab = await portal.new_tab()
                    self.store.mark_started(v.code)
                    hits = await asyncio.wait_for(self.scan_village(tab, v), timeout=self.village_timeout)
                    self.store.mark_done(v.code)
                    self.rate.record_success()
                    self.done_count += 1
                    self.hit_count += hits
                except (PortalError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
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
                    self.claimed.discard(v.code)
                progress.update(task_id, advance=1,
                                description=f"tabs {self.active}/{self.rate.target} · hits {self.hit_count} · {time.time()-t0:.1f}s/village")
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
