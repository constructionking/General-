"""The API transport: envelope/field crypto, the scanner-facing facade, and independence from Playwright.

No network: the client is exercised through a fake that records what it was asked for."""
import asyncio
import json
import sys

import pytest

from bhulekh import api
from bhulekh.rows import PortalDialog, Row


def test_api_module_does_not_pull_in_playwright():
    """`scan --api` must run on a machine with no browser stack installed. Checked in a fresh
    interpreter, because other tests in this process import the browser driver first."""
    import subprocess
    code = ("import sys, bhulekh.api, bhulekh.scanner; "
            "print(sorted(m for m in sys.modules if m == 'playwright' or m.startswith('playwright.')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]", f"bhulekh.api pulled in playwright: {out}"


def test_envelope_roundtrip_and_known_answer():
    text = json.dumps({"userName": "1:x:08/09/26 18", "passWord": "2:y:08/09/26 18", "userTypeId": ""})
    assert api.decrypt_envelope(api.encrypt_envelope(text)) == text
    # AES-256-CBC under the app's fixed key/IV: a value computed once and pinned so a change in the
    # padding, mode or key material is caught rather than silently producing 400s from the portal
    assert api.encrypt_envelope("all") == "DwL1uJyHQVlFL9BiR8BETw=="


def test_session_key_is_sha256_hex_2_to_9():
    jwt = "header.payload.signature"
    import hashlib
    assert api.session_key(jwt) == hashlib.sha256(jwt.encode()).hexdigest()[2:9]
    assert len(api.session_key(jwt)) == 7


def test_field_is_urlsafe_unpadded_and_deterministic():
    f = api.encrypt_field("2590d69", "177")
    assert f == api.encrypt_field("2590d69", 177)
    assert not any(c in f for c in "+/=%")       # base64url, padding stripped, nothing left to escape
    assert api.encrypt_field("2590d69", "स") != f
    # known answer under a fixed session key
    assert api.encrypt_field("2590d69", "all") == "Mdrl8REAO-d2_HLvqYkB8w"


def test_login_payload_shape():
    p = api.login_payload()
    assert set(p) == {"userName", "passWord", "userTypeId"}
    assert p["userTypeId"] == ""
    n, seed, stamp = p["userName"].split(":", 2)
    assert n.isdigit() and seed == api.USER_SEED and len(stamp) == len("08/09/26 18")


class FakeClient:
    """Stands in for ApiClient: answers searches from a table and records the calls."""

    def __init__(self, table):
        self.table = table
        self.calls = []
        self.tokens = 0

    async def ensure_token(self, force=False):
        self.tokens += 1

    async def search(self, district_code, village_code, prefix, fasli="999"):
        self.calls.append((district_code, village_code, prefix, fasli))
        data = self.table[(village_code, prefix)]
        if isinstance(data, dict):
            raise PortalDialog(data["message"])
        return [api.row_from_api(d) for d in data]


def _portal(table, codes):
    p = api.ApiPortal.__new__(api.ApiPortal)
    p.cfg, p.store = {}, None
    p.client = FakeClient(table)
    p._codes, p._codes_lock = codes, asyncio.Lock()
    return p


def test_facade_searches_with_census_codes_and_returns_rows():
    table = {("166870", "स"): [{"khasra_no": "419क", "name": "सच्चितानन्द", "father": "शिवराम",
                                 "unique_code": "1668700419200112", "area": "0.0600"}],
             ("166870", "वि"): []}
    portal = _portal(table, {"Ayodhya (अयोध्या)": "177"})

    async def go():
        tab = await portal.new_tab()
        await tab.set_location("Ayodhya (अयोध्या)", "Bikapur (बीकापुर)", "Ankari (अंकारी) - 166870", "166870")
        rows = await tab.search_name_complete("स", big=5000)
        empty = await tab.search_name_complete("वि", big=5000)
        return tab, rows, empty

    tab, rows, empty = asyncio.run(go())
    assert [(r.khata, r.khatedar, r.father, r.unique_code, r.area) for r in rows] == \
           [("419क", "सच्चितानन्द", "शिवराम", "1668700419200112", 0.06)]
    assert isinstance(rows[0], Row) and empty == []
    assert portal.client.calls == [("177", "166870", "स", "999"), ("177", "166870", "वि", "999")]
    assert "search:स" in tab.timings and "village" in tab.timings


def test_facade_raises_dialog_for_no_records_answer():
    portal = _portal({("1", "स"): {"message": "यह गाँव चकबंदी में है।"}}, {"D (द)": "1"})

    async def go():
        tab = await portal.new_tab()
        await tab.set_location("D (द)", "T (त)", "V (व) - 1", "1")
        await tab.search_name_complete("स")

    with pytest.raises(PortalDialog):
        asyncio.run(go())


def test_facade_unknown_district_is_an_error():
    portal = _portal({}, {})

    async def go():
        tab = await portal.new_tab()
        await tab.set_location("Nowhere (कहीं नहीं)", "T", "V - 1", "1")

    with pytest.raises(api.PortalError):
        asyncio.run(go())
