# argbench

A single-file-per-concern benchmark pipeline that compares four
argument-mapping architectures on one debatable question, has the four
final argument lists scored blind by an LLM judge — or a judge panel,
with median aggregation and per-judge spread — and renders a
self-contained HTML report. Every prompt and raw reply is written to disk
before and after each call, every call is logged to `calls.jsonl` with
token counts and cost, and the report can be regenerated from the run
directory alone. Each known limitation of the design is listed at the
bottom with its mitigation status: fixed in code, reduced, or inherent.

## The four arms

| Arm | Name | What the code does |
|---|---|---|
| **1 — DEBATE** | shared transcript | The role agents' round-1 outputs (see *paired design* below) are followed by debate rounds 2..R. In each round every agent receives the full transcript with all authors re-anonymised under a fresh random `Participant A/B/C…` mapping (saved to `arm1/round<r>_mapping.json`), and is instructed to rebut entries by ID, defend its own, and add new ones. A separate EDITOR model then merges the transcript into a master list, tags entries that survived rebuttal `TESTED: yes`, and lists killed entries under a `GRAVEYARD` heading. The editor receives a fully de-anonymised transcript (role names restored) so rebuttal targets are resolvable. |
| **2 — EXAM + EXAMINER** | isolated | The same round-1 outputs, with no transcript ever shared. The EDITOR merges them into a master list where each entry carries `CONV: k` (how many role lists independently produced the claim) and `SOURCES` (the merged entry IDs). A GAP CRITIC call — same EDITOR model, input is the master list only — appends missing arguments as `CRITIC-n` entries and names the 3 weakest. The critic's output is concatenated onto the master list mechanically; nothing is rewritten. |
| **3 — SOLO CONTROL** | one call | A single model (`solo` in config) answers one multi-pass self-interrogation prompt that covers all the roles' lenses. Minimum entry count is `8 × number-of-roles`. |
| **4 — PERSONA CONTROL** | single model, exam topology | The `solo` model generates a fresh isolated round playing each role (same prompts as the shared round 1), then goes through the identical arm-2 tail: EDITOR synthesis plus gap critic. |

**Paired design.** Round-1 generations are produced once (`gen/`) and reused
as both arm 1's first round and arm 2's generation phase. Their tokens,
cost and wall-clock are attributed to *both* arms in the report, so arm
costs do not sum to total spend. Arm 4 generates its own single-model round
and is not sampling-paired with arms 1/2.

**Why arm 4 exists.** Arms 1/2 differ from arm 3 in two ways at once —
topology and model diversity. Arm 4 (one model, arm-2 topology) completes
the square: arm 4 vs arm 3 isolates persona-splitting, arm 2 vs arm 4
isolates model diversity, arm 1 vs arm 2 (sampling-paired) isolates the
debate transcript.

**Fairness invariants enforced in code:** arms 1 and 2 share one roster
object by construction; one EDITOR model performs synthesis, gap critic and
debate-synth across all arms; one set of generation parameters is used
everywhere; no judge's *provider* may appear in the generation roster; and
a roster that would silently degrade to round-robin fill (missing keys or
`SET_ME` entries) refuses to run. Both refusals can be overridden with
`--force`, and a forced run carries the corresponding caveat
(self-preference / reduced diversity) in `run_meta.json` and the report.

## Files

```
argbench.py    CLI + pipeline: intake, preflight estimate, the four arms,
               anonymisation, judge-input sanitisation, blind judging
               (panel-capable) with schema validation + one repair attempt
               per judge, suspect-quote anchoring, calls.jsonl accounting,
               --mock self-check
providers.py   raw-HTTPS adapters (requests, no SDKs): anthropic (via the
               Messages API or the claude CLI), openai, google (Gemini),
               deepseek, mistral, xai; retries with exponential backoff +
               jitter; secret redaction; model listing; offline mock
report.py      renders report.html / summary.html from a run directory
               alone (python3 report.py runs/<timestamp>)
config.yaml    executor, roster, editor/solo/judge models, params, prices,
               retries, default run count
fixtures/      canned outputs for the offline --mock gate (see
               fixtures/README.md, including deliberately planted defects)
tests/         offline unit tests (stdlib unittest, all network and
               subprocess calls stubbed) for the adapter and judging logic
```

Dependencies: Python 3.11+, `requests`, `pyyaml`.

## Setup

1. Export API keys for the providers you use. Keys are read from the
   environment inside `providers.py` only and never written to disk:
   `ANTHROPIC_API_KEY` (needed for `executor: api` and for listing
   Anthropic models), `OPENAI_API_KEY`, `GEMINI_API_KEY`,
   `DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`, `XAI_API_KEY`.
2. Discover current model IDs (none are hard-coded or guessed):
   ```
   python3 argbench.py --list-models
   ```
   This queries each provider whose key is present and prints the IDs.
3. Replace every `SET_ME` in `config.yaml`: the five roster entries, the
   `editor`, `solo` and `judge` models, and the per-model `prices` table
   with its `as_of` date (ISO format — argbench warns loudly and stamps a
   report caveat when `as_of` is missing, unparseable, or older than 90
   days). All dollar figures in the report come from this table; a model
   without a price entry is logged with `cost_usd: null` and flagged in
   the report, never estimated.
4. Optionally configure a **judge panel**: `judge:` accepts a list of
   entries. Every judge must be from a provider outside the roster. With a
   panel, the scoreboard uses per-metric medians across judges, per-judge
   outputs and min–max spread are reported, and disagreement on the
   headline winner is stamped as a caveat.

If a roster entry is unusable (missing key or still `SET_ME`), the run
**refuses to start**: round-robin fill would silently reduce the model
diversity the arm-2 vs arm-4 comparison depends on. `--force` accepts the
fill, logs which roles were reassigned, and stamps a REDUCED-DIVERSITY
caveat into `run_meta.json` and the report.

## Running

```
python3 argbench.py "Should offices ban meetings on Fridays?" [options]
```

When stdin is a TTY, an interactive intake confirms each input; each step
can be skipped with a flag. With no TTY, defaults are used silently.

| Flag | Effect |
|---|---|
| `--question` | the debatable question (default: the proposition, with `?` appended if absent) |
| `--context` | shared context given verbatim to every agent — always literal text |
| `--context-file` | read the shared context from a file (the only way a file is ever read; `--context` never sniffs paths) |
| `--lens-a`, `--lens-b` | descriptions for the two topic-specific roles (defaults: an economist; an unconsulted affected third party). ADVOCATE, OPPONENT and SKEPTIC are fixed |
| `--quick` | 3 roles, 2 debate rounds (default full scale: 5 roles, 3 rounds) |
| `--runs N` | run the whole pipeline N times (also settable as `runs:` in config); each run gets its own directory and a `summary.html` aggregates them with mean, sample std-dev and min–max |
| `--dry-run` | stop after the preflight estimate; nothing is spent |
| `--yes` | skip the preflight confirmation prompt |
| `--force` | proceed despite judge/roster provider overlap or roster round-robin fill (stamps the corresponding caveat) |
| `--mock` | offline run against `fixtures/`, zero network (see below) |
| `--config`, `--out-dir` | paths (defaults `config.yaml`, `runs/`) |
| `--list-models` | print available model IDs per provider, then exit |

Before any API call, a preflight prints an upper-bound cost estimate per
arm, computed from prompt sizes (a chars/4 token approximation used only
here), the `max_tokens` caps, and the prices table, then asks for
confirmation. Models missing from the prices table are named and excluded
from the bound.

## Executors for Anthropic calls

Set `executor:` in `config.yaml`.

- **`api`** (default) calls the Anthropic Messages API directly over HTTPS
  with `ANTHROPIC_API_KEY`: exact prompts on disk, settable temperature,
  normal truncation semantics. This is the reproducible choice and the
  default for that reason.
- **`claude_cli`** (opt-in convenience) shells out to
  `claude -p --output-format json --tools "" --no-session-persistence
  --strict-mcp-config --system-prompt …` on your existing Claude Code
  login, from a freshly created empty temp directory so the CLI cannot
  pick up a `CLAUDE.md`, project memory, or MCP servers. Flags were
  verified against claude CLI 2.1.211 and may drift with CLI versions.
  Specific behavior, all recorded per call:
  - **temperature is not settable** through the CLI; it is logged as
    `null` in the call's params.
  - **max_tokens** is enforced via `CLAUDE_CODE_MAX_OUTPUT_TOKENS`. The
    CLI does not truncate at the cap: it either errors or internally
    continues across multiple iterations, returning only the final
    segment. Both cases are detected (`is_error`, or
    `usage.iterations > 1`) and the call is marked FAILED — a cap hit
    costs a whole failed call rather than yielding a truncated reply, so
    set caps generously with this executor.
  - token counts come from the CLI's JSON `usage`;
    cache-creation/cache-read tokens are folded into `input_tokens`
    (recorded separately too), so input/cost figures are not directly
    comparable with raw-API arms.
  - the CLI's own `total_cost_usd` is stored as `cli_reported_cost_usd`
    per call as a cross-check; the report's dollar figures always come
    from the config prices table.
  - the CLI injects a small preamble of its own that is **not** captured
    in the saved `p-*.txt` prompts; the report stamps a caveat whenever
    this executor was used. For strictly reproducible prompts use
    `executor: api`.

All other providers use direct HTTPS chat/completions-style calls
(Gemini uses `generateContent`; the API key travels in a header, never in
the URL). For OpenAI-compatible endpoints, a 400 rejecting `max_tokens` or
`temperature` triggers a single deterministic re-issue
(`max_completion_tokens`, or the parameter dropped), noted in the call's
params. Token counts always come from the provider's `usage` field; when
a provider omits it, tokens are recorded as `null` and the affected arm's
totals are flagged as understated in the report — never estimated.

## On-disk layout

```
runs/<UTC timestamp>/
  config_resolved.yaml        exact resolved config, roster, question,
                              context, lenses, seed (keys are never in the
                              config object, so none can be written)
  gen/ arm1/ arm2/ arm3/ arm4/ judge/
    p-<name>.txt              every prompt, saved before its call
    o-<name>.txt              every raw reply (through secret redaction —
                              content is otherwise untouched)
    o-<name>.FAILED.txt       written instead on failure (redacted error
                              banner; content is never substituted)
  arm1/round<r>_mapping.json  per-round anonymisation mapping (role→letter)
  arm1..4/final.txt           each arm's final list
  judge/blind_mapping.json    which arm was SYSTEM-A/B/C/D
  judge/input-<letter>.txt    the sanitised list the judges actually saw
  judge/judge<i>.json         each judge's validated output + audit trail
                              (written on panel runs; call names judge1..N)
  judge/judge.json            aggregate: per-metric medians, every judge's
                              full data under "judges", min-max spread,
                              judge 1's union (single judge = trivial case)
  calls.jsonl                 one line per call: ts, arm, phase, role,
                              provider, model, file paths, input/output
                              tokens, cost_usd, latency_ms, http_status,
                              retries, params, failed (+ error, cost_note,
                              cli_reported_cost_usd when applicable)
  run_meta.json               phase wall-clocks, seed, warnings, failed
                              calls, judging_failed
  report.html
```

`python3 report.py runs/<timestamp>` re-renders the report from these
files alone; `python3 report.py --summary runs <dir> <dir>…` re-renders
the multi-run summary.

## Judging and metrics

Before judging, every final list passes through a **sanitiser**
(`sanitize_for_judge`): `CONV:`/`SOURCES:`/`TESTED:` lines, the
`GRAVEYARD` and `WEAKEST` tail sections and `===` banners are stripped,
every entry is renumbered uniformly `E-1..E-n`, and remaining role tokens
are replaced — so neither role identity nor the architecture-family
*format* fingerprints reach the judge. The sanitised copies are saved as
`judge/input-<letter>.txt`; the full unsanitised lists stay on disk and in
the report.

Each judge (one, or a configured panel) receives the same four sanitised
lists as `SYSTEM-A/B/C/D` in one random order (mapping saved to disk),
with no arm names or metadata. It must return strict JSON: a union of
semantically distinct arguments with `found_in` per system, and per-system
`distinct`, `unique`, `suspect` with `suspect_entries` (each flag must
carry a verbatim quote of the invented fact), `depth` (1–5) and notes,
plus a blind verdict. The JSON is schema-validated in code; on failure one
repair attempt is made per judge with the errors quoted back. A judge that
still fails drops out of the panel (noted); judging is FAILED only when no
judge survives.

Three audits then run in code, per judge:
- `distinct` / `unique` are recomputed from the union matrix and the
  recomputed values are used everywhere (originals preserved as
  `*_claimed`, corrections logged).
- every `suspect` flag is **evidence-anchored**: its quote must appear
  verbatim (whitespace-normalised, case-insensitive, ≥ 8 chars) in that
  system's sanitised list. Flags that fail are dropped with a warning;
  `suspect` becomes the verified count, `suspect_claimed` is preserved.
- with a panel, the scoreboard takes the per-metric **median** across
  judges; per-judge tables, verdicts and min–max spread appear in the
  report, and a disagreement on the headline winner is a stamped caveat.

`depth` (and the union grouping itself) remain judge opinion — the panel
median reduces, but does not remove, that dependence.

Headline metric: **EFFICIENCY = (distinct − suspect) per 10,000 total
tokens** for the arm, using the audited values above. It is only computed
when the arm's token counts are complete; otherwise the cell shows `—`.
`CORE` = union arguments found by all four systems. Coverage % = distinct
/ union size (median across judges on a panel).

The report is one self-contained HTML file (inline CSS, no JS): blind
verdict with un-blinding line, a scoreboard with per-row winner marks, the
efficiency bar chart, a coverage matrix with CORE banding and unique finds
highlighted, per-arm judge cards, a per-judge section with panel spread,
each arm's final list verbatim, links to every raw file, and an
auto-stamped caveat footer (single-run noise, prices as-of date and
staleness, paired-design note, residual-blinding note, arm-4
same-model/same-provider consistency note when applicable, claude_cli
note when applicable, self-preference and reduced-diversity notes on
forced runs, judge-disagreement note, and any FAILED cells).

## Failure behavior

HTTP calls retry up to `retries.max` (default 3) times with exponential
backoff plus jitter on 429/5xx/timeouts (per-attempt HTTP timeout 240 s;
CLI subprocess timeout 600 s — CLI retries cover subprocess timeout and
unparseable output only, since the CLI retries API errors internally). On
final failure the call is marked FAILED in `calls.jsonl` and an
`o-*.FAILED.txt` banner is written; content is never substituted. If all
round-1 generations fail the run aborts. If any arm produces no final
list, judging is skipped and marked FAILED; the report renders with empty
judge-derived rows.

Every error message, log line, failure banner AND every saved model reply
passes through a redaction layer that scrubs the exact key values from the
environment plus common header/token shapes (`Authorization`, `x-api-key`,
`x-goog-api-key`, `Bearer …`, `sk-…`, `AIza…`, `xai-…`) — keys are never
sent to models, so redacting the replies is belt-and-braces that makes the
on-disk zero-secrets guarantee unconditional.

## Offline mock gate

```
python3 argbench.py --mock "any proposition"
```

runs the entire pipeline against canned fixtures with zero network access:
fixed seed 42, full scale forced, a two-judge mock panel, mock provider
usage of 1 token per 4 characters so the accounting is checkable by hand.
`--mock` implies `--force` (the mock judges trivially overlap the mock
roster), so every mock report carries the self-preference caveat.

The fixtures deliberately contain defects — one judge arithmetic error,
one suspect flag whose quote appears nowhere (exercising the drop path),
verified suspect flags with real quotes, judges that disagree, and killed
debate entries — so the recompute, quote-anchoring, panel-aggregation and
GRAVEYARD paths are exercised on every run. After each mock run a
self-check fails the run loudly unless: the 2-judge aggregate exists and
every judge's data passes the schema; the claimed-vs-recomputed audit
trail (`distinct`/`unique`/`suspect` `_claimed`) is preserved for all
systems; at least one suspect flag was verified AND at least one dropped;
per-metric spread is recorded; the sanitised judge inputs contain no
structural markers or role tokens; and arm 1's on-disk final still
carries its GRAVEYARD. See `fixtures/README.md`. The CI workflow
(`.github/workflows/mock-gate.yml`) runs the offline unit tests and then
exactly this gate on every push and pull request.

## Limitations and mitigations

Every limitation this design has ever documented is listed here with its
current status: **FIXED** (removed by code in this repo), **REDUCED**
(materially mitigated in code, residue described), or **INHERENT**
(cannot be removed by this codebase; how to live with it). Nothing is
silently dropped from this list.

1. **Judge output was unverifiable opinion.** — **REDUCED.**
   Three code audits now constrain it: `distinct`/`unique` are recomputed
   from the union matrix (originals kept as `*_claimed`); every `suspect`
   flag must carry a verbatim quote that is checked against the list the
   judge saw — unanchored flags are dropped, so the headline EFFICIENCY
   metric now rests only on evidence-anchored counts; and `judge:` accepts
   a panel, with the scoreboard taking per-metric medians, per-judge
   spread reported, and headline disagreement stamped as a caveat.
   *Residue:* `depth` and the union grouping itself (what counts as "the
   same argument") are still judge opinion — a panel dilutes but cannot
   remove that; a quote can anchor a suspect flag's target without proving
   the flag is deserved.
2. **Blinding was partial: role IDs and format markers reached the
   judge.** — **LARGELY FIXED at the judge; REDUCED inside debate
   rounds.** Judge inputs are now sanitised (`judge/input-*.txt`):
   structural markers and tail sections stripped, entries renumbered
   `E-1..E-n`, role tokens replaced — verified by the mock self-check on
   every run. Debate agents are additionally instructed to refer to
   participants only by letter. *Residue:* a lowercase self-reference
   inside a debate round can still leak a role hint to *other agents*
   (not the judge), and prose style itself can still hint that a list
   came from a single model.
3. **A single run is noise.** — **REDUCED (cost-inherent).**
   `--runs` plus `summary.html` now report mean, sample std-dev, min–max
   and n per metric, and headline win counts; the single-run report still
   carries its noise caveat. *Residue:* statistical power costs real
   money; the code cannot buy runs for you. Use `--runs 3` or more for
   anything you quote.
4. **`claude_cli` runs are not strictly reproducible.** — **FIXED on the
   default path; INHERENT for the CLI itself.** The default executor is
   now `api` (exact prompts on disk, settable temperature, true
   truncation). `claude_cli` remains available as explicit opt-in
   convenience; its preamble/temperature/cache-token/cap-hit caveats are
   recorded per call and stamped into the report.
5. **Costs are only as good as the prices table.** — **REDUCED.**
   `prices.as_of` is now checked at startup: missing, unparseable, or
   older than 90 days produces a loud warning and a report caveat.
   *Residue:* no provider returns authoritative prices over the API; the
   numbers themselves are still yours to keep honest
   (`cli_reported_cost_usd` cross-checks the CLI executor only).
6. **The preflight estimate is a rough upper bound.** — **INHERENT
   (by design).** It exists only to gate spending before it happens,
   from a chars/4 approximation and worst-case caps; unpriced models are
   named and excluded from the bound. Accounting never uses it — real
   token counts come from provider `usage` fields.
7. **The mock gate could not reach the live adapters.** — **LARGELY
   FIXED.** `tests/` now covers the adapter logic offline with stubbed
   HTTP/subprocess: retry/backoff decisions, terminal-vs-retryable
   statuses, response parsing per provider, the OpenAI parameter
   fallbacks, CLI cap-hit and error detection, redaction, JSON
   extraction, sanitisation, quote anchoring and anonymisation
   round-trips. CI runs them on every push. *Residue (INHERENT):* the
   live wire contract — actual provider behavior on a given day — can
   only be tested with real keys.
8. **Raw replies were saved unredacted.** — **FIXED.** Every saved reply
   now passes through the same redaction layer as errors and logs, making
   the on-disk zero-secrets guarantee unconditional (keys are never sent
   to models, so this is belt-and-braces; content is otherwise
   untouched). Note what remains true: anything you paste as context is
   written to `config_resolved.yaml` and sent verbatim to every
   configured provider — do not paste material you cannot share with
   those vendors.
9. **`extract_json` used a naive brace-slice fallback.** — **FIXED.**
   The fallback now uses `json.JSONDecoder.raw_decode` from each
   candidate `{`, parsing the first complete object; strict schema
   validation still follows, and failures are still loud.
10. **`--context` silently read a file when the text matched a path.** —
    **FIXED.** `--context` is now always literal text; files are read
    only via the explicit `--context-file`.
11. **Roster round-robin fill silently degraded model diversity.** —
    **FIXED (loud, opt-in).** A roster that would need fill now refuses
    to run, exactly like a judge/roster overlap; `--force` accepts it and
    stamps a REDUCED-DIVERSITY caveat into `run_meta.json` and the
    report.

## License

MIT — see `LICENSE`.
