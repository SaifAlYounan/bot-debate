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
| `judge.txt` | canned judge JSON over SYSTEM-A/B/C/D |

There is no `judge-repair.txt`: the canned judge JSON is schema-valid, so
the repair path is never invoked in mock runs.

`CONV: k` values in the synth fixtures equal the number of distinct roles
appearing in each entry's `SOURCES` line (k = how many role lists
independently produced the claim).

## Deliberately planted defects (do not "fix" these)

The fixtures are not clean on purpose — they exercise the pipeline's
failure-detection paths, and the `--mock` self-check in `argbench.py` fails
the run loudly if they stop doing so:

1. **One arithmetic error in `judge.txt`**: `per_system.B.distinct` claims
   10, but the union matrix contains B in exactly 9 entries. Every mock run
   must therefore trigger the recompute-and-prefer path (a warning is
   logged; `judge.json` keeps both `distinct` and `distinct_claimed`).
   All other claimed counts match the matrix.
2. **Unsourced statistics for the suspect count** — verbatim phrases and
   their files:
   - "holds below 20 percent overrides" (`r3-ADVOCATE.txt`), carried into
     the master list as "override-rate data below 20 percent"
     (`arm1-synth.txt`);
   - "15 percent under day bans" (`r2-ADVOCATE.txt`) and "the only
     controlled study available shows near-full migration"
     (`r3-OPPONENT.txt`);
   - "routinely exceeds projections" stated as if measured
     (`arm4-gen-LENS_A.txt`).
   The canned judge counts these as `suspect` (A=1, B=0, C=2, D=1) and
   names the reasons in its notes.
3. **Killed entries in the debate**: `arm1-synth.txt` carries a GRAVEYARD
   with three entries killed by rebuttals that received no defence, so the
   report's arm-1 kill handling is always exercised.
4. **Divergence as well as convergence**: the union matrix mixes CORE
   entries (found by all four systems), pairwise overlaps, and
   single-system unique finds, so the coverage matrix renders every case.

## Self-check contract

After every `--mock` run, `mock_self_check` in `argbench.py` verifies:
`judge/judge.json` exists and passes the strict schema; `distinct_claimed`
/ `unique_claimed` are preserved for all of A–D; the total suspect count is
non-zero; and arm 1's final list contains its GRAVEYARD. Editing the
fixtures in a way that breaks any of these fails the mock gate.
