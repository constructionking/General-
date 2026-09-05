# Search logic — the rules the scanner enforces and why

Distilled from an earlier manual/semi-automated sweep of Hardoi, Lucknow and Bara Banki (3,223 villages) and
from this repository's own live runs. Each rule names where it lives in the code.

## Matching intelligence

| # | Rule | Where |
|---|---|---|
| L1 | **The father is the anchor.** A row counts only when both the khatedar and the father match a target; the person's own spelling may vary, the father must match. | `matcher.match_row` |
| L2 | **Surname discipline.** A surname/lineage token on either name that appears in none of the target's spellings (खां, शाह, हुसैन, जैदी, सिंह, अंसारी …) rejects the row outright. सादिक हुसैन s/o साबिर खां shares no lineage with सादिक अली s/o साबिर अली. | `matcher.lineage_conflict`, `LINEAGE_TOKENS` |
| L3 | **Token boundary, never substring.** The given name's first token must equal a configured spelling after normalisation (vowel length folded, व→ब, nukta stripped) or be a same-length near-identical spelling. साबिरा (a woman) is not साबिर; सदाकत is not सादिक. | `matcher._first_token_ok` |
| L4 | **Near-misses are logged, never counted.** Right name / wrong father rows are stored with category `near_miss` and a reason, and appear in the report's "Ruled out" table so the exclusion is auditable. | `matcher.near_miss`, `scanner._scan_once`, `cli rematch` |
| L5 | **Rank, don't just include.** Probable = both names ≥ 90 % with no conflicting tokens (and in the expected district where configured); otherwise less probable, with the weak signal named in the reasoning. Cluster signals (other family hits in the tehsil, several khatas in one village) are recorded. | `matcher.categorise` |

## Automation reliability

| # | Rule | Where |
|---|---|---|
| R1 | **Snapshot the village list first.** District → tehsil → village codes are catalogued once into SQLite; the scan never depends on live dropdown state. | `catalog.py` |
| R2 | **Progress lives outside the page.** Every village is checkpointed in `data/bhulekh.sqlite`; Ctrl-C and re-run resumes. | `store.py` |
| R3 | **A tab is ready only after the portal's own start-up calls.** `api/edata` answered and the JWT stored; a search fired earlier gets a 500 or an empty dropdown. | `browser.Tab._load_search` |
| R4 | **Reload for real.** A `goto` to the same hash URL is a same-document navigation in Angular; reloads go through `about:blank`. | `browser.Tab._load_search` |
| R5 | **Dead-session detector.** A genuine empty village raises the "No Data Found" dialog; a dead session returns nothing and no dialog. Eight seconds of neither → `PortalServerError` → the tab reloads (fresh token) and repeats the village. A 5xx on any step does the same. | `browser.Tab._search_name`, `scanner.scan_village` |
| R6 | **Refresh pre-emptively.** Tabs reload every 150 villages and every 18 minutes, before the ~195-village silent expiry and the 25-minute JWT. | `browser.REFRESH_EVERY_VILLAGES`, `TAB_MAX_AGE_S` |
| R7 | **Stagger start-up and hold during loads.** First tab loads alone, then 3 at a time; API calls made while another tab is loading come back as 500, so tabs wait until no load is in flight. | `browser.Portal.open_search`, `wait_idle` |
| R8 | **Cache the bundle.** The 7 MB Angular bundle has no Cache-Control; it is fetched once and served to later tabs from memory. | `browser.Portal._serve_asset` |
| R9 | **Dismiss dialogs; classify them.** Only "no records" dialogs (चकबंदी, No Data, नहीं, उपलब्ध) skip a village (`skipped`, with the text); anything else is retried. | `browser.dialog_means_no_records` |
| R10 | **Read the decrypted list, not the DOM.** The fast tier captures the portal's own decrypted JSON as it is parsed; rows are never retyped. | `browser.CAPTURE_HOOK` |
| R11 | **Timestamp every step.** Per-village step timings go into the events table; `status` shows medians and the top errors. | `browser.Tab._t`, `store.timing_summary` |

## Verification (after the sweep)

A name hit is a lead, not a conclusion. For every probable hit pull the khatauni (`bhulekh download`) and read,
in order: residence (निवासी), co-owners and their fathers, and the ancestor named in the वरासत (succession)
entry. Group hits by surname line + father + residence: same tehsil, same hamlet, shared ancestor ⇒ one
family; different ancestor ⇒ a separate family even with identical first names. Compare each holder's
residence with the family's actual home region rather than treating "local" as disqualifying.

## Report shape

`bhulekh report` writes `output/summary.md` and a styled `output/summary.html`: the bottom line (a verdict box
per target and one for coverage), where we searched, one section per target with a plot table per district and
area subtotals, ruled-out near-misses, reasoning per hit, method & data confidence, what to confirm next.
