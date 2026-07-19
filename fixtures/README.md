# Mock fixtures

Canned deterministic outputs for `--mock` runs (offline, zero network).
File names equal the pipeline's call names (the mock provider appends
`.txt`), so resolution is mechanical. All content concerns the proposition
"Should offices ban meetings on Fridays?" regardless of what proposition is
passed on the CLI — the mock gate audits accounting, structure and report
logic, not content.

| File(s) | Role in the pipeline |
|---|---|
| `gen-<ROLE>.txt` ×5 | shared round-1 generations (arms 1+2, paired) |
| `r2-<ROLE>.txt`, `r3-<ROLE>.txt` ×5 each | arm-1 debate rounds: rebuttals, defences, new entries |
| `arm1-synth.txt` | arm-1 editor master list with `TESTED: yes` tags and a `GRAVEYARD` |
| `arm2-synth.txt` | arm-2 editor master list with `CONV: k` and `SOURCES` |
| `arm2-critic.txt` | arm-2 gap critic: `CRITIC-n` additions + `WEAKEST` |
| `arm4-gen-<ROLE>.txt` ×5 | arm-4 persona-control generations (single-model voice) |
| `arm4-synth.txt`, `arm4-critic.txt` | arm-4 editor tail (same topology as arm 2) |
| `arm3-solo.txt` | arm-3 solo control's final list |
| `judge1.txt`, `judge2.txt` | canned two-judge panel JSON over SYSTEM-A/B/C/D |

There are no `judge<i>-repair.txt` files: both canned judge JSONs are
schema-valid, so the repair path is never invoked in mock runs.

The mock run uses seed 42, which fixes the blind mapping to
`A=arm4, B=arm2, C=arm3, D=arm1` — the judge fixtures' `suspect_entries`
quotes are written against the *sanitised* finals of those arms and must
keep matching them verbatim (whitespace-normalised, case-insensitive), or
the quote-validation gate will drop them and the self-check will fail.

`CONV: k` values in the synth fixtures equal the number of distinct roles
appearing in each entry's `SOURCES` line (k = how many role lists
independently produced the claim).

## Deliberately planted defects (do not "fix" these)

The fixtures are not clean on purpose — they exercise the pipeline's
failure-detection paths, and the `--mock` self-check in `argbench.py` fails
the run loudly if they stop doing so:

1. **One arithmetic error in `judge1.txt`**: `per_system.B.distinct` claims
   10, but the union matrix contains B in exactly 9 entries. Every mock run
   must therefore trigger the recompute-and-prefer path (a warning is
   logged; the saved judge data keeps both `distinct` and
   `distinct_claimed`). `judge2.txt`'s arithmetic is fully consistent, so
   both the corrected and the clean path run every time.
2. **One unverifiable suspect quote in `judge1.txt`**: system C's second
   `suspect_entries` item quotes "meeting hours fell 42 percent in a
   Helsinki municipal pilot", which appears nowhere in any list. The
   quote-validation gate must drop it (verified=false, suspect 2 → 1,
   `suspect_claimed` preserved, warning logged) on every mock run.
3. **Verified suspect flags with real quotes** — verbatim phrases and the
   fixture files they live in:
   - "efficient meeting load varies an order of magnitude across teams"
     (`arm4-synth.txt` → SYSTEM-A);
   - "the binding input for knowledge work" (`arm3-solo.txt` → SYSTEM-C);
   - "vendor override-rate data below 20 percent" and "near-full migration
     in the only controlled study" (`arm1-synth.txt` → SYSTEM-D);
   - "routinely exceeds first-year projections for blanket rules"
     (`arm2-synth.txt` → SYSTEM-B, flagged only by judge 2).
4. **Judge disagreement**: the two judges differ on B's suspect count
   (0 vs 1) and on several depth scores, so the panel median and
   min–max spread paths are non-trivial on every run.
5. **Killed entries in the debate**: `arm1-synth.txt` carries a GRAVEYARD
   with three entries killed by rebuttals that received no defence, so the
   report's arm-1 kill handling — and the sanitiser's GRAVEYARD stripping
   before judging — is always exercised.
6. **Divergence as well as convergence**: the union matrix mixes CORE
   entries (found by all four systems), pairwise overlaps, and
   single-system unique finds, so the coverage matrix renders every case.

## Self-check contract

After every `--mock` run, `mock_self_check` in `argbench.py` verifies:
`judge/judge.json` holds a 2-judge aggregate; each judge's data passes the
strict schema; `distinct_claimed` / `unique_claimed` / `suspect_claimed`
are preserved for all of A–D in each judge's data; at least one suspect
flag was verified by quote AND at least one was dropped as unverifiable;
per-metric spread is recorded; the sanitised judge inputs
(`judge/input-<letter>.txt`) contain no GRAVEYARD/CONV:/SOURCES:/TESTED:
markers and no role tokens; and arm 1's on-disk final still contains its
GRAVEYARD. Editing the fixtures in a way that breaks any of these fails
the mock gate.
