#!/usr/bin/env python3
"""argbench — four-architecture argument-mapping benchmark.

CLI + pipeline. See config.yaml for models/prices and report.py for the
HTML rendering (which works from the run directory on disk alone).

Arms: ARM 1 debate (shared transcript), ARM 2 exam+examiner (isolated),
ARM 3 solo control, ARM 4 persona control (the solo model plays every
role through the arm-2 topology, disentangling vendor diversity from
topology). Round-1 generations are produced once and reused by arms 1
and 2 (paired design); their cost is attributed to BOTH arms in the
accounting. Arm 4 generates its own (single-model) round.
"""

import argparse
import datetime
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import yaml

import providers
import report as report_mod

# ---------------------------------------------------------------------------
# verbatim prompt building blocks (frozen — do not edit)
# ---------------------------------------------------------------------------

SHARED_FORMAT = """Output only argument entries, nothing else. Each entry:
ID: <ROLE>-<n>
SIDE: FOR | AGAINST | REFRAME
CLAIM: one sentence
REASONING: max 3 sentences
DEFEATER: the single strongest specific fact or argument that would kill this claim
Minimum 8 entries. After the obvious arguments, ask what a specialist would add, and add those too."""

GEN_TAIL = "You work alone. You see no other agent's output."

DEBATE_INSTRUCTION = ("You are in round {r} of a structured moot. Rebut "
                      "entries you believe fail, quoting their ID as REBUT "
                      "<ID>: <reason>. Refine or defend your own positions. "
                      "Add NEW entries the other participants missed, in "
                      "SHARED FORMAT. Do not state agreement without adding "
                      "a new reason.")

SOLO_PROMPT = ("You are one analyst mapping ALL arguments on: {proposition}. "
               "Context: {context}. Work in passes: (1) obvious arguments on "
               "every side; (2) adopt each of these lenses in turn and add "
               "what each would say: {roles}; (3) ask twice: what would a "
               "specialist add, and which stakeholder is missing; (4) attack "
               "your own list for gaps and fill them; (5) output the final "
               "deduplicated list, minimum {min_entries} entries, in SHARED "
               "FORMAT only.\n\nSHARED FORMAT:\n{shared_format}")

SYNTH_PROMPT = """You are the EDITOR. Below are {n} argument lists produced independently by different roles on the question:
{question}

Merge them into ONE master list:
- Merge entries that make the same core claim into a single entry.
- Each master entry keeps SHARED FORMAT and adds two lines:
  CONV: k        (k = how many of the {n} role lists independently produced this core claim)
  SOURCES: the original entry IDs that were merged into it
- When duplicates disagree, keep the strongest, most specific DEFEATER.
- Renumber entries as M-1, M-2, ... and group them by SIDE and then by theme.
- Output ONLY the numbered master list, nothing else.

SHARED FORMAT:
{shared_format}

ROLE LISTS:
{lists}"""

CRITIC_PROMPT = """You are the GAP CRITIC. Below is a master list of arguments on the question:
{question}

This master list is your ONLY input. Do NOT restate or rewrite anything that is already present.

Tasks:
1) Add every missing argument you can find as new entries with IDs CRITIC-1, CRITIC-2, ... in SHARED FORMAT.
2) Then, under a heading WEAKEST, name the 3 weakest entries of the master list by their ID, one line each, with the reason.

SHARED FORMAT:
{shared_format}

MASTER LIST:
{master}"""

DEBATE_SYNTH_PROMPT = """You are the EDITOR. Below is the full transcript of a structured moot on the question:
{question}

Produce the deduplicated master list of arguments that emerged:
- Merge entries that make the same core claim; keep the strongest, most specific DEFEATER.
- Each master entry keeps SHARED FORMAT, renumbered M-1, M-2, ..., grouped by SIDE and theme.
- Add the line "TESTED: yes" to every entry that was rebutted during the moot and survived (the rebuttal was answered or fails).
- Entries that were killed (rebutted with no successful defence) do NOT appear in the master list. Instead list them at the end under a heading GRAVEYARD, one line each: the entry ID, its claim, and the rebuttal that killed it.
- Output ONLY the master list followed by the GRAVEYARD section, nothing else.

SHARED FORMAT:
{shared_format}

TRANSCRIPT:
{transcript}"""

JUDGE_PROMPT = """You are judging four anonymous systems, SYSTEM-A, SYSTEM-B, SYSTEM-C and SYSTEM-D. Each tried to produce the most complete map of distinct, grounded arguments on one question. You do not know what the systems are. Judge only what is on the page.

Question: {question}

SYSTEM-A:
{list_a}

SYSTEM-B:
{list_b}

SYSTEM-C:
{list_c}

SYSTEM-D:
{list_d}

Tasks:
1) Build the union of semantically distinct arguments across the four lists. Two entries count as the same argument only if their core claim is the same. For each union argument give: id "U-n"; theme (2-4 words); side (FOR, AGAINST or REFRAME); short_claim (max 12 words); found_in listing which of A, B, C, D contain that same core claim (strictly by same core claim).
2) For each system: distinct (how many union arguments it contains), unique (how many union arguments only it contains), suspect (how many of its entries have REASONING or DEFEATER text that invents facts, cases, statistics or authorities), depth (1-5: are its DEFEATERs specific and falsifiable), notes (short; name suspect entries by their ID and say why).
3) verdict: 2-3 sentences comparing the four systems, blind.

Output STRICT JSON only, no markdown fences, no prose outside the JSON, exactly this schema:
{{"union":[{{"id":"U-1","theme":"...","side":"FOR","short_claim":"...","found_in":["A","C"]}}],"per_system":{{"A":{{"distinct":0,"unique":0,"suspect":0,"depth":0,"notes":"..."}},"B":{{}},"C":{{}},"D":{{}}}},"verdict":"..."}}"""

JUDGE_REPAIR_PROMPT = """Your previous output was not valid JSON matching the required schema.
Problems: {errors}

Output ONLY the corrected JSON object. No markdown fences, no prose.

Required schema:
{{"union":[{{"id":"U-1","theme":"...","side":"FOR","short_claim":"...","found_in":["A","C"]}}],"per_system":{{"A":{{"distinct":0,"unique":0,"suspect":0,"depth":0,"notes":"..."}},"B":{{}},"C":{{}},"D":{{}}}},"verdict":"..."}}

Previous output:
{previous}"""

ROLE_DESCRIPTIONS = {
    "ADVOCATE": "You are the ADVOCATE. Make the strongest, most complete case FOR the proposition.",
    "OPPONENT": "You are the OPPONENT. Make the strongest, most complete case AGAINST the proposition.",
    "SKEPTIC": "You are the SKEPTIC. Attack the framing, hidden assumptions and false dichotomies on every side; prefer REFRAME entries where the question itself is flawed.",
}

ALL_ROLES = ["ADVOCATE", "OPPONENT", "SKEPTIC", "LENS_A", "LENS_B"]
QUICK_ROLES = ["ADVOCATE", "OPPONENT", "SKEPTIC"]
PARTICIPANT_LETTERS = "ABCDEFGH"

DEFAULT_LENS_A = "an economist focused on incentives, costs and second-order effects"
DEFAULT_LENS_B = "an affected third party who has to live with the outcome but was not consulted"


# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------

def utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def read_text(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def die(msg):
    print("FATAL: " + providers.redact(msg), file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print("WARNING: " + providers.redact(msg), file=sys.stderr)


def est_tokens(text):
    """Upper-bound-ish token estimate for PRE-FLIGHT ONLY (chars/4).

    Never used for accounting — real token counts come from provider usage
    fields, and are recorded as null when a provider omits them.
    """
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# run context: one benchmark run on disk
# ---------------------------------------------------------------------------

class RunContext(object):

    def __init__(self, run_dir, cfg, args, question, context, roles,
                 lens_a, lens_b, rounds, seed):
        self.run_dir = run_dir
        self.cfg = cfg
        self.args = args
        self.question = question
        self.context = context or "none"
        self.roles = roles
        self.lens_a = lens_a
        self.lens_b = lens_b
        self.rounds = rounds
        self.rng = random.Random(seed)
        self.seed = seed
        self.lock = threading.Lock()
        self.phase_walls = {}
        self.warnings = []
        self.failed_calls = []
        for sub in ("gen", "arm1", "arm2", "arm3", "arm4", "judge"):
            os.makedirs(os.path.join(run_dir, sub), exist_ok=True)

    def role_description(self, role):
        if role == "LENS_A":
            return ("You analyse the proposition strictly through this lens: "
                    + self.lens_a + ".")
        if role == "LENS_B":
            return ("You analyse the proposition strictly through this lens: "
                    + self.lens_b + ".")
        return ROLE_DESCRIPTIONS[role]

    def lens_names(self):
        out = []
        for r in self.roles:
            if r == "LENS_A":
                out.append("LENS_A (%s)" % self.lens_a)
            elif r == "LENS_B":
                out.append("LENS_B (%s)" % self.lens_b)
            else:
                out.append(r)
        return out

    def note(self, msg):
        warn(msg)
        self.warnings.append(providers.redact(msg))

    # -- the one gate through which every model call passes ----------------

    def call(self, *, arm, phase, role, provider, model, prompt, name,
             subdir, temperature, max_tokens, json_mode=False):
        """Save prompt, call provider, save raw output, log to calls.jsonl.

        Returns the reply text, or None if the call FAILED (already logged;
        content is never substituted)."""
        prompt_file = os.path.join(subdir, "p-%s.txt" % name)
        output_file = os.path.join(subdir, "o-%s.txt" % name)
        write_text(os.path.join(self.run_dir, prompt_file), prompt)

        executor = self.cfg.get("executor", "claude_cli")
        retries_cfg = int(self.cfg.get("retries", {}).get("max", 3))
        mock = executor == "mock"
        rec, failed_msg = None, None
        try:
            rec = providers.call_model(
                provider, model, prompt,
                temperature=temperature, max_tokens=max_tokens,
                max_retries=retries_cfg, json_mode=json_mode,
                executor=executor, mock=mock,
                fixture_dir=self.cfg.get("fixture_dir"),
                fixture=name + ".txt")
        except providers.CallFailed as exc:
            failed_msg = providers.redact(str(exc))
            rec = {"text": None, "input_tokens": None, "output_tokens": None,
                   "model": model, "latency_ms": None,
                   "http_status": exc.http_status, "retries": exc.retries}

        if failed_msg is None:
            write_text(os.path.join(self.run_dir, output_file), rec["text"])
        else:
            output_file = os.path.join(subdir, "o-%s.FAILED.txt" % name)
            write_text(os.path.join(self.run_dir, output_file),
                       "CALL FAILED (no content substituted)\n" + failed_msg)
            with self.lock:
                self.failed_calls.append("%s/%s (%s)" % (arm, name, failed_msg))
            warn("call FAILED %s/%s: %s" % (arm, name, failed_msg))

        cost, cost_note = self._cost(model, rec["input_tokens"],
                                     rec["output_tokens"])
        params = {"temperature": temperature, "max_tokens": max_tokens}
        if provider == "anthropic" and executor == "claude_cli":
            params["temperature"] = None
            params["note"] = ("claude_cli executor: temperature not settable; "
                              "max_tokens enforced via "
                              "CLAUDE_CODE_MAX_OUTPUT_TOKENS")
        if rec.get("param_note"):
            params["note"] = rec["param_note"]
        line = {
            "ts": utc_now_iso(), "arm": arm, "phase": phase, "role": role,
            "provider": provider, "model": rec.get("model") or model,
            "prompt_file": prompt_file, "output_file": output_file,
            "input_tokens": rec["input_tokens"],
            "output_tokens": rec["output_tokens"],
            "cost_usd": cost, "latency_ms": rec["latency_ms"],
            "http_status": rec["http_status"], "retries": rec["retries"],
            "params": params, "failed": failed_msg is not None,
        }
        if cost_note:
            line["cost_note"] = cost_note
        if failed_msg is not None:
            line["error"] = failed_msg
        if rec.get("cli_reported_cost_usd") is not None:
            line["cli_reported_cost_usd"] = rec["cli_reported_cost_usd"]
        with self.lock:
            with open(os.path.join(self.run_dir, "calls.jsonl"), "a",
                      encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
        return rec["text"] if failed_msg is None else None

    def _cost(self, model, in_tok, out_tok):
        prices = self.cfg.get("prices", {})
        p = prices.get(model)
        if p is None:
            return None, "no price for model %s in config prices" % model
        if in_tok is None or out_tok is None:
            return None, "provider omitted usage; tokens null, cost not computed"
        cost = (in_tok / 1e6) * float(p["input_per_mtok"]) \
             + (out_tok / 1e6) * float(p["output_per_mtok"])
        return round(cost, 6), None

    def timed_phase(self, key):
        ctx = self

        class _T(object):
            def __enter__(self_t):
                self_t.t0 = time.monotonic()
            def __exit__(self_t, *a):
                ctx.phase_walls[key] = round(
                    ctx.phase_walls.get(key, 0.0)
                    + time.monotonic() - self_t.t0, 3)
        return _T()


# ---------------------------------------------------------------------------
# prompt assembly
# ---------------------------------------------------------------------------

def generator_prompt(ctx, role):
    return ("Proposition under debate: %s\n\nContext all agents assume: %s\n\n"
            "%s\n\n%s\n\n%s"
            % (ctx.question, ctx.context, ctx.role_description(role),
               SHARED_FORMAT, GEN_TAIL))


def _placeholder(role, roles):
    # opaque token: must not contain the role name itself, or the
    # role-name substitution pass would match inside it
    return "\x00P%d\x00" % roles.index(role)


def render_contribution(text, source_round, mappings, current_round, roles):
    """Rewrite author-identifying tokens to the current round's letters.

    Round-1 texts contain role names (SHARED FORMAT IDs like ADVOCATE-3).
    Round-k (k>=2) texts additionally contain the participant letters the
    author saw under round k's mapping (e.g. 'REBUT B-2'). Both are
    translated through the saved mappings into the CURRENT round's fresh
    letters, via placeholders so replacements cannot cascade.
    """
    cur = mappings[current_round]  # role -> letter
    if source_round >= 2 and source_round in mappings:
        letter_to_role = {v: k for k, v in mappings[source_round].items()}

        def sub_id(m):
            r = letter_to_role.get(m.group(1))
            return (_placeholder(r, roles) + "-" + m.group(2)) if r \
                else m.group(0)

        def sub_part(m):
            r = letter_to_role.get(m.group(1))
            return ("Participant " + _placeholder(r, roles)) if r \
                else m.group(0)

        text = re.sub(r"\b([A-H])-(\d+)\b", sub_id, text)
        text = re.sub(r"\bParticipant ([A-H])\b", sub_part, text)
    for role in sorted(roles, key=len, reverse=True):
        text = re.sub(r"\b%s\b" % re.escape(role),
                      _placeholder(role, roles).replace("\\", "\\\\"), text)
    for role in roles:
        text = text.replace(_placeholder(role, roles), cur[role])
    return text


def render_transcript(history, mappings, current_round, roles):
    """Anonymised transcript for debate rounds.
    history: list of {round, role, text} in chronological order."""
    parts = []
    for h in history:
        who = "Participant " + mappings[current_round][h["role"]]
        body = render_contribution(h["text"], h["round"], mappings,
                                   current_round, roles)
        parts.append("=== %s (round %d) ===\n%s" % (who, h["round"], body))
    return "\n\n".join(parts)


def deanonymise_contribution(text, source_round, mappings):
    """Translate participant letters an agent wrote under round-k's mapping
    back to role names. Needed for the debate-synth editor: without it the
    transcript mixes role-name headers with letter references (e.g.
    'REBUT B-6') whose letters mean different roles in different rounds,
    making rebuttal targets unresolvable."""
    if source_round < 2 or source_round not in mappings:
        return text
    letter_to_role = {v: k for k, v in mappings[source_round].items()}

    def sub_id(m):
        r = letter_to_role.get(m.group(1))
        return (r + "-" + m.group(2)) if r else m.group(0)

    def sub_part(m):
        r = letter_to_role.get(m.group(1))
        return ("Participant " + r) if r else m.group(0)

    text = re.sub(r"\b([A-H])-(\d+)\b", sub_id, text)
    text = re.sub(r"\bParticipant ([A-H])\b", sub_part, text)
    return text


def render_transcript_for_editor(history, mappings):
    """Fully de-anonymised transcript (role-name headers AND role-name
    rebuttal references) for the debate-synth editor."""
    parts = []
    for h in history:
        body = deanonymise_contribution(h["text"], h["round"], mappings)
        parts.append("=== %s (round %d) ===\n%s"
                     % (h["role"], h["round"], body))
    return "\n\n".join(parts)


def debate_prompt(ctx, role, r, transcript, own_letter):
    return ("Proposition under debate: %s\n\nContext all agents assume: %s\n\n"
            "%s\n\nFull transcript so far (participants are anonymised; your "
            "own earlier entries appear under Participant %s):\n\n%s\n\n%s\n\n"
            "SHARED FORMAT:\n%s"
            % (ctx.question, ctx.context, ctx.role_description(role),
               own_letter, transcript, DEBATE_INSTRUCTION.format(r=r),
               SHARED_FORMAT))


# ---------------------------------------------------------------------------
# pipeline phases
# ---------------------------------------------------------------------------

def phase_gen(ctx):
    """Shared round-1 generation: reused by arms 1 AND 2 (paired design)."""
    gen_params = ctx.cfg["params"]
    outputs = {}

    def one(role):
        entry = ctx.roster_for(role)
        text = ctx.call(
            arm="gen", phase="gen", role=role,
            provider=entry["provider"], model=entry["model"],
            prompt=generator_prompt(ctx, role), name="gen-" + role,
            subdir="gen",
            temperature=gen_params["gen_temperature"],
            max_tokens=gen_params["gen_max_tokens"])
        return role, text

    with ctx.timed_phase("gen"):
        with ThreadPoolExecutor(max_workers=len(ctx.roles)) as pool:
            for role, text in pool.map(one, ctx.roles):
                if text is not None:
                    outputs[role] = text
    if not outputs:
        die("all round-1 generation calls failed; aborting run")
    return outputs


def phase_arm1(ctx, gen_outputs):
    p = ctx.cfg["params"]
    history = [{"round": 1, "role": r, "text": t}
               for r, t in gen_outputs.items()]
    mappings = {}

    with ctx.timed_phase("arm1_rounds"):
        for r in range(2, ctx.rounds + 1):
            letters = list(PARTICIPANT_LETTERS[:len(ctx.roles)])
            ctx.rng.shuffle(letters)
            mappings[r] = dict(zip(ctx.roles, letters))
            write_text(os.path.join(ctx.run_dir, "arm1",
                                    "round%d_mapping.json" % r),
                       json.dumps(mappings[r], indent=2))

            def one(role, rr=r):
                transcript = render_transcript(history, mappings, rr,
                                               ctx.roles)
                entry = ctx.roster_for(role)
                text = ctx.call(
                    arm="arm1", phase="round%d" % rr, role=role,
                    provider=entry["provider"], model=entry["model"],
                    prompt=debate_prompt(ctx, role, rr, transcript,
                                         mappings[rr][role]),
                    name="r%d-%s" % (rr, role), subdir="arm1",
                    temperature=p["gen_temperature"],
                    max_tokens=p["gen_max_tokens"])
                return role, text

            round_outputs = []
            with ThreadPoolExecutor(max_workers=len(ctx.roles)) as pool:
                for role, text in pool.map(one, ctx.roles):
                    if text is not None:
                        round_outputs.append(
                            {"round": r, "role": role, "text": text})
            history.extend(round_outputs)

    with ctx.timed_phase("arm1_synth"):
        transcript_plain = render_transcript_for_editor(history, mappings)
        ed = ctx.cfg["editor"]
        final = ctx.call(
            arm="arm1", phase="synth", role="EDITOR",
            provider=ed["provider"], model=ed["model"],
            prompt=DEBATE_SYNTH_PROMPT.format(
                question=ctx.question, shared_format=SHARED_FORMAT,
                transcript=transcript_plain),
            name="arm1-synth", subdir="arm1",
            temperature=p["editor_temperature"],
            max_tokens=p["editor_max_tokens"])
    if final is not None:
        write_text(os.path.join(ctx.run_dir, "arm1", "final.txt"), final)
    return final


def exam_tail(ctx, gen_outputs, arm):
    """The exam+examiner tail shared by arms 2 and 4: editor synthesis with
    CONV:k tags, then a gap critic that sees ONLY the master list, appended
    mechanically. Same editor model in both arms (fairness invariant)."""
    p = ctx.cfg["params"]
    ed = ctx.cfg["editor"]
    lists = "\n\n".join("--- LIST FROM %s ---\n%s" % (role, text)
                        for role, text in gen_outputs.items())
    with ctx.timed_phase(arm + "_synth"):
        master = ctx.call(
            arm=arm, phase="synth", role="EDITOR",
            provider=ed["provider"], model=ed["model"],
            prompt=SYNTH_PROMPT.format(
                n=len(gen_outputs), question=ctx.question,
                shared_format=SHARED_FORMAT, lists=lists),
            name=arm + "-synth", subdir=arm,
            temperature=p["editor_temperature"],
            max_tokens=p["editor_max_tokens"])
    if master is None:
        return None
    with ctx.timed_phase(arm + "_critic"):
        critic = ctx.call(
            arm=arm, phase="critic", role="EDITOR",
            provider=ed["provider"], model=ed["model"],
            prompt=CRITIC_PROMPT.format(
                question=ctx.question, shared_format=SHARED_FORMAT,
                master=master),
            name=arm + "-critic", subdir=arm,
            temperature=p["editor_temperature"],
            max_tokens=p["editor_max_tokens"])
    # mechanical append, no rewriting
    final = master
    if critic is not None:
        final = (master
                 + "\n\n=== GAP CRITIC ADDITIONS (appended mechanically, "
                   "not edited) ===\n" + critic)
    write_text(os.path.join(ctx.run_dir, arm, "final.txt"), final)
    return final


def phase_arm2(ctx, gen_outputs):
    return exam_tail(ctx, gen_outputs, "arm2")


def phase_arm4(ctx):
    """ARM 4 — PERSONA CONTROL: the solo model plays every role through the
    exact arm-2 topology (isolated generation, then editor + gap critic).
    Its generations are its OWN (not the shared GEN, which uses the diverse
    roster): arm4 vs arm3 isolates persona-splitting, arm2 vs arm4 isolates
    vendor diversity, arm1 vs arm2 isolates the debate transcript."""
    p = ctx.cfg["params"]
    solo = ctx.cfg["solo"]

    def one(role):
        text = ctx.call(
            arm="arm4", phase="gen", role=role,
            provider=solo["provider"], model=solo["model"],
            prompt=generator_prompt(ctx, role), name="arm4-gen-" + role,
            subdir="arm4",
            temperature=p["gen_temperature"],
            max_tokens=p["gen_max_tokens"])
        return role, text

    outputs = {}
    with ctx.timed_phase("arm4_gen"):
        with ThreadPoolExecutor(max_workers=len(ctx.roles)) as pool:
            for role, text in pool.map(one, ctx.roles):
                if text is not None:
                    outputs[role] = text
    if not outputs:
        ctx.note("arm4 FAILED: all persona-control generation calls failed")
        return None
    return exam_tail(ctx, outputs, "arm4")


def phase_arm3(ctx):
    p = ctx.cfg["params"]
    solo = ctx.cfg["solo"]
    prompt = SOLO_PROMPT.format(
        proposition=ctx.question, context=ctx.context,
        roles=", ".join(ctx.lens_names()),
        min_entries=len(ctx.roles) * 8, shared_format=SHARED_FORMAT)
    with ctx.timed_phase("arm3"):
        final = ctx.call(
            arm="arm3", phase="solo", role="SOLO",
            provider=solo["provider"], model=solo["model"],
            prompt=prompt, name="arm3-solo", subdir="arm3",
            temperature=p["gen_temperature"],
            max_tokens=p["gen_max_tokens"])
    if final is not None:
        write_text(os.path.join(ctx.run_dir, "arm3", "final.txt"), final)
    return final


# ---------------------------------------------------------------------------
# judging
# ---------------------------------------------------------------------------

JUDGE_SIDES = ("FOR", "AGAINST", "REFRAME")


def validate_judge_json(data):
    errs = []
    if not isinstance(data, dict):
        return ["top level is not a JSON object"]
    union = data.get("union")
    if not isinstance(union, list) or not union:
        errs.append("union must be a non-empty array")
    else:
        for i, u in enumerate(union):
            if not isinstance(u, dict):
                errs.append("union[%d] is not an object" % i)
                continue
            for k in ("id", "theme", "side", "short_claim", "found_in"):
                if k not in u:
                    errs.append("union[%d] missing key %s" % (i, k))
            if u.get("side") not in JUDGE_SIDES:
                errs.append("union[%d].side must be FOR|AGAINST|REFRAME" % i)
            fi = u.get("found_in")
            if (not isinstance(fi, list) or not fi
                    or not set(fi) <= {"A", "B", "C", "D"}):
                errs.append("union[%d].found_in must be a non-empty subset "
                            "of [A,B,C,D]" % i)
    ps = data.get("per_system")
    if not isinstance(ps, dict) or not {"A", "B", "C", "D"} <= set(ps):
        errs.append("per_system must contain A, B, C and D")
    else:
        for s in ("A", "B", "C", "D"):
            e = ps[s]
            if not isinstance(e, dict):
                errs.append("per_system.%s is not an object" % s)
                continue
            for k in ("distinct", "unique", "suspect", "depth"):
                if not isinstance(e.get(k), (int, float)) \
                        or isinstance(e.get(k), bool):
                    errs.append("per_system.%s.%s must be a number" % (s, k))
            if not isinstance(e.get("notes", ""), str):
                errs.append("per_system.%s.notes must be a string" % s)
    if not isinstance(data.get("verdict"), str) or not data.get("verdict"):
        errs.append("verdict must be a non-empty string")
    return errs


def extract_json(text):
    """Parse model output as JSON, tolerating markdown fences and prose
    around a single top-level object."""
    if text is None:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except ValueError:
            return None
    return None


def phase_judge(ctx, finals):
    """finals: {'arm1': text|None, 'arm2': ..., 'arm3': ...}"""
    p = ctx.cfg["params"]
    jd = ctx.cfg["judge"]
    missing = [a for a, t in finals.items() if t is None]
    if missing:
        ctx.note("judging FAILED: missing final list(s) from %s"
                 % ", ".join(missing))
        write_text(os.path.join(ctx.run_dir, "judge", "FAILED.txt"),
                   "judging skipped: missing final lists: %s" % missing)
        return None

    arms = ["arm1", "arm2", "arm3", "arm4"]
    ctx.rng.shuffle(arms)
    blind = dict(zip(["A", "B", "C", "D"], arms))  # letter -> arm
    write_text(os.path.join(ctx.run_dir, "judge", "blind_mapping.json"),
               json.dumps(blind, indent=2))

    prompt = JUDGE_PROMPT.format(
        question=ctx.question,
        list_a=finals[blind["A"]], list_b=finals[blind["B"]],
        list_c=finals[blind["C"]], list_d=finals[blind["D"]])

    with ctx.timed_phase("judge"):
        text = ctx.call(
            arm="judge", phase="judge", role="JUDGE",
            provider=jd["provider"], model=jd["model"], prompt=prompt,
            name="judge", subdir="judge",
            temperature=p["judge_temperature"],
            max_tokens=p["judge_max_tokens"], json_mode=True)
        data = extract_json(text)
        errs = validate_judge_json(data) if data is not None else \
            ["output was not parseable JSON"]
        if errs and text is not None:
            ctx.note("judge output invalid (%s); one repair attempt"
                     % "; ".join(errs[:3]))
            repair = ctx.call(
                arm="judge", phase="judge-repair", role="JUDGE",
                provider=jd["provider"], model=jd["model"],
                prompt=JUDGE_REPAIR_PROMPT.format(
                    errors="; ".join(errs), previous=text),
                name="judge-repair", subdir="judge",
                temperature=p["judge_temperature"],
                max_tokens=p["judge_max_tokens"], json_mode=True)
            data = extract_json(repair)
            errs = validate_judge_json(data) if data is not None else \
                ["repair output was not parseable JSON"]
        if errs or data is None:
            ctx.note("judging FAILED after repair attempt: %s"
                     % "; ".join(errs[:5]))
            write_text(os.path.join(ctx.run_dir, "judge", "FAILED.txt"),
                       "judge JSON invalid after one repair attempt:\n"
                       + "\n".join(errs))
            return None

    # sanity-check the judge's arithmetic: recompute distinct/unique from
    # the union matrix and prefer the recomputed values
    recomputed = {s: {"distinct": 0, "unique": 0} for s in "ABCD"}
    for u in data["union"]:
        fi = sorted(set(u["found_in"]))
        for s in fi:
            recomputed[s]["distinct"] += 1
        if len(fi) == 1:
            recomputed[fi[0]]["unique"] += 1
    for s in "ABCD":
        for k in ("distinct", "unique"):
            claimed = data["per_system"][s].get(k)
            if claimed != recomputed[s][k]:
                ctx.note("judge arithmetic: %s.%s claimed %s, recomputed %s "
                         "from union matrix; using recomputed"
                         % (s, k, claimed, recomputed[s][k]))
            data["per_system"][s][k + "_claimed"] = claimed
            data["per_system"][s][k] = recomputed[s][k]

    write_text(os.path.join(ctx.run_dir, "judge", "judge.json"),
               json.dumps(data, indent=2))
    return data


# ---------------------------------------------------------------------------
# config resolution & fairness invariants
# ---------------------------------------------------------------------------

def load_config(path):
    if not os.path.exists(path):
        die("config not found: %s" % path)
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def provider_available(provider, executor):
    if executor == "mock":
        return True  # offline fixtures, no keys involved
    if provider == "anthropic" and executor == "claude_cli":
        return True  # runs on existing Claude Code auth
    return bool(os.environ.get(providers.KEY_ENV.get(provider, ""), ""))


def resolve_roster(cfg, roles, notes):
    """Enforce: identical roster for arms 1 and 2 (one shared roster object);
    round-robin fill when fewer providers than roles are usable."""
    executor = cfg.get("executor", "claude_cli")
    configured = {e["role"]: e for e in cfg.get("roster", [])}
    usable = []
    for role in roles:
        e = configured.get(role)
        if e and e.get("model") not in (None, "", "SET_ME") \
                and provider_available(e["provider"], executor):
            usable.append({"provider": e["provider"], "model": e["model"]})
    if not usable:
        for cand in (cfg.get("editor"), cfg.get("solo")):
            if cand and cand.get("model") not in (None, "", "SET_ME") \
                    and provider_available(cand["provider"], executor):
                usable.append({"provider": cand["provider"],
                               "model": cand["model"]})
                break
    if not usable:
        die("no usable roster entries: fill config.yaml model IDs "
            "(--list-models) and/or export provider API keys")
    roster = {}
    for i, role in enumerate(roles):
        e = configured.get(role)
        if e and e.get("model") not in (None, "", "SET_ME") \
                and provider_available(e["provider"], executor):
            roster[role] = {"provider": e["provider"], "model": e["model"]}
        else:
            pick = usable[i % len(usable)]
            roster[role] = dict(pick)
            notes.append("role %s assigned %s/%s round-robin (configured "
                         "entry missing, SET_ME, or key absent); model "
                         "diversity is reduced"
                         % (role, pick["provider"], pick["model"]))
    return roster


def check_judge_provider(cfg, roster, force, notes):
    roster_providers = {e["provider"] for e in roster.values()}
    jp = cfg["judge"]["provider"]
    if jp in roster_providers:
        msg = ("JUDGE PROVIDER '%s' IS IN THE GENERATION ROSTER %s — the "
               "blind judge may favour its own family (self-preference). "
               "The design requires a judge from a provider outside the "
               "roster." % (jp, sorted(roster_providers)))
        print("\n" + "!" * 78 + "\n" + msg + "\n" + "!" * 78 + "\n",
              file=sys.stderr)
        if not force:
            die("refusing to run: judge provider overlaps roster "
                "(use --force to override)")
        notes.append("SELF-PREFERENCE CAVEAT: " + msg + " Run forced with "
                     "--force.")
        return True
    return False


MOCK_CONFIG = {
    "executor": "mock",
    "roster": [
        {"role": r, "provider": "mock", "model": "mock-gen"}
        for r in ALL_ROLES
    ],
    "editor": {"provider": "mock", "model": "mock-editor"},
    "solo": {"provider": "mock", "model": "mock-solo"},
    "judge": {"provider": "mock", "model": "mock-judge"},
    "params": {
        "gen_temperature": 0.7, "editor_temperature": 0.2,
        "judge_temperature": 0.0, "gen_max_tokens": 4000,
        "editor_max_tokens": 8000, "judge_max_tokens": 8000,
    },
    "prices": {
        "as_of": "2026-07-19",
        "mock-gen": {"input_per_mtok": 1.0, "output_per_mtok": 4.0},
        "mock-editor": {"input_per_mtok": 2.0, "output_per_mtok": 8.0},
        "mock-solo": {"input_per_mtok": 1.0, "output_per_mtok": 4.0},
        "mock-judge": {"input_per_mtok": 2.0, "output_per_mtok": 8.0},
    },
    "retries": {"max": 3},
    "runs": 1,
    "fixture_dir": "fixtures",
}


# ---------------------------------------------------------------------------
# preflight cost estimate
# ---------------------------------------------------------------------------

def preflight_estimate(cfg, question, context, roles, rounds, roster):
    """Upper-bound estimate per arm from prompt sizes, max_tokens caps and
    the prices table. Estimates only; accounting uses exact usage."""
    p = cfg["params"]
    prices = cfg.get("prices", {})
    unknown = set()

    def price(model, in_tok, out_tok):
        pr = prices.get(model)
        if pr is None:
            unknown.add(model)
            return 0.0
        return (in_tok / 1e6) * float(pr["input_per_mtok"]) \
             + (out_tok / 1e6) * float(pr["output_per_mtok"])

    n = len(roles)
    base = est_tokens(question) + est_tokens(context or "none") \
        + est_tokens(SHARED_FORMAT) + 150
    gmax, emax, jmax = (p["gen_max_tokens"], p["editor_max_tokens"],
                        p["judge_max_tokens"])
    ed_model = cfg["editor"]["model"]

    gen_cost = sum(price(roster[r]["model"], base, gmax) for r in roles)
    arm1 = 0.0
    for r in range(2, rounds + 1):
        transcript = n * gmax * (r - 1)
        arm1 += sum(price(roster[role]["model"], base + transcript, gmax)
                    for role in roles)
    arm1 += price(ed_model, base + n * gmax * rounds, emax)
    arm2 = price(ed_model, base + n * gmax, emax) \
         + price(ed_model, base + emax, emax)
    solo_cost = price(cfg["solo"]["model"], base + 100, gmax)
    solo_model = cfg["solo"]["model"]
    arm4 = n * price(solo_model, base, gmax) \
         + price(ed_model, base + n * gmax, emax) \
         + price(ed_model, base + emax, emax)
    judge_cost = price(cfg["judge"]["model"], base + 4 * emax, jmax)

    est = {
        "arm1 (debate)":   gen_cost + arm1,
        "arm2 (exam)":     gen_cost + arm2,
        "arm3 (solo)":     solo_cost,
        "arm4 (persona)":  arm4,
        "judge":           judge_cost,
    }
    actual_total = gen_cost + arm1 + arm2 + solo_cost + arm4 + judge_cost
    return est, actual_total, unknown


# ---------------------------------------------------------------------------
# intake
# ---------------------------------------------------------------------------

def ask(prompt_text, default=None):
    suffix = (" [%s]" % default) if default else ""
    try:
        val = input(prompt_text + suffix + ": ").strip()
    except EOFError:
        val = ""
    return val or (default or "")


def intake(args):
    """Interactive intake, skippable via flags. Never spends tokens."""
    proposition = args.proposition
    interactive = sys.stdin.isatty() and not args.mock

    # 1. debatable question
    if args.question:
        question = args.question
    else:
        default_q = proposition if proposition.rstrip().endswith("?") \
            else proposition.rstrip(".") + "?"
        if interactive:
            print("\nProposition: %s" % proposition)
            question = ask("Restated as a single debatable question — "
                           "confirm or correct", default_q)
        else:
            question = default_q

    # 2. shared context
    if args.context is not None:
        context = args.context
    elif interactive:
        raw = ask("Context all agents should assume (inline text, a file "
                  "path, or empty for none)", "")
        context = raw
    else:
        context = ""
    if context and os.path.exists(context):
        context = read_text(context)

    # 3. lenses
    lens_a = args.lens_a or DEFAULT_LENS_A
    lens_b = args.lens_b or DEFAULT_LENS_B
    if interactive and not (args.lens_a and args.lens_b):
        print("\nADVOCATE, OPPONENT and SKEPTIC are fixed. Proposed "
              "topic-specific lenses:")
        lens_a = ask("  LENS_A — confirm or edit", lens_a)
        lens_b = ask("  LENS_B — confirm or edit", lens_b)

    # 4. scale
    if args.quick:
        scale = "quick"
    elif interactive:
        scale = ask("Scale: quick (3 roles, 2 rounds) or full "
                    "(5 roles, 3 rounds)", "full").lower()
        scale = "quick" if scale.startswith("q") else "full"
    else:
        scale = "full"
    return question, context, lens_a, lens_b, scale


# ---------------------------------------------------------------------------
# main pipeline
# ---------------------------------------------------------------------------

def execute_run(run_dir, cfg, args, question, context, roles, lens_a, lens_b,
                rounds, seed, notes):
    ctx = RunContext(run_dir, cfg, args, question, context, roles,
                     lens_a, lens_b, rounds, seed)
    ctx.warnings.extend(notes)
    roster = cfg["_resolved_roster"]
    ctx.roster_for = lambda role: roster[role]
    started = utc_now_iso()

    # secrets never reach this file: cfg contains no keys by construction
    resolved = {k: v for k, v in cfg.items() if k != "_resolved_roster"}
    resolved["resolved_roster"] = roster
    resolved["question"] = question
    resolved["context"] = context or "none"
    resolved["lens_a"] = lens_a
    resolved["lens_b"] = lens_b
    resolved["rounds"] = rounds
    resolved["roles"] = roles
    resolved["seed"] = seed
    write_text(os.path.join(run_dir, "config_resolved.yaml"),
               yaml.safe_dump(resolved, sort_keys=False))

    print("\n[1/7] shared GEN round (counted into arms 1 AND 2)")
    gen_outputs = phase_gen(ctx)
    print("[2/7] arm 3 — solo control")
    final3 = phase_arm3(ctx)
    print("[3/7] arm 4 — persona control (solo model, exam topology)")
    final4 = phase_arm4(ctx)
    print("[4/7] arm 2 — exam + examiner tail")
    final2 = phase_arm2(ctx, gen_outputs)
    print("[5/7] arm 1 — debate rounds 2..%d + synthesis" % rounds)
    final1 = phase_arm1(ctx, gen_outputs)
    print("[6/7] blind judge")
    judge_data = phase_judge(ctx, {"arm1": final1, "arm2": final2,
                                   "arm3": final3, "arm4": final4})
    print("[7/7] metrics + report")

    meta = {
        "run_id": os.path.basename(run_dir),
        "question": question,
        "context": context or "none",
        "roles": roles, "rounds": rounds,
        "lens_a": lens_a, "lens_b": lens_b,
        "scale": "quick" if len(roles) == 3 else "full",
        "seed": seed,
        "started_utc": started, "finished_utc": utc_now_iso(),
        "phase_walls_s": ctx.phase_walls,
        "warnings": ctx.warnings,
        "failed_calls": ctx.failed_calls,
        "judging_failed": judge_data is None,
    }
    write_text(os.path.join(run_dir, "run_meta.json"),
               json.dumps(meta, indent=2))

    report_mod.render_run_report(run_dir)
    print("run complete: %s" % run_dir)
    print("  report: %s" % os.path.join(run_dir, "report.html"))
    return run_dir


def mock_self_check(run_dir):
    """Prove on-disk delivery of the audit-trail contract after a --mock run:
    judge/judge.json exists, still passes the strict schema, and preserves
    the judge's claimed arithmetic next to the recomputed values. Fails the
    run loudly if any part of the contract is missing."""
    path = os.path.join(run_dir, "judge", "judge.json")
    if not os.path.exists(path):
        die("mock self-check FAILED: %s was not written" % path)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    errs = validate_judge_json(data)
    if errs:
        die("mock self-check FAILED: judge.json violates schema: %s"
            % "; ".join(errs[:5]))
    for s in "ABCD":
        for k in ("distinct_claimed", "unique_claimed"):
            if k not in data["per_system"][s]:
                die("mock self-check FAILED: per_system.%s.%s missing — "
                    "claimed-vs-recomputed audit trail not preserved" % (s, k))
    calls_path = os.path.join(run_dir, "calls.jsonl")
    n_calls = sum(1 for line in open(calls_path, encoding="utf-8") if line.strip())
    print("MOCK SELF-CHECK OK: judge.json present, schema-valid, "
          "claimed-vs-recomputed audit trail preserved; %d calls logged"
          % n_calls)


def cmd_list_models(cfg):
    executor = cfg.get("executor", "claude_cli") if cfg else "claude_cli"
    any_key = False
    for provider, env in providers.KEY_ENV.items():
        if not os.environ.get(env):
            note = ""
            if provider == "anthropic" and executor == "claude_cli":
                note = (" (executor=claude_cli runs on Claude Code auth; "
                        "use an alias like sonnet/opus/haiku or a full "
                        "model id; the list endpoint itself needs "
                        "ANTHROPIC_API_KEY)")
            print("%-10s %s not set%s" % (provider, env, note))
            continue
        any_key = True
        try:
            ids = providers.list_models(provider)
            print("%-10s %d models:" % (provider, len(ids)))
            for mid in sorted(filter(None, ids)):
                print("             %s" % mid)
        except providers.CallFailed as exc:
            print("%-10s FAILED: %s" % (provider, exc))
    if not any_key:
        print("\nno provider API keys found in the environment")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="argbench",
        description="Four-architecture argument-mapping benchmark.")
    ap.add_argument("proposition", nargs="?", help="the debatable proposition")
    ap.add_argument("--runs", type=int, default=None)
    ap.add_argument("--quick", action="store_true",
                    help="3 roles, 2 debate rounds")
    ap.add_argument("--dry-run", action="store_true",
                    help="stop after the preflight cost estimate")
    ap.add_argument("--mock", action="store_true",
                    help="offline run against canned fixtures, zero network")
    ap.add_argument("--force", action="store_true",
                    help="override the judge/roster provider-overlap check")
    ap.add_argument("--yes", action="store_true",
                    help="accept the preflight estimate without prompting")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out-dir", default="runs")
    ap.add_argument("--question", help="skip intake step 1")
    ap.add_argument("--context", help="skip intake step 2 (text or file path)")
    ap.add_argument("--lens-a", help="skip intake step 3 (LENS_A)")
    ap.add_argument("--lens-b", help="skip intake step 3 (LENS_B)")
    ap.add_argument("--list-models", action="store_true",
                    help="query each provider's model list for every key "
                         "present in the environment, then exit")
    args = ap.parse_args(argv)

    if args.list_models:
        cfg = load_config(args.config) if os.path.exists(args.config) else None
        cmd_list_models(cfg)
        return 0

    if not args.proposition:
        ap.error("proposition is required (or use --list-models)")

    if args.mock:
        cfg = dict(MOCK_CONFIG)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        cfg["fixture_dir"] = os.path.join(base_dir, "fixtures")
    else:
        cfg = load_config(args.config)

    question, context, lens_a, lens_b, scale = intake(args)
    if args.mock:
        scale = "full"  # mock fixtures exercise the full design
    roles = QUICK_ROLES if scale == "quick" else ALL_ROLES
    rounds = 2 if scale == "quick" else 3
    if scale == "quick":
        print("quick scale: roles=%s, rounds=%d" % (roles, rounds))

    notes = []
    roster = resolve_roster(cfg, roles, notes)
    cfg["_resolved_roster"] = roster
    check_judge_provider(cfg, roster, args.force or args.mock, notes)
    for n in notes:
        warn(n)

    # preflight: always before spending
    est, actual_total, unknown = preflight_estimate(
        cfg, question, context, roles, rounds, roster)
    print("\nPREFLIGHT — upper-bound cost estimate (prices as of %s):"
          % cfg.get("prices", {}).get("as_of", "UNKNOWN"))
    for k, v in est.items():
        print("  %-16s <= $%.4f" % (k, v))
    print("  %-16s <= $%.4f   (GEN round counted once)"
          % ("TOTAL SPEND", actual_total))
    if unknown:
        print("  WARNING: no price entries for: %s — those calls are NOT "
              "included in the bound above" % ", ".join(sorted(unknown)))
    runs = args.runs or int(cfg.get("runs", 1))
    if runs > 1:
        print("  x %d runs => TOTAL <= $%.4f" % (runs, actual_total * runs))
    if args.dry_run:
        print("--dry-run: stopping before any spend.")
        return 0
    if not (args.yes or args.mock):
        try:
            ok = input("\nProceed and spend up to this amount? [y/N]: ")
        except EOFError:
            ok = ""
        if ok.strip().lower() not in ("y", "yes"):
            print("aborted at preflight; nothing was spent.")
            return 1

    run_dirs = []
    for i in range(runs):
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ")
        if runs > 1:
            stamp += "-r%02d" % (i + 1)
        run_dir = os.path.join(args.out_dir, stamp)
        if os.path.exists(run_dir):
            stamp += "-%04d" % random.randint(0, 9999)
            run_dir = os.path.join(args.out_dir, stamp)
        seed = 42 if args.mock else random.SystemRandom().randint(0, 2**31)
        execute_run(run_dir, cfg, args, question, context, roles,
                    lens_a, lens_b, rounds, seed, list(notes))
        if args.mock:
            mock_self_check(run_dir)
        run_dirs.append(run_dir)

    if len(run_dirs) > 1:
        summary = report_mod.render_summary(args.out_dir, run_dirs)
        print("summary: %s" % summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
