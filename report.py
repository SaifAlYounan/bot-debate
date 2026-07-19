"""argbench report rendering.

Everything here is computed from the run directory on disk alone
(calls.jsonl, run_meta.json, config_resolved.yaml, judge/*, */final.txt),
so any report can be re-rendered and audited after the fact:

    python3 report.py runs/<timestamp>            # re-render report.html
    python3 report.py --summary runs <dir> <dir>  # re-render summary.html
"""

import html
import json
import os
import sys

import yaml

ARMS = ["arm1", "arm2", "arm3", "arm4"]
ARM_TITLES = {
    "arm1": "ARM 1 — DEBATE (shared transcript)",
    "arm2": "ARM 2 — EXAM + EXAMINER (isolated)",
    "arm3": "ARM 3 — SOLO CONTROL",
    "arm4": "ARM 4 — PERSONA CONTROL (single model, exam topology)",
}
ALL_LETTERS = {"A", "B", "C", "D"}


# ---------------------------------------------------------------------------
# metrics from disk
# ---------------------------------------------------------------------------

def _read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _read_text(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def load_calls(run_dir):
    calls = []
    path = os.path.join(run_dir, "calls.jsonl")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    calls.append(json.loads(line))
    return calls


def compute_metrics(run_dir):
    """All scoreboard numbers, recomputed from raw on-disk records."""
    calls = load_calls(run_dir)
    meta = _read_json(os.path.join(run_dir, "run_meta.json"), {})
    cfg = yaml.safe_load(
        _read_text(os.path.join(run_dir, "config_resolved.yaml"), "") or "{}")
    judge = _read_json(os.path.join(run_dir, "judge", "judge.json"))
    blind = _read_json(os.path.join(run_dir, "judge", "blind_mapping.json"))

    walls = meta.get("phase_walls_s", {})
    arm_wall = {
        # paired design: the shared GEN phase is attributed to arms 1 AND 2
        "arm1": walls.get("gen", 0) + walls.get("arm1_rounds", 0)
                + walls.get("arm1_synth", 0),
        "arm2": walls.get("gen", 0) + walls.get("arm2_synth", 0)
                + walls.get("arm2_critic", 0),
        "arm3": walls.get("arm3", 0),
        "arm4": walls.get("arm4_gen", 0) + walls.get("arm4_synth", 0)
                + walls.get("arm4_critic", 0),
    }
    arm_rows = {
        "arm1": [c for c in calls if c["arm"] in ("gen", "arm1")],
        "arm2": [c for c in calls if c["arm"] in ("gen", "arm2")],
        "arm3": [c for c in calls if c["arm"] == "arm3"],
        "arm4": [c for c in calls if c["arm"] == "arm4"],
    }
    judge_rows = [c for c in calls if c["arm"] == "judge"]

    m = {"run_dir": run_dir, "meta": meta, "cfg": cfg, "judge": judge,
         "blind": blind, "arms": {}}
    letter_of = {}
    if blind:
        letter_of = {arm: letter for letter, arm in blind.items()}
    union = judge["union"] if judge else []
    m["union"] = union
    m["core_ids"] = [u["id"] for u in union
                     if set(u["found_in"]) == ALL_LETTERS]

    for arm in ARMS:
        rows = arm_rows[arm]
        in_tok = sum(c["input_tokens"] for c in rows
                     if c["input_tokens"] is not None)
        out_tok = sum(c["output_tokens"] for c in rows
                      if c["output_tokens"] is not None)
        null_tok = any(c["input_tokens"] is None or c["output_tokens"] is None
                       for c in rows if not c["failed"])
        cost = sum(c["cost_usd"] for c in rows if c["cost_usd"] is not None)
        cost_missing = any(c["cost_usd"] is None for c in rows
                           if not c["failed"])
        entry = {
            "input_tokens": in_tok, "output_tokens": out_tok,
            "tokens_flagged": null_tok,
            "cost_usd": round(cost, 4), "cost_missing": cost_missing,
            "wall_s": round(arm_wall[arm], 1),
            "calls": len(rows),
            "failed": sum(1 for c in rows if c["failed"]),
            "letter": letter_of.get(arm),
            "distinct": None, "unique": None, "suspect": None,
            "depth": None, "coverage_pct": None, "notes": None,
            "efficiency": None,
        }
        if judge and entry["letter"]:
            ps = judge["per_system"][entry["letter"]]
            entry.update(distinct=ps["distinct"], unique=ps["unique"],
                         suspect=ps["suspect"], depth=ps["depth"],
                         notes=ps.get("notes", ""))
            if ps.get("coverage_pct") is not None:
                # panel aggregate: median of per-judge coverage
                entry["coverage_pct"] = ps["coverage_pct"]
            elif union:
                entry["coverage_pct"] = round(
                    100.0 * ps["distinct"] / len(union), 1)
            total = in_tok + out_tok
            entry["total_tokens"] = total if not null_tok else None
            if total > 0 and not null_tok:
                entry["efficiency"] = round(
                    (ps["distinct"] - ps["suspect"]) / (total / 10000.0), 1)
        m["arms"][arm] = entry

    m["judge_cost_usd"] = round(
        sum(c["cost_usd"] for c in judge_rows if c["cost_usd"] is not None), 4)
    m["judge_tokens"] = (
        sum(c["input_tokens"] or 0 for c in judge_rows),
        sum(c["output_tokens"] or 0 for c in judge_rows))
    m["total_cost_usd"] = round(
        sum(c["cost_usd"] for c in calls if c["cost_usd"] is not None), 4)
    m["judging_failed"] = judge is None

    # panel disagreement: does the headline winner depend on the judge?
    m["n_judges"] = (judge or {}).get("n_judges", 1 if judge else 0)
    m["headline_winners_by_judge"] = []
    if judge and judge.get("judges") and letter_of:
        for j in judge["judges"]:
            effs = {}
            for arm in ARMS:
                letter = m["arms"][arm]["letter"]
                total = m["arms"][arm].get("total_tokens")
                if letter and total:
                    ps = j["data"]["per_system"][letter]
                    effs[arm] = (ps["distinct"] - ps["suspect"]) \
                        / (total / 10000.0)
            if effs:
                best = max(effs.values())
                m["headline_winners_by_judge"].append(
                    sorted(a for a, v in effs.items() if v == best))
    winners = {tuple(w) for w in m["headline_winners_by_judge"]}
    m["judges_disagree"] = len(winners) > 1
    return m


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = """
:root { --paper:#f6f2e8; --ink:#221d16; --rule:#b9ad97; --accent:#7a1f1f;
        --dim:#6d6353; --band:#ebe3d0; }
* { box-sizing: border-box; }
body { background: var(--paper); color: var(--ink);
  font-family: Georgia, 'Times New Roman', serif;
  max-width: 66rem; margin: 0 auto; padding: 2rem 1.5rem 4rem;
  line-height: 1.45; }
h1 { font-size: 1.6rem; margin: 0 0 .2rem; }
h2 { font-size: 1.1rem; letter-spacing: .06em; text-transform: uppercase;
  border-bottom: 1px solid var(--rule); padding-bottom: .25rem;
  margin: 2.2rem 0 .8rem; }
.mono, td.num, .id { font-family: ui-monospace, 'Courier New', monospace; }
.meta { color: var(--dim); font-size: .9rem; }
table { border-collapse: collapse; width: 100%; margin: .6rem 0; }
th, td { border: 1px solid var(--rule); padding: .3rem .55rem;
  text-align: left; font-size: .92rem; vertical-align: top; }
th { background: var(--band); font-weight: 600; }
td.num { text-align: right; white-space: nowrap; }
td.win { color: var(--accent); font-weight: 700; }
tr.headline td, tr.headline th { font-weight: 700; border-top: 2px solid var(--ink); }
.verdict { border: 2px solid var(--ink); background: #fffdf6;
  padding: .9rem 1.1rem; margin: 1rem 0; }
.verdict .tag { font-size: .75rem; letter-spacing: .12em;
  text-transform: uppercase; color: var(--dim); }
.failedbox { border: 2px solid var(--accent); color: var(--accent);
  padding: .9rem 1.1rem; margin: 1rem 0; font-weight: 700; }
.bars { margin: .8rem 0; }
.barrow { display: flex; align-items: center; margin: .3rem 0; }
.barlabel { width: 14rem; font-size: .85rem; flex: none; }
.bartrack { flex: 1; border: 1px solid var(--rule); background: #fffdf6;
  height: 1.15rem; }
.barfill { background: var(--accent); height: 100%; }
.barval { width: 4.5rem; text-align: right; flex: none;
  font-family: ui-monospace, monospace; font-size: .9rem; padding-left: .5rem; }
tr.core td { background: var(--band); }
td.found { text-align: center; }
td.missed { text-align: center; color: var(--dim); }
td.uniq { color: var(--accent); font-weight: 700; text-align: center; }
.cards { display: flex; gap: 1rem; flex-wrap: wrap; }
.card { border: 1px solid var(--rule); background: #fffdf6;
  padding: .7rem .9rem; flex: 1 1 18rem; font-size: .9rem; }
.card h3 { margin: 0 0 .4rem; font-size: .95rem; }
details { border: 1px solid var(--rule); background: #fffdf6;
  margin: .6rem 0; }
details summary { cursor: pointer; padding: .45rem .7rem; font-weight: 600;
  background: var(--band); }
details pre { margin: 0; padding: .8rem; white-space: pre-wrap;
  font-size: .8rem; font-family: ui-monospace, 'Courier New', monospace; }
.foot { margin-top: 2.5rem; border-top: 2px solid var(--ink);
  padding-top: .8rem; font-size: .85rem; color: var(--dim); }
.foot li { margin: .25rem 0; }
a { color: var(--ink); }
@media print { details[open] summary ~ * { display: block; }
  body { max-width: none; } }
"""


def esc(x):
    return html.escape("" if x is None else str(x))


def _fmt(v, kind=""):
    if v is None:
        return "—"
    if kind == "usd":
        return "$%.4f" % v
    if kind == "pct":
        return "%.1f%%" % v
    if kind == "int":
        # panel medians over an even judge count can be fractional
        if isinstance(v, float) and v != int(v):
            return "%.1f" % v
        return "{:,}".format(int(v))
    return str(v)


def scoreboard_html(m):
    a = m["arms"]
    rows = [
        # (label, key, kind, higher_wins, headline)
        ("Distinct arguments", "distinct", "int", True, False),
        ("Unique to system", "unique", "int", True, False),
        ("Coverage of union", "coverage_pct", "pct", True, False),
        ("Suspect entries", "suspect", "int", False, False),
        ("Depth (1–5)", "depth", "", True, False),
        ("Input tokens (exact)", "input_tokens", "int", False, False),
        ("Output tokens (exact)", "output_tokens", "int", False, False),
        ("Cost USD (prices table)", "cost_usd", "usd", False, False),
        ("Wall-clock seconds", "wall_s", "", False, False),
        ("Calls", "calls", "int", False, False),
        ("Grounded args per 10k tokens", "efficiency", "", True, True),
    ]
    out = ["<table><tr><th></th>"]
    for arm in ARMS:
        letter = a[arm]["letter"]
        out.append("<th>%s%s</th>" % (
            esc(ARM_TITLES[arm]),
            (" <span class='mono'>[SYSTEM-%s]</span>" % letter)
            if letter else ""))
    out.append("</tr>")
    for label, key, kind, higher, headline in rows:
        vals = {arm: a[arm][key] for arm in ARMS}
        present = [v for v in vals.values() if v is not None]
        best = (max(present) if higher else min(present)) if present else None
        out.append("<tr%s><th>%s</th>" % (
            " class='headline'" if headline else "", esc(label)))
        for arm in ARMS:
            v = vals[arm]
            win = (best is not None and v is not None and v == best
                   and len(present) > 1)
            flag = ""
            if key in ("input_tokens", "output_tokens") \
                    and a[arm]["tokens_flagged"]:
                flag = " *"
            if key == "cost_usd" and a[arm]["cost_missing"]:
                flag = " *"
            out.append("<td class='num%s'>%s%s%s</td>" % (
                " win" if win else "",
                "◆ " if win else "", _fmt(v, kind), flag))
        out.append("</tr>")
    out.append("</table>")
    flagged = [arm for arm in ARMS
               if a[arm]["tokens_flagged"] or a[arm]["cost_missing"]]
    if flagged:
        out.append("<p class='meta'>* a provider omitted usage for at least "
                   "one call in this arm, or a model had no price entry: the "
                   "affected tokens/cost are understated, never estimated.</p>")
    out.append("<p class='meta'>◆ marks the winning cell per row (higher "
               "wins for distinct, unique, coverage, depth and the headline; "
               "lower wins for suspect, tokens, cost, seconds, calls). The "
               "shared round-1 GEN phase is counted into arms 1 AND 2 "
               "(paired design). Judge overhead ($%s) is not attributed to "
               "any arm.</p>" % _fmt(m["judge_cost_usd"], "usd").lstrip("$"))
    return "".join(out)


def bars_html(m):
    a = m["arms"]
    effs = {arm: a[arm]["efficiency"] for arm in ARMS}
    present = [v for v in effs.values() if v is not None]
    if not present:
        return "<p class='meta'>headline metric unavailable (judging failed "\
               "or token counts incomplete).</p>"
    top = max(max(present), 0.001)
    out = ["<div class='bars'>"]
    for arm in ARMS:
        v = effs[arm]
        w = 0 if v is None or v < 0 else 100.0 * v / top
        out.append(
            "<div class='barrow'><div class='barlabel'>%s</div>"
            "<div class='bartrack'><div class='barfill' style='width:%.1f%%'>"
            "</div></div><div class='barval'>%s</div></div>"
            % (esc(ARM_TITLES[arm]), w, _fmt(v)))
    out.append("</div><p class='meta'>EFFICIENCY = (distinct − suspect) per "
               "10,000 total tokens.</p>")
    return "".join(out)


def matrix_html(m):
    if not m["union"]:
        return "<p class='meta'>no union available (judging failed).</p>"
    letter_to_arm = {v: k for k, v in (m["blind"] or {}).items()}
    cols = [(m["arms"][arm]["letter"], arm) for arm in ARMS]
    by_theme = {}
    order = []
    for u in m["union"]:
        t = u.get("theme", "untitled")
        if t not in by_theme:
            by_theme[t] = []
            order.append(t)
        by_theme[t].append(u)
    out = ["<table><tr><th>ID</th><th>Theme</th><th>Side</th>"
           "<th>Short claim</th>"]
    for letter, arm in cols:
        out.append("<th>Arm %s<br><span class='mono'>SYSTEM-%s</span></th>"
                   % (arm[-1], esc(letter)))
    out.append("</tr>")
    for theme in order:
        for u in by_theme[theme]:
            fi = set(u["found_in"])
            core = fi == ALL_LETTERS
            uniq = len(fi) == 1
            out.append("<tr%s>" % (" class='core'" if core else ""))
            out.append("<td class='id'>%s%s</td><td>%s</td><td>%s</td>"
                       "<td>%s</td>"
                       % (esc(u["id"]), " · CORE" if core else "",
                          esc(theme), esc(u["side"]),
                          esc(u["short_claim"])))
            for letter, arm in cols:
                if letter in fi:
                    klass = "uniq" if uniq else "found"
                    mark = "● unique" if uniq else "●"
                    out.append("<td class='%s'>%s</td>" % (klass, mark))
                else:
                    out.append("<td class='missed'>—</td>")
            out.append("</tr>")
    out.append("</table>")
    out.append("<p class='meta'>banded rows are CORE (found by all four "
               "systems: %d of %d). Accented ● marks a find unique to one "
               "system.</p>" % (len(m["core_ids"]), len(m["union"])))
    return "".join(out)


def cards_html(m):
    if m["judging_failed"]:
        return "<p class='meta'>no judge notes (judging failed).</p>"
    out = ["<div class='cards'>"]
    for arm in ARMS:
        e = m["arms"][arm]
        out.append(
            "<div class='card'><h3>%s <span class='mono'>[SYSTEM-%s]</span>"
            "</h3><p class='mono'>depth %s/5 · %s suspect · %s distinct · "
            "%s unique</p><p>%s</p></div>"
            % (esc(ARM_TITLES[arm]), esc(e["letter"]), _fmt(e["depth"]),
               _fmt(e["suspect"]), _fmt(e["distinct"]), _fmt(e["unique"]),
               esc(e["notes"])))
    out.append("</div>")
    return "".join(out)


def _judge_line(jd):
    """Config `judge` may be a single mapping or a panel list."""
    entries = jd if isinstance(jd, list) else [jd]
    return " + ".join("%s/%s" % (e.get("provider"), e.get("model"))
                      for e in entries)


def judges_html(m):
    """Per-judge metrics, verdicts and panel spread. Renders for both the
    panel aggregate and legacy single-judge judge.json files."""
    judge = m["judge"]
    if not judge:
        return "<p class='meta'>no judge output (judging failed).</p>"
    entries = judge.get("judges")
    if not entries:  # legacy single-judge file
        return ("<p class='meta'>single judge (legacy run format); "
                "scoreboard values are that judge's, audited as usual.</p>")
    cols = [(m["arms"][arm]["letter"], arm) for arm in ARMS]
    out = []
    for i, j in enumerate(entries, 1):
        ps = j["data"]["per_system"]
        out.append("<h3>Judge %d — <span class='mono'>%s/%s</span></h3>"
                   % (i, esc(j.get("provider")), esc(j.get("model"))))
        out.append("<table><tr><th></th>")
        for letter, arm in cols:
            out.append("<th>Arm %s <span class='mono'>[%s]</span></th>"
                       % (esc(arm[-1]), esc(letter)))
        out.append("</tr>")
        for key, label in (("distinct", "Distinct"), ("unique", "Unique"),
                           ("suspect", "Suspect (verified)"),
                           ("suspect_claimed", "Suspect (judge claimed)"),
                           ("depth", "Depth")):
            out.append("<tr><th>%s</th>" % esc(label))
            for letter, arm in cols:
                out.append("<td class='num'>%s</td>"
                           % _fmt(ps[letter].get(key), "int"))
            out.append("</tr>")
        out.append("</table>")
        out.append("<p class='meta'><b>Verdict:</b> %s</p>"
                   % esc(j["data"].get("verdict", "")))
        dropped = [
            "%s/%s" % (letter, item.get("id"))
            for letter, arm in cols
            for item in ps[letter].get("suspect_entries", [])
            if item.get("verified") is False]
        if dropped:
            out.append("<p class='meta'>Dropped suspect flags (quote not "
                       "found verbatim in the list): %s</p>"
                       % esc(", ".join(dropped)))
    if len(entries) > 1 and judge.get("spread"):
        out.append("<h3>Panel spread (min–max across judges)</h3>"
                   "<table><tr><th></th>")
        for letter, arm in cols:
            out.append("<th>Arm %s <span class='mono'>[%s]</span></th>"
                       % (esc(arm[-1]), esc(letter)))
        out.append("</tr>")
        for key in ("distinct", "unique", "suspect", "depth"):
            out.append("<tr><th>%s</th>" % esc(key))
            for letter, arm in cols:
                lo, hi = judge["spread"][letter][key]
                out.append("<td class='num'>%s–%s</td>"
                           % (_fmt(lo, "int"), _fmt(hi, "int")))
            out.append("</tr>")
        out.append("</table>")
        agree = ("judges DISAGREE on the headline winner — treat the "
                 "headline as unresolved" if m["judges_disagree"] else
                 "all judges agree on the headline winner")
        out.append("<p class='meta'>Scoreboard values are medians across "
                   "the panel; %s.</p>" % agree)
    return "".join(out)


def details_html(m):
    out = []
    for arm in ARMS:
        text = _read_text(os.path.join(m["run_dir"], arm, "final.txt"))
        body = esc(text) if text is not None else \
            "<b>FAILED — no final list was produced for this arm.</b>"
        out.append("<details><summary>%s — final list (verbatim)</summary>"
                   "<pre>%s</pre></details>" % (esc(ARM_TITLES[arm]), body))
    return "".join(out)


def render_run_report(run_dir):
    m = compute_metrics(run_dir)
    meta, cfg = m["meta"], m["cfg"]
    roster = cfg.get("resolved_roster", {})
    roster_line = "; ".join("%s → %s/%s" % (r, e["provider"], e["model"])
                            for r, e in roster.items())
    warnings = meta.get("warnings", [])
    failed = meta.get("failed_calls", [])

    if m["judging_failed"]:
        verdict_html = ("<div class='failedbox'>JUDGING FAILED — see "
                        "<a href='judge/'>judge/</a> for the raw attempts. "
                        "Judge-derived rows below are empty.</div>")
    else:
        blind_line = ", ".join("SYSTEM-%s = %s" % (l, ARM_TITLES[a])
                               for l, a in sorted(m["blind"].items()))
        verdict_html = (
            "<div class='verdict'><div class='tag'>Blind verdict "
            "(judge saw only SYSTEM-A/B/C/D, in random order)</div>"
            "<p>%s</p></div><p class='meta'>Un-blinded mapping: %s "
            "(saved in <a href='judge/blind_mapping.json'>"
            "judge/blind_mapping.json</a>).</p>"
            % (esc(m["judge"]["verdict"]), esc(blind_line)))

    caveats = [
        "A single run is noise: treat nothing here as significant below "
        "3 runs (this report covers 1 run).",
        "prices as of %s, verify before quoting externally."
        % esc(cfg.get("prices", {}).get("as_of", "UNKNOWN")),
        "suspect counts include only judge flags verified by a verbatim "
        "quote from the list; depth remains judge opinion.",
        "Paired design: round-1 generations were produced once and reused "
        "as Arm 1 round 1 and Arm 2's generation phase; their tokens and "
        "cost are attributed to BOTH arms, so arm costs do not sum to "
        "total spend ($%s). Arm 4 (persona control) generated its own "
        "single-model round and is not sampling-paired with arms 1/2."
        % _fmt(m["total_cost_usd"], "usd").lstrip("$"),
        "Blinding: judges saw sanitised lists (structural markers stripped, "
        "entries renumbered E-n, role tokens removed — saved as "
        "judge/input-*.txt). Residual leak: prose style itself can still "
        "hint that a list came from a single model.",
    ]
    if m.get("n_judges", 1) > 1:
        caveats.append(
            "Judge panel of %d: scoreboard metrics are medians across "
            "judges; the coverage matrix uses judge 1's union (per-judge "
            "unions are in judge/judge&lt;i&gt;.json)." % m["n_judges"])
        if m.get("judges_disagree"):
            caveats.append(
                "JUDGES DISAGREE on the headline winner — see the Judges "
                "section; treat the headline as unresolved for this run.")
    solo_cfg = cfg.get("solo", {}) or {}
    ed_cfg = cfg.get("editor", {}) or {}
    # two tiers, both guarded against absent config keys (None == None must
    # never trigger a caveat)
    if solo_cfg.get("model") and solo_cfg.get("model") == ed_cfg.get("model"):
        caveats.append(
            "Arm 4's persona generations and the editor are the SAME model "
            "(%s/%s): arm 4's result includes a same-model consistency "
            "component, not pure persona-splitting — read arm 4 vs arm 3 "
            "with that in mind."
            % (esc(solo_cfg.get("provider")), esc(solo_cfg.get("model"))))
    elif solo_cfg.get("provider") \
            and solo_cfg.get("provider") == ed_cfg.get("provider"):
        caveats.append(
            "Arm 4's persona model and the editor come from the same "
            "provider family (%s: %s vs %s): a weaker same-family "
            "consistency component may be present in arm 4's result."
            % (esc(solo_cfg.get("provider")), esc(solo_cfg.get("model")),
               esc(ed_cfg.get("model"))))
    if cfg.get("executor") == "claude_cli":
        caveats.append(
            "Anthropic calls used the claude_cli executor: the CLI injects "
            "a small machine-local preamble that is NOT captured in the "
            "saved p-*.txt prompts (calls run from an empty directory with "
            "MCP disabled to minimise it). For strictly reproducible, "
            "quotable prompts use executor: api.")
    for w in warnings:
        if "SELF-PREFERENCE" in w:
            caveats.append(esc(w))
    if failed:
        caveats.append("FAILED cells (content never substituted): "
                       + esc("; ".join(failed)))
    for w in warnings:
        if "SELF-PREFERENCE" not in w:
            caveats.append(esc(w))

    page = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>argbench — {run_id}</title><style>{css}</style></head><body>
<h1>argbench — argument-architecture benchmark</h1>
<p><b>Question:</b> {question}</p>
<p class="meta">run <span class="mono">{run_id}</span> · finished {finished}
 · scale {scale} ({nroles} roles, {rounds} rounds) · seed
 <span class="mono">{seed}</span><br>
roster (identical for arms 1 and 2): {roster}<br>
editor: {editor} · solo: {solo} · judge: {judge_m}<br>
note: paired round-1 — the shared GEN phase is Arm 1 round 1 AND Arm 2's
generation phase; its cost is counted into both arms. Arm 4 is the persona
control: the solo model plays every role through the exam topology, so
arm4 vs arm3 isolates persona-splitting and arm2 vs arm4 isolates vendor
diversity.</p>
{verdict}
<h2>Scoreboard</h2>
{scoreboard}
<h2>Headline — grounded arguments per 10k tokens</h2>
{bars}
<h2>Coverage matrix (union of distinct arguments)</h2>
{matrix}
<h2>Depth &amp; grounding</h2>
{cards}
<h2>Judges</h2>
{judges}
<h2>Final lists</h2>
{details}
<h2>Raw records</h2>
<p class="meta">every prompt (<span class="mono">p-*.txt</span>) and raw
reply (<span class="mono">o-*.txt</span>) is on disk:
<a href="gen/">gen/</a> · <a href="arm1/">arm1/</a> ·
<a href="arm2/">arm2/</a> · <a href="arm3/">arm3/</a> ·
<a href="arm4/">arm4/</a> ·
<a href="judge/">judge/</a> · <a href="calls.jsonl">calls.jsonl</a> ·
<a href="config_resolved.yaml">config_resolved.yaml</a> ·
<a href="run_meta.json">run_meta.json</a></p>
<div class="foot"><b>Caveats</b><ul>{caveats}</ul></div>
</body></html>"""

    out = page.format(
        css=CSS, run_id=esc(meta.get("run_id", os.path.basename(run_dir))),
        question=esc(meta.get("question", "")),
        finished=esc(meta.get("finished_utc", "")),
        scale=esc(meta.get("scale", "")),
        nroles=len(meta.get("roles", [])), rounds=meta.get("rounds", ""),
        seed=esc(meta.get("seed", "")), roster=esc(roster_line),
        editor=esc("%s/%s" % (cfg.get("editor", {}).get("provider"),
                              cfg.get("editor", {}).get("model"))),
        solo=esc("%s/%s" % (cfg.get("solo", {}).get("provider"),
                            cfg.get("solo", {}).get("model"))),
        judge_m=esc(_judge_line(cfg.get("judge", {}))),
        verdict=verdict_html, scoreboard=scoreboard_html(m),
        bars=bars_html(m), matrix=matrix_html(m), cards=cards_html(m),
        judges=judges_html(m), details=details_html(m),
        caveats="".join("<li>%s</li>" % c for c in caveats))
    path = os.path.join(run_dir, "report.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(out)
    return path


# ---------------------------------------------------------------------------
# multi-run summary
# ---------------------------------------------------------------------------

SUMMARY_METRICS = [
    ("distinct", "Distinct arguments"), ("unique", "Unique to system"),
    ("coverage_pct", "Coverage of union %"), ("suspect", "Suspect entries"),
    ("depth", "Depth (1–5)"), ("input_tokens", "Input tokens"),
    ("output_tokens", "Output tokens"), ("cost_usd", "Cost USD"),
    ("wall_s", "Wall-clock s"), ("efficiency", "Grounded/10k tokens"),
]


def render_summary(out_dir, run_dirs):
    all_m = [compute_metrics(d) for d in run_dirs]
    wins = {arm: 0 for arm in ARMS}
    for m in all_m:
        effs = {arm: m["arms"][arm]["efficiency"] for arm in ARMS}
        present = {a: v for a, v in effs.items() if v is not None}
        if present:
            best = max(present.values())
            for a, v in present.items():
                if v == best:
                    wins[a] += 1
    rows = []
    for key, label in SUMMARY_METRICS:
        cells = []
        for arm in ARMS:
            vals = [m["arms"][arm][key] for m in all_m
                    if m["arms"][arm][key] is not None]
            if not vals:
                cells.append("—")
            else:
                mean = sum(vals) / len(vals)
                if len(vals) >= 2:
                    sd = (sum((v - mean) ** 2 for v in vals)
                          / (len(vals) - 1)) ** 0.5
                    cells.append(
                        "%.1f <span class='meta'>±%.1f sd "
                        "(%.1f–%.1f, n=%d)</span>"
                        % (mean, sd, min(vals), max(vals), len(vals)))
                else:
                    cells.append("%.1f <span class='meta'>(n=1)</span>"
                                 % mean)
        rows.append("<tr><th>%s</th>%s</tr>" % (
            esc(label), "".join("<td class='num'>%s</td>" % c for c in cells)))
    runs_list = "".join(
        "<li><a href='%s/report.html'><span class='mono'>%s</span></a></li>"
        % (esc(os.path.basename(d)), esc(os.path.basename(d)))
        for d in run_dirs)
    page = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>argbench summary</title>
<style>{css}</style></head><body>
<h1>argbench — summary over {n} runs</h1>
<p><b>Question:</b> {question}</p>
<h2>Mean (min–max) per metric per arm</h2>
<table><tr><th></th>{heads}</tr>{rows}</table>
<h2>Headline wins</h2>
<p>{wins}</p>
<h2>Runs</h2><ul>{runs}</ul>
<div class="foot"><b>Caveats</b><ul>
<li>Aggregates below 3 runs are noise.</li>
<li>prices as of {asof}, verify before quoting externally.</li>
</ul></div></body></html>""".format(
        css=CSS, n=len(run_dirs),
        question=esc(all_m[0]["meta"].get("question", "")),
        heads="".join("<th>%s</th>" % esc(ARM_TITLES[a]) for a in ARMS),
        rows="".join(rows),
        wins=" · ".join("%s: %d/%d" % (ARM_TITLES[a], wins[a], len(run_dirs))
                        for a in ARMS),
        runs=runs_list,
        asof=esc(all_m[0]["cfg"].get("prices", {}).get("as_of", "UNKNOWN")))
    path = os.path.join(out_dir, "summary.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return path


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "--summary":
        print(render_summary(argv[1], argv[2:]))
    elif len(argv) == 1:
        print(render_run_report(argv[0]))
    else:
        print("usage: report.py <run_dir> | report.py --summary "
              "<out_dir> <run_dir>...", file=sys.stderr)
        sys.exit(2)
