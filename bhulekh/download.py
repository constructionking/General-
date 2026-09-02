"""Khatauni extract downloader — human-in-the-loop.

For every hit without an extract: open a VISIBLE browser, pin the village, run the name search,
tick the exact khata, press उद्धरण देखें, then wait for the user to type the CAPTCHA.
The tool never reads or solves the CAPTCHA. When the extract renders, it is saved as PNG + HTML + PDF.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Optional

from PIL import Image
from rich.console import Console

from .browser import Portal, PortalError, Tab
from .store import Store, split_label

console = Console()
BANNER_JS = """(msg) => { let b = document.getElementById('bhulekh-banner');
  if (!b) { b = document.createElement('div'); b.id = 'bhulekh-banner';
    b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;background:#b30000;color:#fff;font:bold 18px sans-serif;padding:10px;text-align:center';
    document.body.appendChild(b); }
  b.textContent = msg; }"""


def _safe(s: str) -> str:
    return re.sub(r"[^\wऀ-ॿ.-]+", "_", s).strip("_")[:80]


async def _wait_for_extract(tab: Tab, portal: Portal, timeout_s: float):
    """Wait until the user has passed the CAPTCHA and an extract is visible. Returns the page holding it."""
    p = tab.page
    start_url = p.url
    new_pages: list = []
    portal.context.on("page", lambda pg: new_pages.append(pg))
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if new_pages:
            pg = new_pages[-1]
            try:
                await pg.wait_for_load_state("networkidle", timeout=30000)
            except Exception:  # noqa: BLE001
                pass
            return pg
        if p.url != start_url:
            try:
                await p.wait_for_load_state("networkidle", timeout=30000)
            except Exception:  # noqa: BLE001
                pass
            return p
        # same-page render: look for a khatauni table (many portals render it inline)
        found = await p.evaluate(
            "() => !!([...document.querySelectorAll('table')].find(t => /खतौनी|खाता संख्या|गाटा/.test(t.innerText) && t.rows.length > 3))")
        if found and not await p.evaluate("() => !!document.querySelector('.contact-page input.input')"):
            return p
        await asyncio.sleep(0.5)
    raise PortalError("timed out waiting for the CAPTCHA / extract")


async def _save(page, base: str) -> tuple[str, str, str]:
    png = base + ".png"
    html = base + ".html"
    pdf = base + ".pdf"
    await page.screenshot(path=png, full_page=True)
    with open(html, "w", encoding="utf-8") as f:
        f.write(await page.content())
    try:
        await page.pdf(path=pdf, format="A4", print_background=True)   # works only in headless chromium
    except Exception:  # noqa: BLE001
        img = Image.open(png).convert("RGB")
        img.save(pdf, "PDF", resolution=110)
    return pdf, png, html


async def download_extracts(cfg: dict, store: Store, only_probable: bool = False, old_fasli: bool = False,
                            wait_s: float = 300.0, limit: Optional[int] = None):
    hits = store.hits("probable") if only_probable else store.hits()
    todo = [h for h in hits if not h["pdf_path"]]
    if limit:
        todo = todo[:limit]
    if not todo:
        console.print("[green]every hit already has an extract[/green]")
        return
    console.print(f"[bold]{len(todo)} extract(s) to download. A browser window will open; for each one type the "
                  f"CAPTCHA the portal shows and press Submit. The tool saves the extract automatically.[/bold]")
    root = cfg["extracts_dir"]
    async with Portal(cfg["portal_url"], headless=False) as portal:
        tab = await portal.new_tab()
        await tab.page.bring_to_front()
        for n, h in enumerate(todo, 1):
            d_en, t_en = split_label(h["district"])[0], split_label(h["tehsil"])[0]
            base_dir = os.path.join(root, _safe(d_en), _safe(t_en))
            os.makedirs(base_dir, exist_ok=True)
            faslis = [h["fasli"]]
            if old_fasli:
                faslis.append("__old__")
            for fasli in faslis:
                try:
                    await tab.set_location(h["district"], h["tehsil"], h["village_label"], h["village_code"])
                    if fasli == "__old__":
                        opts = [o for o in await tab.fasli_options() if o != "999"]
                        if not opts:
                            continue
                        fasli = opts[0]
                    await tab.set_fasli(fasli)
                    rows = await tab.search_name(h["prefix"])
                    if not await tab.select_result(h["unique_code"]):
                        # older band: the code may differ; match on khatedar+father text instead
                        ok = await tab.page.evaluate(
                            """(k) => { const pg=document.querySelector('.contact-page');
                                const r=[...pg.querySelectorAll('input[type=radio]')].find(r=>r.name!=='nav' && (r.parentElement.innerText||'').includes(k));
                                if(!r) return false; r.click(); return true; }""", h["khatedar"])
                        if not ok:
                            console.print(f"[yellow]{n}/{len(todo)} {h['khatedar']} not in the {fasli} list ({len(rows)} rows); skipped[/yellow]")
                            continue
                    await tab.page.click(".contact-page button.btn-danger")
                    msg = (f"{n}/{len(todo)} — {h['khatedar']} / {h['father']} — खाता {h['khata']} — "
                           f"कृपया CAPTCHA भरें और Submit दबाएँ (type the CAPTCHA and press Submit)")
                    await tab.page.evaluate(BANNER_JS, msg)
                    console.print(f"[cyan]{msg}[/cyan]")
                    page = await _wait_for_extract(tab, portal, wait_s)
                    base = os.path.join(base_dir, _safe(f"{h['village_en'] or h['village_code']}_{h['village_code']}_khata{h['khata']}_{fasli if fasli!='999' else 'current'}"))
                    pdf, png, html = await _save(page, base)
                    store.add_extract(h["id"], "999" if fasli == "999" else fasli, pdf, png, html)
                    console.print(f"[green]saved {pdf}[/green]")
                    if page is not tab.page:
                        await page.close()
                        await tab.page.bring_to_front()
                    else:
                        await tab.open_search()
                except PortalError as e:
                    console.print(f"[red]{n}/{len(todo)}: {e}[/red]")
                    await tab.close()
                    tab = await portal.new_tab()
        console.print("[green]download run finished[/green]")
