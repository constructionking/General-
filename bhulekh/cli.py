"""bhulekh command line."""
from __future__ import annotations

import asyncio
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table

from .config import load_config
from .store import Store, split_label

app = typer.Typer(add_completion=False, help="UP Bhulekh district-wide khatedar finder")
console = Console()
_cfg_path: Optional[str] = None


def _ctx():
    cfg = load_config(_cfg_path)
    return cfg, Store(cfg["db_path"])


@app.callback()
def main(config: Optional[str] = typer.Option(None, "--config", "-c", help="path to config.yaml")):
    global _cfg_path
    _cfg_path = config


@app.command()
def catalog(district: List[str] = typer.Option(None, "--district", "-d", help="district name(s); default all 75"),
            tabs: int = typer.Option(4, help="parallel tabs"), force: bool = typer.Option(False, help="rebuild even if cached"),
            headed: bool = typer.Option(False, help="show the browser")):
    """Fetch district → tehsil → village lists into the local database."""
    from .catalog import build_catalog, ensure_districts, resolve_districts
    from .browser import Portal
    cfg, store = _ctx()

    async def go():
        async with Portal(cfg["portal_url"], headless=not headed) as portal:
            await ensure_districts(store, portal)
        ds = resolve_districts(store, district) if district else None
        await build_catalog(cfg, store, ds, tabs=tabs, headless=not headed, force=force)
    asyncio.run(go())


@app.command()
def scan(district: List[str] = typer.Option(None, "--district", "-d", help="district name(s), e.g. -d Amroha -d Lucknow"),
         all_districts: bool = typer.Option(False, "--all", help="scan every district in the catalog"),
         limit: Optional[int] = typer.Option(None, help="stop after this many villages (testing)"),
         strategy: Optional[str] = typer.Option(None, help="strategy.yaml to (re)apply before scanning"),
         max_tabs: Optional[int] = typer.Option(None, help="override concurrency.max"),
         start_tabs: Optional[int] = typer.Option(None, help="override concurrency.start"),
         old_fasli: Optional[bool] = typer.Option(None, help="also search the older fasli band"),
         reset_errors: bool = typer.Option(False, help="retry villages that errored out earlier"),
         fast: bool = typer.Option(True, "--fast/--render", help="fast tier reads the decrypted list in-page instead of rendering rows"),
         affinity: bool = typer.Option(True, "--tehsil-affinity/--no-tehsil-affinity", help="keep each tab on one tehsil"),
         headed: bool = typer.Option(False, help="show the browser")):
    """Scan villages for the configured targets. Resumable; re-run to continue."""
    from .catalog import resolve_districts
    from .scanner import Scanner
    from .strategy import apply_strategy, load_strategy
    cfg, store = _ctx()
    if strategy:
        s = apply_strategy(store, load_strategy(strategy))
        console.print(f"strategy applied: {s}")
    if not district and not all_districts:
        raise typer.BadParameter("give --district NAME (repeatable) or --all")
    ds = None
    if district:
        if not store.districts():
            from .browser import Portal
            from .catalog import ensure_districts

            async def _d():
                async with Portal(cfg["portal_url"], headless=True) as portal:
                    await ensure_districts(store, portal)
            asyncio.run(_d())
        ds = resolve_districts(store, district)
    if reset_errors:
        store.reset_errors(ds)
    sc = Scanner(cfg, store, ds, limit, headless=not headed, old_fasli=old_fasli, max_tabs=max_tabs, capture=fast,
                 start_tabs=start_tabs, affinity=affinity)
    try:
        asyncio.run(sc.run())
    except KeyboardInterrupt:
        console.print("[yellow]interrupted — progress is saved; re-run the same command to resume[/yellow]")


@app.command()
def strategy(file: Optional[str] = typer.Option(None, "--file", "-f", help="strategy.yaml"),
             text: Optional[str] = typer.Option(None, "--text", "-t", help="plain-language description to record"),
             example: bool = typer.Option(False, help="print an example strategy.yaml")):
    """Apply a search-priority hierarchy (district/tehsil/village order, skips, adaptive boosting)."""
    from .strategy import EXAMPLE, apply_strategy, load_strategy
    if example:
        print(EXAMPLE)
        return
    cfg, store = _ctx()
    if not file:
        raise typer.BadParameter("--file strategy.yaml (or --example)")
    if not store.village_count():
        console.print("[yellow]catalog is empty — run `bhulekh catalog` first so villages can be ordered[/yellow]")
    s = apply_strategy(store, load_strategy(file), text)
    console.print(s)


@app.command()
def report(live: bool = typer.Option(False, help="mid-scan snapshot with coverage + ETA")):
    """Write summary.md / summary.html (Probable vs Less probable, reasoning, clusters, coverage)."""
    from .report import build_report
    cfg, store = _ctx()
    md, html = build_report(store, cfg["output_dir"], live=live, cfg=cfg)
    console.print(f"[green]report written:[/green] {md}\n                {html}")


@app.command()
def download(so_far: bool = typer.Option(True, "--so-far/--all-hits", help="only hits without an extract yet"),
             only_probable: bool = typer.Option(False, help="skip less-probable hits"),
             old_fasli: bool = typer.Option(False, help="also fetch the older fasli band extract"),
             wait: float = typer.Option(300.0, help="seconds to wait for you to type each CAPTCHA"),
             limit: Optional[int] = typer.Option(None)):
    """Open a visible browser and save the khatauni extract of every hit (you type the CAPTCHA)."""
    from .download import download_extracts
    cfg, store = _ctx()
    asyncio.run(download_extracts(cfg, store, only_probable=only_probable, old_fasli=old_fasli, wait_s=wait, limit=limit))


@app.command()
def export(district: List[str] = typer.Option(None, "--district", "-d", help="also write per-district workbooks")):
    """Write output/hits.xlsx (+ hits.csv) and optional per-district workbooks with every raw row."""
    from .catalog import resolve_districts
    from .export import export_district, export_hits
    cfg, store = _ctx()
    p = export_hits(store, cfg["output_dir"])
    console.print(f"[green]{p}[/green]")
    for d in (resolve_districts(store, district) if district else []):
        console.print(f"[green]{export_district(store, d, cfg['output_dir'])}[/green]")


@app.command()
def status():
    """One-screen snapshot: coverage, hits by category, extracts, recent rate."""
    cfg, store = _ctx()
    t = store.totals()
    rate, err = store.recent_rate()
    console.print(f"villages {t['done']}/{t['villages']} scanned, {t['errors']} errors, {t['skipped']} skipped (no khatauni on portal) · "
                  f"rows {t['rows']} · "
                  f"hits: [green]{t['probable']} probable[/green], [yellow]{t['less_probable']} less probable[/yellow], "
                  f"{t['near_miss']} near-misses · "
                  f"extracts {t['extracts']} · last 2 min: {rate*60:.0f} villages/min, {err*60:.1f} errors/min")
    ts = store.timing_summary()
    if ts["villages"]:
        steps = ", ".join(f"{k} {v[0]}s" for k, v in sorted(ts["steps"].items()))
        console.print(f"last 30 min: {ts['villages']} villages · median per step: {steps}")
    if ts["errors"]:
        console.print("errors (30 min): " + ", ".join(f"{k} ×{n}" for k, n in ts["errors"].items()))
    tbl = Table("district", "villages", "done", "errors", "skipped", "pending", "%")
    for r in store.coverage():
        if (r["done"] or 0) + (r["errors"] or 0) + (r["skipped"] or 0) == 0:
            continue
        tbl.add_row(split_label(r["district"])[0], str(r["total"]), str(r["done"] or 0), str(r["errors"] or 0),
                    str(r["skipped"] or 0), str(r["pending"] or 0),
                    f"{100.0*((r['done'] or 0) + (r['skipped'] or 0))/max(r['total'],1):.0f}")
    console.print(tbl)
    for h in [x for x in store.hits() if x["category"] != "near_miss"][:20]:
        console.print(f"  [{'green' if h['category']=='probable' else 'yellow'}]{h['category']}[/] {h['target']} "
                      f"{split_label(h['district'])[0]} › {h['village_label']} · {h['khata']} · {h['khatedar']} / {h['father']}")


@app.command()
def doctor(district: Optional[str] = typer.Option(None, "--district", "-d", help="district to test (default: first in the list)"),
           prefix: str = typer.Option("स", help="on-screen-keyboard prefix for the test search")):
    """End-to-end health check: portal, browser, page readiness, district/tehsil/village, one name search."""
    from .doctor import run_doctor
    cfg, _ = _ctx()
    raise typer.Exit(code=0 if run_doctor(cfg, district, prefix) else 1)


@app.command()
def rematch():
    """Re-score every stored khatedar row against the targets in config.yaml (no rescan needed)."""
    from .matcher import categorise, match_row, near_miss, targets_from_config
    from .scanner import FAMILY_TARGETS
    from collections import Counter
    cfg, store = _ctx()
    targets = targets_from_config(cfg)
    matched, misses = [], []
    for r in store.all_rows():
        m = match_row(r["khatedar"], r["father"], targets)
        if m:
            matched.append((r, m))
        else:
            nm = near_miss(r["khatedar"], r["father"], targets)
            if nm:
                misses.append((r["id"], nm.target.id, nm.name_score, nm.father_score, nm.name_score, "near_miss", nm.reason))
    # cluster signals computed in memory (no DB reads between the writes)
    fam = Counter((r["district"], r["tehsil"]) for r, m in matched if m.target.id in FAMILY_TARGETS)
    sib = Counter((r["village_code"], m.target.id) for r, m in matched)
    hits = []
    for r, m in matched:
        cat, why = categorise(m, split_label(r["district"])[0], sib[(r["village_code"], m.target.id)],
                              fam[(r["district"], r["tehsil"])])
        hits.append((r["id"], m.target.id, m.name_score, m.father_score, m.score, cat, why))
    store.replace_hits(hits + misses)
    t = store.totals()
    console.print(f"[green]rematched: {t['probable']} probable, {t['less_probable']} less probable, "
                  f"{t['near_miss']} near-misses (right name, wrong father)[/green]")


@app.command("reset-errors")
def reset_errors_cmd(district: List[str] = typer.Option(None, "--district", "-d"),
                     include_skipped: bool = typer.Option(False, help="also re-queue villages the portal reported as having no khatauni")):
    """Put errored villages back in the queue."""
    from .catalog import resolve_districts
    cfg, store = _ctx()
    store.reset_errors(resolve_districts(store, district) if district else None, include_skipped=include_skipped)
    console.print("[green]errors reset[/green]")


if __name__ == "__main__":
    app()
