# argbench

A single-file-per-concern benchmark pipeline that compares four
argument-mapping architectures on one debatable question, has the four
final argument lists scored by a blind LLM judge, and renders a
self-contained HTML report. Every prompt and raw reply is written to disk
before and after each call, every call is logged to `calls.jsonl` with
token counts and cost, and the report can be regenerated from the run
directory alone.

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
everywhere; and the judge's *provider* must not appear in the generation
roster — an overlap aborts the run unless `--force` is given, and a forced
run carries a self-preference caveat in `run_meta.json` and the report.

## Files

```
argbench.py    CLI + pipeline: intake, preflight estimate, the four arms,
               anonymisation, blind judging with schema validation + one
               repair attempt, calls.jsonl accounting, --mock self-check
providers.py   raw-HTTPS adapters (requests, no SDKs): anthropic (via the
               claude CLI or the Messages API), openai, google (Gemini),
               deepseek, mistral, xai; retries with exponential backoff +
               jitter; secret redaction; model listing; offline mock
report.py      renders report.html / summary.html from a run directory
               alone (python3 report.py runs/<timestamp>)
config.yaml    executor, roster, editor/solo/judge models, params, prices,
               retries, default run count
fixtures/      canned outputs for the offline --mock gate (see
               fixtures/README.md, including deliberately planted defects)
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
   with its `as_of` date. All dollar figures in the report come from this
   table; a model without a price entry is logged with `cost_usd: null`
   and flagged in the report, never estimated.

If a roster entry is unusable (missing key or still `SET_ME`), remaining
usable models are assigned round-robin with a logged warning that model
diversity is reduced. If nothing in the roster is usable, the editor or
solo entry is used for all roles — the run proceeds but the "diverse
roster" premise no longer holds; the warnings say so.

## Running

```
python3 argbench.py "Should offices ban meetings on Fridays?" [options]
```

When stdin is a TTY, an interactive intake confirms each input; each step
can be skipped with a flag. With no TTY, defaults are used silently.

| Flag | Effect |
|---|---|
| `--question` | the debatable question (default: the proposition, with `?` appended if absent) |
| `--context` | shared context given verbatim to every agent; if the value is a path to an existing file, the file's contents are used |
| `--lens-a`, `--lens-b` | descriptions for the two topic-specific roles (defaults: an economist; an unconsulted affected third party). ADVOCATE, OPPONENT and SKEPTIC are fixed |
| `--quick` | 3 roles, 2 debate rounds (default full scale: 5 roles, 3 rounds) |
| `--runs N` | run the whole pipeline N times (also settable as `runs:` in config); each run gets its own directory and a `summary.html` aggregates them |
| `--dry-run` | stop after the preflight estimate; nothing is spent |
| `--yes` | skip the preflight confirmation prompt |
| `--force` | proceed despite judge/roster provider overlap (stamps a caveat) |
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

- **`claude_cli`** (default) shells out to
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
- **`api`** calls the Anthropic Messages API directly over HTTPS with
  `ANTHROPIC_API_KEY`, with normal truncation semantics.

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
    o-<name>.txt              every raw reply, verbatim
    o-<name>.FAILED.txt       written instead on failure (redacted error
                              banner; content is never substituted)
  arm1/round<r>_mapping.json  per-round anonymisation mapping (role→letter)
  arm1..4/final.txt           each arm's final list
  judge/blind_mapping.json    which arm was SYSTEM-A/B/C/D
  judge/judge.json            validated judge output + recompute audit trail
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

The judge receives the four final lists as `SYSTEM-A/B/C/D` in a random
order (mapping saved to disk), with no arm names or metadata. It must
return strict JSON: a union of semantically distinct arguments with
`found_in` per system, and per-system `distinct`, `unique`, `suspect`
(entries whose REASONING/DEFEATER invent facts, cases, statistics or
authorities), `depth` (1–5) and notes, plus a blind verdict. The JSON is
schema-validated in code; on failure one repair attempt is made with the
errors quoted back, after which judging is marked FAILED.

`distinct` and `unique` are recomputed from the union matrix and the
recomputed values are used everywhere; the judge's originals are preserved
as `distinct_claimed` / `unique_claimed` in `judge/judge.json` and every
correction is logged as a warning. `suspect` and `depth` cannot be
recomputed and are taken verbatim from the judge.

Headline metric: **EFFICIENCY = (distinct − suspect) per 10,000 total
tokens** for the arm. It is only computed when the arm's token counts are
complete; otherwise the cell shows `—`. `CORE` = union arguments found by
all four systems. Coverage % = distinct / union size.

The report is one self-contained HTML file (inline CSS, no JS): blind
verdict with un-blinding line, a scoreboard with per-row winner marks, the
efficiency bar chart, a coverage matrix with CORE banding and unique finds
highlighted, per-arm judge cards, each arm's final list verbatim, links to
every raw file, and an auto-stamped caveat footer (single-run noise,
prices as-of date, paired-design note, blinding-format note, arm-4
same-model/same-provider consistency note when applicable, claude_cli
note when applicable, self-preference note on forced runs, and any FAILED
cells).

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

Every error message, log line and failure banner passes through a
redaction layer that scrubs the exact key values from the environment plus
common header/token shapes (`Authorization`, `x-api-key`,
`x-goog-api-key`, `Bearer …`, `sk-…`, `AIza…`, `xai-…`).

## Offline mock gate

```
python3 argbench.py --mock "any proposition"
```

runs the entire pipeline against canned fixtures with zero network access:
fixed seed 42, full scale forced, mock provider usage of 1 token per 4
characters so the accounting is checkable by hand. `--mock` implies
`--force` (the mock judge trivially overlaps the mock roster), so every
mock report carries the self-preference caveat.

The fixtures deliberately contain defects — one judge arithmetic error,
unsourced statistics for the suspect count, and killed debate entries —
so the recompute path, suspect counting and GRAVEYARD handling are
exercised on every run. After each mock run a self-check fails the run
loudly unless `judge/judge.json` exists and passes the schema, the
claimed-vs-recomputed audit trail is preserved for all four systems, at
least one suspect entry was counted, and arm 1's final list carries its
GRAVEYARD. See `fixtures/README.md`. The CI workflow
(`.github/workflows/mock-gate.yml`) runs exactly this on every push and
pull request.

## Limitations

Everything below is a property of the current code, not a footnote.

- **The judge is one LLM call and most of its output is unverifiable.**
  Only the `distinct`/`unique` arithmetic is recomputed; the union itself
  (which entries count as "the same argument"), `suspect` and `depth` are
  the judge's unaudited opinion — and the headline EFFICIENCY metric
  depends directly on `suspect`.
- **Blinding is partial.** Anonymisation replaces UPPERCASE role tokens
  and participant letters only, so a lowercase self-reference ("as the
  advocate…") survives into debate transcripts; final lists keep
  role-prefixed `SOURCES` IDs (revealing roles, not architectures, to the
  judge); and structural markers (`GRAVEYARD`/`TESTED` in arm 1,
  `CONV:`/`CRITIC-n` in arms 2 and 4) can reveal the architecture family.
  The blinding hides identity and order, not format.
- **A single run is noise.** One question, one judge sample per run. The
  report footer says to treat nothing as significant below 3 runs; the
  code provides `--runs` and a summary but no statistics beyond
  mean/min/max and headline win counts.
- **`claude_cli` results are not strictly reproducible.** The CLI injects
  an uncaptured preamble, temperature cannot be set, cache tokens are
  folded into input counts (breaking cross-executor comparability), and a
  cap hit costs an entire failed call. All of this is stamped into the
  report, but only `executor: api` avoids it.
- **Costs are only as good as the config prices table.** The pipeline
  multiplies provider-reported token counts by user-entered prices; the
  only independent cross-check (`cli_reported_cost_usd`) exists for the
  CLI executor. Unpriced models yield `null` cost, flagged as understated.
- **The preflight estimate is a rough upper bound**, built from a chars/4
  token approximation and worst-case `max_tokens`; unpriced models are
  excluded from the bound entirely (with a warning).
- **The mock gate does not cover the live adapters.** Provider HTTP
  handling, the CLI cap-hit detection and the parameter-fallback paths
  are only exercised with real keys.
- **Raw replies are saved unredacted by design.** Redaction covers error
  paths, logs and metadata; `o-*.txt` files are verbatim model output.
  Anything you paste as context is written to `config_resolved.yaml` and
  sent verbatim to every configured provider.
- **`extract_json` is heuristic**: it strips markdown fences and falls
  back to slicing from the first `{` to the last `}`. Failures are loud
  (schema validation, then FAILED), but pathological judge output could
  in principle parse to something unintended yet schema-valid.
- **A `--context` value that happens to match an existing file path is
  silently read as a file**, not used as literal text.
- **Round-robin roster fill silently degrades diversity** (with a
  warning): with missing keys, arms 1/2 can end up far less
  model-diverse than the config suggests, weakening the arm-2-vs-arm-4
  comparison.

## License

MIT — see `LICENSE`.
