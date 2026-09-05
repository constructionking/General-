"""Summary report in two forms: summary.md (plain) and summary.html (styled compilation).

Layout of the compilation, in reading order: the bottom line (verdict boxes per target), where we searched
(coverage), one section per target with a plot table per district and area subtotals, what was ruled out
(right name / wrong father), reasoning per hit, method & data confidence, what to confirm next.
"""
from __future__ import annotations

import html
import os
from collections import defaultdict
from datetime import datetime
from typing import Optional

from .store import Store, split_label

CSS = """
:root{--ink:#1a1a1a;--mut:#666;--line:#d5d5d5;--band:#1F4E78;--band2:#2c6cae;
--good:#0b6b3a;--goodbg:#e7f5ec;--goodbd:#9fd4b4;--warn:#5c3d00;--warnbg:#fff6e0;--warnbd:#e5c264;
--bad:#7a1d13;--badbg:#fadbd7;--badbd:#e6a79f;--page:#eef1f4}
*{box-sizing:border-box}
body{font-family:"Noto Sans Devanagari","Nirmala UI","Mangal",system-ui,-apple-system,Arial,sans-serif;color:var(--ink);
margin:0;background:var(--page);padding:28px 18px;line-height:1.55;font-size:14px}
.wrap{max-width:1000px;margin:0 auto}
header.top{background:linear-gradient(135deg,var(--band),var(--band2));color:#fff;border-radius:12px;padding:24px 30px;margin-bottom:8px}
header.top h1{margin:0 0 6px;font-size:24px}header.top .s{opacity:.94}
.stamp{margin-top:12px;font-size:12px;opacity:.85;border-top:1px solid rgba(255,255,255,.25);padding-top:9px}
h2{font-size:19px;color:var(--band);margin:32px 0 12px;padding-bottom:6px;border-bottom:2px solid var(--band)}
h3{font-size:15.5px;margin:20px 0 8px;color:#243b53}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px 22px;margin:14px 0}
.tldr{border:2px solid var(--good);border-radius:10px;background:#fff;padding:18px 24px;margin:14px 0}
.tldr h2{border:0;margin:0 0 10px;font-size:18px;color:var(--good)}
.verdict-row{display:flex;gap:14px;flex-wrap:wrap;margin:14px 0}
.vbox{flex:1;min-width:210px;border-radius:9px;padding:13px 15px;border:1px solid}
.vbox .n{font-size:19px;font-weight:800;line-height:1.15}.vbox .l{font-size:12px;margin-top:4px}
.vgood{background:var(--goodbg);border-color:var(--goodbd);color:var(--good)}
.vwarn{background:var(--warnbg);border-color:var(--warnbd);color:var(--warn)}
.vbad{background:var(--badbg);border-color:var(--badbd);color:var(--bad)}
table{border-collapse:collapse;width:100%;font-size:12.8px;margin:10px 0;background:#fff}
th,td{border:1px solid var(--line);padding:6px 9px;text-align:left;vertical-align:top}
th{background:#eaf0f7;color:#243b53}tr:nth-child(even) td{background:#fafbfc}
td.num,th.num{text-align:right;white-space:nowrap}.tot td{font-weight:800;background:#eef3f8!important}
.pill{display:inline-block;border-radius:11px;padding:1px 9px;font-size:11px;font-weight:700;white-space:nowrap}
.p-good{background:var(--goodbg);color:var(--good);border:1px solid var(--goodbd)}
.p-warn{background:var(--warnbg);color:var(--warn);border:1px solid var(--warnbd)}
.p-bad{background:var(--badbg);color:var(--bad);border:1px solid var(--badbd)}
.note{font-size:11.5px;color:var(--mut)}ul{margin:8px 0 8px 20px;padding:0}li{margin:4px 0}
.callout{border-radius:8px;padding:12px 16px;margin:12px 0;font-size:13px}
.c-good{background:var(--goodbg);border:1px solid var(--goodbd);color:var(--good)}
.c-warn{background:var(--warnbg);border:1px solid var(--warnbd);color:var(--warn)}
.scroll{overflow-x:auto}
"""


def _area(a) -> Optional[float]:
    return float(a) if isinstance(a, (int, float)) else None


def _fmt(a: Optional[float]) -> str:
    return f"{a:.3f}" if a is not None else "—"


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


class ReportData:
    """Everything both renderers need, computed once from the store."""

    def __init__(self, store: Store, cfg_targets: list[dict], live: bool):
        self.now = datetime.now().strftime("%d %B %Y, %H:%M")
        self.live = live
        self.tot = store.totals()
        self.cov = list(store.coverage())
        self.rate, self.err_rate = store.recent_rate()
        self.pending = sum((r["pending"] or 0) for r in self.cov)
        self.targets = {t["id"]: t for t in cfg_targets}
        allhits = store.hits()
        self.family = [h for h in allhits if h["category"] in ("probable", "less_probable")]
        self.misses = [h for h in allhits if h["category"] == "near_miss"]
        self.strategy_text = store.get_meta("strategy_text")
        # per target → district → rows
        self.by_target: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for h in self.family:
            self.by_target[h["target"]][split_label(h["district"])[0]].append(h)

    def target_summary(self, tid: str) -> dict:
        rows = [h for h in self.family if h["target"] == tid]
        prob = [h for h in rows if h["category"] == "probable"]
        area = sum(_area(h["area"]) or 0.0 for h in rows)
        return {"plots": len(rows), "probable": len(prob), "villages": len({h["village_code"] for h in rows}),
                "districts": sorted({split_label(h["district"])[0] for h in rows}), "area": area}

    def coverage_pct(self) -> float:
        return 100.0 * (self.tot["done"] + self.tot["skipped"]) / max(self.tot["villages"], 1)

    def miss_groups(self) -> list[tuple]:
        """(target, khatedar, father, count, districts) for the ruled-out table, most frequent first."""
        c: dict = defaultdict(lambda: [0, set(), ""])
        for h in self.misses:
            k = (h["target"], h["khatedar"], h["father"])
            c[k][0] += 1
            c[k][1].add(split_label(h["district"])[0])
            c[k][2] = h["reasoning"]
        out = [(t, k, f, n, sorted(d), why) for (t, k, f), (n, d, why) in c.items()]
        return sorted(out, key=lambda x: -x[3])


def build_report(store: Store, out_dir: str, live: bool = False, cfg: Optional[dict] = None) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    data = ReportData(store, (cfg or {}).get("targets", []), live)
    md_path = os.path.join(out_dir, "summary_live.md" if live else "summary.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(data, out_dir))
    html_path = md_path[:-3] + ".html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(data, out_dir))
    return md_path, html_path


# ---- markdown -----------------------------------------------------------------
def render_markdown(d: ReportData, out_dir: str) -> str:
    md = [f"# UP Bhulekh khatedar search — {'LIVE ' if d.live else ''}summary", f"_As of {d.now}_\n", "## The bottom line\n"]
    for tid, t in d.targets.items():
        s = d.target_summary(tid)
        if s["plots"]:
            md.append(f"- **{tid} {t.get('label', '')}**: {s['plots']} plot(s) in {s['villages']} village(s), "
                      f"{s['probable']} probable, {_fmt(s['area'])} ha plot total — {', '.join(s['districts'])}")
        else:
            md.append(f"- **{tid} {t.get('label', '')}**: nothing found yet")
    md.append(f"- Coverage: {d.tot['done']} of {d.tot['villages']} villages scanned "
              f"({d.coverage_pct():.0f}% incl. {d.tot['skipped']} skipped), {d.tot['errors']} errors, "
              f"{d.tot['near_miss']} near-misses ruled out\n")
    if d.live:
        eta = f"~{d.pending / d.rate / 60:.0f} min" if d.rate > 0 else "n/a"
        md.append(f"_Scan rate {d.rate*60:.0f} villages/min, errors {d.err_rate*60:.1f}/min, {d.pending} pending, ETA {eta}_\n")
    md.append("## Where we searched\n")
    md.append("| District | Villages | Scanned | Errors | Skipped | Pending | % |\n|---|---|---|---|---|---|---|")
    for r in d.cov:
        md.append(f"| {split_label(r['district'])[0]} | {r['total']} | {r['done'] or 0} | {r['errors'] or 0} | "
                  f"{r['skipped'] or 0} | {r['pending'] or 0} | "
                  f"{100.0*((r['done'] or 0) + (r['skipped'] or 0))/max(r['total'],1):.0f}% |")
    md.append("\n_A district with pending or errored villages is not fully searched; 'not found' there is not final._\n")
    for tid, t in d.targets.items():
        md.append(f"## {tid} · {t.get('label', '')}\n")
        if not d.by_target.get(tid):
            md.append("_No record with this khatedar AND this father yet._\n")
            continue
        for district, rows in d.by_target[tid].items():
            md.append(f"### {district}\n")
            md.append("| Tehsil | Village | Code | खाता | खातेदार | पिता | Area (ha) | Score | Category | Extract |\n|---|---|---|---|---|---|---|---|---|---|")
            tot = 0.0
            for h in sorted(rows, key=lambda x: (x["tehsil"], x["village_label"], x["khata"])):
                a = _area(h["area"])
                tot += a or 0.0
                ex = f"[pdf]({os.path.relpath(h['pdf_path'], out_dir)})" if h["pdf_path"] else "—"
                md.append(f"| {split_label(h['tehsil'])[0]} | {split_label(h['village_label'].rsplit(' - ', 1)[0])[0]} | {h['village_code']} | "
                          f"{h['khata']} | {h['khatedar']} | {h['father']} | {_fmt(a)} | {h['score']:.0f} | {h['category']} | {ex} |")
            md.append(f"| **{district} — {len(rows)} plot(s)** | | | | | | **{_fmt(tot)}** | | | |\n")
    md.append("## Ruled out — right name, wrong father\n")
    groups = d.miss_groups()
    if groups:
        md.append("| Target | खातेदार | पिता | Records | Districts | Why excluded |\n|---|---|---|---|---|---|")
        for t, k, f, n, ds, why in groups[:60]:
            md.append(f"| {t} | {k} | {f} | {n} | {', '.join(ds)} | {why} |")
        if len(groups) > 60:
            md.append(f"\n_… and {len(groups) - 60} more groups (see hits.xlsx, category near_miss)._")
    else:
        md.append("_None recorded._")
    md.append("\n## Reasoning per hit\n")
    for h in d.family:
        md.append(f"- **{h['category'].upper()}** {h['target']} · {split_label(h['district'])[0]} › {split_label(h['tehsil'])[0]} › "
                  f"{h['village_label']} · खाता {h['khata']} · {h['khatedar']} / {h['father']}\n  - {h['reasoning']}")
    if not d.family:
        md.append("_No hits to explain yet._")
    md.append("\n## Method & data confidence\n")
    md += _method_lines()
    md.append("\n## What to confirm next\n")
    md += [f"- {x}" for x in _next_steps(d)]
    return "\n".join(md) + "\n"


def _method_lines() -> list[str]:
    return [
        "- Every village of the selected districts is searched on the portal's खातेदार-name tab with the configured "
        "prefixes; rows are read from the portal's own decrypted response, so nothing is retyped.",
        "- **Father-match rule.** A row counts only when both the khatedar and the father match a target; the given "
        "name must match on a token boundary (साबिरा is not साबिर) and a surname line the family never uses "
        "(खां, शाह, हुसैन, जैदी, सिंह …) rejects the row. Right-name / wrong-father rows are logged as near-misses.",
        "- **Session guard.** The portal's search session dies silently; an empty result with no 'No Data' dialog is "
        "treated as a dead session and the tab reloads and repeats the village. Tabs also reload every 150 villages.",
        "- **Categories.** *Probable*: both names ≥ 90 % with no conflicting tokens (and in the expected district where "
        "one is configured). *Less probable*: a spelling variant, a conflicting token, or an isolated hit.",
        "- Areas are plot totals from the khatauni list; a co-owner's share can be smaller. Extracts are unofficial "
        "(अप्रमाणित प्रति); certified copies come from e-District / the Tehsil computer centre.",
    ]


def _next_steps(d: ReportData) -> list[str]:
    out = []
    prob = [h for h in d.family if h["category"] == "probable" and not h["pdf_path"]]
    if prob:
        out.append(f"Pull the khatauni extract for the {len(prob)} probable hit(s) without one yet "
                   f"(`bhulekh download --only-probable`) and read residence, co-owners and the वरासत (succession) entry.")
    less = [h for h in d.family if h["category"] == "less_probable"]
    if less:
        out.append(f"Review the {len(less)} less-probable hit(s): the reasoning column says which signal was weak.")
    unfinished = [split_label(r["district"])[0] for r in d.cov if (r["pending"] or 0) + (r["errors"] or 0) > 0]
    if unfinished:
        out.append("Finish scanning: " + ", ".join(unfinished[:12]) + (" …" if len(unfinished) > 12 else "") +
                   " still have pending or errored villages (`bhulekh scan --reset-errors`).")
    if d.misses:
        out.append("Skim the ruled-out table for a father spelling the config does not know yet; add it to "
                   "config.yaml and run `bhulekh rematch` (no rescan needed).")
    if not out:
        out.append("Nothing outstanding: all selected districts scanned and every probable hit has its extract.")
    return out


# ---- html ---------------------------------------------------------------------
def render_html(d: ReportData, out_dir: str) -> str:
    o = [f"<!doctype html><html lang='hi'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
         f"<title>Bhulekh search — {'live ' if d.live else ''}summary</title><style>{CSS}</style></head><body><div class='wrap'>"]
    labels = " · ".join(f"<b>{_esc(t.get('label', tid))}</b>" for tid, t in d.targets.items())
    o.append(f"<header class='top'><h1>Ancestral land search — {'live ' if d.live else ''}compilation</h1>"
             f"<div class='s'>Targets: {labels}</div>"
             f"<div class='stamp'>Source: UP Bhulekh (upbhulekh.gov.in) · compiled {_esc(d.now)} · Match rule: a record counts only "
             f"when <b>both</b> the khatedar and the <b>father's name</b> match; the father is the anchor.</div></header>")
    # bottom line
    o.append("<div class='tldr'><h2>The bottom line</h2><div class='verdict-row'>")
    for tid, t in d.targets.items():
        s = d.target_summary(tid)
        if s["probable"]:
            cls, n = "vgood", f"{s['probable']} probable · {s['plots']} plot(s)"
        elif s["plots"]:
            cls, n = "vwarn", f"{s['plots']} less-probable plot(s)"
        else:
            cls, n = "vbad", "nothing found yet"
        where = ", ".join(s["districts"]) if s["districts"] else "—"
        o.append(f"<div class='vbox {cls}'><div class='n'>{_esc(n)}</div><div class='l'><b>{_esc(tid)}</b> {_esc(t.get('label', ''))}"
                 f"<br>{_esc(where)} · {s['villages']} village(s) · {_fmt(s['area'])} ha plot total</div></div>")
    cov_cls = "vgood" if d.coverage_pct() >= 99.5 else "vwarn"
    o.append(f"<div class='vbox {cov_cls}'><div class='n'>{d.coverage_pct():.0f}% searched</div><div class='l'>"
             f"{d.tot['done']} of {d.tot['villages']} villages scanned · {d.tot['skipped']} skipped (no khatauni) · "
             f"{d.tot['errors']} errors · {d.tot['near_miss']} near-misses ruled out</div></div>")
    o.append("</div>")
    if d.live:
        eta = f"~{d.pending / d.rate / 60:.0f} min" if d.rate > 0 else "n/a"
        o.append(f"<p class='note'>Live: {d.rate*60:.0f} villages/min, errors {d.err_rate*60:.1f}/min, {d.pending} pending, ETA {eta}.</p>")
    o.append("</div>")
    # coverage
    o.append("<h2>1 · Where we searched</h2><div class='scroll'><table><tr><th>District</th><th class='num'>Villages</th>"
             "<th class='num'>Scanned</th><th class='num'>Errors</th><th class='num'>Skipped</th><th class='num'>Pending</th><th>Status</th></tr>")
    for r in d.cov:
        done, err, sk, pend = r["done"] or 0, r["errors"] or 0, r["skipped"] or 0, r["pending"] or 0
        pct = 100.0 * (done + sk) / max(r["total"], 1)
        pill = ("<span class='pill p-good'>COMPLETE</span>" if pct >= 99.5 else
                f"<span class='pill p-warn'>{pct:.0f}%</span>" if done else "<span class='pill p-bad'>NOT STARTED</span>")
        o.append(f"<tr><td><b>{_esc(split_label(r['district'])[0])}</b></td><td class='num'>{r['total']}</td><td class='num'>{done}</td>"
                 f"<td class='num'>{err}</td><td class='num'>{sk}</td><td class='num'>{pend}</td><td>{pill}</td></tr>")
    o.append("</table></div><p class='note'>A district with pending or errored villages is not fully searched; "
             "'not found' there is not final.</p>")
    # per target
    n = 2
    for tid, t in d.targets.items():
        o.append(f"<h2>{n} · {_esc(tid)} — {_esc(t.get('label', ''))}</h2>")
        n += 1
        if not d.by_target.get(tid):
            o.append("<div class='callout c-warn'>No record with this khatedar <b>and</b> this father yet.</div>")
            continue
        s = d.target_summary(tid)
        o.append(f"<div class='callout c-good'><b>{s['plots']} plot(s)</b> in {s['villages']} village(s) across "
                 f"{_esc(', '.join(s['districts']))}; {s['probable']} probable. Plot total {_fmt(s['area'])} ha "
                 f"(a co-owner's share can be smaller).</div>")
        for district, rows in d.by_target[tid].items():
            o.append(f"<h3>{_esc(district)}</h3><div class='scroll'><table><tr><th>Tehsil</th><th>Village</th><th>Code</th><th>खाता</th>"
                     "<th>खातेदार</th><th>पिता</th><th class='num'>Area (ha)</th><th class='num'>Score</th><th>Category</th><th>Extract</th></tr>")
            tot = 0.0
            for h in sorted(rows, key=lambda x: (x["tehsil"], x["village_label"], x["khata"])):
                a = _area(h["area"])
                tot += a or 0.0
                pill = "<span class='pill p-good'>PROBABLE</span>" if h["category"] == "probable" else "<span class='pill p-warn'>LESS PROBABLE</span>"
                ex = (f"<a href='{_esc(os.path.relpath(h['pdf_path'], out_dir))}'>pdf</a>" if h["pdf_path"]
                      else "<span class='pill p-warn'>name-search</span>")
                vname = split_label(h["village_label"].rsplit(" - ", 1)[0])[0]
                o.append(f"<tr><td>{_esc(split_label(h['tehsil'])[0])}</td><td>{_esc(vname)}</td><td>{_esc(h['village_code'])}</td>"
                         f"<td>{_esc(h['khata'])}</td><td>{_esc(h['khatedar'])}</td><td>{_esc(h['father'])}</td>"
                         f"<td class='num'>{_fmt(a)}</td><td class='num'>{h['score']:.0f}</td><td>{pill}</td><td>{ex}</td></tr>")
            o.append(f"<tr class='tot'><td colspan='6'>{_esc(district)} — {len(rows)} plot(s)</td><td class='num'>{_fmt(tot)}</td><td colspan='3'></td></tr>")
            o.append("</table></div>")
    # ruled out
    o.append(f"<h2>{n} · Ruled out — right name, wrong father</h2>")
    n += 1
    groups = d.miss_groups()
    if groups:
        o.append("<p>These rows carry a matching given name but fail the father rule or belong to another surname line. "
                 "They are kept for audit only and never counted as family land.</p>")
        o.append("<div class='scroll'><table><tr><th>Target</th><th>खातेदार</th><th>पिता</th><th class='num'>Records</th><th>Districts</th><th>Why excluded</th></tr>")
        for t, k, f, cnt, ds, why in groups[:60]:
            o.append(f"<tr><td>{_esc(t)}</td><td>{_esc(k)}</td><td>{_esc(f)}</td><td class='num'>{cnt}</td>"
                     f"<td>{_esc(', '.join(ds))}</td><td class='note'>{_esc(why)}</td></tr>")
        o.append("</table></div>")
        if len(groups) > 60:
            o.append(f"<p class='note'>… and {len(groups) - 60} more groups (hits.xlsx, category near_miss).</p>")
    else:
        o.append("<p class='note'>None recorded.</p>")
    # reasoning
    o.append(f"<h2>{n} · Reasoning per hit</h2>")
    n += 1
    if d.family:
        o.append("<ul>")
        for h in d.family:
            pill = "<span class='pill p-good'>PROBABLE</span>" if h["category"] == "probable" else "<span class='pill p-warn'>LESS PROBABLE</span>"
            o.append(f"<li>{pill} <b>{_esc(h['target'])}</b> · {_esc(split_label(h['district'])[0])} › {_esc(split_label(h['tehsil'])[0])} › "
                     f"{_esc(h['village_label'])} · खाता {_esc(h['khata'])} · {_esc(h['khatedar'])} / {_esc(h['father'])}"
                     f"<br><span class='note'>{_esc(h['reasoning'])}</span></li>")
        o.append("</ul>")
    else:
        o.append("<p class='note'>No hits to explain yet.</p>")
    # method + next
    o.append(f"<h2>{n} · Method &amp; data confidence</h2><div class='card'><ul>")
    n += 1
    for ln in _method_lines():
        o.append("<li>" + _md_inline(ln[2:]) + "</li>")
    o.append("</ul></div>")
    o.append(f"<h2>{n} · What to confirm next</h2><ul>")
    for ln in _next_steps(d):
        o.append("<li>" + _md_inline(ln) + "</li>")
    o.append("</ul>")
    if d.strategy_text:
        o.append(f"<p class='note'>Search order used: {_esc(d.strategy_text)}</p>")
    o.append(f"<p class='note' style='text-align:center;margin-top:22px;border-top:1px solid var(--line);padding-top:14px'>"
             f"Compiled from UP Bhulekh (upbhulekh.gov.in), {_esc(d.now)}. Unofficial extracts for information only; "
             f"the original Hindi records are authoritative. Areas in hectares are plot totals.</p>")
    o.append("</div></body></html>")
    return "\n".join(o)


def _md_inline(s: str) -> str:
    import re
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"\*(.+?)\*", r"<i>\1</i>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return s
