"""`bhulekh doctor`: end-to-end health check with one tab, one district, one village, one name search.

Prints a PASS/FAIL table with the time each stage took, so a slow link, a blocked proxy, a portal outage
and a broken selector can be told apart. Returns False (CLI exit status 1) when any stage fails.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from urllib.request import Request, urlopen

from rich.console import Console
from rich.table import Table

from .browser import Portal
from .store import village_code

console = Console()


class _Stop(Exception):
    """A stage failed; later stages depend on it."""


@dataclass
class Step:
    name: str
    ok: bool
    seconds: float
    detail: str = ""


@dataclass
class Doctor:
    portal_url: str
    district: Optional[str] = None
    prefix: str = "स"
    steps: list[Step] = field(default_factory=list)

    async def stage(self, name: str, fn: Callable[[], Awaitable[Any]],
                    describe: Optional[Callable[[Any], str]] = None) -> Any:
        t = time.time()
        try:
            result = await fn()
        except Exception as e:  # noqa: BLE001 — every failure becomes a FAIL row
            self.steps.append(Step(name, False, time.time() - t,
                                   f"{type(e).__name__}: {str(e)[:120]}".replace("\n", " ")))
            raise _Stop(name) from e
        detail = describe(result) if describe else (result if isinstance(result, str) else "")
        self.steps.append(Step(name, True, time.time() - t, detail))
        return result

    async def run(self) -> bool:
        base = self.portal_url.rstrip("/") + "/"

        def http_get() -> str:
            req = Request(base, headers={"User-Agent": "bhulekh-doctor/0.1"})
            with urlopen(req, timeout=120) as r:   # noqa: S310 — the configured https portal URL
                n = len(r.read())
            return f"HTTP {r.status}, {n/1e3:.0f} kB"

        try:
            await self.stage("portal reachable (GET /)", lambda: asyncio.to_thread(http_get))
            async with Portal(base, headless=True, capture=True) as portal:
                tab = await self.stage("browser launched, search page ready (bundle, api/edata, JWT)",
                                       portal.new_tab, lambda _: f"{len(portal._assets)} assets cached")
                labels = await self.stage("district list", lambda: tab.ng_options("districtSelect"),
                                          lambda l: f"{len(l)} districts")
                if self.district:
                    matches = [l for l in labels if self.district.lower() in l.lower()]
                    if not matches:
                        self.steps.append(Step(f"district '{self.district}' present", False, 0.0,
                                               "not in the portal's list"))
                        raise _Stop("district")
                    district = matches[0]
                else:
                    district = labels[0]
                await self.stage(f"select district {district}", lambda: tab.set_district(district))
                tehsils = await self.stage("tehsil list", lambda: tab.ng_options("tehsilSelect"),
                                           lambda l: f"{len(l)} tehsils")
                await self.stage(f"select tehsil {tehsils[0]}", lambda: tab.set_tehsil(tehsils[0]))
                villages = await self.stage("village list", lambda: tab.ng_options("villageSelect"),
                                            lambda l: f"{len(l)} villages")
                vlabel = villages[0]
                await self.stage(f"select village {vlabel}",
                                 lambda: tab.set_village(vlabel, village_code(vlabel) or ""))
                await self.stage(f"khatedar name search, prefix {self.prefix!r} (fast tier)",
                                 lambda: tab.search_name(self.prefix), lambda rows: f"{len(rows)} rows")
                await tab.close()
        except _Stop:
            pass
        return all(s.ok for s in self.steps)

    def print(self):
        tbl = Table("stage", "result", "seconds", "detail")
        for s in self.steps:
            tbl.add_row(s.name, "[green]PASS[/green]" if s.ok else "[red]FAIL[/red]", f"{s.seconds:.1f}",
                        s.detail if len(s.detail) <= 100 else s.detail[:97] + "…")
        console.print(tbl)
        ok = all(s.ok for s in self.steps)
        console.print(f"{'[green]all stages passed[/green]' if ok else '[red]FAILED[/red]'} · "
                      f"{sum(s.seconds for s in self.steps):.0f}s total")


def run_doctor(cfg: dict, district: Optional[str], prefix: str) -> bool:
    d = Doctor(cfg["portal_url"], district, prefix)
    ok = asyncio.run(d.run())
    d.print()
    return ok
