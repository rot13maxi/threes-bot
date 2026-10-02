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
           "max_tile_displaced", "chain_pairs_after",
           # C3 additions
           "spawn_edge_empties", "spawn_edge_has_big",
           "isolated_big_after", "second_max_pair_exists",
           # D6 additions (per-option max-tile anchoring)
           "pins_max_tile", "anchor_kept", "max_tile_corner_after",
           # D11 additions (worst-case survival + runner-up supply count)
           "min_wiggles_after_spawn", "second_max_count",
           # planner additions (expectimax verdict per option)
           "plan_score", "plan_rank", "plan_best")
D5_BOARD_KEYS = ("max_tile", "max_tile_pos", "max_tile_pin_dirs",
                 "score", "empty_count", "corner_values",
                 "worst_tile_count", "worst_tile_value")


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

    def test_board_text_is_exact_ascii_view(self):
        # hand-derived from SAMPLE_STATE["grid"]:
        #   [0, 1, 2, 0] / [0, 3, 3, 6] / [1, 0, 12, 24] / [2, 0, 0, 48]
        out = compiler.compile_state(SAMPLE_STATE)
        self.assertEqual(
            out["board_text"],
            ". 1 2 .\n"
            ". 3 3 6\n"
            "1 . 12 24\n"
            "2 . . 48")
        self.assertEqual(len(out["board_text"].split("\n")), 4)

    def test_board_text_renders_zeros_as_dot(self):
        out = compiler.compile_state(
            self._state([[0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [24, 0, 0, 48]],
                        ["left", "up"]))
        self.assertEqual(
            out["board_text"],
            ". . . .\n"
            ". . . .\n"
            ". . . .\n"
            "24 . . 48")

    def test_compile_state_top_level_keys_exactly_board_options_board_text(self):
        out = compiler.compile_state(SAMPLE_STATE)
        self.assertEqual(set(out.keys()), {"board", "options", "board_text"})

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

    # --- C3: max-tile anchoring facts ------------------------------------

    def _state(self, grid, valid, queue=(1, 2, 3)):
        st = json.loads(json.dumps(SAMPLE_STATE))
        st["grid"] = grid
        st["validMoves"] = valid
        st["queue"] = list(queue)
        return st

    def test_c3_corner_max_tile_pos_and_pin_dirs(self):
        out = compiler.compile_state(
            self._state([[48, 3, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
                        ["up", "left", "down", "right"]))
        self.assertEqual(out["board"]["max_tile_pos"], [0, 0])
        # a swipe toward an edge the tile already sits on can never move it
        self.assertEqual(out["board"]["max_tile_pin_dirs"], ["up", "left"])

    def test_c3_edge_max_tile_pin_dirs(self):
        # max at (0,2): only the "up" swipe (toward row 0) pins it in place
        out = compiler.compile_state(
            self._state([[0, 0, 48, 0], [0, 0, 0, 1], [0, 0, 0, 0], [0, 0, 0, 0]],
                        ["up", "left", "down", "right"]))
        self.assertEqual(out["board"]["max_tile_pos"], [0, 2])
        self.assertEqual(out["board"]["max_tile_pin_dirs"], ["up"])

    def test_c3_middle_max_tile_has_no_pin_dirs(self):
        out = compiler.compile_state(
            self._state([[0, 0, 0, 0], [0, 0, 0, 0], [0, 48, 1, 0], [0, 0, 0, 0]],
                        ["up", "left", "down", "right"]))
        self.assertEqual(out["board"]["max_tile_pos"], [2, 1])
        self.assertEqual(out["board"]["max_tile_pin_dirs"], [])

    def test_c3_empty_board_pos_and_worst_value_are_none(self):
        out = compiler.compile_state(self._state([[0] * 4] * 4, []))
        self.assertIsNone(out["board"]["max_tile_pos"])
        self.assertEqual(out["board"]["max_tile_pin_dirs"], [])
        self.assertIsNone(out["board"]["worst_tile_value"])

    def test_c3_worst_tile_value(self):
        out = compiler.compile_state(
            self._state([[0, 2, 0, 0], [0, 0, 6, 0], [0, 0, 0, 24], [0, 0, 0, 0]],
                        ["left"]))
        self.assertEqual(out["board"]["worst_tile_value"], 2)

    # --- C3: spawn-edge facts ---------------------------------------------

    def test_c3_spawn_edge_empties_and_big_handverified(self):
        # swipe up: col0 [3,6,12,24] is locked (every pair unmergeable, every
        # cell blocked), so the bottom row post-slide is [24,0,0,0]:
        # 3 empty cells on the spawn edge and a stranded 24 (>6) on it.
        st = self._state([[3, 1, 0, 0], [6, 0, 0, 0], [12, 0, 0, 0], [24, 0, 0, 2]],
                         ["up"])
        up = compiler.compile_state(st)["options"]["up"]
        self.assertEqual(up["spawn_edge_empties"], 3)
        self.assertTrue(up["spawn_edge_has_big"])

    def test_c3_spawn_edge_fully_clear(self):
        # swipe up on bottom row [1,2,1,2]: every tile slides one step up,
        # leaving all 4 spawn-edge cells empty and nothing big on it.
        st = self._state([[0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [1, 2, 1, 2]],
                         ["up"])
        up = compiler.compile_state(st)["options"]["up"]
        self.assertEqual(up["spawn_edge_empties"], 4)
        self.assertFalse(up["spawn_edge_has_big"])

    # --- C3: isolation + next-big feed -------------------------------------

    def test_c3_isolated_big_post_slide_via_compiler(self):
        # swipe down: row0 [1,48,3,3] lands as row3 [1,48,3,3]. The 48 has
        # neighbours 1 and 3 (no equal partner) -> counted once; the 3s pair
        # with each other; 1 is never counted.
        st = self._state([[1, 48, 3, 3], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
                         ["down"])
        self.assertEqual(compiler.compile_state(st)["options"]["down"]["isolated_big_after"], 1)

    def test_c3_adjacent_twentyfours_not_isolated(self):
        # swipe down: row2 [0,24,24,0] lands as row3 [0,24,24,0]; the 24s are
        # orthogonally adjacent -> no isolated big tiles.
        st = self._state([[0, 0, 0, 0], [0, 0, 0, 0], [0, 24, 24, 0], [0, 0, 0, 0]],
                         ["down"])
        self.assertEqual(compiler.compile_state(st)["options"]["down"]["isolated_big_after"], 0)

    def test_c3_isolated_big_helper_edges(self):
        # direct helper: 48 alone among 1s/3s counts; 1s and 2s never count
        self.assertEqual(compiler._isolated_big_after(
            [[3, 3, 0, 0], [1, 48, 0, 0], [24, 24, 0, 0], [0, 0, 0, 0]]), 1)
        self.assertEqual(compiler._isolated_big_after(
            [[1, 0, 0, 2], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]), 0)

    def test_c3_second_max_pair_exists_feed(self):
        # swipe right: post-slide row1/row2 each hold a 24 at col3 (pair),
        # row3 becomes [0,48,1,0]; second-biggest value (pre-slide max=48,
        # second=24) appears twice -> next big merge has a feed.
        st = self._state([[0, 0, 0, 0], [24, 0, 0, 0], [24, 0, 0, 0], [48, 1, 0, 0]],
                         ["right"])
        self.assertTrue(compiler.compile_state(st)["options"]["right"]["second_max_pair_exists"])
        # same shape with a single 24 -> no pair
        st = self._state([[0, 0, 0, 0], [24, 0, 0, 0], [48, 1, 0, 0], [0, 0, 0, 0]],
                         ["right"])
        self.assertFalse(compiler.compile_state(st)["options"]["right"]["second_max_pair_exists"])

    def test_c3_second_max_pair_single_distinct_value(self):
        # only one distinct value on the board (24s): use its own second pair
        # post-slide — swipe down keeps both 24s side by side at row3.
        st = self._state([[0, 0, 0, 0], [0, 0, 0, 0], [0, 24, 24, 0], [0, 0, 0, 0]],
                         ["down"])
        self.assertTrue(compiler.compile_state(st)["options"]["down"]["second_max_pair_exists"])

    def test_c3_criterion_line_renders_new_facts(self):
        out = compiler.compile_state(SAMPLE_STATE)
        line = compiler.criterion_line("up", out["options"]["up"])
        self.assertIn("spawn-edge empties 4", line)
        self.assertIn("big tile stranded on spawn edge no", line)
        self.assertIn("isolated big tiles left after", line)
        self.assertIn("second-biggest tile x1 on board no", line)
        self.assertIn("worst-case spawn wiggles 4", line)
        self.assertNotIn("grid", line)

    def test_c3_criterion_line_renders_none_as_na(self):
        f = dict(compiler.compile_state(SAMPLE_STATE)["options"]["up"])
        for k in ("spawn_edge_empties", "spawn_edge_has_big",
                  "isolated_big_after", "second_max_pair_exists",
                  "min_wiggles_after_spawn", "second_max_count"):
            f[k] = None
        line = compiler.criterion_line("up", f)
        self.assertIn("spawn-edge empties n/a", line)
        self.assertIn("big tile stranded on spawn edge n/a", line)
        self.assertIn("n/a isolated big tiles left after", line)
        self.assertIn("second-biggest tile n/a on board n/a", line)
        self.assertIn("worst-case spawn wiggles n/a", line)

    # --- D6: per-option max-tile pin / anchor facts ----------------------

    def test_d6_corner_max_tile_pins_and_anchor(self):
        # 48 anchored at (0,0) with a spare 3; junk tile at (3,2) keeps every
        # swipe legal. up/left swipe toward the tile's own edges => pinned.
        out = compiler.compile_state(self._state(
            [[48, 3, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 1, 0]],
            ["up", "left", "down", "right"]))
        o = out["options"]
        self.assertEqual(out["board"]["max_tile_pin_dirs"], ["up", "left"])
        for d in ("up", "left"):
            self.assertTrue(o[d]["pins_max_tile"], d)
            self.assertTrue(o[d]["anchor_kept"], d)        # unmoved + corner
            self.assertFalse(o[d]["max_tile_displaced"], d)
            self.assertTrue(o[d]["max_tile_corner_after"], d)
        for d in ("down", "right"):
            self.assertFalse(o[d]["pins_max_tile"], d)
            self.assertFalse(o[d]["anchor_kept"], d)       # tile got dragged
            self.assertTrue(o[d]["max_tile_displaced"], d)
            self.assertFalse(o[d]["max_tile_corner_after"], d)

    def test_d6_midboard_max_tile_never_pins(self):
        # 48 at (1,1): no pin dirs at all, every legal swipe drags it
        out = compiler.compile_state(self._state(
            [[0, 0, 0, 0], [0, 48, 0, 0], [0, 0, 0, 0], [1, 0, 2, 0]],
            ["up", "left", "down", "right"]))
        self.assertEqual(out["board"]["max_tile_pin_dirs"], [])
        for d, f in out["options"].items():
            self.assertFalse(f["pins_max_tile"], d)
            self.assertTrue(f["max_tile_displaced"], d)      # every dir drags it
            self.assertFalse(f["anchor_kept"], d)
            self.assertFalse(f["max_tile_corner_after"], d)

    def test_d6_unmoved_midboard_max_tile_is_not_anchored(self):
        # up-swipe: 48 at (1,1) is blocked by the 1 above it -> unmoved, but
        # it never sat on a corner => anchor kept no
        out = compiler.compile_state(self._state(
            [[0, 1, 0, 0], [0, 48, 0, 0], [0, 0, 0, 0], [0, 0, 0, 2]],
            ["up"]))["options"]["up"]
        self.assertFalse(out["pins_max_tile"])
        self.assertFalse(out["max_tile_displaced"])
        self.assertFalse(out["anchor_kept"])
        self.assertFalse(out["max_tile_corner_after"])

    def test_d6_corner_after_edge_tile_to_corner(self):
        # 48 at (0,1): swipe left pushes it INTO corner (0,0)
        out = compiler.compile_state(self._state(
            [[0, 48, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 1, 0]],
            ["left", "up"]))
        left = out["options"]["left"]
        self.assertFalse(left["pins_max_tile"])            # (0,1) pins "up" only
        self.assertTrue(left["max_tile_displaced"])
        self.assertFalse(left["anchor_kept"])              # moved without merging
        self.assertTrue(left["max_tile_corner_after"])     # lands on (0,0)
        # up: 48 pinned in place at (0,1) — an edge cell, not a corner
        up = out["options"]["up"]
        self.assertTrue(up["pins_max_tile"])
        self.assertFalse(up["max_tile_displaced"])
        self.assertFalse(up["anchor_kept"])
        self.assertFalse(up["max_tile_corner_after"])

    def test_d6_corner_after_midboard_up_land_not_corner(self):
        # 48 at (1,1) swiped up lands (0,1): edge cell, not a corner
        out = compiler.compile_state(self._state(
            [[0, 0, 0, 0], [0, 48, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
            ["up"]))["options"]["up"]
        self.assertTrue(out["max_tile_displaced"])
        self.assertFalse(out["anchor_kept"])
        self.assertFalse(out["max_tile_corner_after"])

    def test_d6_max_merge_anchors_into_corner(self):
        # [48,48,0,0] swiped left merges at (3,0): merged tile counts as the
        # max tile's cell, and (3,0) is a corner => both flags True
        out = compiler.compile_state(self._state(
            [[0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [48, 48, 0, 0]],
            ["left", "right"]))
        left = out["options"]["left"]
        self.assertTrue(left["max_tile_mergeable"])
        self.assertTrue(left["anchor_kept"])
        self.assertTrue(left["max_tile_corner_after"])
        # right: the 48s only cascade one cell each ([_,48,48,_]) — no merge
        right = out["options"]["right"]
        self.assertFalse(right["max_tile_mergeable"])
        self.assertFalse(right["anchor_kept"])
        self.assertFalse(right["max_tile_corner_after"])

    def test_d6_subthree_max_tile_anchor_trivially_true(self):
        # max tile 1: nothing to anchor yet -> both anchor facts True
        out = compiler.compile_state(self._state(
            [[1, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
            ["down"]))["options"]["down"]
        self.assertTrue(out["anchor_kept"])
        self.assertTrue(out["max_tile_corner_after"])

    def test_d6_criterion_line_leads_with_decisive_facts(self):
        out = compiler.compile_state(SAMPLE_STATE)
        line = compiler.criterion_line("up", out["options"]["up"])
        for phrase in ("plan rank", "max-tile merge", "pins max tile",
                       "max tile displaced",
                       "anchor kept", "big tile in corner after",
                       "low tiles (1/2) removed", "tiles move"):
            self.assertIn(phrase, line)
        idx = [line.index(p) for p in
               ("plan rank", "max-tile merge", "pins max tile",
                "max tile displaced",
                "anchor kept", "big tile in corner after",
                "low tiles (1/2) removed", "tiles move")]
        self.assertEqual(idx, sorted(idx))
        self.assertNotIn("grid", line)

    def test_d6_criterion_line_renders_anchor_facts_none_as_na(self):
        f = dict(compiler.compile_state(SAMPLE_STATE)["options"]["up"])
        for k in ("pins_max_tile", "anchor_kept", "max_tile_corner_after"):
            f[k] = None
        line = compiler.criterion_line("up", f)
        self.assertIn("pins max tile n/a", line)
        self.assertIn("anchor kept n/a", line)
        self.assertIn("big tile in corner after n/a", line)

    # --- D11: worst-case spawn survival + runner-up supply count ---------

    def test_d11_min_wiggles_death_trap_handverified(self):
        # Swipe UP on this board slides: row3 col2's 3 rises into r2c2 (the
        # 0 there), everything else is blocked -> post-slide
        #   [24, 3, 2, 0]
        #   [ 1,96,48, 0]
        #   [ 3, 6, 3, 0]
        #   [12, 3, 0, 0]
        # so the spawn (queue[0] = 1) may land at (3,2) or (3,3).
        # WORST case (3,2): remaining empty is only (3,3). Hand-check of
        # _advance_possible on [[24,3,2,0],[1,96,48,0],[3,6,3,0],[12,3,1,0]]:
        #   left : row3 [12,3,1,_]: 3->12, 1->3, none merge (1+2 only), 0 at
        #          the far end blocks nothing but no tile slides left; every
        #          other row is a solid blocked lane; cols keep blocked runs.
        #   up   : col2 [2,48,3,1]: 48->2 no, 3->48 no, 1->3 no (1+2=3 only);
        #          col3 all empty; col0/col1 runs [24,1,3,12] & [3,96,6,3]
        #          have no mergeable pair nor empty gap -> no.
        #   down : col2: 3->1? no (3+1 not a merge); col0: 3->12, 12->3? no
        #          equal-double nor 1+2; col1: 6->3? no -> no.
        #   right: tile 1 at (3,2) slides into the empty (3,3) -> YES.
        # Exactly ONE wiggle. The (3,3) placement leaves 4; min = 1.
        out = compiler.compile_state(self._state(
            [[24, 3, 2, 0], [1, 96, 48, 0], [3, 6, 0, 0], [12, 3, 3, 0]],
            ["up", "left", "down", "right"], queue=(1, 2, 3)))
        self.assertEqual(out["options"]["up"]["min_wiggles_after_spawn"], 1)

    def test_d11_min_wiggles_total_lock_is_zero(self):
        # Swipe UP: only col3 moves (its bottom 24 steps up one); post-slide
        #   [48, 3,12, 6]
        #   [24, 6,24,12]
        #   [12,24, 6,24]
        #   [24,48,12, 0]
        # leaves the SINGLE spawn cell (3,3). Placing queue[0]=1 there makes
        # a fully packed board; hand-check: no adjacent equal 3*2^k pair and
        # no (1,2) pair exist in any row ([48,3,12,6],[24,6,24,12],
        # [12,24,6,24],[24,48,12,1]) or column ([48,24,12,24],[3,6,24,48],
        # [12,24,6,12],[6,12,24,1]) -> zero lanes can advance: the game is
        # over the instant the tile lands. This is the true death trap.
        out = compiler.compile_state(self._state(
            [[48, 3, 12, 0], [24, 6, 24, 6], [12, 24, 6, 12], [24, 48, 12, 24]],
            ["up", "right"], queue=(1, 2, 3)))
        self.assertEqual(out["options"]["up"]["min_wiggles_after_spawn"], 0)
        self.assertTrue(compiler.simulate_slide(
            [[48, 3, 12, 0], [24, 6, 24, 6], [12, 24, 6, 12], [24, 48, 12, 24]],
            "up"), "server-valid move: spawn cell exists")

    def test_d11_min_wiggles_safe_board_wide_open(self):
        # Swipe UP: the 24 pair slides up one, spawn edge (bottom row) is
        # entirely empty -> 4 placements. Any single 1 dropped on the bottom
        # row of an otherwise [24,24]-plus-13-empties board still lets all
        # four directions advance (24 pair merges left/right; empties give
        # the vertical slides) -> min = 4 >= 3.
        out = compiler.compile_state(self._state(
            [[0, 0, 0, 0], [0, 0, 0, 0], [24, 24, 0, 0], [0, 0, 0, 0]],
            ["up", "left", "down", "right"], queue=(1, 2, 3)))
        self.assertGreaterEqual(out["options"]["up"]["min_wiggles_after_spawn"], 3)
        self.assertEqual(out["options"]["up"]["min_wiggles_after_spawn"], 4)

    def test_d11_min_wiggles_placement_sensitive(self):
        # Checkerboard with NO mergeable adjacency (no equal pairs, no 1+2
        # pair): swiping UP only frees cells the slide vacated. Post-slide:
        #   [48, 1,24, 2]
        #   [ 1, 6, 2,48]
        #   [12, 2, 3, 1]
        #   [ 0,24, 0, 6]
        # Candidates: (3,0) and (3,2). Placing the 1 at (3,2) SURROUNDS it
        # (row3 [_,24,1,6], col2 [24,2,3,1] all unmergeable against it);
        # the only surviving advances come from the leftover empty (3,0):
        #   left  : 24 slides into (3,0)          -> YES
        #   down  : (2,0)=12 slides into (3,0)    -> YES
        #   right : 1 blocked by 6, 24 blocked by 1, rows above solid   -> no
        #   up    : col2 stack 3->2? no, 2->24? no; col0 bottom empty    -> no
        # -> 2 wiggles. The (3,0) placement keeps left+right+down = 3.
        # min == the surrounded placement's count.
        out = compiler.compile_state(self._state(
            [[0, 1, 0, 2], [48, 6, 24, 48], [1, 2, 2, 1], [12, 24, 3, 6]],
            ["up", "left", "right"], queue=(1, 2, 3)))
        self.assertEqual(out["options"]["up"]["min_wiggles_after_spawn"], 2)

    def test_d11_second_max_count_exact(self):
        # Swipe DOWN drops each test row intact onto row3 (pre-spawn).
        # {48,24,24,3}: second distinct value 24 appears twice -> merge-ready
        out = compiler.compile_state(self._state(
            [[0, 0, 0, 0], [0, 0, 0, 0], [48, 24, 24, 3], [0, 0, 0, 0]],
            ["up", "left", "down", "right"]))
        self.assertEqual(out["options"]["down"]["second_max_count"], 2)
        # {48,24,3,1}: lone 24 waiting for its partner
        out = compiler.compile_state(self._state(
            [[0, 0, 0, 0], [0, 0, 0, 0], [48, 24, 3, 1], [0, 0, 0, 0]],
            ["up", "down"]))
        self.assertEqual(out["options"]["down"]["second_max_count"], 1)
        # single distinct value 24: its own post-slide count (3 copies)
        out = compiler.compile_state(self._state(
            [[0, 0, 0, 0], [0, 0, 0, 0], [24, 24, 24, 0], [0, 0, 0, 0]],
            ["up", "left", "down", "right"]))
        self.assertEqual(out["options"]["down"]["second_max_count"], 3)

    def test_d11_advance_possible_mirrors_engine(self):
        # Fully packed unmergeable board: no direction can advance anything
        # (hand-verified against the lane geometry above)
        lock = [[48, 3, 12, 6], [24, 6, 24, 12], [12, 24, 6, 24], [24, 48, 12, 1]]
        for d in ("up", "left", "down", "right"):
            self.assertFalse(compiler._advance_possible(lock, d), d)
        # the surrounded SENS placement (1 wedged at (3,2) with the leftover
        # empty at (3,0)): only left (24 -> (3,0)) and down ((2,0)=12 ->
        # (3,0)) can advance; right is blocked (1 vs 6, 24 vs 1) and every
        # column stack is unmergeable -> up no, right no
        wedged = [[48, 1, 24, 2], [1, 6, 2, 48], [12, 2, 3, 1], [0, 24, 1, 6]]
        self.assertTrue(compiler._advance_possible(wedged, "left"))
        self.assertTrue(compiler._advance_possible(wedged, "down"))
        self.assertFalse(compiler._advance_possible(wedged, "up"))
        self.assertFalse(compiler._advance_possible(wedged, "right"))
        # fully packed row0 [1,2,48,24]: the 1|2 pair can merge leftward AND
        # rightward; no other lane holds a slide or a merge
        packed = [[1, 2, 48, 24], [3, 6, 12, 48], [6, 12, 24, 3], [12, 24, 3, 6]]
        self.assertTrue(compiler._advance_possible(packed, "left"))
        self.assertTrue(compiler._advance_possible(packed, "right"))
        self.assertFalse(compiler._advance_possible(packed, "up"))
        self.assertFalse(compiler._advance_possible(packed, "down"))

    def test_d11_self_consistency_advance_vs_slide(self):
        # Cross-check the survival fact against an INDEPENDENT reference
        # written from the engine's lane rule ("some tile can advance" <=>
        # some tile has an empty forward neighbour or a mergeable pair),
        # plus the slide gate: a dir is compile-able (in validMoves with a
        # fact) iff simulate_slide says legal. A legal move must always
        # expose an int 0..4: a server-legal swipe has at least one spawn
        # placement, and 0 is allowed — a random spawn CAN lock the board
        # instantly (see the total-lock test above).
        def ref_advance(g, d):
            step = {"up": (-1, 0), "down": (1, 0), "left": (0, -1),
                    "right": (0, 1)}[d]
            for r in range(4):
                for c in range(4):
                    v = g[r][c]
                    if v == 0:
                        continue
                    nr, nc = r + step[0], c + step[1]
                    if not (0 <= nr < 4 and 0 <= nc < 4):
                        continue
                    t = g[nr][nc]
                    if t == 0 or compiler.merge_result(t, v) is not None:
                        return True
            return False

        boards = [
            [[0, 1, 2, 0], [0, 3, 3, 6], [1, 0, 12, 24], [2, 0, 0, 48]],
            [[24, 3, 2, 0], [1, 96, 48, 0], [3, 6, 0, 0], [12, 3, 3, 0]],
            [[48, 3, 12, 0], [24, 6, 24, 6], [12, 24, 6, 12], [24, 48, 12, 24]],
            [[0, 1, 0, 2], [48, 6, 24, 48], [1, 2, 2, 1], [12, 24, 3, 6]],
            [[3, 6, 24, 0], [6, 2, 0, 0], [3, 1, 0, 0], [24, 24, 0, 0]],
            [[96, 3, 12, 6], [48, 6, 24, 12], [24, 48, 12, 24], [12, 96, 48, 0]],
        ]
        for grid in boards:
            legal_dirs = [d for d in ("up", "left", "down", "right")
                          if compiler.simulate_slide(grid, d) is not None]
            st = self._state([row[:] for row in grid],
                             ["up", "left", "down", "right"])
            out = compiler.compile_state(st)
            self.assertEqual(sorted(out["options"]), sorted(legal_dirs), grid)
            for d in legal_dirs:
                # mirror agrees with the independent advance reference
                self.assertEqual(compiler._advance_possible(grid, d),
                                 ref_advance(grid, d), (grid, d))
                mw = out["options"][d]["min_wiggles_after_spawn"]
                self.assertIsInstance(mw, int)
                self.assertIn(mw, (0, 1, 2, 3, 4), (grid, d))
                # and the reference min: brute force over spawn-edge empties
                nxt = compiler.simulate_slide(grid, d)["next"]
                worst = None
                for (r, c) in compiler._opposite_edge_cells(d):
                    if nxt[r][c] != 0:
                        continue
                    h = [row[:] for row in nxt]
                    h[r][c] = st["queue"][0]
                    n = sum(1 for dd in ("up", "left", "down", "right")
                            if ref_advance(h, dd))
                    worst = n if worst is None else min(worst, n)
                self.assertEqual(mw, worst, (grid, d))


# ---------------- planner facts in the compiled options ------------------

class PlannerFacts(unittest.TestCase):
    """compile_state() attaches the expectimax verdict per option so Jev
    (which cannot do math) sees each swipe's search result. The planner is
    deterministic, so these tests pin RELATIVE properties (rank structure,
    determinism, gating), never absolute scores."""

    # four legal dirs with strictly distinct depth-3 scores (verified):
    #   up 99957, left 101866, down 96310, right -10**9 (root-pruned)
    DISTINCT_BOARD = [[3, 1, 0, 0], [6, 0, 0, 0], [12, 0, 0, 0], [24, 0, 0, 2]]

    def setUp(self):
        self._saved_depth = compiler.get_planner_depth()

    def tearDown(self):
        compiler.set_planner_depth(self._saved_depth)

    def _state(self, grid, valid):
        st = json.loads(json.dumps(SAMPLE_STATE))
        st["grid"] = [row[:] for row in grid]
        st["validMoves"] = list(valid)
        st["queue"] = [1, 2, 3]
        return st

    def test_plan_keys_present_on_every_legal_option(self):
        out = compiler.compile_state(SAMPLE_STATE)
        self.assertTrue(out["options"])
        for d, f in out["options"].items():
            self.assertIsInstance(f["plan_score"], int)
            self.assertIsInstance(f["plan_rank"], int)
            self.assertIsInstance(f["plan_best"], bool)
        self.assertEqual(out["board"]["planner_depth"],
                         self._saved_depth)

    def test_ranks_are_a_permutation_of_1_to_n(self):
        for grid in (SAMPLE_STATE["grid"], self.DISTINCT_BOARD):
            out = compiler.compile_state(
                self._state(grid, ["up", "left", "down", "right"]))
            ranks = sorted(f["plan_rank"] for f in out["options"].values())
            self.assertEqual(ranks, list(range(1, len(out["options"]) + 1)),
                             grid)

    def test_plan_best_unique_and_consistent_with_scores(self):
        out = compiler.compile_state(
            self._state(self.DISTINCT_BOARD,
                        ["up", "left", "down", "right"]))
        opts = out["options"]
        best = [d for d, f in opts.items() if f["plan_best"]]
        self.assertEqual(len(best), 1)
        self.assertEqual(opts[best[0]]["plan_rank"], 1)
        top = max(f["plan_score"] for f in opts.values())
        self.assertEqual(opts[best[0]]["plan_score"], top)
        # ranks respect score order: rank i score >= rank i+1 score
        by_rank = sorted(opts.values(), key=lambda f: f["plan_rank"])
        scores = [f["plan_score"] for f in by_rank]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_set_planner_depth_zero_drops_all_plan_keys(self):
        compiler.set_planner_depth(0)
        out = compiler.compile_state(SAMPLE_STATE)
        self.assertTrue(out["options"])
        for d, f in out["options"].items():
            for k in ("plan_score", "plan_rank", "plan_best", "plan_total"):
                self.assertNotIn(k, f, d)
        self.assertNotIn("planner_depth", out["board"])
        # the criterion line simply has no plan fragment to lead with
        line = compiler.criterion_line("up", out["options"]["up"])
        self.assertNotIn("plan rank", line)
        self.assertTrue(line.startswith("up: max-tile merge"))
        # restoring the depth brings the facts back
        compiler.set_planner_depth(self._saved_depth)
        out2 = compiler.compile_state(SAMPLE_STATE)
        self.assertIn("plan_score", out2["options"]["up"])

    def test_plan_facts_deterministic_repeat(self):
        st = self._state(self.DISTINCT_BOARD,
                         ["up", "left", "down", "right"])
        a = compiler.compile_state(st)["options"]
        b = compiler.compile_state(st)["options"]
        self.assertEqual(json.dumps(a, sort_keys=True),
                         json.dumps(b, sort_keys=True))

    def test_criterion_line_leads_with_plan_verdict(self):
        out = compiler.compile_state(SAMPLE_STATE)
        f = out["options"]["up"]
        line = compiler.criterion_line("up", f)
        lead = "up: plan rank %d of %d, plan score %d, max-tile merge" % (
            f["plan_rank"], f["plan_total"], f["plan_score"])
        self.assertTrue(line.startswith(lead), line)

    def test_planner_failure_degrades_to_no_plan_keys(self):
        # a broken planner must NEVER crash the bot: simulate plan() raising
        saved = compiler._PLANNER
        try:
            def boom(*a, **kw):
                raise RuntimeError("planner under repair")
            compiler._PLANNER = boom
            compiler._PLAN_TRIED = True
            out = compiler.compile_state(SAMPLE_STATE)
            for d, f in out["options"].items():
                self.assertNotIn("plan_score", f, d)
            self.assertNotIn("planner_depth", out["board"])
            # and the game-relevant facts are all still there
            for k in D5_KEYS:
                if k.startswith("plan_"):
                    continue
                self.assertIn(k, out["options"]["up"], k)
        finally:
            compiler._PLANNER = saved


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
