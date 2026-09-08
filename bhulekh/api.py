"""Direct client for the portal's own JSON API (PublicBhuApi), used instead of driving the UI.

The Angular app talks to `https://upbhulekh.gov.in/PublicBhuApi/api` with two layers of AES:

  outer  every request body is `{"edata": <base64>}` where the plaintext is the JSON payload,
         encrypted AES-256-CBC/Pkcs7 under a key and IV hardcoded in the app's bundle. Responses
         come back the same way and are decrypted with the same key.
  inner  each *field* of that payload (village code, district code, the name prefix) is itself
         encrypted AES-128-CBC/Pkcs7 under a key derived from the session's JWT — key and IV are
         both the first 16 bytes of `sha256(jwt).hex()[2:9]`, zero padded — then base64url encoded
         without padding and percent-encoded.

The session is opened by POSTing a timestamped pseudo-credential to `/edata`, which returns the JWT.

This module is transport only: it produces exactly the `Row` objects the browser driver produces
(via the same `row_from_api`), so matching, scoring and storage are untouched.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import time
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from .rows import CURRENT_FASLI, PortalDialog, PortalError, PortalServerError, Row, row_from_api

API_BASE = os.environ.get("BHULEKH_API_URL", "https://upbhulekh.gov.in/PublicBhuApi/api")
# key/IV of the outer envelope, read out of the app bundle's encryption service
OUTER_KEY = b"12345678901234567890123456789012"
OUTER_IV = b"1234567890123456"
# seeds the app mixes into its throwaway login credential
USER_SEED = "fgfgsdfiutrgkdfgdhgkdfkgkjgdh"
PASS_SEED = "fghkjfghuyrtigvxcvbjgfghdgsdgdfg"
TOKEN_MAX_AGE_S = 18 * 60        # the JWT lives ~25 min; mint a fresh one well before it expires
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 bhulekh-finder/0.2")
# the portal sits behind a load balancer whose nodes do not all carry every route: one answers
# "No static resource api/…" for a path its sibling serves. Such an answer is retried, not fatal.
_WRONG_NODE = "No static resource"


def _pad(b: bytes) -> bytes:
    n = 16 - len(b) % 16
    return b + bytes([n]) * n


def _unpad(b: bytes) -> bytes:
    if not b or b[-1] > 16 or b[-1] > len(b):
        raise PortalError("bad padding in portal response")
    return b[:-b[-1]]


def _aes_cbc(key: bytes, iv: bytes):
    from Crypto.Cipher import AES
    return AES.new(key, AES.MODE_CBC, iv)


def encrypt_envelope(text: str) -> str:
    return base64.b64encode(_aes_cbc(OUTER_KEY, OUTER_IV).encrypt(_pad(text.encode()))).decode()


def decrypt_envelope(b64: str) -> str:
    return _unpad(_aes_cbc(OUTER_KEY, OUTER_IV).decrypt(base64.b64decode(b64))).decode("utf-8")


def session_key(jwt: str) -> str:
    """The app's `hashInput(jwt, 9)`: sha256 hex, characters 2..9."""
    return hashlib.sha256(jwt.encode()).hexdigest()[2:9]


def encrypt_field(skey: str, value) -> str:
    """The app's `cryptoService.encryptText`: key = IV = first 16 bytes of the session key."""
    k = skey.encode()[:16].ljust(16, b"\0")
    raw = base64.b64encode(_aes_cbc(k, k).encrypt(_pad(str(value).encode()))).decode()
    return quote(raw.replace("+", "-").replace("/", "_").replace("=", ""), safe="")


def login_payload() -> dict:
    def custom(seed: str) -> str:
        now = datetime.now()
        return (f"{random.randrange(1000000)}:{seed}:"
                f"{now.day:02d}/{now.month:02d}/{str(now.year)[-2:]} {now.hour:02d}")
    return {"userName": custom(USER_SEED), "passWord": custom(PASS_SEED), "userTypeId": ""}


class ApiClient:
    """One HTTP session against the portal API, shared by every worker."""

    def __init__(self, base_url: str = API_BASE, concurrency: int = 8, timeout_s: float = 30.0,
                 retries: int = 3):
        # timeout × retries stays inside the scanner's per-village budget (concurrency.village_timeout_s)
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.retries = retries
        self._sem = asyncio.Semaphore(concurrency)
        self._client = None
        self._jwt: Optional[str] = None
        self._skey: Optional[str] = None
        self._minted_at = 0.0
        self._token_lock: Optional[asyncio.Lock] = None

    async def __aenter__(self):
        import httpx
        self._token_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=self.timeout_s, headers={"User-Agent": USER_AGENT},
                                         follow_redirects=True)
        await self.ensure_token()
        return self

    async def __aexit__(self, *exc):
        if self._client is not None:
            await self._client.aclose()

    # ---- session -------------------------------------------------------
    async def ensure_token(self, force: bool = False):
        async with self._token_lock:
            if not force and self._jwt and time.time() - self._minted_at < TOKEN_MAX_AGE_S:
                return
            body = {"edata": encrypt_envelope(json.dumps(login_payload()))}
            data = await self._raw("/edata", body, authed=False)
            jwt = data.get("jwt") if isinstance(data, dict) else None
            if not jwt:
                raise PortalError(f"login did not return a jwt: {str(data)[:120]}")
            self._jwt, self._skey, self._minted_at = jwt, session_key(jwt), time.time()

    @property
    def session_age(self) -> float:
        return time.time() - self._minted_at

    # ---- transport -----------------------------------------------------
    async def _raw(self, path: str, body: dict, authed: bool = True) -> object:
        import httpx
        headers = {"Authorization": f"Bearer {self._jwt}"} if authed and self._jwt else {}
        last = ""
        for attempt in range(self.retries):
            async with self._sem:
                try:
                    r = await self._client.post(self.base_url + path, json=body, headers=headers)
                except httpx.HTTPError as e:            # reset/TLS/timeout: the tunnel, not the data
                    last = f"{type(e).__name__}: {e}"
                    await asyncio.sleep(0.4 * (attempt + 1))
                    continue
            if r.status_code == 200:
                if not r.text.strip():
                    return []
                payload = r.json()
                if isinstance(payload, dict) and "edata" in payload:
                    return json.loads(decrypt_envelope(payload["edata"]))
                return payload
            last = f"http {r.status_code}: {r.text[:120]}"
            if r.status_code == 401 or "Token expired" in r.text:
                await self.ensure_token(force=True)
                headers = {"Authorization": f"Bearer {self._jwt}"}
                continue
            if r.status_code == 429:
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code >= 500 and _WRONG_NODE in r.text:
                await asyncio.sleep(0.3 * (attempt + 1))   # a sibling node serves this route
                continue
            if r.status_code >= 500:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            raise PortalError(f"{path} {last}")
        raise PortalServerError(f"{path} failed after {self.retries} attempts — {last}")

    async def _call(self, path: str, payload: dict) -> object:
        await self.ensure_token()
        return await self._raw(path, {"edata": encrypt_envelope(json.dumps(payload))})

    def f(self, value) -> str:
        return encrypt_field(self._skey, value)

    # ---- catalog -------------------------------------------------------
    async def districts(self) -> list[dict]:
        return await self._call("/districts", {"districtCode": self.f("all")})

    async def tehsils(self, district_code: str) -> list[dict]:
        return await self._call("/tehsils", {"districtCode": self.f(district_code)})

    async def villages(self, district_code: str, tehsil_code: str) -> list[dict]:
        return await self._call("/villages", {"districtCode": self.f(district_code),
                                              "tehsilCode": self.f(tehsil_code)})

    # ---- the search ----------------------------------------------------
    async def search(self, district_code: str, village_code: str, prefix: str,
                     fasli: str = CURRENT_FASLI) -> list[Row]:
        """Khatedar-name search for one village. Returns the same Rows the browser driver returns."""
        payload = {"villageCode": self.f(village_code), "name": self.f(prefix),
                   "districtCode": self.f(district_code)}
        if fasli and fasli != CURRENT_FASLI:
            payload["fasliYear"] = self.f(fasli)
        data = await self._call("/uniqueCoden", payload)
        if data in (None, "", []):
            return []
        if isinstance(data, dict):
            text = str(data.get("message") or data.get("error") or data)
            raise PortalDialog(text[:160])
        return [row_from_api(d) for d in data]


# --------------------------------------------------------------------------
# Facade: the scanner drives "tabs", so the API client wears the same shape.
# --------------------------------------------------------------------------
class ApiTab:
    """A worker's handle on the API. Mirrors the parts of browser.Tab the scanner uses."""

    def __init__(self, portal: "ApiPortal"):
        self.portal = portal
        self.district: Optional[str] = None
        self.tehsil: Optional[str] = None
        self.village_code: Optional[str] = None
        self.district_code: Optional[str] = None
        self.fasli = CURRENT_FASLI
        self.timings: dict = {}

    async def refresh_if_stale(self):
        await self.portal.client.ensure_token()

    async def open_search(self):
        """The scanner's recovery step after a server error: for the API that is a fresh token."""
        await self.portal.client.ensure_token(force=True)

    async def close(self):
        return None

    async def set_location(self, district: str, tehsil: str, village_label: str, code: str):
        self.timings = {}
        t0 = time.time()
        self.district_code = await self.portal.district_code(district)
        self.district, self.tehsil, self.village_code = district, tehsil, code
        self.timings["village"] = round(time.time() - t0, 2)

    async def fasli_options(self) -> list[str]:
        return [CURRENT_FASLI]

    async def set_fasli(self, value: str):
        self.fasli = value

    async def search_name(self, prefix, timeout_s: float = 45.0) -> list[Row]:
        label = "".join(prefix) if not isinstance(prefix, str) else prefix
        t0 = time.time()
        try:
            return await self.portal.client.search(self.district_code, self.village_code, label, self.fasli)
        finally:
            self.timings[f"search:{label}"] = round(time.time() - t0, 2)

    async def search_name_complete(self, prefix: str, big: int = 1500,
                                   expand_keys: Optional[list[str]] = None) -> list[Row]:
        """The API returns the village's whole matching list in one call, so there is nothing to expand;
        `big` is accepted so the scanner's call site is identical for both transports."""
        return await self.search_name(prefix)


class ApiPortal:
    """Async context manager with the same surface the scanner expects from browser.Portal."""

    def __init__(self, cfg: dict, store=None, concurrency: int = 8):
        self.cfg = cfg
        self.store = store
        self.client = ApiClient(base_url=cfg.get("api", {}).get("base_url", API_BASE),
                                concurrency=concurrency)
        self._codes: Optional[dict] = None
        self._codes_lock: Optional[asyncio.Lock] = None

    async def __aenter__(self):
        self._codes_lock = asyncio.Lock()
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self.client.__aexit__(*exc)

    async def new_tab(self) -> ApiTab:
        return ApiTab(self)

    # ---- district label -> census code ---------------------------------
    async def district_code(self, label: str) -> str:
        codes = await self._district_codes()
        code = codes.get(label)
        if not code:
            raise PortalError(f"no census district code known for {label!r}")
        return code

    async def _district_codes(self) -> dict:
        if self._codes is not None:
            return self._codes
        async with self._codes_lock:
            if self._codes is not None:
                return self._codes
            cached = self.store.get_meta("district_codes", None) if self.store else None
            if cached:
                self._codes = cached
                return cached
            self._codes = await self._resolve_district_codes()
            if self.store:
                self.store.set_meta("district_codes", self._codes)
            return self._codes

    async def _resolve_district_codes(self) -> dict:
        """Map every catalog district label to its census code.

        The portal's own district list endpoint is not reliably deployed on every node, so when it is
        unavailable the codes are recovered from the tehsil endpoint: each candidate code answers with
        the tehsils of one district, and the catalog already knows each district's tehsil names."""
        from .store import split_label
        labels = self.store.districts() if self.store else []
        try:
            listing = await self.client.districts()
        except PortalError:
            listing = None
        if isinstance(listing, list) and listing:
            by_en, by_hi = {}, {}
            for d in listing:
                code = str(d.get("district_code_census") or d.get("dcc") or "")
                by_en[_norm(d.get("district_name"))] = code
                by_hi[_norm(d.get("district_name_hindi"))] = code
            out = {}
            for lb in labels:
                en, hi = split_label(lb)
                code = by_en.get(_norm(en)) or by_hi.get(_norm(hi))
                if code:
                    out[lb] = code
            if len(out) >= len(labels) * 0.9:
                return out
        return await self._discover_by_tehsils(labels)

    async def _discover_by_tehsils(self, labels: list[str]) -> dict:
        """Census codes of Uttar Pradesh's 2011 districts are 118–188; districts carved out since carry
        other numbers, so the sweep starts in the dense band and widens only while labels are unmatched.
        Candidate codes are probed a few at a time (the client's own semaphore caps the concurrency)."""
        from .store import split_label
        want = {lb: {_norm(split_label(t)[0]) for t in self.store.tehsils(lb)} for lb in labels}
        want = {lb: names for lb, names in want.items() if names}
        out: dict = {}

        async def probe(code: int):
            try:
                rows = await self.client.tehsils(str(code))
            except PortalError:
                return code, []
            return code, rows if isinstance(rows, list) else []

        for band in (range(100, 200), range(200, 300), range(1, 100), range(300, 1000)):
            if len(out) >= len(want):
                break
            band = list(band)
            for i in range(0, len(band), 8):
                for code, rows in await asyncio.gather(*(probe(c) for c in band[i:i + 8])):
                    if not rows:
                        continue
                    names = {_norm(r.get("tname_eng")) for r in rows if r.get("tname_eng")}
                    best, score = None, 0.0
                    for lb, mine in want.items():
                        if lb in out:
                            continue
                        overlap = len(names & mine) / max(len(mine), 1)
                        if overlap > score:
                            best, score = lb, overlap
                    if best and score >= 0.5:
                        out[best] = str(rows[0].get("district_code_census") or code)
                if len(out) >= len(want):
                    break
        return out


def _norm(s) -> str:
    return "".join((s or "").split()).lower()
