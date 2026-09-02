# UP Bhulekh khatedar finder

Scans whole Uttar Pradesh districts on **upbhulekh.gov.in** for land recorded under specific
khatedar + father name pairs, categorises the hits, and helps you download every matching khatauni extract.

Targets live in `config.yaml` (khatedar spellings, father spellings, on-screen-keyboard prefixes).
A row is a hit only when **both** the khatedar and the father match.

## Setup (once)

```bash
cd "khasra:khatauni search"
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

## Everyday commands

```bash
./bhulekh.sh catalog                      # fetch all 75 districts → tehsils → villages (one-off, cached)
./bhulekh.sh scan -d Amroha -d Lucknow    # scan districts (resumable; re-run to continue)
./bhulekh.sh scan --all                   # the whole state
./bhulekh.sh status                       # one-screen snapshot
./bhulekh.sh report --live                # mid-scan summary (output/summary_live.md + .html)
./bhulekh.sh report                       # final summary (output/summary.md + .html)
./bhulekh.sh download                     # opens a browser; you type each CAPTCHA, it saves the extract
./bhulekh.sh export -d Amroha             # output/hits.xlsx (+csv) and per-district workbooks
```

Useful scan flags: `--max-tabs 24 --start-tabs 12` (concurrency), `--limit 50` (test run),
`--old-fasli` (also search the older fasli band), `--reset-errors` (retry failed villages),
`--render` (disable the fast in-page capture and read the rendered rows instead), `--headed`.

## Search strategy (priority order)

```bash
./bhulekh.sh strategy --example > strategy.yaml     # edit it
./bhulekh.sh strategy -f strategy.yaml -t "west UP first, Amroha/Hasanpur before anything else"
./bhulekh.sh scan --all                              # follows the order; can be re-applied mid-run
```

Levels: regions → districts → tehsils → village name patterns / specific codes; `skip.districts`;
`adaptive.boost_neighbours_on_hit` moves the rest of a tehsil (then district) to the front after a Probable hit.

## How it works

- Playwright drives the portal's own Angular UI (the API payloads are encrypted by the portal's code, so no
  request is ever forged). One tab per worker; the pool ramps up while the portal stays healthy and halves on errors.
- Fast tier: a JSON.parse hook inside the page captures the decrypted khatedar list as soon as the portal decrypts
  it, and hands the component an empty list so 1,000-row lists are not rendered. `--render` switches it off.
- Results, hits, extracts and progress live in `data/bhulekh.sqlite` (WAL mode, so `report --live` and
  `download` work while a scan runs). Every village is checkpointed; Ctrl-C and re-run to resume.
- The extract page is CAPTCHA-gated. The downloader never reads or solves it: it opens the exact khata, presses
  उद्धरण देखें, waits for you to type the number, then saves PNG + HTML + PDF under `output/extracts/`.

## Outputs

- `output/summary.md` / `.html` — Probable vs Less probable hits with reasoning, clusters, coverage, ETA.
- `output/hits.xlsx`, `hits.csv` — every hit with scores, reasoning and extract paths.
- `output/<District>.xlsx` — every raw khatedar row collected for that district (prefixes स and वि).
- `output/extracts/<District>/<Tehsil>/<village>_<code>_khata<no>_<fasli>.pdf|png|html`.
