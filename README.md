# UP Bhulekh khatedar finder

Scans whole Uttar Pradesh districts on **upbhulekh.gov.in** for land recorded under specific
khatedar + father name pairs, categorises the hits, and helps you download every matching khatauni extract.

Targets live in `config.yaml` (khatedar spellings, father spellings, on-screen-keyboard prefixes).
A row is a hit only when **both** the khatedar and the father match.

## Setup (once, on the machine that will run the sweep)

Needs git and Python 3.9+ (macOS: `xcode-select --install` provides git; python3 from python.org or Homebrew).

```bash
git clone https://github.com/constructionking/General- bhulekh && cd bhulekh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium     # downloads the browser Playwright drives (~150 MB)
./bhulekh.sh doctor -d Amroha                        # every stage should PASS, each in a few seconds
```

Run the sweep on a normal home/office connection, not from a cloud sandbox: the scanner is bound by the
round trip to upbhulekh.gov.in, and 16 tabs on a normal link measured about 1.3 villages/s. Progress lives in
`data/bhulekh.sqlite`, so Ctrl-C and re-running the same command resumes. To keep the code current later:
`git pull`.

## Everyday commands

```bash
./bhulekh.sh doctor -d Amroha             # PASS/FAIL per stage with timings: portal, browser, page, selects, search
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

## Environment overrides

- `BHULEKH_CHROMIUM=/path/to/chrome` — drive an existing Chromium/Chrome instead of Playwright's downloaded one.
- `BHULEKH_CHROMIUM_ARGS="--flag …"` — extra Chromium launch flags. Behind a TLS-intercepting proxy that cannot
  finish a TLS 1.3 handshake with Chromium (symptom: `ERR_CONNECTION_RESET` on the first navigation while
  `curl` works), use `--ssl-version-max=tls1.2`.
- `HTTPS_PROXY` / `NO_PROXY` are passed to the browser automatically (Chromium ignores them on its own).

Start-up is deliberately staggered: the first tab loads the search page alone, then at most three tabs load at a
time. The portal serves its 7 MB Angular bundle with no `Cache-Control`, so Chromium would re-download it for
every tab; the scanner fetches each JS/CSS asset once and answers later tabs from memory. Without these two
measures a burst of half-loaded tabs saturated the link and produced 500s and timeouts on the first villages of
every run.

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

- `output/summary.md` / `.html` — the compilation: bottom line per target, coverage, plot tables per district with
  area subtotals, ruled-out near-misses (right name, wrong father), reasoning, method, next steps. The rules the
  matcher and scanner enforce are written up in `docs/search-logic.md`.
- `output/hits.xlsx`, `hits.csv` — every hit with scores, reasoning and extract paths.
- `output/<District>.xlsx` — every raw khatedar row collected for that district (prefixes स and वि).
- `output/extracts/<District>/<Tehsil>/<village>_<code>_khata<no>_<fasli>.pdf|png|html`.
