#!/usr/bin/env python3
"""Offline test suite for the Jev Threes bot (no api.typesafe.ai traffic).

Covers acceptance criteria 2(a)-(h). The game server is NOT mocked — a real
`node server.js` is spawned on PORT=8399 (an alt port so QA's 8321 server can
coexist) and the driver talks to it via --url. Zero Jev calls: either
policy-only or an injected fake responder.

Run: python3 test_offline.py   (exit 0 = pass)
"""
import json
import os
import shutil
import subprocess
import time
import unittest
import urllib.error
import urllib.request

import compiler
import policy
import report
from jev_threes_bot import (play_game, load_instructions, lastresort_dir,
                            DEFAULT_INSTRUCTIONS_PATH)

HERE = os.path.dirname(os.path.abspath(__file__))
THREES_DIR = os.path.join(HERE, "..", "game")
TEST_PORT = 8399
TEST_URL = "http://localhost:%d" % TEST_PORT

SAMPLE_STATE = {
    "ok": True, "seed": 99,
    "grid": [[0, 1, 2, 0],
             [0, 3, 3, 6],
             [1, 0, 12, 24],
             [2, 0, 0, 48]],
    "score": 120, "maxTile": 48, "moves": 10, "version": 10,
    "gameOver": False, "validMoves": ["up", "left", "down", "right"],
    "queue": [1, 2, 3], "lastMove": None, "canUndo": True,
}

D5_KEYS = ("moves", "merges", "merge_counts_by_value", "max_tile_mergeable",
           "empty_before", "empty_after", "new_worst_row", "new_worst_col",
           "sum_of_edges_opposite", "edge_worst_count", "next_spawn_value",
           # C2 additions
           "worst_tiles_removed", "worst_tile_companion",
           "max_tile_displaced", "chain_pairs_after")
D5_BOARD_KEYS = ("max_tile", "score", "empty_count", "corner_values",
                 "worst_tile_count")


def _deep_has_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return True
        return any(_deep_has_key(v, key) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_deep_has_key(v, key) for v in obj)
    return False


# ---------------- compiler (AC 2a, 2b) ----------------

class CompilerFacts(unittest.TestCase):
    def test_a_emits_all_d5_keys_for_every_legal_dir(self):
        out = compiler.compile_state(SAMPLE_STATE)
        for k in D5_BOARD_KEYS:
            self.assertIn(k, out["board"])
        for d in SAMPLE_STATE["validMoves"]:
            self.assertIn(d, out["options"])
            for k in D5_KEYS:
                self.assertIn(k, out["options"][d])

    def test_b_never_emits_raw_grid(self):
        out = compiler.compile_state(SAMPLE_STATE)
        self.assertFalse(_deep_has_key(out, "grid"))
        # and criterion line text carries no grid either
        for d, f in out["options"].items():
            self.assertFalse("grid" in compiler.criterion_line(d, f))

    def test_compiler_agrees_with_server_valid_moves(self):
        # cross-check our slide legality vs the authoritative engine
        with _Server():
            st = _api("/api/new", {"seed": 123})
            mine = sorted(d for d in ("up", "left", "down", "right")
                          if compiler.simulate_slide(st["grid"], d) is not None)
            self.assertEqual(mine, sorted(st["validMoves"]))

    def test_max_tile_mergeable_flag(self):
        st = json.loads(json.dumps(SAMPLE_STATE))
        st["grid"] = [[0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [24, 24, 0, 0]]
        st["validMoves"] = ["left", "up"]
        out = compiler.compile_state(st)
        self.assertTrue(out["options"]["left"]["max_tile_mergeable"])

    def test_c2_fact_values_handbuilt(self):
        # row3 = [1, 2, 24, 24]; swipe left merges 1+2->3 and 24+24->48.
        st = json.loads(json.dumps(SAMPLE_STATE))
        st["grid"] = [[0, 0, 0, 0],
                      [0, 0, 0, 0],
                      [0, 0, 0, 0],
                      [1, 2, 24, 24]]
        st["maxTile"] = 24
        st["validMoves"] = ["left", "right", "up"]
        out = compiler.compile_state(st)
        left = out["options"]["left"]
        # both 1 and 2 consumed by the 1+2 merge; worst tile (1) is the origin
        self.assertEqual(left["worst_tiles_removed"], 2)
        self.assertTrue(left["worst_tile_companion"])
        # 24s slide: at least one origin max tile moves off its cell
        self.assertTrue(left["max_tile_displaced"])
        # 24s slide but don't merge (first moved lane blocks): post-slide row
        # is [3,24,24,0] -> one adjacent equal 24/24 pair remains unmerged
        self.assertEqual(left["chain_pairs_after"], 1)
        # right-swipe: [_,_,_,?] — nothing merges (1,2 adjacent? 2|24|24 no;
        # 1|2 merge is leftward only? right swipe: 2 next to 24 no, 24+24 no
        # (already adjacent, merges anyway). Just assert key presence + types.
        r = out["options"]["right"]
        self.assertIsInstance(r["worst_tiles_removed"], int)
        self.assertIsInstance(r["worst_tile_companion"], bool)
        self.assertIsInstance(r["max_tile_displaced"], bool)
        self.assertIsInstance(r["chain_pairs_after"], int)

    def test_c2_chain_pairs_counts_post_slide(self):
        # after sliding left, row0 [3,3,0,0] -> [6] merged (1 pair consumed);
        # row1 [12,12,0,0] stays? it merges too. Craft: row0 [6,6,...] merges;
        # row2 has [3,3,6,6]? too complex — simplest: post-slide adjacency
        st = json.loads(json.dumps(SAMPLE_STATE))
        # down-swipe: cols keep values; make col0 [0,0,0,0], col1 [3,3,0,0]
        st["grid"] = [[0, 3, 0, 0],
                      [0, 0, 0, 0],
                      [0, 0, 12, 24],
                      [0, 0, 0, 0]]
        st["maxTile"] = 24
        st["validMoves"] = ["down", "up", "left", "right"]
        # left swipe: [3] alone per row -> col0 [3,0,12,0] no merges; no pairs
        out = compiler.compile_state(st)
        self.assertEqual(out["options"]["left"]["chain_pairs_after"], 0)


# ---------------- fallback policy (AC 2c, 2d) ----------------

class FallbackPolicy(unittest.TestCase):
    def test_c_deterministic_same_state_same_dir(self):
        facts = compiler.compile_state(SAMPLE_STATE)
        picks = [policy.choose_fallback(list(SAMPLE_STATE["validMoves"]),
                                        facts["options"]) for _ in range(3)]
        self.assertEqual(len(set(picks)), 1)

    def test_d_always_member_of_valid_moves(self):
        facts = compiler.compile_state(SAMPLE_STATE)
        for dirs in (["up"], ["down", "right"], ["left", "up"],
                     ["right", "down", "left", "up"]):
            self.assertIn(policy.choose_fallback(dirs, facts["options"]), dirs)

    def test_score_formula_exact(self):
        f = {"merges": 2, "max_tile_mergeable": True, "empty_after": 3,
             "edge_worst_count": 1}
        self.assertEqual(policy.fallback_score("up", f),
                         100 * 2 + 40 + 15 * 3 - 10 * 1)

    def test_score_formula_exact_with_c2_terms(self):
        f = {"merges": 1, "max_tile_mergeable": False, "empty_after": 2,
             "edge_worst_count": 0,
             "worst_tiles_removed": 2, "worst_tile_companion": True,
             "max_tile_displaced": True, "chain_pairs_after": 3}
        self.assertEqual(policy.fallback_score("up", f),
                         100 + 0 + 30 - 0 + 60 + 20 - 40 + 15)

    def test_c2_max_displaced_penalised_over_equal_merge(self):
        # two dirs, identical base facts; only difference: displaced max tile
        base = {"merges": 1, "max_tile_mergeable": False, "empty_after": 2,
                "edge_worst_count": 0, "worst_tiles_removed": 0,
                "worst_tile_companion": False, "chain_pairs_after": 0}
        facts = {d: dict(base) for d in ("up", "left", "down", "right")}
        facts["up"]["max_tile_displaced"] = True
        # up is top of DIR_PRIORITY but displaced => must NOT win
        self.assertEqual(policy.choose_fallback(
            ["up", "left", "down", "right"], facts), "left")

    def test_tie_break_priority(self):
        base = {"merges": 0, "max_tile_mergeable": False, "empty_after": 1,
                "edge_worst_count": 0}
        facts = {d: dict(base) for d in ("up", "left", "down", "right")}
        # iteration order shuffled -> still picks up (top of priority)
        self.assertEqual(policy.choose_fallback(["right", "down", "left", "up"],
                                                facts), "up")


# ---------------- game loop vs real server (AC 2e, 2f, 2g, 8) ----------------

class _Server(object):
    """Spawn `node server.js` on TEST_PORT for the duration of a context."""

    def __enter__(self):
        if shutil.which("node") is None:
            raise unittest.SkipTest("node not found")
        self.proc = subprocess.Popen(
            ["node", "server.js"], cwd=THREES_DIR,
            env=dict(os.environ, PORT=str(TEST_PORT)),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(TEST_URL + "/api/health",
                                            timeout=1) as r:
                    if json.loads(r.read().decode()).get("ok"):
                        return self
            except (urllib.error.URLError, OSError):
                time.sleep(0.15)
        self.proc.terminate()
        raise RuntimeError("test server did not come up on %d" % TEST_PORT)

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        return False


def _api(path, body=None):
    req = urllib.request.Request(
        TEST_URL + path,
        data=(json.dumps(body).encode() if body is not None else None),
        method=("POST" if body is not None else "GET"),
        headers={"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _dir_seq(rec):
    return [d["dir"] for d in rec["decisions"]]


class GameLoop(unittest.TestCase):
    def test_e_policy_only_seed42_terminates(self):
        with _Server():
            rec = play_game(42, policy_only=True, base=TEST_URL)
        self.assertTrue(rec["game_over"])
        self.assertIsNone(rec["aborted"])
        self.assertGreater(rec["moves"], 0)

    def test_f_seed42_twice_identical(self):
        with _Server():
            r1 = play_game(42, policy_only=True, base=TEST_URL)
            r2 = play_game(42, policy_only=True, base=TEST_URL)
        self.assertEqual(_dir_seq(r1), _dir_seq(r2))
        self.assertEqual(r1["score"], r2["score"])

    def test_g_different_seeds_complete(self):
        with _Server():
            r1 = play_game(42, policy_only=True, base=TEST_URL)
            r2 = play_game(7, policy_only=True, base=TEST_URL)
        self.assertTrue(r1["game_over"] and r2["game_over"])

    def test_8_moves_always_within_logged_valid_moves(self):
        with _Server():
            rec = play_game(11, policy_only=True, base=TEST_URL)
        for d in rec["decisions"]:
            self.assertIn(d["dir"], d["validMoves"])


# ---------------- fake Jev responder, jev-only live mode (AC 2h) ---------

class JevOnlyLive(unittest.TestCase):
    """Inject fake responders. Live mode is jev-only: any legal choice is
    used regardless of confidence; an unusable answer is re-asked once; two
    unusable answers -> audited `lastresort` legal-direction stub.
    `fallback` is produced ONLY by --policy-only."""

    def _play_with(self, jev_fn, seed=5):
        with _Server():
            return play_game(seed, client=object(), jev_fn=jev_fn,
                             base=TEST_URL, max_moves=200)

    @staticmethod
    def _expected_lastresort(valid):
        # independent restatement of up,left,down,right priority order
        return [d for d in ("up", "left", "down", "right") if d in valid][0]

    def test_legal_choice_low_confidence_is_used(self):
        def fn(facts, dirs):
            return (dirs[0], 0.1)          # no gate anymore: 0.1 is fine
        rec = self._play_with(fn, seed=5)
        self.assertTrue(rec["decisions"])
        for d in rec["decisions"]:
            self.assertEqual(d["source"], "jev")
            self.assertEqual(d["retries"], 0)
            self.assertEqual(d["dir"], d["validMoves"][0])
            self.assertIn(d["dir"], d["validMoves"])
            self.assertEqual(d["confidence"], 0.1)   # recorded as returned
            self.assertNotIn("jevOutcome", d)

    def test_string_confidence_does_not_reject_legal_choice(self):
        def fn(facts, dirs):
            return (dirs[0], "high")       # non-numeric confidence accepted
        rec = self._play_with(fn, seed=5)
        for d in rec["decisions"]:
            self.assertEqual(d["source"], "jev")
            self.assertEqual(d["confidence"], "high")

    def test_illegal_then_legal_on_reask_accepted_as_jev(self):
        seen = {"n": 0}
        def fn(facts, dirs):              # stateful: bad ONCE, then good
            seen["n"] += 1
            if seen["n"] == 1:
                return ("sideways", 0.99)
            return (dirs[0], 0.5)
        rec = self._play_with(fn, seed=5)
        first = rec["decisions"][0]
        self.assertEqual(first["source"], "jev")
        self.assertEqual(first["retries"], 1)
        self.assertEqual(first["dir"], first["validMoves"][0])
        # exactly two fake-responder calls for that turn (answer + re-ask),
        # then one call per remaining turn
        self.assertEqual(seen["n"], 1 + len(rec["decisions"]))
        for d in rec["decisions"][1:]:
            self.assertEqual(d["source"], "jev")
            self.assertEqual(d["retries"], 0)
            self.assertNotIn("jevOutcome", d)

    def test_none_then_legal_on_reask_accepted_as_jev(self):
        seen = {"n": 0}
        def fn(facts, dirs):
            seen["n"] += 1
            if seen["n"] == 1:
                return (None, None)        # null/null like a transport error
            return (dirs[0], 0.2)
        rec = self._play_with(fn, seed=5)
        first = rec["decisions"][0]
        self.assertEqual(first["source"], "jev")
        self.assertEqual(first["retries"], 1)
        self.assertIn(first["dir"], first["validMoves"])

    def test_both_attempts_illegal_routes_to_lastresort(self):
        rec = self._play_with(lambda f, dirs: ("sideways", 0.99), seed=5)
        self.assertTrue(rec["decisions"])
        for d in rec["decisions"]:
            self.assertEqual(d["source"], "lastresort")
            self.assertEqual(d["retries"], 1)
            self.assertIn(d["dir"], d["validMoves"])
            # stub picks by up,left,down,right priority over validMoves
            self.assertEqual(d["dir"], self._expected_lastresort(d["validMoves"]))
            self.assertIsNone(d["confidence"])
            # last-attempt telemetry, audited
            self.assertEqual(d["jevOutcome"], "illegal")
            self.assertEqual(d["jevChoice"], "sideways")
            self.assertEqual(d["jevConfidence"], 0.99)

    def test_both_attempts_none_routes_to_lastresort_transport_error(self):
        rec = self._play_with(lambda f, dirs: (None, None), seed=5)
        for d in rec["decisions"]:
            self.assertEqual(d["source"], "lastresort")
            self.assertEqual(d["jevOutcome"], "transport_error")
            self.assertIsNone(d["jevChoice"])
            self.assertIsNone(d["jevConfidence"])
            self.assertEqual(d["dir"], self._expected_lastresort(d["validMoves"]))

    def test_both_attempts_raising_routes_to_lastresort_transport_error(self):
        def boom(facts, dirs):
            raise RuntimeError("network down")
        rec = self._play_with(boom, seed=6)
        self.assertTrue(rec["decisions"])
        for d in rec["decisions"]:
            self.assertEqual(d["source"], "lastresort")
            self.assertEqual(d["jevOutcome"], "transport_error")
            self.assertIsNone(d["jevChoice"])
            self.assertIsNone(d["jevConfidence"])
            self.assertIn(d["dir"], d["validMoves"])

    def test_live_mode_never_produces_fallback_source(self):
        for fn in (lambda f, dirs: ("sideways", 0.99),
                   lambda f, dirs: (None, None),
                   lambda f, dirs: (dirs[0], 0.01)):
            rec = self._play_with(fn, seed=7)
            for d in rec["decisions"]:
                self.assertIn(d["source"], ("jev", "lastresort"))

    def test_lastresort_direction_priority_order(self):
        self.assertEqual(lastresort_dir(["right", "down", "left", "up"]), "up")
        self.assertEqual(lastresort_dir(["right", "down", "left"]), "left")
        self.assertEqual(lastresort_dir(["right", "down"]), "down")
        self.assertEqual(lastresort_dir(["right"]), "right")
        self.assertIsNone(lastresort_dir([]))

    def test_policy_only_uses_fallback_source_and_no_jev_telemetry(self):
        # --policy-only path unchanged: greedy fallback, zero Jev calls
        with _Server():
            rec = play_game(5, policy_only=True, base=TEST_URL)
        self.assertTrue(rec["decisions"])
        for d in rec["decisions"]:
            self.assertEqual(d["source"], "fallback")
            for k in ("turn", "dir", "source", "confidence", "validMoves"):
                self.assertIn(k, d)
            self.assertNotIn("jevOutcome", d)
            self.assertNotIn("jevChoice", d)
            self.assertNotIn("jevConfidence", d)

    def test_load_instructions_default_file(self):
        text = load_instructions()          # default = iter-00-base
        self.assertEqual(text, load_instructions(DEFAULT_INSTRUCTIONS_PATH))
        self.assertIn("Threes-like", text)
        self.assertIn("pick the single best option", text)
        self.assertNotIn("\n", text)         # single plain-text block
        with self.assertRaises(RuntimeError):
            load_instructions(os.path.join(HERE, "prompts", "no-such-iter.txt"))


# ---------------- report tool ----------------

class ReportTool(unittest.TestCase):
    def test_summarize_counts(self):
        games = [
            {"max_tile": 96, "score": 500, "decisions": [{"source": "jev"}]},
            {"max_tile": 24, "score": 100, "decisions": [{"source": "fallback"},
                                                         {"source": "jev"}]},
        ]
        s = report.summarize(games, win_tile=96)
        self.assertEqual(s["games"], 2)
        self.assertEqual(s["wins"], 1)
        self.assertEqual(s["losses"], 1)
        self.assertEqual(s["best_score"], 500)
        self.assertEqual(s["jev_decisions"], 2)
        self.assertEqual(s["fallback_decisions"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
