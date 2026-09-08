"""Portal value types shared by the browser driver and the API client.

These live apart from `browser.py` so the API client can be used on a machine with no Playwright
and no Chromium installed. `browser.py` re-exports everything here, so existing imports of
`from .browser import Row, PortalError, ...` keep working unchanged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

CURRENT_FASLI = "999"

ROW_RE = re.compile(
    r"^\s*(?P<khata>.+?)\s*:\s*(?P<khatedar>.+?)\s*:\s*(?P<father>.+?)\s*:\s*(?P<code>\d{14,18})\s*:\s*\((?P<area>[\d.]+)\s*(?:हे[०0]?|ha)?\s*\)\s*$"
)


class PortalError(RuntimeError):
    pass


class PortalDialog(PortalError):
    """The portal answered with a dialog instead of data, e.g. 'यह गाँव चकबंदी में है।' (village under
    consolidation — no khatauni available). A statement about the village, not a transient failure."""


class PortalServerError(PortalError):
    """The portal answered 5xx. Seen on a fresh tab's first calls while other tabs start up; retryable."""


NO_RECORDS_MARKERS = ("चकबंदी", "No Data", "नहीं", "उपलब्ध", "No Record", "no record")


def dialog_means_no_records(text: str) -> bool:
    """True for portal dialogs that state the village has no searchable khatauni (skip, don't retry);
    False for anything else (session/maintenance/error popups), which is retried like any failure."""
    return any(m in text for m in NO_RECORDS_MARKERS)


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
