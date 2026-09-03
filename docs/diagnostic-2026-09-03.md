# Diagnostic report — 2026-09-03

Branch `feature/fast-tier-and-scan-fixes-fhcw2p`, commits 715d8f2 → c4decc3 → d565c38 (+ this report).
Environment: Claude Code cloud sandbox, Python 3.11.15, Playwright 1.62 driving the pre-installed
Chromium 141 through the sandbox's TLS-intercepting egress proxy (`BHULEKH_CHROMIUM_ARGS=--ssl-version-max=tls1.2`).

## 1. Static suite

| Check | Result |
|---|---|
| `pytest tests` (parse/match, store queue semantics, browser helpers) | **15 passed**, 0 failed |
| `pyflakes bhulekh tests scripts` | **clean** (two unused imports removed) |
| `python -m compileall bhulekh tests scripts` | **ok** |
| CLI smoke (`--help` for root, `scan`, `reset-errors --include-skipped`) | **ok** |
| `bhulekh status` / `report --live` / `export -d Amroha` on the scan database | **ok** — skipped column present in status table, summary.md and hits.xlsx coverage sheet |

Tests cover: rendered-row and API-row parsing parity, target matching, `--limit`/retry/skip/done queue
semantics, proxy URL parsing with credentials, portal dialog classification, exception hierarchy.

## 2. Code review (high effort) and what was done with each finding

| # | Finding | Action |
|---|---|---|
| 1 | Any portal popup permanently skipped a village | Fixed: only "no records" dialogs (चकबंदी / No Data / नहीं / उपलब्ध) skip; others are retryable errors. `reset-errors --include-skipped` added |
| 2 | 1,023 rows might be a server cap | Not a cap: another village returned 1,604 rows in one search. Comment corrected |
| 3 | Asset cache: unguarded `body()` could hang a tab | Fixed: whole fetch/body path guarded, falls back to a normal request |
| 4 | `HTTPS_PROXY` credentials dropped | Fixed: user:pass parsed into Playwright's proxy fields (unit-tested) |
| 5 | Failed first load leaked a page | Fixed: page closed on failure in `new_tab` |
| 6 | Reloads bypassed the start-up gate | Fixed: gating lives in `Tab.open_search`, so retries and 18-minute refreshes are throttled |
| 7 | 5xx retry keyed on a message substring | Fixed: `PortalServerError` type |
| 8 | report/export ignored `skipped` | Fixed: column + percent in both |
| 9 | 200 ms dialog polling in `ng_select` | Fixed: response future raced against the dialog selector |
| — | Reviewer's dropped candidate: same-URL `goto` never reloads | **Was real** (all 7 in-tab retries in a live run died waiting for `api/edata`). Fixed: reload goes through `about:blank` |

## 3. Live findings about the portal (verified in this session)

- The 7.4 MB Angular bundle is served without `Cache-Control`; every tab re-downloaded it. In-memory asset
  cache: tab 2+ open in ~5 s instead of ~21 s.
- A tab is usable only after `api/landTypeReport` + `api/edata` and the sessionStorage JWT exist; a search
  fired earlier gets a 500 or an empty dropdown. `open_search` waits for both.
- The portal answers `api/tehsils` / `api/villages` / `api/uniqueCoden` with **HTTP 500** when the call is made
  while another tab is loading the page (token minting race). Six tabs opened and then driven concurrently
  produced 0 × 500 across 24 calls; the same six tabs calling *during* each other's loads produced 4–7 × 500.
  Tabs now hold their next step while any page load is in flight.
- Villages under chakbandi (e.g. Phoolpur Adalpur 117955) answer with a dialog instead of fasli data; these
  are recorded as `skipped` with the dialog text.
- No cookies; JWT is HS512 with a shared `sub` per client and per-second `iat`. Seeding other tabs with one
  token does not work (the app overwrites it, and a tab that kept it got no API responses at all).
- A single name search returned 1,604 rows, so there is no 1,000-row cap; the two-character expansion is
  reserved for ≥ 5,000 rows.

## 4. Live integration runs (this sandbox)

The sandbox egress tunnel degraded during the session: bundle download 17 s → 66 s → 112 s (65 KB/s);
first-byte on the root page up to 13 s. All numbers below are dominated by that and are **not**
representative of a normal connection (the earlier session measured ~3 s page loads).

| Run (Amroha) | Villages | Rate | Errors | Notes |
|---|---|---|---|---|
| Before fixes, 40 villages | 40 | 0.16 /s | 8 | all errors in the first 55 s (tab burst) |
| + staggered opens | 34 | 0.25 /s | 6 | |
| + readiness wait | 38 | 0.31 /s | 2 | one chakbandi village, one disabled input |
| + fail-fast 5xx (pre-review) | 20 of 30 | 0.04 /s | 10 | tunnel stalls + same-URL reload bug (all 7 retries timed out) |
| Final code, 12 villages | _run in progress at the time of this commit; result appended in a follow-up commit_ | | | |

Hit found during the session: target T2 (`सादिक` s/o `साबिर अली`) in Amroha › Amroha › Haryana (118073),
khata 906, 0.227 ha — categorised *probable*.

## 5. Known limits

- The 500-on-concurrent-load behaviour is server-side; the gate avoids it but every page load pauses the
  other tabs for the duration of that load (~5 s with the asset cache, longer on slow links).
- Throughput target (a district in 4–5 min) cannot be measured from this sandbox; validate on a normal link
  with `scan -d Amroha --max-tabs 24`.
- The extract download stage is CAPTCHA-gated and remains manual.
