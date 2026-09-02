"""Build the district → tehsil → village catalog from the portal's dropdowns (cached in SQLite)."""
from __future__ import annotations

import asyncio
from typing import Optional

from rich.console import Console

from .browser import Portal, PortalError
from .store import Store, split_label

console = Console()


def resolve_districts(store: Store, names: list[str]) -> list[str]:
    """Map user-typed names ('amroha', 'Lucknow', 'लखनऊ') to exact catalog labels."""
    labels = store.districts()
    out = []
    for n in names:
        key = n.strip().lower()
        found = None
        for lb in labels:
            en, hi = split_label(lb)
            if key in (lb.lower(), en.lower(), hi.lower()) or key == en.lower().replace(" ", ""):
                found = lb
                break
        if not found:
            cands = [lb for lb in labels if key in lb.lower()]
            if len(cands) == 1:
                found = cands[0]
        if not found:
            raise SystemExit(f"Unknown district '{n}'. Known: " + ", ".join(split_label(l)[0] for l in labels))
        out.append(found)
    return out


async def ensure_districts(store: Store, portal: Portal):
    if store.districts():
        return
    tab = await portal.new_tab()
    try:
        store.upsert_districts(await tab.ng_options("districtSelect"))
    finally:
        await tab.close()


async def build_catalog(cfg: dict, store: Store, districts: Optional[list[str]] = None,
                        tabs: int = 4, headless: bool = True, force: bool = False):
    async with Portal(cfg["portal_url"], headless=headless) as portal:
        await ensure_districts(store, portal)
        todo = districts or store.districts()
        if not force:
            todo = [d for d in todo if not store.catalog_done(d)]
        if not todo:
            console.print("[green]catalog already complete for the requested districts[/green]")
            return
        q: asyncio.Queue = asyncio.Queue()
        for d in todo:
            q.put_nowait(d)
        console.print(f"building catalog for {len(todo)} district(s) with {tabs} tab(s)…")

        async def worker(i: int):
            tab = await portal.new_tab()
            try:
                while True:
                    try:
                        d = q.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    for attempt in range(3):
                        try:
                            await tab.set_district(d)
                            tehsils = await tab.ng_options("tehsilSelect")
                            store.upsert_tehsils(d, tehsils)
                            n = 0
                            for t in tehsils:
                                await tab.set_tehsil(t)
                                villages = await tab.ng_options("villageSelect")
                                store.upsert_villages(d, t, villages)
                                n += len(villages)
                            console.print(f"  [cyan]{d}[/cyan]: {len(tehsils)} tehsils, {n} villages")
                            break
                        except (PortalError, Exception) as e:  # noqa: BLE001
                            console.print(f"  [yellow]{d}: retry {attempt+1} ({e})[/yellow]")
                            await tab.close()
                            tab = await portal.new_tab()
            finally:
                await tab.close()

        await asyncio.gather(*(worker(i) for i in range(min(tabs, len(todo)))))
    console.print(f"[green]catalog: {store.village_count()} villages total[/green]")
