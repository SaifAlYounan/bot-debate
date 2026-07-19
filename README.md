# argbench

A benchmarking pipeline that compares four **argument-mapping
architectures** on a single debatable proposition, scores them with a blind
judge, and reports exact token and dollar cost per architecture — designed to
be audited: every prompt, raw reply, mapping, and metric is written to disk,
and the HTML report can be regenerated from the run directory alone.

## The four arms

| Arm | Architecture | How it works |
|---|---|---|
| **1 — DEBATE** | Shared transcript | N role agents generate independently, then debate for further rounds. Each round every agent sees the full transcript with authors re-anonymised under a fresh random `Participant A/B/C…` mapping (saved to disk per round), rebuts weak entries by ID, defends its own, and adds new ones. An EDITOR model then produces a deduplicated master list, tags entries that survived attack `TESTED: yes`, and lists killed entries in a `GRAVEYARD` section. |
| **2 — EXAM + EXAMINER** | Isolated | The same role-model roster generates independently (no transcript, ever). The EDITOR merges duplicates into a master list tagged `CONV: k` (how many roles independently produced each claim), then a GAP CRITIC — which sees only the master list — appends missing arguments as `CRITIC-n` entries and names the 3 weakest. Critic output is appended mechanically, never rewritten. |
| **3 — SOLO CONTROL** | One call | A single Anthropic model runs a multi-pass self-interrogation prompt covering all roles' lenses. No subcalls. |
| **4 — PERSONA CONTROL** | Single model, exam topology | The same model as Arm 3 plays every role through the exact Arm-2 topology: isolated per-role generation, then the shared EDITOR's synthesis and gap critic. |

**Paired design:** round-1 generations are produced once and reused as both
Arm 1's round 1 and Arm 2's generation phase. Their tokens and cost are
attributed to *both* arms, so sampling luck is held constant and only the
topology differs. Arm costs therefore do not sum to total spend. Arm 4
generates its own single-model round (it cannot share the diverse-roster
generations by definition), so it is not sampling-paired with arms 1/2.

**Why Arm 4 exists:** arms 1/2 versus arm 3 change two things at once —
topology *and* vendor diversity — so a multi-agent win would be
unattributable. The persona control completes the comparison: arm 4 vs
arm 3 isolates persona-splitting, arm 2 vs arm 4 isolates vendor
diversity, and arm 1 vs arm 2 (sampling-paired) isolates the debate
transcript.

**Interpretation footnote for Arm 4:** when the arm-4 persona model and the
EDITOR are the same model (or the same provider family), arm 4's merged
output carries a self-consistency bonus that arms 1/2 do not get — the
editor is merging text sampled from its own distribution. Arm 4 vs arm 3
therefore measures persona-splitting *plus* same-model consistency; if
arm 4 outperforms arm 3, part of that win may be self-consistency rather
than topology. The headline metric is unaffected; read the comparison with
this footnote in mind (the report auto-stamps a caveat whenever the models
overlap).

**Fairness invariants (enforced in code):** identical roster for arms 1 and 2;
one EDITOR model for synthesis, gap critic, and debate-synth across ALL
arms; one set of generation parameters everywhere; and the JUDGE must come
from a provider
outside the generation roster — a violation refuses to run without `--force`
and stamps a self-preference caveat into the report.

## Files

```
argbench.py    CLI + pipeline: intake, preflight cost estimate, the four arms,
               anonymisation, blind judging with schema validation + one repair
               attempt, calls.jsonl accounting
providers.py   raw-HTTPS adapters (no SDKs): anthropic (claude_cli / api),
               openai, google (Gemini), deepseek, mistral, xai; retries with
               exponential backoff + jitter; secret redaction; --list-models;
               offline mock provider
report.py      renders report.html / summary.html from the run directory on
               disk alone (standalone: python3 report.py runs/<timestamp>)
config.yaml    roster, editor/solo/judge models, params, prices, retries
fixtures/      canned deterministic outputs for the offline --mock gate
```

Dependencies: Python 3.11+, `requests`, `pyyaml`. Nothing else.

## Setup

1. Export API keys for the providers you use (environment only — keys are
   never written to disk, and every log/error path is redacted):
   `ANTHROPIC_API_KEY` (only if `executor: api`), `OPENAI_API_KEY`,
   `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`, `XAI_API_KEY`.
2. Discover model IDs — they drift, so none are guessed:
   ```
   python3 argbench.py --list-models
   ```
3. Fill every `SET_ME` in `config.yaml` (roster, editor, solo, judge, and the
   per-model `prices` table with its `as_of` date).

## Running

```
python3 argbench.py "Should offices ban meetings on Fridays?" [options]
```

Interactive intake (each step skippable via flags) confirms the debatable
question (`--question`), shared context (`--context`, inline text or a file
path), the two topic lenses (`--lens-a`, `--lens-b`; ADVOCATE, OPPONENT and
SKEPTIC are fixed), and scale: `--quick` = 3 roles, 2 rounds; default full =
5 roles, 3 rounds.

Before any spend, a preflight prints an upper-bound cost estimate per arm from
prompt sizes, `max_tokens` caps and the prices table, and asks for
confirmation (`--yes` to accept, `--dry-run` to stop there).

Other flags: `--runs N` (each run in its own directory plus an aggregate
`summary.html`), `--force` (override the judge/roster overlap check),
`--mock` (full offline run against `fixtures/`, zero network), `--config`,
`--out-dir`.

## Executors for Anthropic calls

- **`claude_cli`** (default): shells out to `claude -p --output-format json`
  on your existing Claude Code login; exact usage and cost come from the CLI's
  JSON. Two caveats, both recorded in each call's params: temperature is not
  settable through the CLI (logged as `null`), and the CLI does **not**
  truncate at an output cap — a call that hits `max_tokens` (enforced via
  `CLAUDE_CODE_MAX_OUTPUT_TOKENS`) is detected through `usage.iterations` and
  marked FAILED rather than silently saving a fragment. Set generous caps
  with this executor.
- **`api`**: direct HTTPS to the Anthropic Messages API using
  `ANTHROPIC_API_KEY`, with normal truncation semantics.

All other providers use direct HTTPS chat/completions-style calls. Token
counts always come from each provider's `usage` field; if a provider omits it
the tokens are recorded as `null` and flagged in the report — never estimated.

## On-disk layout

```
runs/<UTC timestamp>/
  config_resolved.yaml      exact config used (secrets excluded by construction)
  gen/ arm1/ arm2/ arm3/ arm4/ judge/
    p-<name>.txt            every prompt, saved before its call
    o-<name>.txt            every raw reply, verbatim
  arm1/round<r>_mapping.json  per-round anonymisation mappings
  judge/blind_mapping.json    which arm was SYSTEM-A/B/C/D
  calls.jsonl               one line per call: tokens, cost, latency, retries,
                            params, failed flag
  run_meta.json             phase wall-clocks, seed, warnings, failed calls
  report.html
```

## Judging and metrics

The judge receives the four final lists as `SYSTEM-A/B/C/D` in a saved random
order, with no architecture names or metadata, and must return strict JSON:
a union of semantically distinct arguments (`found_in` per system), per-system
`distinct` / `unique` / `suspect` (entries that invent facts, cases, statistics
or authorities) / `depth` (1–5), and a blind verdict. The JSON is validated in
code; one repair attempt is allowed, after which judging is marked FAILED.
`distinct` and `unique` are always recomputed from the union matrix and the
recomputed values are preferred over the judge's own arithmetic. The full
audit trail lives on disk: `judge/judge.json` keeps the judge's original
numbers alongside the recomputed ones (`distinct_claimed` /
`unique_claimed`), and every correction is logged as a warning in
`run_meta.json`. The report always uses the recomputed values.

Headline metric: **EFFICIENCY = (distinct − suspect) per 10,000 total tokens**.
`CORE` = union arguments found by all four systems.

The report is a single self-contained HTML file (inline CSS, no JS, printable):
blind verdict + un-blinding line, a scoreboard with winner marks per row, the
headline bar chart, a coverage matrix with CORE banding and unique finds
highlighted, depth-and-grounding cards, each arm's final list verbatim, links
to the raw files, and a footer of caveats (single-run noise below 3 runs,
prices as-of date, paired-design note, self-preference caveat when forced,
and any FAILED cells).

## Failure behavior

Calls retry at most 3 times with exponential backoff + jitter on 429/5xx/
timeouts. On final failure the cell is marked FAILED in `calls.jsonl` and the
report; content is never substituted and retries never continue silently. If
an arm produces no final list, judging for that run is marked FAILED.

## Deployment checklist (user-side)

Things the pipeline cannot do for you. Work through these before quoting any
result externally:

1. **Keys and models.** Export your provider keys, run
   `python3 argbench.py --list-models`, and fill every `SET_ME` in
   `config.yaml` — including the `prices` table with a current `as_of` date.
2. **Judge from outside the roster.** Pick a judge provider not in the
   generation roster. `--force` exists for degraded testing only; every forced
   run carries a self-preference caveat in its report.
3. **Executor choice is a validity choice.** For results you will quote, use
   `executor: api`. The `claude_cli` executor runs on your Claude Code login
   but injects a small machine-local preamble that is not captured in the
   saved `p-*.txt` prompts (argbench minimises it by running the CLI from an
   empty directory with MCP disabled, and the report is stamped with a
   caveat) — convenient, but not strictly reproducible across machines.
4. **Cap sizing under `claude_cli`.** The CLI does not truncate at
   `max_tokens` — a cap hit FAILS the call and wastes a full call cycle. Set
   caps 30–50% larger than the output you expect; use `executor: api` if you
   need true truncation semantics.
5. **Re-verify the CLI contract on your machine** (it drifts between
   versions):
   `claude --help | grep -E 'tools|session|system-prompt|strict-mcp'`
   then force a cap hit and confirm the failure is loud:
   `echo "write 300 words on anything" | CLAUDE_CODE_MAX_OUTPUT_TOKENS=50 \
   claude -p --output-format json --tools ""` — expect an error result or a
   multi-entry `usage.iterations`, either of which argbench treats as FAILED.
6. **Escalate in stages.** `--mock` first (offline, free), then one `--quick`
   smoke run on a trivial proposition, then full scale. Use `--runs 3`
   or more for anything you intend to quote — a single run is noise, and the
   report footer says so.
7. **Mind the context you paste at intake.** It is written to
   `config_resolved.yaml` on disk and sent verbatim to every configured
   provider. Do not paste privileged or confidential material you cannot
   share with those vendors.
8. **Reproduce the secrets audit locally.** Export a fake key (e.g.
   `OPENAI_API_KEY=sk-test-123456789012345678`), run a mock or smoke, then
   `grep -rE 'sk-|AIza|xai-' runs/` — expect zero hits.

Known limitations to keep in mind when reading transcripts: anonymisation
replaces UPPERCASE role tokens, so a lowercase self-reference ("as the
advocate…") can leak a role hint inside debate rounds; and final lists retain
role-prefixed `SOURCES` IDs, which reveal roles (not architectures) to the
judge.

## Offline verification

```
python3 argbench.py --mock "any proposition"
```

runs the entire pipeline against canned deterministic fixtures with zero
network access and produces a fully populated report — useful for auditing
the accounting and report logic without spending anything. The canned judge
JSON deliberately contains one arithmetic error to exercise the
recompute-and-prefer path.
