"""Offline unit tests for argbench.py's pure logic: JSON extraction, judge
schema validation, judge-input sanitisation, suspect-quote anchoring,
arithmetic recompute, panel aggregation, anonymisation round-trips, cost
computation and roster-fill refusal."""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argbench


def judge_data(**overrides):
    """A minimal schema-valid judge output."""
    per = {}
    for s in "ABCD":
        per[s] = {"distinct": 1, "unique": 0, "suspect": 0,
                  "suspect_entries": [], "depth": 3, "notes": ""}
    data = {
        "union": [{"id": "U-1", "theme": "t", "side": "FOR",
                   "short_claim": "c", "found_in": ["A", "B", "C", "D"]}],
        "per_system": per,
        "verdict": "v",
    }
    data.update(overrides)
    return data


class TestExtractJson(unittest.TestCase):

    def test_plain_and_fenced(self):
        self.assertEqual(argbench.extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(argbench.extract_json('```json\n{"a": 1}\n```'),
                         {"a": 1})

    def test_prose_around_object(self):
        self.assertEqual(
            argbench.extract_json('Here you go: {"a": {"b": 2}} hope it helps'),
            {"a": {"b": 2}})

    def test_trailing_brace_in_prose(self):
        # the naive first-{-to-last-} slice would fail on this input
        self.assertEqual(
            argbench.extract_json('{"a": 1} and later a stray }'),
            {"a": 1})

    def test_unparseable(self):
        self.assertIsNone(argbench.extract_json("no json here"))
        self.assertIsNone(argbench.extract_json(None))


class TestValidateJudgeJson(unittest.TestCase):

    def test_valid(self):
        self.assertEqual(argbench.validate_judge_json(judge_data()), [])

    def test_missing_suspect_entries(self):
        d = judge_data()
        del d["per_system"]["B"]["suspect_entries"]
        errs = argbench.validate_judge_json(d)
        self.assertTrue(any("B.suspect_entries" in e for e in errs))

    def test_bad_suspect_entry_item(self):
        d = judge_data()
        d["per_system"]["A"]["suspect_entries"] = [{"id": "E-1"}]
        errs = argbench.validate_judge_json(d)
        self.assertTrue(any("suspect_entries[0]" in e for e in errs))

    def test_bad_side_and_found_in(self):
        d = judge_data()
        d["union"][0]["side"] = "MAYBE"
        d["union"][0]["found_in"] = ["A", "Z"]
        errs = argbench.validate_judge_json(d)
        self.assertEqual(len(errs), 2)


SAMPLE_FINAL = """ID: M-1
SIDE: FOR
CLAIM: First claim.
REASONING: Contains vendor data below 20 percent overrides.
DEFEATER: Something specific.
CONV: 2
SOURCES: ADVOCATE-1, LENS_A-3
TESTED: yes

=== GAP CRITIC ADDITIONS (appended mechanically, not edited) ===

ID: CRITIC-1
SIDE: AGAINST
CLAIM: The OPPONENT missed this.
REASONING: Reasoning here.
DEFEATER: Defeater here.

WEAKEST
- M-1: weak because reasons.

GRAVEYARD
- ADVOCATE-4: killed.
"""


class TestSanitizeForJudge(unittest.TestCase):

    def setUp(self):
        self.out = argbench.sanitize_for_judge(SAMPLE_FINAL,
                                               argbench.ALL_ROLES)

    def test_structural_markers_stripped(self):
        for marker in ("CONV:", "SOURCES:", "TESTED", "GRAVEYARD",
                       "WEAKEST", "==="):
            self.assertNotIn(marker, self.out)

    def test_ids_renumbered_uniformly(self):
        self.assertIn("ID: E-1", self.out)
        self.assertIn("ID: E-2", self.out)
        self.assertNotIn("M-1", self.out)
        self.assertNotIn("CRITIC-1", self.out)

    def test_role_tokens_replaced(self):
        self.assertNotIn("ADVOCATE", self.out)
        self.assertNotIn("OPPONENT", self.out)
        self.assertIn("PARTICIPANT", self.out)

    def test_content_preserved(self):
        self.assertIn("vendor data below 20 percent overrides", self.out)
        self.assertIn("Something specific.", self.out)


class TestSuspectQuotes(unittest.TestCase):

    def test_verified_and_dropped(self):
        d = judge_data()
        d["per_system"]["A"]["suspect"] = 2
        d["per_system"]["A"]["suspect_entries"] = [
            {"id": "E-1", "quote": "Below 20  PERCENT overrides",
             "why": "unsourced"},
            {"id": "E-2", "quote": "totally absent phrase xyz",
             "why": "unsourced"},
        ]
        sanitized = {s: "reasoning has below 20 percent overrides in it"
                     for s in "ABCD"}
        warnings = argbench.validate_suspect_quotes(d, sanitized)
        a = d["per_system"]["A"]
        self.assertEqual(a["suspect"], 1)
        self.assertEqual(a["suspect_claimed"], 2)
        self.assertTrue(a["suspect_entries"][0]["verified"])
        self.assertFalse(a["suspect_entries"][1]["verified"])
        self.assertTrue(any("DROPPED" in w for w in warnings))

    def test_short_quote_rejected(self):
        d = judge_data()
        d["per_system"]["B"]["suspect"] = 1
        d["per_system"]["B"]["suspect_entries"] = [
            {"id": "E-1", "quote": "percent", "why": "too short to anchor"}]
        sanitized = {s: "percent appears here" for s in "ABCD"}
        argbench.validate_suspect_quotes(d, sanitized)
        self.assertEqual(d["per_system"]["B"]["suspect"], 0)


class TestRecomputeAndAggregate(unittest.TestCase):

    def test_recompute_prefers_matrix(self):
        d = judge_data()
        d["per_system"]["A"]["distinct"] = 99
        warnings = argbench.recompute_counts(d)
        self.assertEqual(d["per_system"]["A"]["distinct"], 1)
        self.assertEqual(d["per_system"]["A"]["distinct_claimed"], 99)
        self.assertTrue(any("A.distinct" in w for w in warnings))

    def test_aggregate_medians_and_spread(self):
        d1, d2 = judge_data(), judge_data()
        d1["per_system"]["A"]["suspect"] = 0
        d2["per_system"]["A"]["suspect"] = 2
        agg = argbench.aggregate_judges([
            {"name": "judge1", "provider": "p", "model": "m1", "data": d1},
            {"name": "judge2", "provider": "p", "model": "m2", "data": d2},
        ])
        self.assertEqual(agg["n_judges"], 2)
        self.assertEqual(agg["per_system"]["A"]["suspect"], 1.0)
        self.assertEqual(agg["spread"]["A"]["suspect"], [0, 2])
        self.assertIn("JUDGE 1", agg["verdict"])
        self.assertIn("JUDGE 2", agg["verdict"])

    def test_single_judge_aggregate_is_trivial(self):
        agg = argbench.aggregate_judges(
            [{"name": "judge", "provider": "p", "model": "m",
              "data": judge_data()}])
        self.assertEqual(agg["n_judges"], 1)
        self.assertEqual(agg["verdict"], "v")

    def test_judges_of_accepts_dict_or_list(self):
        self.assertEqual(len(argbench.judges_of({"judge": {"a": 1}})), 1)
        self.assertEqual(len(argbench.judges_of({"judge": [{}, {}]})), 2)


class TestAnonymisation(unittest.TestCase):

    ROLES = ["ADVOCATE", "OPPONENT", "SKEPTIC"]

    def test_round1_role_names_become_current_letters(self):
        mappings = {2: {"ADVOCATE": "C", "OPPONENT": "A", "SKEPTIC": "B"}}
        out = argbench.render_contribution(
            "ID: ADVOCATE-3 rebuts OPPONENT-1", 1, mappings, 2, self.ROLES)
        self.assertEqual(out, "ID: C-3 rebuts A-1")

    def test_cross_round_letters_translate_without_cascade(self):
        # round-2 text references letters under round 2's mapping; when
        # shown in round 3 they must translate through roles to round 3's
        # letters even when the letter sets overlap
        mappings = {
            2: {"ADVOCATE": "A", "OPPONENT": "B", "SKEPTIC": "C"},
            3: {"ADVOCATE": "B", "OPPONENT": "C", "SKEPTIC": "A"},
        }
        out = argbench.render_contribution(
            "REBUT A-2 and Participant B", 2, mappings, 3, self.ROLES)
        self.assertEqual(out, "REBUT B-2 and Participant C")

    def test_deanonymise_restores_role_names(self):
        mappings = {2: {"ADVOCATE": "A", "OPPONENT": "B", "SKEPTIC": "C"}}
        out = argbench.deanonymise_contribution(
            "REBUT A-2: weak. Participant C agrees.", 2, mappings)
        self.assertEqual(out,
                         "REBUT ADVOCATE-2: weak. Participant SKEPTIC agrees.")


class TestCostAndRoster(unittest.TestCase):

    def _cost(self, cfg, model, i, o):
        dummy = types.SimpleNamespace(cfg=cfg)
        return argbench.RunContext._cost(dummy, model, i, o)

    def test_cost_computed(self):
        cfg = {"prices": {"m": {"input_per_mtok": 1.0,
                                "output_per_mtok": 4.0}}}
        cost, note = self._cost(cfg, "m", 1_000_000, 500_000)
        self.assertEqual(cost, 3.0)
        self.assertIsNone(note)

    def test_missing_price_and_missing_usage(self):
        cost, note = self._cost({"prices": {}}, "m", 10, 10)
        self.assertIsNone(cost)
        self.assertIn("no price", note)
        cfg = {"prices": {"m": {"input_per_mtok": 1, "output_per_mtok": 1}}}
        cost, note = self._cost(cfg, "m", None, 10)
        self.assertIsNone(cost)
        self.assertIn("omitted usage", note)

    def test_roster_fill_noted_and_refused_without_force(self):
        cfg = {"executor": "mock",
               "roster": [{"role": "ADVOCATE", "provider": "mock",
                           "model": "mock-gen"}]}
        notes = []
        roster = argbench.resolve_roster(cfg, ["ADVOCATE", "OPPONENT"],
                                         notes)
        self.assertEqual(roster["OPPONENT"]["model"], "mock-gen")
        self.assertTrue(notes[0].startswith(argbench.REDUCED_DIVERSITY))
        with self.assertRaises(SystemExit):
            argbench.check_roster_fill(notes, force=False)
        argbench.check_roster_fill(notes, force=True)  # no exit

    def test_full_roster_no_note(self):
        cfg = {"executor": "mock",
               "roster": [{"role": r, "provider": "mock", "model": "mock-gen"}
                          for r in argbench.ALL_ROLES]}
        notes = []
        argbench.resolve_roster(cfg, argbench.ALL_ROLES, notes)
        self.assertEqual(notes, [])


class TestPricesFreshness(unittest.TestCase):

    def test_missing_and_stale_and_fresh(self):
        notes = []
        argbench.check_prices_freshness({"prices": {}}, notes)
        self.assertTrue(notes and "PRICES CAVEAT" in notes[0])
        notes = []
        argbench.check_prices_freshness(
            {"prices": {"as_of": "2000-01-01"}}, notes)
        self.assertTrue(notes and "days old" in notes[0])
        notes = []
        import datetime
        today = datetime.date.today().isoformat()
        argbench.check_prices_freshness({"prices": {"as_of": today}}, notes)
        self.assertEqual(notes, [])


if __name__ == "__main__":
    unittest.main()
