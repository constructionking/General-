"""Summary report: Probable / Less probable hits with reasoning, clusters, coverage, ETA."""
from __future__ import annotations

import html
import os
from collections import Counter
from datetime import datetime

from .store import Store, split_label


def _fmt_area(a) -> str:
    return f"{a:.4f} हे०" if isinstance(a, (int, float)) else "—"


def _hit_line(h) -> str:
    return (f"**{h['target']}** · {split_label(h['district'])[0]} › {split_label(h['tehsil'])[0]} › "
            f"{h['village_label']} · खाता {h['khata']} · {h['khatedar']} / {h['father']} · {_fmt_area(h['area'])} · "
            f"score {h['score']:.0f}")


def build_report(store: Store, out_dir: str, live: bool = False) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    tot = store.totals()
    prob = store.hits("probable")
    less = store.hits("less_probable")
    cov = store.coverage()
    rate, err_rate = store.recent_rate()
    pending = sum((r["pending"] or 0) for r in cov)
    eta = f"~{pending / rate / 60:.0f} min at the current rate" if rate > 0 else "n/a (no scan running)"
    strategy_text = store.get_meta("strategy_text")
    strategy_yaml = store.get_meta("strategy_yaml")

    md = []
    md.append(f"# UP Bhulekh khatedar search — {'LIVE ' if live else ''}summary report")
    md.append(f"_As of {now}_\n")
    md.append("## Totals\n")
    md.append("| Metric | Value |\n|---|---|")
    md.append(f"| Villages in catalog | {tot['villages']} |")
    md.append(f"| Villages scanned | {tot['done']} ({100.0*tot['done']/max(tot['villages'],1):.1f}%) |")
    md.append(f"| Villages with errors (unscanned) | {tot['errors']} |")
    md.append(f"| Villages skipped (portal has no khatauni, e.g. under chakbandi) | {tot['skipped']} |")
    md.append(f"| Khatedar rows collected | {tot['rows']} |")
    md.append(f"| **Probable hits** | **{tot['probable']}** |")
    md.append(f"| Less probable hits | {tot['less_probable']} |")
    md.append(f"| Extracts downloaded | {tot['extracts']} |")
    if live:
        md.append(f"| Scan rate (last 2 min) | {rate*60:.0f} villages/min, errors {err_rate*60:.1f}/min |")
        md.append(f"| Remaining | {pending} villages, ETA {eta} |")
    md.append("")
    md.append("Targets: T1 साबिर अली s/o भल्लू · T2 सादिक अली s/o साबिर अली · T3 विजय शुक्ला s/o R.S. शुक्ला (Lucknow). "
              "A row is a hit only when both khatedar and father match; father-only matches are excluded by design.\n")
    if strategy_text or strategy_yaml:
        md.append("## Search order used\n")
        if strategy_text:
            md.append(f"> {strategy_text}\n")
        if strategy_yaml:
            md.append("```yaml\n" + strategy_yaml.strip() + "\n```\n")

    def table(hits):
        out = ["| # | Target | District › Tehsil › Village | खाता | खातेदार | पिता | Area | Score | Extract |", "|---|---|---|---|---|---|---|---|---|"]
        for i, h in enumerate(hits, 1):
            ex = f"[pdf]({os.path.relpath(h['pdf_path'], out_dir)})" if h["pdf_path"] else "—"
            out.append(f"| {i} | {h['target']} | {split_label(h['district'])[0]} › {split_label(h['tehsil'])[0]} › {h['village_label']} | "
                       f"{h['khata']} | {h['khatedar']} | {h['father']} | {_fmt_area(h['area'])} | {h['score']:.0f} | {ex} |")
        return "\n".join(out)

    md.append(f"## Probable hits ({len(prob)})\n")
    md.append(table(prob) if prob else "_None yet._")
    md.append("")
    md.append(f"## Less probable hits ({len(less)})\n")
    md.append(table(less) if less else "_None yet._")
    md.append("")
    md.append("## Reasoning per hit\n")
    if not (prob or less):
        md.append("_No hits to explain yet._")
    for h in prob:
        md.append(f"- **PROBABLE** — {_hit_line(h)}\n  - {h['reasoning']}")
    for h in less:
        md.append(f"- **LESS PROBABLE** — {_hit_line(h)}\n  - {h['reasoning']}")
    md.append("")
    md.append("## Where the hits cluster\n")
    c = Counter((split_label(h["district"])[0], split_label(h["tehsil"])[0]) for h in prob + less)
    if c:
        md.append("| District | Tehsil | Hits |\n|---|---|---|")
        for (d, t), n in c.most_common():
            md.append(f"| {d} | {t} | {n} |")
    else:
        md.append("_No clusters yet._")
    md.append("")
    md.append("## Coverage (how much of the state has actually been searched)\n")
    md.append("| District | Villages | Scanned | Errors | Skipped | Pending | % |\n|---|---|---|---|---|---|---|")
    for r in cov:
        md.append(f"| {split_label(r['district'])[0]} | {r['total']} | {r['done'] or 0} | {r['errors'] or 0} | "
                  f"{r['skipped'] or 0} | {r['pending'] or 0} | "
                  f"{100.0*((r['done'] or 0) + (r['skipped'] or 0))/max(r['total'],1):.0f}% |")
    md.append("")
    md.append("_A district with pending or errored villages is not fully searched; 'not found' there is not final._\n")

    md_text = "\n".join(md)
    md_path = os.path.join(out_dir, "summary_live.md" if live else "summary.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_text)
    html_path = md_path[:-3] + ".html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(_to_html(md_text))
    return md_path, html_path


def _to_html(md: str) -> str:
    """Very small Markdown → HTML for our own report (headings, tables, lists, code, bold)."""
    import re
    out = ["<!doctype html><html lang='hi'><head><meta charset='utf-8'><title>Bhulekh summary</title><style>"
           "body{font-family:-apple-system,Segoe UI,Noto Sans Devanagari,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#222}"
           "table{border-collapse:collapse;margin:1rem 0;font-size:14px}th,td{border:1px solid #ccc;padding:4px 8px;text-align:left}"
           "th{background:#f3f3f3}code,pre{background:#f6f8fa;padding:2px 4px}li{margin:.3rem 0}"
           "h2{border-bottom:1px solid #ddd;padding-bottom:.2rem}</style></head><body>"]
    lines = md.split("\n")
    i, in_table, in_code = 0, False, False
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            in_code = not in_code
            out.append("<pre>" if in_code else "</pre>")
            i += 1
            continue
        if in_code:
            out.append(html.escape(ln))
            i += 1
            continue
        if ln.startswith("|"):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                i += 1
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
                out.append("<tr>" + "".join(f"<th>{_inline(c)}</th>" for c in cells) + "</tr>")
            else:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
            i += 1
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        m = re.match(r"^(#+)\s+(.*)$", ln)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
        elif ln.startswith("- "):
            out.append(f"<li>{_inline(ln[2:])}</li>")
        elif ln.startswith("  - "):
            out.append(f"<ul><li>{_inline(ln[4:])}</li></ul>")
        elif ln.startswith("> "):
            out.append(f"<blockquote>{_inline(ln[2:])}</blockquote>")
        elif ln.strip():
            out.append(f"<p>{_inline(ln)}</p>")
        i += 1
    if in_table:
        out.append("</table>")
    out.append("</body></html>")
    return "\n".join(out)


def _inline(s: str) -> str:
    import re
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"_(.+?)_", r"<i>\1</i>", s)
    s = re.sub(r"\[(.+?)\]\((.+?)\)", r"<a href='\2'>\1</a>", s)
    return s
