"""Playwright driver for upbhulekh.gov.in.

Tier 1 drives the real UI (district/tehsil/village dropdowns, on-screen keyboard, खोजें button).
Tier 2 ("capture mode") keeps the same UI flow but hooks JSON.parse inside the page so the decrypted
khatedar list is read the moment the portal's own code decrypts it — and an empty list is handed to the
Angular component so it does not render thousands of radio rows. Nothing about the server side changes;
this only reduces rendering work in our own browser.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

from playwright.async_api import Browser, BrowserContext, Page, Playwright, TimeoutError as PWTimeout, async_playwright

SEARCH_ROUTE = "#/khatauni_rtk"
CURRENT_FASLI = "999"
PAGE_LOAD_TIMEOUT_MS = 90_000    # first load pulls a ~3 MB Angular bundle; slow links need well over 20 s
OPEN_CONCURRENCY = 3             # tabs allowed to load the search page at the same time (after the first)
TAB_MAX_AGE_S = 18 * 60          # portal JWT lives 25 min; reopen the page well before that
ROW_RE = re.compile(
    r"^\s*(?P<khata>.+?)\s*:\s*(?P<khatedar>.+?)\s*:\s*(?P<father>.+?)\s*:\s*(?P<code>\d{14,18})\s*:\s*\((?P<area>[\d.]+)\s*(?:हे[०0]?|ha)?\s*\)\s*$"
)
_KEY_ALIASES = {" ": "space"}

CAPTURE_HOOK = """
(() => {
  const orig = JSON.parse;
  window.__bhu = { capture: false, rows: null, seq: 0 };
  JSON.parse = function (t, r) {
    const v = orig(t, r);
    try {
      if (window.__bhu.capture && Array.isArray(v) && v.length && v[0] && 'unique_code' in v[0] && 'father' in v[0]) {
        window.__bhu.rows = v; window.__bhu.seq++;
        return [];               // hand the component an empty list: no DOM rendering of the rows
      }
    } catch (e) {}
    return v;
  };
})();
"""


QUIET_HOOK = """
(() => {
  const css = '*,*::before,*::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}' +
              'app-loading-spinner,.spinner,.spinner-border,.loader{display:none!important}';
  const add = () => { const s = document.createElement('style'); s.id = 'bhu-quiet'; s.textContent = css;
                      (document.head || document.documentElement).appendChild(s); };
  if (document.head) add(); else document.addEventListener('DOMContentLoaded', add, {once: true});
})();
"""


class PortalError(RuntimeError):
    pass


class PortalDialog(PortalError):
    """The portal answered with a dialog instead of data, e.g. 'यह गाँव चकबंदी में है।' (village under
    consolidation — no khatauni available). A statement about the village, not a transient failure."""


@dataclass
class Row:
    khata: str
    khatedar: str
    father: str
    unique_code: str
    area: Optional[float]
    raw: str

    def as_dict(self) -> dict:
        return {"khata": self.khata, "khatedar": self.khatedar, "father": self.father,
                "unique_code": self.unique_code, "area": self.area, "raw": self.raw}


def parse_row(text: str) -> Optional[Row]:
    m = ROW_RE.match(text.replace("\n", " "))
    if not m:
        return None
    try:
        area = float(m.group("area"))
    except ValueError:
        area = None
    return Row(m.group("khata").strip(), m.group("khatedar").strip(), m.group("father").strip(),
               m.group("code"), area, text.strip())


def row_from_api(d: dict) -> Row:
    try:
        area = float(d.get("area")) if d.get("area") not in (None, "") else None
    except ValueError:
        area = None
    khata = (d.get("khasra_no") or d.get("khata_number") or "").strip()
    name, father = (d.get("name") or "").strip(), (d.get("father") or "").strip()
    return Row(khata, name, father, str(d.get("unique_code") or ""), area,
               f"{khata} : {name} : {father} : {d.get('unique_code')} : ({d.get('area')} हे०)")


def _env_proxy() -> Optional[dict]:
    """Chromium does not read HTTPS_PROXY on its own; pass it through Playwright's proxy option."""
    server = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not server:
        return None
    proxy = {"server": server}
    bypass = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
    if bypass:
        proxy["bypass"] = bypass
    return proxy


def _label_regex(text: str) -> re.Pattern:
    """Exact label match that tolerates the portal's inconsistent whitespace."""
    parts = [re.escape(p) for p in text.split()]
    return re.compile(r"^\s*" + r"\s+".join(parts) + r"\s*$")


class Portal:
    def __init__(self, base_url: str, headless: bool = True, capture: bool = False):
        self.base_url = base_url.rstrip("/") + "/"
        self.headless = headless
        self.capture = capture
        self._pw: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self._warm = False                      # first page load done (bundles now in the asset cache)
        self._first_lock: Optional[asyncio.Lock] = None
        self._open_gate: Optional[asyncio.Semaphore] = None
        # the portal serves its 7 MB Angular bundle with no Cache-Control, so Chromium re-downloads it for
        # every tab; we fetch each JS/CSS asset once and answer later tabs from memory
        self._assets: dict[str, tuple[int, dict, bytes]] = {}
        self._asset_locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self):
        self._first_lock = asyncio.Lock()
        self._open_gate = asyncio.Semaphore(OPEN_CONCURRENCY)
        self._pw = await async_playwright().start()
        # BHULEKH_CHROMIUM=/path/to/chrome uses an existing Chromium instead of Playwright's own download;
        # BHULEKH_CHROMIUM_ARGS="--flag1 --flag2" appends launch flags (e.g. --ssl-version-max=tls1.2
        # behind a TLS-intercepting proxy that cannot negotiate TLS 1.3 with Chromium)
        exe = os.environ.get("BHULEKH_CHROMIUM") or None
        args = (os.environ.get("BHULEKH_CHROMIUM_ARGS") or "").split()
        self.browser = await self._pw.chromium.launch(headless=self.headless, executable_path=exe, args=args,
                                                      proxy=_env_proxy())
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 900}, locale="hi-IN",
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 bhulekh-finder/0.1"))
        await self.context.route(re.compile(r"\.(png|jpe?g|gif|woff2?|ttf|svg)(\?.*)?$"), lambda r: r.abort())
        await self.context.route(re.compile(re.escape(self.base_url) + r"[^?]*\.(js|css)(\?.*)?$"), self._serve_asset)
        if self.headless:
            await self.context.add_init_script(QUIET_HOOK)
        if self.capture:
            await self.context.add_init_script(CAPTURE_HOOK)
        return self

    async def __aexit__(self, *exc):
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def _serve_asset(self, route, request):
        """Route handler: first request for a static asset fetches it, everyone else gets the cached copy."""
        if request.method != "GET":
            await route.continue_()
            return
        key = request.url
        ent = self._assets.get(key)
        if ent is None:
            lock = self._asset_locks.setdefault(key, asyncio.Lock())
            async with lock:
                ent = self._assets.get(key)
                if ent is None:
                    try:
                        resp = await route.fetch(timeout=PAGE_LOAD_TIMEOUT_MS)
                    except Exception:  # noqa: BLE001 — proxy/tunnel stall: let the browser try on its own
                        await route.continue_()
                        return
                    if resp.status != 200:
                        await route.fulfill(response=resp)
                        return
                    body = await resp.body()
                    ctype = resp.headers.get("content-type", "application/octet-stream")
                    ent = (resp.status, {"content-type": ctype}, body)
                    self._assets[key] = ent
        await route.fulfill(status=ent[0], headers=ent[1], body=ent[2])

    async def new_tab(self) -> "Tab":
        """Open a tab on the search screen. The very first load runs alone so the Angular bundle lands in
        the asset cache before other tabs start; later opens are limited to OPEN_CONCURRENCY at a time, which
        stops a burst of half-loaded pages hitting the portal with 500s/timeouts at start-up."""
        page = await self.context.new_page()
        page.set_default_timeout(20000)
        tab = Tab(self, page)
        if not self._warm:
            async with self._first_lock:
                if not self._warm:
                    await tab.open_search()
                    self._warm = True
                    return tab
        async with self._open_gate:
            await tab.open_search()
        return tab


class Tab:
    """A single browser tab pinned to the search screen."""

    def __init__(self, portal: Portal, page: Page):
        self.portal = portal
        self.page = page
        self.district: Optional[str] = None
        self.tehsil: Optional[str] = None
        self.village_code: Optional[str] = None
        self.fasli: str = CURRENT_FASLI
        self.opened_at = 0.0

    # ---- navigation ----------------------------------------------------
    async def open_search(self):
        p = self.page
        # the page is usable only once its own start-up calls are back: the district list (api/edata) and the
        # JWT the portal stores in sessionStorage — a search fired before that gets a 500 or an empty dropdown
        async with p.expect_response(lambda r: "api/edata" in r.url, timeout=PAGE_LOAD_TIMEOUT_MS) as edata:
            await p.goto(self.portal.base_url + SEARCH_ROUTE, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            try:
                await p.wait_for_selector("#districtSelect", timeout=30000)
            except PWTimeout:
                await p.goto(self.portal.base_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
                await p.get_by_text("खतौनी (अधिकार अभिलेख) की नक़ल देखे").first.click()
                await p.wait_for_selector("#districtSelect", timeout=30000)
        await edata.value
        await p.wait_for_function("() => !!sessionStorage.getItem('jwtToken')", timeout=15000)
        self.district = self.tehsil = self.village_code = None
        self.fasli = CURRENT_FASLI
        self.opened_at = time.time()
        if self.portal.capture:
            await p.evaluate("() => { if (window.__bhu) window.__bhu.capture = true; }")

    async def refresh_if_stale(self):
        if time.time() - self.opened_at > TAB_MAX_AGE_S:
            await self.open_search()

    async def close(self):
        try:
            await self.page.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- ng-select helpers ---------------------------------------------
    async def _open_dropdown(self, sel_id: str):
        p = self.page
        await self.dismiss_dialog()
        # ng-select stays disabled until the previous step's option list has landed; a click on a disabled
        # control opens nothing, so wait for it to be enabled before clicking
        await p.wait_for_selector(f"#{sel_id}:not(.ng-select-disabled)", timeout=30000)
        for attempt in range(2):
            await p.keyboard.press("Escape")
            await p.click(f"#{sel_id} .ng-select-container")
            try:
                await p.wait_for_selector(".ng-dropdown-panel .ng-option", timeout=10000 if attempt == 0 else 25000)
                return
            except PWTimeout:
                if attempt:
                    diag = await p.evaluate(
                        """(id) => { const e = document.querySelector('#' + id);
                                     return {cls: e ? e.className : null, panels: document.querySelectorAll('.ng-dropdown-panel').length,
                                             placeholder: e ? e.textContent.replace(/\\s+/g, ' ').trim().slice(0, 40) : null}; }""", sel_id)
                    raise PortalError(f"dropdown #{sel_id} showed no options ({diag})")

    async def ng_options(self, sel_id: str) -> list[str]:
        await self._open_dropdown(sel_id)
        opts = await self.page.evaluate(
            "() => [...document.querySelectorAll('.ng-dropdown-panel .ng-option')].map(o => o.textContent.replace(/\\s+/g, ' ').trim())")
        await self.page.keyboard.press("Escape")
        return [o for o in opts if o and o != "No items found"]

    async def ng_select(self, sel_id: str, text: str, wait_url: Optional[str] = None, typed: Optional[str] = None):
        """Select the option whose label equals `text` (whitespace-tolerant). `typed` filters first."""
        p = self.page
        await self.dismiss_dialog()
        if typed:
            # typing into the search box opens the panel with only the matching options rendered
            # (focus + fill, no click: a click would first render the entire option list)
            await p.keyboard.press("Escape")
            # ng-select keeps the input disabled until the option list from the previous step has landed
            await p.wait_for_selector(f"#{sel_id} input[type=text]:not([disabled])", timeout=30000)
            inp = p.locator(f"#{sel_id} input[type=text]")
            await inp.focus()
            await inp.fill(typed)
        else:
            await self._open_dropdown(sel_id)
        target = p.locator(".ng-dropdown-panel .ng-option").filter(has_text=_label_regex(text))
        try:
            await target.first.wait_for(timeout=10000)
        except PWTimeout:
            await p.keyboard.press("Escape")
            raise PortalError(f"option not found in #{sel_id}: {text}")
        if not wait_url:
            await target.first.click()
            return
        # wait for the follow-up API call — or for a portal dialog that replaces it (e.g. village under chakbandi)
        waiter = asyncio.ensure_future(p.wait_for_event("response", lambda r: wait_url in r.url, timeout=45000))
        try:
            await target.first.click()
            while not waiter.done():
                text = await self.dismiss_dialog()
                if text:
                    raise PortalDialog(text[:160])
                await asyncio.sleep(0.2)
            resp = await waiter   # re-raises the 45 s timeout if the call never came
            if resp.status >= 500:
                # the dropdown would stay disabled for good; fail now so the village is retried on a fresh tab
                raise PortalError(f"server error {resp.status} on {wait_url.split('/')[-1]} (session token rejected)")
        finally:
            if not waiter.done():
                waiter.cancel()

    # ---- location ------------------------------------------------------
    async def set_district(self, label: str):
        if self.district == label:
            return
        await self.ng_select("districtSelect", label, wait_url="api/tehsils")
        await asyncio.sleep(0.15)
        self.district, self.tehsil, self.village_code = label, None, None

    async def set_tehsil(self, label: str):
        if self.tehsil == label:
            return
        await self.ng_select("tehsilSelect", label, wait_url="api/villages")
        await asyncio.sleep(0.15)
        self.tehsil, self.village_code = label, None

    async def set_village(self, label: str, code: str):
        if self.village_code == code:
            return
        await self.ng_select("villageSelect", label, wait_url="api/fasli", typed=code)
        await asyncio.sleep(0.15)
        self.village_code = code
        self.fasli = CURRENT_FASLI

    async def set_location(self, district: str, tehsil: str, village_label: str, code: str):
        await self.set_district(district)
        await self.set_tehsil(tehsil)
        await self.set_village(village_label, code)

    async def fasli_options(self) -> list[str]:
        return await self.page.evaluate(
            "() => [...document.querySelectorAll('#fasliSelect option')].map(o => o.value)")

    async def set_fasli(self, value: str):
        if self.fasli == value:
            return
        await self.page.select_option("#fasliSelect", value)
        await asyncio.sleep(0.3)
        self.fasli = value

    # ---- dialogs -------------------------------------------------------
    async def dismiss_dialog(self) -> Optional[str]:
        """Close any SweetAlert dialog; returns its text if one was open."""
        text = await self.page.evaluate(
            """() => { const sw = document.querySelector('.swal2-popup.swal2-show');
                       if (!sw) return null;
                       const t = sw.textContent.replace(/\\s+/g, ' ').trim();
                       const b = sw.querySelector('.swal2-confirm'); if (b) b.click(); return t; }""")
        if text:
            try:
                await self.page.wait_for_selector(".swal2-popup.swal2-show", state="detached", timeout=3000)
            except PWTimeout:
                await self.page.keyboard.press("Escape")
        return text

    # ---- on-screen keyboard --------------------------------------------
    async def keyboard_keys(self) -> list[str]:
        """All keys of the name-search keyboard except control keys."""
        keys = await self.page.evaluate(
            "() => [...document.querySelector('.contact-page').querySelectorAll('.tab')].map(e => e.textContent.trim())")
        return [k for k in keys if k and k not in ("clear", "space", ".")]

    async def _click_key(self, ch: str):
        key = _KEY_ALIASES.get(ch, ch)
        ok = await self.page.evaluate(
            """(k) => { const pg = document.querySelector('.contact-page');
                        const el = [...pg.querySelectorAll('.tab')].find(e => e.textContent.trim() === k);
                        if (!el) return false; el.click(); return true; }""", key)
        if not ok:
            raise PortalError(f"on-screen key not found: {ch!r}")

    async def _wait_input(self, expected: str, timeout_ms: int = 4000):
        """ngModel writes the input value asynchronously; wait until the DOM shows what we typed."""
        try:
            await self.page.wait_for_function(
                "(v) => (document.querySelector('.contact-page input.input') || {value: null}).value === v",
                arg=expected, timeout=timeout_ms)
        except PWTimeout:
            got = await self.page.input_value(".contact-page input.input")
            raise PortalError(f"keyboard entry mismatch: wanted {expected!r} got {got!r}")

    async def type_prefix(self, prefix: str):
        """Type prefix through the on-screen keyboard. Multi-character keys (e.g. 'क्ष') are supported
        when passed as a single element of a list; a plain string is typed character by character."""
        p = self.page
        await self.dismiss_dialog()
        if not await p.evaluate("() => document.querySelector('#contact').checked"):
            await p.click("label[for=contact]")
        await self._click_key("clear")
        await self._wait_input("")
        chunks = list(prefix) if isinstance(prefix, str) else list(prefix)
        for ch in chunks:
            await self._click_key(ch)
        await self._wait_input("".join(chunks))

    # ---- khatedar name search ------------------------------------------
    async def search_name(self, prefix, timeout_s: float = 45.0) -> list[Row]:
        """Run 'खातेदार के नाम द्वारा खोजें' for a Devanagari prefix and return parsed rows."""
        p = self.page
        await self.type_prefix(prefix)
        capture = self.portal.capture
        seq_before = await p.evaluate("() => window.__bhu ? window.__bhu.seq : -1") if capture else -1

        for attempt in range(2):
            async with p.expect_response(lambda r: "api/uniqueCoden" in r.url, timeout=timeout_s * 1000) as resp_info:
                await p.click(".contact-page button.btn-primary")
            resp = await resp_info.value
            if resp.status < 500 or attempt:
                break
            # a fresh tab's first search sometimes gets a 500; one in-tab retry is far cheaper than a new tab
            await asyncio.sleep(2.0)
            await self.dismiss_dialog()
            seq_before = await p.evaluate("() => window.__bhu ? window.__bhu.seq : -1") if capture else -1
        if resp.status >= 500:
            raise PortalError(f"server error {resp.status} on uniqueCoden (session token may have expired)")
        if resp.status == 429:
            raise PortalError("rate limited (429)")
        if resp.status >= 400:
            raise PortalError(f"http {resp.status} on uniqueCoden")

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            state = await p.evaluate(
                """(cap) => {
                     if (cap && window.__bhu && window.__bhu.seq > cap.seq) return {api: window.__bhu.rows};
                     const sw = document.querySelector('.swal2-popup.swal2-show');
                     if (sw) return {swal: sw.textContent.replace(/\\s+/g, ' ').trim()};
                     const pg = document.querySelector('.contact-page');
                     const rows = [...pg.querySelectorAll('input[type=radio]')]
                         .filter(r => r.name !== 'nav')
                         .map(r => (r.parentElement ? r.parentElement.textContent : '').replace(/\\s+/g, ' ').trim());
                     return {rows}; }""", {"seq": seq_before} if capture else None)
            if "api" in state:
                await self.dismiss_dialog()      # the component was given [] and shows "No Data Found"
                return [row_from_api(d) for d in state["api"]]
            if "swal" in state:
                text = state["swal"]
                await self.dismiss_dialog()
                if capture:
                    # the dialog may have appeared before our hook's seq was polled — re-check once
                    late = await p.evaluate("(s) => (window.__bhu && window.__bhu.seq > s) ? window.__bhu.rows : null", seq_before)
                    if late:
                        return [row_from_api(d) for d in late]
                if "No Data" in text or "नहीं" in text:
                    return []
                raise PortalError("portal dialog: " + text[:120])
            if state["rows"]:
                rows = [parse_row(t) for t in state["rows"]]
                bad = [t for t, r in zip(state["rows"], rows) if r is None]
                if bad:
                    raise PortalError("unparsed row: " + bad[0][:120])
                return [r for r in rows if r]
            await asyncio.sleep(0.1)
        raise PortalError("timeout waiting for name-search results")

    async def search_name_complete(self, prefix: str, big: int = 1000) -> list[Row]:
        """search_name, expanded into two-character prefixes when the result is suspiciously large."""
        rows = await self.search_name(prefix)
        if len(rows) < big:
            return rows
        seen = {(r.unique_code, r.khatedar, r.father): r for r in rows}
        for k in await self.keyboard_keys():
            try:
                more = await self.search_name([*prefix, k])
            except PortalError:
                continue
            for r in more:
                seen.setdefault((r.unique_code, r.khatedar, r.father), r)
        return list(seen.values())

    async def select_result(self, unique_code: str) -> bool:
        """Tick the result radio whose text contains unique_code (downloader; needs capture=False)."""
        return await self.page.evaluate(
            """(code) => { const pg = document.querySelector('.contact-page');
                           const r = [...pg.querySelectorAll('input[type=radio]')]
                               .find(r => r.name !== 'nav' && (r.parentElement.textContent || '').includes(code));
                           if (!r) return false; r.click(); return true; }""", unique_code)
