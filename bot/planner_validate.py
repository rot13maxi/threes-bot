"""Validation harness for bot/planner.py (runs standalone: python3 planner_validate.py).

    python3 planner_validate.py            # default: ex-t-d3 ladder (12/20)
    python3 planner_validate.py --depth 5  # ex-t-d5-np ladder (slow, ~2 s/move)

Determinism: stdout is byte-identical across runs (all wall-clock / latency
stats go to stderr). The only randomness anywhere is the *seeded* engine
PRNG plus seeded state sampling in the slide cross-check.

Contents:
  1. ThreesSim — faithful Python port of game/lib/engine.js (mulberry32 PRNG,
     queue, initial placement retry, spawn holes, game over). Asserted
     move-for-move against an embedded node-generated golden replay.
  2. planner slide cross-check vs the engine mirror (all 4 dirs incl. illegal).
  3. Seeds 101-120 played with argmax(plan(...)) (rank-1 compliance) printing
     per-seed max_tile + wins/20 on stdout; timing stats on stderr.
  4. Move-for-move agreement vs the JS oracle (game/_search_oracle.js): the
     full per-seed dir line AND the turn-10/30/60 board snapshots must match
     the embedded `probe` dumps from the JS run (seeds 101-120).
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import planner  # noqa: E402
from planner import plan, rank_options, self_time  # noqa: E402

SEEDS = list(range(101, 121))
WIN = 96

# ---------------------------------------------------------------------------
# engine port (game/lib/engine.js)
# ---------------------------------------------------------------------------

DIRS = ("up", "down", "left", "right")


def mulberry32(seed):
    """Exact port of engine mulberry32 (uint32 semantics; Math.imul via _imul)."""
    MASK = 0xFFFFFFFF
    s = [seed & MASK]

    def _imul(a, b):
        # low 32 bits of a*b (unsigned emulation of Math.imul)
        mid = (((a >> 16) * (b & 0xFFFF) + (a & 0xFFFF) * (b >> 16)) << 16)
        return (mid + (a & 0xFFFF) * (b & 0xFFFF)) & MASK

    def rand():
        s[0] = (s[0] + 0x6D2B79F5) & MASK
        a = s[0]
        t = _imul(a ^ (a >> 15), a | 1)
        t = ((t + _imul(t ^ (t >> 7), t | 61)) & MASK) ^ t
        t ^= t >> 14
        return (t & MASK) / 4294967296.0

    return rand


def merge_result(a, b):
    if a == 0 or b == 0:
        return None
    if (a == 1 and b == 2) or (a == 2 and b == 1):
        return 3
    if a == b and a >= 3 and _is_three_tile(a):
        return 2 * a
    return None


def _is_three_tile(v):
    if v < 3 or v % 3 != 0:
        return False
    v //= 3
    return (v & (v - 1)) == 0


class ThreesSim:
    """Faithful port of class Threes (game/lib/engine.js)."""

    def __init__(self, seed):
        self.seed = seed & 0xFFFFFFFF
        self.rand = mulberry32(self.seed)
        self.score = 0
        self.moves = 0
        self.game_over = False
        self.queue = []
        self.grid = [[0] * 4 for _ in range(4)]
        self._spawn_queue()
        self._place_initial()

    # -- spawn / initial placement -------------------------------------
    def _spawn_queue(self):
        while len(self.queue) < 3:
            self.queue.append(self._weighted_spawn())

    def _weighted_spawn(self):
        mx = self.max_tile()
        r = self.rand()
        if mx >= 96 and r < 0.01:
            return 12
        if mx >= 24 and r < 0.05:
            return 6
        if mx >= 6 and r < 0.12:
            return 3
        return 1 if r < 0.5 else 2

    def _place_initial(self):
        for _attempt in range(50):
            self.grid = [[0] * 4 for _ in range(4)]
            self.score = 0
            self.queue = []
            self._spawn_queue()
            cells = [(r, c) for r in range(4) for c in range(4)]
            for i in range(len(cells) - 1, 0, -1):
                j = int(self.rand() * (i + 1))
                cells[i], cells[j] = cells[j], cells[i]
            (r0, c0), (r1, c1) = cells[0], cells[1]
            self.grid[r0][c0] = self.queue[0]
            self.grid[r1][c1] = self.queue[1]
            self.queue = self.queue[2:]
            self._spawn_queue()
            if self._any_valid():
                return

    # -- slide mirror ----------------------------------------------------
    def _lane_steps(self, direction):
        steps = []
        moved_lanes = set()
        for k in range(4):
            if direction == "left":
                lane = [[k, c] for c in range(4)]
            elif direction == "right":
                lane = [[k, 3 - c] for c in range(4)]
            elif direction == "up":
                lane = [[r, k] for r in range(4)]
            else:
                lane = [[3 - r, k] for r in range(4)]
            moved = [False] * 4
            lane_moved = False
            for i in range(1, 4):
                fr, fc = lane[i]
                f = self.grid[fr][fc]
                if f == 0:
                    continue
                tr, tc = lane[i - 1]
                t = self.grid[tr][tc]
                if moved[i - 1]:
                    steps.append((fr, fc, tr, tc, False, f))
                    moved[i] = True
                    lane_moved = True
                elif t == 0:
                    steps.append((fr, fc, tr, tc, False, f))
                    moved[i] = True
                    lane_moved = True
                else:
                    m = merge_result(t, f)
                    if m is not None:
                        steps.append((fr, fc, tr, tc, True, m))
                        moved[i] = True
                        lane_moved = True
            if lane_moved:
                moved_lanes.add(k)
        return steps, moved_lanes

    def _simulate(self, direction):
        steps, moved_lanes = self._lane_steps(direction)
        if not steps:
            return None
        next_grid = [row[:] for row in self.grid]
        for (fr, fc, tr, tc, _mg, val) in steps:
            next_grid[fr][fc] = 0
            next_grid[tr][tc] = val
        holes = []
        for i in range(4):
            if direction == "up":
                r, c = 3, i
            elif direction == "down":
                r, c = 0, i
            elif direction == "left":
                r, c = i, 3
            else:
                r, c = i, 0
            if next_grid[r][c] == 0:
                lane_idx = c if direction in ("up", "down") else r
                if lane_idx in moved_lanes:
                    holes.append((r, c))
        if not holes:
            return None
        return steps, holes, next_grid

    def valid_moves(self):
        if self.game_over:
            return []
        return [d for d in DIRS if self._simulate(d) is not None]

    def _any_valid(self):
        return any(self._simulate(d) is not None for d in DIRS)

    def max_tile(self):
        return max(max(row) for row in self.grid)

    def apply_move(self, direction):
        if self.game_over or direction not in DIRS:
            return None
        sim = self._simulate(direction)
        if sim is None:
            return None
        steps, holes, _next = sim
        for (fr, fc, tr, tc, mg, val) in steps:
            self.grid[fr][fc] = 0
            self.grid[tr][tc] = val
            if mg:
                self.score += val
        self._spawn_queue()
        tile = self.queue.pop(0)
        self._spawn_queue()
        sr, sc = holes[int(self.rand() * len(holes))]
        self.grid[sr][sc] = tile
        self.moves += 1
        spawn = {"value": tile, "row": sr, "col": sc}
        if not self._any_valid():
            self.game_over = True
        return {"ok": True, "spawn": spawn, "score": self.score,
                "moves": self.moves, "gameOver": self.game_over,
                "next": list(self.queue)}


# ---------------------------------------------------------------------------
# engine goldens (node game/lib/engine.js, embedded)
# ---------------------------------------------------------------------------
# Golden blob assigned after the defs (see bottom of file).
ENGINE_GOLDEN = None


def check_engine():
    """Replay ThreesSim against the node goldens: initial placement and the
    first 15 scripted moves per seed (grid, spawn, score, queue, validMoves)."""
    gold = ENGINE_GOLDEN
    if not gold:
        raise SystemExit("ENGINE_GOLDEN missing — harness must ship its goldens")
    checks = 0
    for seed_str, spec in gold.items():
        sim = ThreesSim(int(seed_str))
        assert sim.grid == spec["initGrid"], ("init grid", seed_str)
        assert sim.queue == spec["initQueue"], ("init queue", seed_str)
        assert sim.valid_moves() == spec["initVM"], ("init vm", seed_str)
        for row in spec["rows"]:
            r = sim.apply_move(row["dir"])
            assert r is not None and r["ok"], (seed_str, row["dir"])
            assert r["spawn"] == row["spawn"], ("spawn", seed_str, row)
            assert sim.grid == row["grid"], ("grid", seed_str, row["dir"])
            assert sim.score == row["score"], ("score", seed_str)
            assert sim.queue == row["queue"], ("queue", seed_str)
            assert sim.valid_moves() == row["vm"], ("vm", seed_str)
            assert sim.game_over == row["gameOver"], ("over", seed_str)
            checks += 6
    print("engine mirror vs lib/engine.js: %d checks, 0 mismatches" % checks)
    return checks


def check_slide_vs_planner():
    """planner._slide must agree with the ThreesSim/compiler slide mirror on
    random-played states (legality + hole count + next grid)."""
    import random  # only for state *generation* order; results unaffected

    mismatches = 0
    states = 0
    for seed in (101, 102, 107, 113):
        rnd = random.Random(seed)
        sim = ThreesSim(seed)
        guard = 0
        while not sim.game_over and guard < 400:
            vm = sim.valid_moves()
            if not vm:
                break
            # probe ALL four dirs every state, incl. illegal ones
            grid = [row[:] for row in sim.grid]
            for d in DIRS:
                sim_res = sim._simulate(d)
                p = planner._slide(planner._pack_grid(grid),
                                   ("up", "down", "left", "right").index(d))
                ok = (sim_res is not None) == (p is not None)
                if ok and p is not None:
                    holes_eng = sorted(r * 4 + c for (r, c) in sim_res[1])
                    ok = (tuple(holes_eng) == p[1]
                          and planner._pack_grid(sim_res[2]) == p[0])
                if not ok:
                    mismatches += 1
                states += 1
            if seed == 101 and guard % 3:
                d = vm[0]
            else:
                d = vm[rnd.randrange(len(vm))]
            sim.apply_move(d)
            guard += 1
    print("planner._slide vs engine mirror: %d states, %d mismatches"
          % (states, mismatches))
    assert mismatches == 0
    return states


# ---------------------------------------------------------------------------
# full-game runner with the planner
# ---------------------------------------------------------------------------

def play_planner(seed, depth, nodes, prune_delta=None):
    sim = ThreesSim(seed)
    guard = 0
    t_plan = 0.0
    worst_t = 0.0
    worst_move = 0
    exhausted = 0
    total_nodes = 0
    worst_nodes = 0
    dirs = []
    snaps = {}
    while not sim.game_over and guard < 3000:
        guard += 1
        vm = sim.valid_moves()
        if not vm:
            break
        grid = [row[:] for row in sim.grid]
        queue = list(sim.queue)
        pd = prune_delta
        if pd is None:
            pd = 0.25 if depth <= 3 else 0.0
        scores = plan(grid, queue, vm, depth=depth, node_budget=nodes,
                      prune_delta=pd)
        st = self_time()
        t_plan += st.get("time_s", 0.0)
        if st.get("time_s", 0.0) > worst_t:
            worst_t = st["time_s"]
            worst_move = guard
        exhausted += 1 if st.get("budget_exhausted") else 0
        total_nodes += st.get("nodes", 0)
        worst_nodes = max(worst_nodes, st.get("nodes", 0))
        ranked = rank_options(vm, scores)
        d = ranked[0]
        dirs.append(d)
        if guard in (10, 30, 60):
            snaps[guard] = planner._pack_grid(grid)
        r = sim.apply_move(d)
        if r is None:                     # rank-1 compliance breach — server
            break                          # would reject; must never happen
    n_moves = max(1, len(dirs))
    return {"seed": seed, "maxTile": sim.max_tile(), "moves": sim.moves,
            "score": sim.score, "plan_s": t_plan, "avg_ms": 1000.0 * t_plan / n_moves,
            "worst_ms": 1000.0 * worst_t, "worst_move": worst_move,
            "exhausted": exhausted, "avg_nodes": total_nodes // n_moves,
            "worst_nodes": worst_nodes, "dirs": "".join(dirs), "snaps": snaps}


# ---------------------------------------------------------------------------
# JS oracle cross-check (probe dumps from game/_search_oracle.js:
#   cd game && node _search_oracle.js probe ex-t-d3 | ex-t-d5-np)
# ---------------------------------------------------------------------------

def load_js_probe(depth):
    blob = {3: _JS_PROBE_D3, 5: _JS_PROBE_D5NP}.get(depth)
    if not blob:
        return None
    return {int(r["seed"]): r for r in json.loads(blob)}


def _pack_codes(csv_str):
    g = 0
    for i, tok in enumerate(csv_str.split(",")):
        g |= (int(tok) & 0xF) << (4 * i)
    return g


def cross_check(results, depth, prune_delta, nodes):
    js = load_js_probe(depth)
    if js is None:
        print("SKIP JS cross-check depth=%d (no embedded probe)" % depth)
        return False
    bad = []
    snaps_ok = 0
    for res in results:
        ref = js[res["seed"]]
        if res["dirs"] != ref["dirs"]:
            mine, theirs = res["dirs"], ref["dirs"]
            first = next((k for k in range(min(len(mine), len(theirs)))
                          if mine[k] != theirs[k]), min(len(mine), len(theirs)))
            bad.append("seed %d dir line diverges at move %d (%s vs %s)"
                       % (res["seed"], first + 1, mine[max(0, first - 2):first + 3],
                          theirs[max(0, first - 2):first + 3]))
            continue
        if res["maxTile"] != ref["maxTile"]:
            bad.append("seed %d maxTile %s vs %s" % (res["seed"], res["maxTile"],
                                                     ref["maxTile"]))
            continue
        if set(map(str, res["snaps"])) != set(ref["snaps"]):
            bad.append("seed %d snapshot turns %s vs %s"
                       % (res["seed"], sorted(res["snaps"]), sorted(ref["snaps"])))
            continue
        for turn, csv_str in ref["snaps"].items():
            if res["snaps"].get(int(turn)) != _pack_codes(csv_str):
                bad.append("seed %d turn-%s grid mismatch" % (res["seed"], turn))
            else:
                snaps_ok += 1
    print("JS cross-check depth=%d: %s (%d seeds move-for-move, %d turn-"
          "10/30/60 grids)" % (
        depth, "OK" if not bad else "FAIL", len(results), snaps_ok))
    for b in bad[:8]:
        print("   " + b)
    assert not bad, "JS/Python divergence"
    return True


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def run_depth(depth, nodes, prune_delta, label):
    """stdout: deterministic (seed, max_tile) table + wins line only.
    stderr: every wall-clock / latency / node statistic (varies per run)."""
    t0 = time.time()
    results = [play_planner(s, depth, nodes, prune_delta) for s in SEEDS]
    wall = time.time() - t0
    wins = sum(1 for r in results if r["maxTile"] >= WIN)
    best = max(r["maxTile"] for r in results)
    avg_all = sum(r["avg_ms"] for r in results) / len(results)
    worst = max(results, key=lambda r: r["worst_ms"])
    tot_nodes = sum(r["avg_nodes"] * r["moves"] for r in results)
    tot_moves = sum(r["moves"] for r in results)
    print("\n== %s depth=%d nodes=%d prune=%s ==" % (label, depth, nodes, prune_delta))
    print("seed  max_tile")
    for r in results:
        print("%4d  %8d" % (r["seed"], r["maxTile"]))
    print("%s depth=%d: wins=%d/%d best=%d" % (label, depth, wins, len(SEEDS), best))
    print("timings %s d%d: avg=%.1f ms/move worst=%.1f ms (seed %d move %d) "
          "avg_nodes=%d/move exhausted=%d wall=%.1fs"
          % (label, depth, avg_all, worst["worst_ms"], worst["seed"],
             worst["worst_move"], tot_nodes // max(1, tot_moves),
             sum(r["exhausted"] for r in results), wall),
          file=sys.stderr)
    return {"depth": depth, "wins": wins, "best": best, "avg_ms": avg_all,
            "wall": wall, "results": results}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=int, default=None,
                    help="single depth to run (default: ex-t-d3 only)")
    ap.add_argument("--ladder", action="store_true",
                    help="run the d3 + d5 ladder (d5 is slow: ~2 s/move)")
    ap.add_argument("--nodes", type=int, default=None)
    ap.add_argument("--prune", type=float, default=None)
    ap.add_argument("--seeds", type=str, default=None)
    ap.add_argument("--no-js", action="store_true")
    args = ap.parse_args()

    global SEEDS
    if args.seeds:
        SEEDS = []
        for part in args.seeds.split(","):
            if "-" in part:
                a, b = part.split("-")
                SEEDS += list(range(int(a), int(b) + 1))
            else:
                SEEDS.append(int(part))

    check_engine()
    check_slide_vs_planner()

    # quick sanity: plan never raises & returns ints for the acceptance board
    sc = plan([[48, 24, 0, 0], [0, 0, 0, 0], [0, 1, 2, 0], [0, 0, 0, 3]],
              [1, 2, 3], ["up", "left", "down", "right"])
    assert all(isinstance(v, int) for v in sc.values()), sc
    assert rank_options(["up", "left", "down", "right"], sc)[0] in sc
    print("smoke plan(): %s rank1=%s" % (sc, rank_options(list(sc), sc)[0]))

    if args.depth is not None:
        pr = args.prune
        if pr is None:
            pr = 0.25 if args.depth <= 3 else 0.0
        nd = args.nodes or (40000 if args.depth <= 3 else 400000)
        plans = [(args.depth, nd, pr, "ex-t-d%d" % args.depth)]
    elif args.ladder:
        plans = [(3, args.nodes or 40000,
                  0.25 if args.prune is None else args.prune, "ex-t-d3"),
                 (5, args.nodes or 400000,
                  0.0 if args.prune is None else args.prune, "ex-t-d5-np")]
    else:
        plans = [(3, args.nodes or 40000,
                  0.25 if args.prune is None else args.prune, "ex-t-d3")]

    summaries = []
    for (d, n, pd, label) in plans:
        summaries.append(run_depth(d, n, pd, label))
        if not args.no_js:
            cross_check(summaries[-1]["results"], d, pd, n)

    print("\nSUMMARY")
    for s in summaries:
        print("depth %d: %d/%d wins (target d3>=11, d5>=12), best %d"
              % (s["depth"], s["wins"], len(SEEDS), s["best"]))



# ---------------------------------------------------------------------------
# Embedded node-generated goldens (regenerate: see header comment above each).
# ---------------------------------------------------------------------------
_ENGINE_GOLDEN_JSON = """{"101":{"initGrid":[[0,0,0,0],[0,0,0,0],[0,0,0,2],[1,0,0,0]],"initQueue":[2,1,1],"initVM":["up","down","left","right"],"rows":[{"dir":"up","spawn":{"value":2,"row":3,"col":0},"score":0,"moves":1,"grid":[[0,0,0,0],[0,0,0,2],[1,0,0,0],[2,0,0,0]],"queue":[1,1,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"left","spawn":{"value":1,"row":1,"col":3},"score":0,"moves":2,"grid":[[0,0,0,0],[0,0,2,1],[1,0,0,0],[2,0,0,0]],"queue":[1,2,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"down","spawn":{"value":1,"row":0,"col":3},"score":3,"moves":3,"grid":[[0,0,0,1],[0,0,0,0],[0,0,2,1],[3,0,0,0]],"queue":[2,2,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"right","spawn":{"value":2,"row":3,"col":0},"score":6,"moves":4,"grid":[[0,0,0,1],[0,0,0,0],[0,0,0,3],[2,3,0,0]],"queue":[2,1,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"up","spawn":{"value":2,"row":3,"col":1},"score":6,"moves":5,"grid":[[0,0,0,1],[0,0,0,3],[2,3,0,0],[0,2,0,0]],"queue":[1,1,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"left","spawn":{"value":1,"row":0,"col":3},"score":6,"moves":6,"grid":[[0,0,1,1],[0,0,3,0],[2,3,0,0],[2,0,0,0]],"queue":[1,2,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"down","spawn":{"value":1,"row":0,"col":2},"score":6,"moves":7,"grid":[[0,0,1,0],[0,0,1,1],[2,0,3,0],[2,3,0,0]],"queue":[2,1,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"right","spawn":{"value":2,"row":2,"col":0},"score":6,"moves":8,"grid":[[0,0,0,1],[0,0,1,1],[2,2,0,3],[0,2,3,0]],"queue":[1,1,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"up","spawn":{"value":1,"row":3,"col":0},"score":6,"moves":9,"grid":[[0,0,1,1],[2,2,0,1],[0,2,3,3],[1,0,0,0]],"queue":[1,1,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"left","spawn":{"value":1,"row":2,"col":3},"score":6,"moves":10,"grid":[[0,1,1,0],[2,2,1,0],[2,3,3,1],[1,0,0,0]],"queue":[1,1,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"down","spawn":{"value":1,"row":0,"col":0},"score":9,"moves":11,"grid":[[1,0,0,0],[0,1,1,0],[2,2,1,0],[3,3,3,1]],"queue":[1,1,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"right","spawn":{"value":1,"row":1,"col":0},"score":15,"moves":12,"grid":[[0,1,0,0],[1,0,1,1],[0,2,2,1],[0,3,6,1]],"queue":[1,1,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"up","spawn":{"value":1,"row":3,"col":0},"score":15,"moves":13,"grid":[[1,1,1,1],[0,2,2,1],[0,3,6,1],[1,0,0,0]],"queue":[1,2,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"left","spawn":{"value":1,"row":2,"col":3},"score":15,"moves":14,"grid":[[1,1,1,1],[2,2,1,0],[3,6,1,1],[1,0,0,0]],"queue":[2,2,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"down","spawn":{"value":2,"row":0,"col":1},"score":18,"moves":15,"grid":[[0,2,0,0],[3,1,1,1],[3,2,1,0],[1,6,1,1]],"queue":[2,1,1],"vm":["up","down","left","right"],"gameOver":false}]},"102":{"initGrid":[[0,0,0,2],[2,0,0,0],[0,0,0,0],[0,0,0,0]],"initQueue":[2,2,1],"initVM":["up","down","left","right"],"rows":[{"dir":"up","spawn":{"value":2,"row":3,"col":0},"score":0,"moves":1,"grid":[[2,0,0,2],[0,0,0,0],[0,0,0,0],[2,0,0,0]],"queue":[2,1,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"left","spawn":{"value":2,"row":0,"col":3},"score":0,"moves":2,"grid":[[2,0,2,2],[0,0,0,0],[0,0,0,0],[2,0,0,0]],"queue":[1,1,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"down","spawn":{"value":1,"row":0,"col":0},"score":0,"moves":3,"grid":[[1,0,0,0],[2,0,2,2],[0,0,0,0],[2,0,0,0]],"queue":[1,2,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"right","spawn":{"value":1,"row":0,"col":0},"score":0,"moves":4,"grid":[[1,1,0,0],[0,2,2,2],[0,0,0,0],[0,2,0,0]],"queue":[2,2,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"up","spawn":{"value":2,"row":3,"col":2},"score":3,"moves":5,"grid":[[1,3,2,2],[0,0,0,0],[0,2,0,0],[0,0,2,0]],"queue":[2,2,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"left","spawn":{"value":2,"row":3,"col":3},"score":3,"moves":6,"grid":[[1,3,2,2],[0,0,0,0],[2,0,0,0],[0,2,0,2]],"queue":[2,1,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"down","spawn":{"value":2,"row":0,"col":1},"score":3,"moves":7,"grid":[[0,2,0,0],[1,3,2,2],[0,0,0,0],[2,2,0,2]],"queue":[1,1,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"right","spawn":{"value":1,"row":0,"col":0},"score":3,"moves":8,"grid":[[1,0,2,0],[1,3,2,2],[0,0,0,0],[0,2,2,2]],"queue":[1,1,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"up","spawn":{"value":1,"row":3,"col":2},"score":3,"moves":9,"grid":[[1,3,2,2],[1,0,2,0],[0,2,2,2],[0,0,1,0]],"queue":[1,2,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"left","spawn":{"value":1,"row":1,"col":3},"score":3,"moves":10,"grid":[[1,3,2,2],[1,2,0,1],[2,2,2,0],[0,1,0,0]],"queue":[2,1,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"down","spawn":{"value":2,"row":0,"col":1},"score":6,"moves":11,"grid":[[0,2,0,0],[1,3,2,2],[1,2,0,1],[2,3,2,0]],"queue":[1,1,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"right","spawn":{"value":1,"row":0,"col":0},"score":6,"moves":12,"grid":[[1,0,2,0],[1,3,2,2],[0,1,2,1],[0,2,3,2]],"queue":[1,2,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"up","spawn":{"value":1,"row":3,"col":3},"score":6,"moves":13,"grid":[[1,3,2,2],[1,1,2,1],[0,2,2,2],[0,0,3,1]],"queue":[2,2,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"left","spawn":{"value":2,"row":3,"col":3},"score":9,"moves":14,"grid":[[1,3,2,2],[1,3,1,0],[2,2,2,0],[0,3,1,2]],"queue":[2,1,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"down","spawn":{"value":2,"row":0,"col":1},"score":18,"moves":15,"grid":[[0,2,0,0],[1,6,2,2],[1,2,1,0],[2,3,3,2]],"queue":[1,1,2],"vm":["up","down","left","right"],"gameOver":false}]},"103":{"initGrid":[[0,0,0,0],[0,1,2,0],[0,0,0,0],[0,0,0,0]],"initQueue":[1,2,2],"initVM":["up","down","left","right"],"rows":[{"dir":"up","spawn":{"value":1,"row":3,"col":1},"score":0,"moves":1,"grid":[[0,1,2,0],[0,0,0,0],[0,0,0,0],[0,1,0,0]],"queue":[2,2,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"left","spawn":{"value":2,"row":0,"col":3},"score":0,"moves":2,"grid":[[1,2,0,2],[0,0,0,0],[0,0,0,0],[1,0,0,0]],"queue":[2,2,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"down","spawn":{"value":2,"row":0,"col":0},"score":0,"moves":3,"grid":[[2,0,0,0],[1,2,0,2],[0,0,0,0],[1,0,0,0]],"queue":[2,1,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"right","spawn":{"value":2,"row":1,"col":0},"score":0,"moves":4,"grid":[[0,2,0,0],[2,1,2,2],[0,0,0,0],[0,1,0,0]],"queue":[1,2,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"up","spawn":{"value":1,"row":3,"col":0},"score":3,"moves":5,"grid":[[2,3,2,2],[0,0,0,0],[0,1,0,0],[1,0,0,0]],"queue":[2,2,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"left","spawn":{"value":2,"row":2,"col":3},"score":3,"moves":6,"grid":[[2,3,2,2],[0,0,0,0],[1,0,0,2],[1,0,0,0]],"queue":[2,1,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"down","spawn":{"value":2,"row":0,"col":1},"score":3,"moves":7,"grid":[[0,2,0,0],[2,3,2,2],[1,0,0,0],[1,0,0,2]],"queue":[1,2,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"right","spawn":{"value":1,"row":3,"col":0},"score":3,"moves":8,"grid":[[0,0,2,0],[2,3,2,2],[0,1,0,0],[1,1,0,2]],"queue":[2,1,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"up","spawn":{"value":2,"row":3,"col":3},"score":3,"moves":9,"grid":[[2,3,2,2],[0,1,2,0],[1,1,0,2],[0,0,0,2]],"queue":[1,1,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"left","spawn":{"value":1,"row":1,"col":3},"score":3,"moves":10,"grid":[[2,3,2,2],[1,2,0,1],[1,1,2,0],[0,0,2,0]],"queue":[1,2,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"down","spawn":{"value":1,"row":0,"col":1},"score":3,"moves":11,"grid":[[0,1,0,0],[2,3,2,2],[1,2,2,1],[1,1,2,0]],"queue":[2,2,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"right","spawn":{"value":2,"row":0,"col":0},"score":6,"moves":12,"grid":[[2,0,1,0],[2,3,2,2],[0,1,2,3],[0,1,1,2]],"queue":[2,1,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"up","spawn":{"value":2,"row":3,"col":2},"score":9,"moves":13,"grid":[[2,3,3,2],[2,1,2,3],[0,1,1,2],[0,0,2,0]],"queue":[1,1,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"left","spawn":{"value":1,"row":3,"col":3},"score":18,"moves":14,"grid":[[2,6,2,0],[3,2,3,0],[1,1,2,0],[0,2,0,1]],"queue":[1,1,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"down","spawn":{"value":1,"row":0,"col":1},"score":21,"moves":15,"grid":[[0,1,0,0],[2,6,2,0],[3,2,3,0],[1,3,2,1]],"queue":[1,2,1],"vm":["up","left","right"],"gameOver":false}]},"104":{"initGrid":[[0,0,0,0],[0,1,0,0],[0,0,0,0],[1,0,0,0]],"initQueue":[1,1,2],"initVM":["up","down","left","right"],"rows":[{"dir":"up","spawn":{"value":1,"row":3,"col":1},"score":0,"moves":1,"grid":[[0,1,0,0],[0,0,0,0],[1,0,0,0],[0,1,0,0]],"queue":[1,2,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"left","spawn":{"value":1,"row":3,"col":3},"score":0,"moves":2,"grid":[[1,0,0,0],[0,0,0,0],[1,0,0,0],[1,0,0,1]],"queue":[2,1,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"down","spawn":{"value":2,"row":0,"col":0},"score":0,"moves":3,"grid":[[2,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,1]],"queue":[1,2,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"right","spawn":{"value":1,"row":1,"col":0},"score":0,"moves":4,"grid":[[0,2,0,0],[1,1,0,0],[0,1,0,0],[0,1,0,1]],"queue":[2,1,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"up","spawn":{"value":2,"row":3,"col":3},"score":3,"moves":5,"grid":[[1,3,0,0],[0,1,0,0],[0,1,0,1],[0,0,0,2]],"queue":[1,2,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"left","spawn":{"value":1,"row":3,"col":3},"score":3,"moves":6,"grid":[[1,3,0,0],[1,0,0,0],[1,0,1,0],[0,0,2,1]],"queue":[2,1,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"down","spawn":{"value":2,"row":0,"col":2},"score":6,"moves":7,"grid":[[0,0,2,0],[1,3,0,0],[1,0,0,0],[1,0,3,1]],"queue":[1,2,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"right","spawn":{"value":1,"row":0,"col":0},"score":6,"moves":8,"grid":[[1,0,0,2],[0,1,3,0],[0,1,0,0],[0,1,3,1]],"queue":[2,2,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"up","spawn":{"value":2,"row":3,"col":1},"score":6,"moves":9,"grid":[[1,1,3,2],[0,1,0,0],[0,1,3,1],[0,2,0,0]],"queue":[2,1,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"left","spawn":{"value":2,"row":2,"col":3},"score":6,"moves":10,"grid":[[1,1,3,2],[1,0,0,0],[1,3,1,2],[2,0,0,0]],"queue":[1,1,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"down","spawn":{"value":1,"row":0,"col":2},"score":9,"moves":11,"grid":[[0,0,1,0],[1,1,3,2],[1,0,0,0],[3,3,1,2]],"queue":[1,1,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"right","spawn":{"value":1,"row":0,"col":0},"score":12,"moves":12,"grid":[[1,0,0,1],[1,1,3,2],[0,1,0,0],[0,3,3,3]],"queue":[1,2,1],"vm":["up","down","left","right"],"gameOver":false},{"dir":"up","spawn":{"value":1,"row":3,"col":2},"score":15,"moves":13,"grid":[[1,1,3,3],[1,1,0,0],[0,3,3,3],[0,0,1,0]],"queue":[2,1,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"left","spawn":{"value":2,"row":3,"col":3},"score":21,"moves":14,"grid":[[1,1,6,0],[1,1,0,0],[3,3,3,0],[0,1,0,2]],"queue":[1,2,2],"vm":["up","down","left","right"],"gameOver":false},{"dir":"down","spawn":{"value":1,"row":0,"col":0},"score":21,"moves":15,"grid":[[1,1,0,0],[1,1,6,0],[1,3,0,0],[3,1,3,2]],"queue":[2,2,3],"vm":["up","down","right"],"gameOver":false}]}}"""
_JS_PROBE_D3 = """[{"avgNodes":194,"dirs":"rightleftrightrightrightrightleftleftdownrightupdownupdowndowndowndownleftdowndownleftrightrightdowndowndownleftrightdowndowndownupdownleftrightrightdownupuprightuprightrightrightrightrightrightuprightupleftupuprightupleftupleftrightupleftupupupleftleftrightrightleftrightleftupupleftrightupuprightuprightupleftupupdownrightuprightdownupleftupright","maxNodes":581,"maxTile":96,"moves":93,"seed":101,"snaps":{"10":"0,1,0,0,0,0,0,0,0,0,0,0,4,4,1,2","30":"0,0,0,1,0,1,0,0,3,1,3,3,2,5,5,5","60":"2,2,6,7,0,4,4,3,1,4,0,0,0,2,0,0"}},{"avgNodes":201,"dirs":"leftleftleftrightrightleftleftleftrightrightupupupupleftupupupupdownupleftdownupuprightupleftuprightleftdownrightleftdownrightrightrightupleftleftdowndownleftleftleftupupup","maxNodes":695,"maxTile":48,"moves":49,"seed":102,"snaps":{"10":"2,3,2,3,1,2,2,3,0,0,0,0,0,0,0,0","30":"2,4,4,3,5,5,2,0,0,0,3,0,0,0,2,2"}},{"avgNodes":249,"dirs":"rightrightupleftleftrightupupupuprightleftupupdownupleftdownleftleftupleftleftleftleftleftleftdownleftleftdownupleftleftupleftupupleftleftdownleftleftrightleftleftleftdownleftupupupdownleftleftleftuprightrightupleftupupleftup","maxNodes":813,"maxTile":48,"moves":65,"seed":103,"snaps":{"10":"2,3,3,3,2,2,0,0,0,1,0,0,0,2,0,0","30":"5,0,1,0,6,3,1,2,1,0,0,0,1,0,0,0","60":"2,0,5,3,0,2,5,2,2,7,3,2,0,2,5,2"}},{"avgNodes":140,"dirs":"updowndownupupupupdowndownupleftdownleftupleftleftleftdownupupdownrightdownrightdownleftrightdownleftdownupupuprightdownuprightdownrightdownleftdownrightdownleftupuprightrightdownupupleftupleftdownupdownleftuprightrightupleftrightupleftrightleftupupleftupupupleftupdowndownleftleftleftrightrightupupupleftuprightrightrightrightupleftleftupleftdowndownleftupleftdownleftleftdownupupleftleftleftdowndowndownup","maxNodes":380,"maxTile":96,"moves":116,"seed":104,"snaps":{"10":"2,0,0,0,1,3,0,0,1,1,0,0,4,1,0,0","30":"1,0,1,0,4,4,2,0,1,1,3,0,4,5,5,2","60":"3,2,0,1,4,1,5,2,1,7,5,3,1,5,3,0"}},{"avgNodes":96,"dirs":"rightrightrightleftleftdownupupupdowndowndowndownleftleftupleftleftleftupleftdownleftleftleftdownleftleftrightrightdownleftleftdownrightleftdowndownleftupupdownleftrightupleftdownleftdownuprightupdown","maxNodes":446,"maxTile":24,"moves":53,"seed":105,"snaps":{"10":"4,2,2,2,0,2,2,0,0,1,0,0,0,3,0,0","30":"0,2,3,3,2,2,2,4,0,4,2,2,5,4,2,3"}},{"avgNodes":197,"dirs":"upupdownupdowndowndowndownleftleftrightleftrightleftleftdownuprightleftdownleftleftdowndowndownrightdownleftdowndownupupdownupdownleftupleftrightrightuprightleftdowndownupleftdownrightleftleftdownrightdownleftdowndownleftdowndowndowndowndownuprightleftupdowndownrightupdownleftleftdownupdowndownrightuprightupleftleftleftupdownleft","maxNodes":576,"maxTile":96,"moves":88,"seed":106,"snaps":{"10":"1,0,0,0,1,0,0,0,4,0,0,2,4,0,0,0","30":"2,0,0,0,1,1,0,0,3,4,0,1,5,5,5,3","60":"1,2,2,0,0,2,2,2,3,6,2,0,5,7,3,0"}},{"avgNodes":246,"dirs":"leftleftleftleftleftrightupupupleftleftupupdownupleftleftrightleftleftupupupupupupupupleftleftupupleftupuprightleftupleftdownleftrightright","maxNodes":690,"maxTile":24,"moves":43,"seed":107,"snaps":{"10":"1,2,3,3,0,3,0,2,2,0,0,0,1,0,0,0","30":"6,2,4,0,3,2,2,3,0,2,2,2,0,0,2,0"}},{"avgNodes":114,"dirs":"downdowndownupdowndowndowndowndownleftdowndownrightdownupdownrightrightdowndownuprightrightrightrightrightdownleftdownrightdowndownupdownleftupupdowndowndownleftdownleftupleftdowndownrightrightdownrightdownrightrightdownleftrightleftdowndownleftrightdowndownleftleftdowndownrightuprightrightdownrightdowndownupupuprightleftdowndownrightupdownrightupupupleftdowndownleftdownrightleftdownupleftright","maxNodes":369,"maxTile":96,"moves":101,"seed":108,"snaps":{"10":"0,0,0,2,0,0,0,2,0,0,0,2,0,0,0,5","30":"0,0,2,2,0,0,5,0,0,2,2,2,3,5,5,0","60":"0,2,0,0,2,0,0,0,4,3,3,3,3,4,7,6"}},{"avgNodes":133,"dirs":"updownuprightupdownrightrightrightdownrightrightupdownuprightupleftleftupupleftleftupleftleftleftleftleftleftleftupdownleftupleftleftdownupleftleftupuprightleftrightleftuprightleftupleftupuprightuprightdowndownupleftuprightupleftrightuprightrightleftuprightupupleftuprightrightupdownleftupupuprightdownleftuprightleftdownleftdownleftleftleftleftdowndownupleftdownleftuprightleftdowndownrightrightdownleftrightrightright","maxNodes":770,"maxTile":96,"moves":115,"seed":109,"snaps":{"10":"2,2,2,2,0,0,0,3,0,0,2,3,0,0,0,3","30":"6,2,0,0,4,3,0,0,4,3,2,2,3,3,0,0","60":"0,3,0,0,7,0,3,2,6,4,2,3,1,3,4,4"}},{"avgNodes":226,"dirs":"upupdownrightupleftrightrightrightrightdownupleftrightdownrightrightrightleftleftdowndowndownleftupupleftleftleftupleftleftleftleftleftleftdownleftdownrightleftleftdownleftrightup","maxNodes":614,"maxTile":48,"moves":46,"seed":110,"snaps":{"10":"1,2,2,3,0,0,0,3,0,0,2,3,0,0,0,2","30":"2,2,0,0,5,2,0,0,3,0,2,0,6,3,0,1"}},{"avgNodes":149,"dirs":"upupdowndownleftleftleftleftrightrightdowndowndowndowndownrightdowndownuprightrightdownupuprightrightrightuprightdownuprightupleftrightupdownleftuprightrightdowndowndowndowndowndowndownupleftdownleftrightupuprightupupupleftdownleftleftupleftrightupupleftleftleftdownleftupupuprightdownleftdownupleftdownleftleftrightleftupleftdownleftupleftleftuprightleftupleftupleftrightrightleftuprightleftdownleftdownleftupleftdownleftleftleftdownleftdownleftdownupleftdowndownupleftleftdownrightleftdowndownleftrightdownleftupleftleftleftupleftleftdownleftdowndowndownupupupleftleftupleftupupdownleftdownleftdowndowndownleftleftleftupleftleftleftdowndownleftupupupleftleftdownupupupleftdownupupupdownleftupdowndownleftdowndown","maxNodes":797,"maxTile":192,"moves":198,"seed":111,"snaps":{"10":"2,1,0,0,0,2,0,0,0,3,3,3,0,3,0,0","30":"1,2,4,5,0,2,2,6,0,0,0,3,0,0,0,4","60":"3,1,4,5,0,6,7,4,1,0,0,1,1,0,0,1"}},{"avgNodes":144,"dirs":"updowndownupdowndownupdowndownuprightleftrightrightrightleftrightleftrightleftdownleftupleftupuprightupupupdownupuprightupupupupuprightrightrightrightdownrightrightuprightrightrightdownrightuprightdownrightdownrightrightrightrightrightrightdowndownrightrightrightdownrightrightrightrightrightrightdownrightrightupdownuprightrightrightdownrightdownuprightrightupdownrightrightupuprightrightdowndownuprightuprightrightrightupdownrightrightrightrightupdownrightupupuprightdowndownrightupdownupdowndownrightdowndowndownleftdowndownuprightdownrightrightleftleftdownrightdownleftleftdownrightrightrightdowndownupupleftleftdown","maxNodes":557,"maxTile":192,"moves":157,"seed":112,"snaps":{"10":"0,0,0,2,0,0,3,1,0,0,1,4,0,0,1,1","30":"2,5,5,4,0,1,4,3,0,3,1,1,0,0,1,2","60":"0,0,3,7,0,0,3,6,0,0,2,5,1,1,2,4"}},{"avgNodes":116,"dirs":"downupdownrightuprightupdownrightdownupupuprightdowndownleftupupleftleftupleftdownleftleftupdownleftupupupleftupdownupupleftupupupleftupupuprightleftrightrightuprightupleftleftupupdownupdowndownupleftupdowndownleftrightdownrightdownrightdownrightuprightupleftrightuprightleftdowndownrightrightupdownleftupdownupuprightrightrightdownrightuprightdownrightupleftdownrightdownleftleftdownrightrightuprightuprightuprightrightdownrightuprightdownrightdownrightdownupuprightleftdownrightdownrightuprightrightdowndownleftdownrightdownrightdownupleftdowndowndowndowndownleftrightdowndownrightleftdownleftupupleftrightrightrightdownrightdownleftdownrightdowndownleftdowndownrightdownleftdownrightupdowndownleftrightdownrightrightupdownupuprightrightuprightrightupdownrightdownrightdownrightuprightrightleftdowndownrightrightleftuprightdowndownupup","maxNodes":874,"maxTile":192,"moves":222,"seed":113,"snaps":{"10":"0,0,0,2,0,0,3,3,0,0,0,3,2,3,2,0","30":"6,0,1,1,5,1,1,1,3,1,0,0,1,0,0,0","60":"7,1,2,0,1,1,1,6,4,1,1,1,1,4,4,1"}},{"avgNodes":103,"dirs":"rightleftleftleftleftrightleftrightrightdownleftdownrightdowndownleftleftleftrightrightleftleftdowndownrightdowndowndownrightupdownuprightdownupleftdownleftdownleftdownleftrightdowndowndowndownrightleftdownupupdownrightdown","maxNodes":444,"maxTile":48,"moves":55,"seed":114,"snaps":{"10":"0,0,0,0,2,2,3,2,0,0,0,0,2,3,3,2","30":"2,1,0,2,2,2,2,2,2,5,2,2,0,5,2,5"}},{"avgNodes":283,"dirs":"downrightdownrightleftrightdownupuprightrightrightleftdownleftrightrightleftupdownupupleftrightleftleftdownleftleftupleftleftleftleftdownupdownupuprightupupleftleftdownleftleftupdownupdowndownrightupupupleft","maxNodes":743,"maxTile":48,"moves":57,"seed":115,"snaps":{"10":"0,0,0,4,2,3,0,3,0,2,0,0,0,0,0,1","30":"2,0,0,0,3,5,3,2,3,5,3,0,3,3,2,1"}},{"avgNodes":168,"dirs":"leftleftleftleftupupdowndownrightdownleftrightleftuprightupupleftleftleftleftleftupleftleftleftleftupupleftdownleftdowndownleftdowndowndowndowndownupleftleftleftdownupupleftuprightleftleftleftrightleftrightleftleftleftdowndownleftupleftleftupleftupleftdowndownleftleftdownleftrightupleftupleftdowndownleftupleftleftupdowndownupleftupleftleftleftupupdownleftrightdownrightdownleftleftdownleftrightleftupleftupupupdownleftdownleftdownupleftupupupleftleftleftupupleftupupupdownleftleftdownrightdownleftrightupleftupupleftupleftdownright","maxNodes":736,"maxTile":192,"moves":150,"seed":116,"snaps":{"10":"0,0,0,1,0,0,0,1,1,0,0,3,0,3,3,3","30":"6,4,2,0,4,3,1,2,3,1,0,0,0,1,0,0","60":"4,5,3,0,7,5,3,2,5,3,1,1,4,3,2,0"}},{"avgNodes":91,"dirs":"upuprightrightleftrightupleftrightdownupleftleftupupupleftdownupupleftupdowndownupdownleftleftdownuprightdownleftleftleftdowndownupupdownrightupleftdownuprightrightrightdownupleftupupuprightuprightupdownupleftleftdownuprightuprightleftupupleftleftdown","maxNodes":563,"maxTile":48,"moves":73,"seed":117,"snaps":{"10":"2,3,3,2,0,0,0,3,0,2,0,2,0,0,0,2","30":"5,2,2,0,2,5,3,2,2,2,2,2,2,2,2,3","60":"0,0,1,3,5,2,2,5,3,7,2,4,4,2,2,3"}},{"avgNodes":108,"dirs":"downupupupdowndowndownupdownrightdownupleftleftupupleftupleftupdownrightupleftleftleftrightdownrightleftleftupuprightleftrightupleftrightupupuprightdownleftdownupuprightuprightdowndownleftuprightleftuprightupleftdownleftupleftleftdowndowndowndownrightleftdownleftdownupleftdownleftupdownleftupleftleftleftupleftupleftupleftleftleftupleftdownleftleftdownupdownupdowndownleftdownleftupleftupuprightdown","maxNodes":559,"maxTile":96,"moves":114,"seed":118,"snaps":{"10":"2,1,0,0,3,3,0,0,1,1,0,0,3,1,0,0","30":"0,0,0,1,0,1,5,4,0,4,5,1,1,1,1,4","60":"5,7,4,1,2,4,1,1,1,6,1,3,0,0,1,3"}},{"avgNodes":137,"dirs":"downleftdowndowndowndowndowndowndownleftleftleftleftleftdowndowndowndownleftrightdowndowndownleftleftupleftdownupupupdownleftleftupupleftdownleftdowndownleftleftdowndownupdownleftleftleftupupleftleftupupupdownleftupupupleftleftupdowndownleftdownupleftleftupupleftupdownleftleftupleftdownleftleftupupleftleftleftleftleftleftrightdownleftdownleftdownupdownleftleftleftupleftleftdownupleftleftupleftleftupuprightupleftupupleftleftupupupleftdownleftdownrightleftleftdowndownrightuprightupleftleftleftupleftdownleftdowndownupdownleftupdownupupleftupleftdownleftupdownleftleftupdownupleftdowndowndownleftupleftupupright","maxNodes":473,"maxTile":192,"moves":176,"seed":119,"snaps":{"10":"2,0,0,0,1,0,0,0,4,0,0,0,4,0,0,1","30":"2,2,0,2,4,4,0,0,2,2,1,0,6,1,0,0","60":"4,2,0,0,6,1,3,1,5,3,0,2,7,3,0,0"}},{"avgNodes":157,"dirs":"leftleftrightleftleftleftrightrightupupleftrightrightupupupupupuprightrightrightdownrightrightrightdownleftdownrightrightrightdowndowndowndownleftrightdowndowndownrightrightuprightdownleftrightleftdownleftrightleftupdownrightupupuprightdownrightdownupleftupleftdownrightrightdownrightrightdownrightleftrightdowndownleftdowndownrightleftleftleftdownleftleftleftleftdownrightdownleftrightdownrightrightleftleftleftdownrightleftdownrightrightleftdownrightleftleftdownleftdownleftrightleftleftdowndownleftdownrightrightrightleftrightleftdownupleftup","maxNodes":630,"maxTile":96,"moves":134,"seed":120,"snaps":{"10":"0,1,3,4,2,0,2,2,0,0,0,0,1,0,0,0","30":"0,0,2,0,0,0,1,0,0,2,5,1,4,4,6,0","60":"3,3,1,1,0,6,6,4,0,1,3,1,0,4,3,7"}}]"""
_JS_PROBE_D5NP = """[{"avgNodes":322689,"dirs":"rightleftrightrightrightrightleftupupdownupleftupleftuprightleftupleftleftleftleftleftupleftupleftleftleftupleftleftupleftdowndownleftleftupupleftleftleftleftleftleftleftupupuprightupupleftleftleftuprightupupupuprightrightupupleftupupleftupupupleftdownleftdowndownleftdowndownleftleftleftupleftupdowndowndownleftdownupleftleftdownupleftdowndowndowndowndownleftupleftuprightleftleftrightupleftupleftdowndownleftupleftleftupupupupupdownleftrightupupleftrightupleftrightdownleftdowndownleftleftupupleftupupleftrightdownupleftuprightdowndownupleftdownleftupdown","maxNodes":400108,"maxTile":192,"moves":162,"seed":101,"snaps":{"10":"0,0,3,0,3,3,3,1,0,0,2,0,1,0,0,0","30":"5,1,3,1,5,3,1,0,4,2,0,0,1,0,3,1","60":"7,6,4,3,0,2,4,0,1,3,0,0,0,2,1,0"}},{"avgNodes":308265,"dirs":"leftupuprightdownleftupdownupdownleftupleftleftupleftdowndownleftupupupleftleftleftleftrightdownrightrightrightdownleftdownrightdowndowndownrightrightupupdownleftupupdownrightup","maxNodes":400083,"maxTile":24,"moves":49,"seed":102,"snaps":{"10":"3,2,2,0,3,0,0,0,3,2,0,2,0,1,0,0","30":"0,0,3,2,2,0,5,2,0,2,5,3,0,4,3,3"}},{"avgNodes":313063,"dirs":"leftleftupleftupleftupupdownupleftupleftdownupdowndowndownleftdownleftleftleftleftleftleftleftleftrightupdownrightleftupleftdowndowndownrightdownrightrightleftleftupleftleftrightdowndownrightrightdownupdownleftleftupleftdownleftdownrightrightup","maxNodes":400094,"maxTile":48,"moves":65,"seed":103,"snaps":{"10":"0,0,0,2,3,3,2,2,3,0,0,0,3,0,0,0","30":"4,3,1,1,0,3,1,1,2,5,0,0,0,5,3,0","60":"5,2,2,2,4,3,2,0,4,7,3,2,5,0,2,2"}},{"avgNodes":274992,"dirs":"upleftleftdownuprightrightrightrightleftrightupleftdownupdownuprightupuprightuprightupupupuprightdownupupuprightdownrightleftrightdownrightuprightrightleftdownrightupleftrightupupleftupdownrightdownleftdownleftdowndownleftdownrightrightdownrightrightdownrightrightuprightleftdownrightdowndowndownrightleftdowndownleftleftdowndownrightdownleftrightdowndowndownrightdownleftdownleftdowndowndownleftrightleftdownleftdownleftleftleftdownrightrightdownleftleftdownrightdownupleftdownleftupleftleftdownleftdownleftleftleftleftdownleftdownrightdownupdownleftupupdownleftdowndownrightleftdownleftrightdownleftdownrightrightupupupdownleftleftleftupdownupleftleftdownupdownleftleftupupupdownuprightrightrightleftrightrightrightdownrightdown","maxNodes":400119,"maxTile":192,"moves":189,"seed":104,"snaps":{"10":"2,1,0,1,0,0,0,1,0,0,1,3,0,0,3,3","30":"0,0,2,6,1,3,1,1,0,4,1,1,0,0,4,1","60":"1,1,0,0,3,1,2,0,7,1,1,3,1,5,4,6"}},{"avgNodes":173310,"dirs":"rightrightdownleftleftrightdownleftupleftleftupdowndowndownleftdowndownupdowndowndownleftleftupleftupleftupleftrightleftleftupdowndownleftleftupdownleftdowndownleftleftrightdowndowndowndownleftleftrightrightdownrightdowndownleftdowndownrightrightdownrightdownleftleftleftdownrightupleftleftdownleftupleftupleftdowndowndownleftrightrightleftrightrightupdownupupleftrightrightleftleftdownleftdowndowndownrightdownleftupdowndownleftdownupdowndownrightdowndowndownupleftdownleftdownrightrightdownleftdownleftrightdownleftdownleftrightdownleftupleftdownleftleftupleftupleftupdowndownupleftupdownleftdowndowndowndownupleftupup","maxNodes":400086,"maxTile":192,"moves":162,"seed":105,"snaps":{"10":"0,2,2,1,2,4,2,2,0,0,0,0,1,0,0,0","30":"2,2,4,0,2,2,2,0,2,5,3,1,5,1,2,2","60":"4,2,2,2,3,4,2,0,3,4,2,2,7,5,4,2"}},{"avgNodes":273127,"dirs":"upupdownupdowndownleftdownleftrightuprightdownuprightrightuprightuprightupupleftleftupuprightleftdownuprightdownrightuprightuprightrightrightrightrightrightrightrightuprightuprightuprightdownrightupdownrightuprightdownuprightuprightrightuprightrightdownrightupupleftuprightupleftleftleftupupleftdowndownleftright","maxNodes":400102,"maxTile":96,"moves":84,"seed":106,"snaps":{"10":"1,0,0,0,3,0,0,0,3,0,0,0,4,0,1,2","30":"2,1,0,0,1,1,4,5,1,3,5,4,3,2,0,3","60":"0,2,4,7,0,2,4,6,2,2,2,4,1,0,0,2"}},{"avgNodes":266434,"dirs":"rightuprightrightrightdownuprightrightleftleftdownuprightleftleftdownupleftupupdownupupupdownrightupupleftuprightupdownuprightdownrightdownrightupdownup","maxNodes":400093,"maxTile":24,"moves":43,"seed":107,"snaps":{"10":"0,0,0,3,0,0,0,3,1,2,3,3,0,0,0,2","30":"0,3,2,4,2,2,5,3,2,5,2,0,2,2,0,0"}},{"avgNodes":300769,"dirs":"downdownleftupupupdownrightrightrightdownrightdownrightrightuprightrightuprightrightupdownrightrightrightupleftupleftleftupupleftrightuprightdowndownleftupleftdownupuprightrightdownrightleftdownrightrightdowndownleftrightleftdowndowndownuprightupleftleftdownupdownrightupleftdowndown","maxNodes":400086,"maxTile":48,"moves":74,"seed":108,"snaps":{"10":"0,0,3,2,2,2,0,3,0,0,0,3,0,0,0,3","30":"2,3,1,6,0,4,3,2,2,2,2,0,0,0,0,2","60":"2,0,2,0,2,4,1,0,2,6,3,1,3,3,7,3"}},{"avgNodes":309234,"dirs":"updownupdowndowndownleftupleftleftleftleftrightuprightleftdowndownleftleftdownrightdownrightdownleftdownrightdowndownleftdownleftdowndownrightleftdowndowndowndownleftdownupdownleftupleftupleftdownleftdownrightdownleftdowndownleftrightdownrightdowndownrightrightuprightrightdowndownleftrightdownleftdowndownrightleftrightleftrightdowndowndowndownrightuprightdownrightdowndownrightrightupupdownrightdownrightrightuprightdownrightdownrightuprightdownuprightuprightleftrightdown","maxNodes":400115,"maxTile":96,"moves":118,"seed":109,"snaps":{"10":"3,2,2,0,0,2,0,2,3,3,0,0,2,0,0,0","30":"2,0,0,0,2,0,0,4,0,0,2,4,0,5,5,5","60":"1,0,1,3,4,2,4,0,2,2,3,0,7,6,4,3"}},{"avgNodes":273901,"dirs":"leftleftupleftupupleftuprightdownleftupleftleftleftrightleftleftleftupupdownupleftupleftleftleftleftleftdownrightuprightupupleftdownupleftdownleftleftdownrightupdownleft","maxNodes":400085,"maxTile":48,"moves":48,"seed":110,"snaps":{"10":"1,4,2,2,0,2,0,0,0,3,0,0,0,2,0,0","30":"5,4,2,0,5,2,2,0,4,0,0,0,3,3,2,1"}},{"avgNodes":354353,"dirs":"upupdowndownupupupupdownrightrightrightrightrightleftrightleftrightrightdownrightleftrightdowndownleftleftleftrightrightrightupuprightrightuprightuprightuprightrightuprightdownrightdownrightrightupdownuprightupdowndownrightdownrightuprightuprightrightleftdownuprightrightdownrightdownupleftuprightdownupdownleftdownleftdownleftleftdowndownleftleftleftupleftleftupleftleftupleftdowndowndowndownupdownupdownleftdownleftupleftdownupleftupleftleftleftleftleftleftdownuprightupleftdownleftdownupleftupupleftdownleftleftupleftleftleftdownleftleftdownleftdowndownleftupdowndowndownleftleftdowndownleftupupleftleftdownuprightupupleftrightupleftleftupupleftupupdowndowndownleftuprightrightleftupleftupupdownrightdowndownrightleftuprightdown","maxNodes":400126,"maxTile":192,"moves":198,"seed":111,"snaps":{"10":"0,2,0,0,0,4,0,0,0,4,0,0,0,3,0,0","30":"1,3,3,0,0,2,2,4,0,5,0,0,3,2,2,6","60":"0,0,1,7,0,0,3,6,0,3,5,4,1,1,1,4"}},{"avgNodes":329177,"dirs":"leftleftrightleftleftrightrightleftrightleftdownleftdowndownupupleftdownleftleftdownleftdownleftdownrightdownleftdowndownrightrightleftdowndowndownrightdownleftdowndownleftdownrightleftdowndowndownleftrightdownupleftdowndowndowndowndownleftleftleftrightdownleftdowndownleftleftdowndowndowndowndowndowndowndowndownleftleftleftleftleftleftupleftleftdownleftdownleftleftdownleftleftleftleftupleftdowndownleftleftupleftupleftdownleftupdownupleftdowndowndownupleftleftupupdownleftdowndowndownleftdownrightdownupdownupleftupleftdowndownleftdownleftupdowndowndownupdowndownleftupdowndownrightdownleftright","maxNodes":400113,"maxTile":192,"moves":155,"seed":112,"snaps":{"10":"0,0,0,0,0,0,0,0,0,1,1,1,2,3,4,1","30":"0,3,0,2,3,1,0,0,3,4,0,0,6,1,4,1","60":"2,3,1,0,2,2,5,0,3,6,1,1,7,2,1,0"}},{"avgNodes":288387,"dirs":"downupdownrightuprightuprightrightrightuprightdownupupuprightrightrightdownrightrightrightrightrightdownrightrightrightuprightrightrightrightrightrightrightuprightleftrightdownleftleftleftleftdownrightupupleftupdownleftdownupdownrightdowndowndowndownupleftdowndownrightupleftleftleftdowndownrightdowndownleftdownrightrightdownuprightrightuprightrightdowndownrightdowndowndownrightrightleftrightrightrightdownrightupdownuprightrightdowndownupdowndowndownrightrightuprightrightdowndownrightdowndownrightleftdownrightdownleftdownrightrightdownleftleftdowndownrightrightleftdownleftdownrightdowndowndownrightdownleftdownleftdownleftleftdownrightleftdownrightleftdowndownrightdownleftrightdownleftdownrightuprightuprightupdownupupupdownrightupuprightrightdownleftuprightdownrightrightdowndowndowndownrightdownleftleftleftleftupuprightdowndownleftuprightrightdownrightdownuprightrightrightupdown","maxNodes":400113,"maxTile":192,"moves":220,"seed":113,"snaps":{"10":"0,0,0,4,0,0,0,3,0,0,1,2,2,2,2,0","30":"0,0,1,5,0,0,1,4,1,1,1,4,1,1,3,5","60":"4,0,2,1,6,1,1,1,1,1,3,1,1,1,4,7"}},{"avgNodes":196049,"dirs":"rightleftrightleftrightleftrightupdowndownleftleftleftrightleftupdowndowndownleftleftleftdowndownuprightrightrightupupdownrightleftupdownleftupleftdowndownuprightrightdowndownuprightrightuprightupleftrightleftdownrightrightrightupup","maxNodes":400064,"maxTile":48,"moves":60,"seed":114,"snaps":{"10":"2,0,0,0,0,2,2,2,0,0,0,0,3,3,3,2","30":"2,2,2,2,2,2,2,2,1,5,4,4,2,2,5,0","60":"2,2,2,6,2,2,2,2,1,2,7,2,2,3,5,2"}},{"avgNodes":319180,"dirs":"rightleftleftrightleftdownrightrightleftrightleftdowndowndowndowndownrightrightrightdownleftdownrightdowndowndownleftrightdowndowndowndowndownrightrightuprightuprightupupdownrightupdownuprightdownupupdownrightleftdownleftdownuprightdownleftupup","maxNodes":400104,"maxTile":48,"moves":62,"seed":115,"snaps":{"10":"0,0,2,0,0,0,0,0,0,0,2,0,3,3,4,1","30":"0,1,0,0,0,2,0,4,0,3,3,2,2,4,5,5","60":"1,2,2,2,3,5,2,2,2,6,2,2,2,3,7,2"}},{"avgNodes":356780,"dirs":"leftleftuprightrightrightrightupdowndownleftleftrightleftdownleftdownleftdowndowndowndownrightleftleftleftrightrightdownleftdownrightdowndownleftleftdowndowndowndowndowndowndownleftleftleftdowndownleftupleftleftleftleftupleftrightdownleftrightupuprightupdowndowndowndownleftdownleftrightdownupleftrightrightdownrightleftleftdownleftleftdownrightrightdownrightdownleftrightleftdownrightrightdownrightrightdowndowndownrightdowndowndownupdowndownleftdowndownleftupuprightuprightupdownrightrightdownrightuprightrightuprightupupupuprightrightrightupuprightleftdownrightdownrightupuprightupdownright","maxNodes":400128,"maxTile":192,"moves":150,"seed":116,"snaps":{"10":"0,1,0,0,0,0,0,0,0,2,2,4,1,1,1,1","30":"1,0,0,0,1,0,3,0,2,2,2,1,6,4,4,1","60":"5,2,1,0,4,5,2,1,1,4,4,0,3,7,2,5"}},{"avgNodes":95779,"dirs":"downrightupdownrightupleftupleftdownupuprightupupdownupleftleftupupleftdowndownupupuprightrightuprightrightleftleftuprightupleftrightdownrightrightdownupdowndownrightuprightleftrightupleftleftupdownrightdowndowndownleftdownleftleftleftrightupleftdownrightrightrightrightleftdowndownleftupleftleftleft","maxNodes":400072,"maxTile":96,"moves":81,"seed":117,"snaps":{"10":"2,3,0,2,3,3,0,0,0,2,2,0,2,0,0,0","30":"2,2,5,5,2,2,2,3,0,2,2,3,2,2,2,2","60":"1,0,2,3,4,2,2,6,4,5,3,5,6,2,2,3"}},{"avgNodes":255086,"dirs":"downupdowndownupupdowndownupdownleftupleftleftdownrightleftleftdownupupupleftdowndownleftrightdowndownleftleftrightdowndowndownrightuprightuprightdownuprightrightdownrightdownrightupdowndowndownupupleftuprightdownleftdownrightdownuprightuprightuprightdownuprightupupuprightdownrightdownrightrightdownuprightupuprightuprightrightrightupleftleftupupuprightleftuprightupright","maxNodes":400129,"maxTile":96,"moves":102,"seed":118,"snaps":{"10":"3,1,0,0,0,1,0,0,4,1,0,0,2,1,0,0","30":"0,1,0,1,0,3,0,1,5,1,3,0,1,5,5,3","60":"3,1,1,2,3,1,6,1,5,1,1,7,4,4,2,0"}},{"avgNodes":299593,"dirs":"downdownleftleftrightrightdowndownuprightdownrightrightrightrightrightupleftdownrightdownrightrightrightrightrightrightrightdownrightdownrightrightdownrightleftdownrightdowndownrightdownupdowndownrightdownrightrightrightdownrightdowndownrightuprightupdownrightrightdownrightuprightdownrightrightrightrightdowndowndowndowndowndowndownleftleftrightdownrightupupupuprightrightupdowndownrightuprightrightuprightdowndowndownrightupdowndownupuprightupuprightuprightdownrightrightdowndownrightrightleftdownupupdownupdownuprightrightdowndownleftuprightuprightdownupupdownleftupdownrightupupleftdownleftupupuprightrightuprightupuprightleftleftuprightrightrightuprightupleftuprightupdownuprightleftrightuprightleftupupuprightrightleftleftleftuprightleftupuprightupleftuprightrightrightdownupuprightdownrightupleftupleftrightrightuprightuprightrightrightrightrightupdownrightleftupright","maxNodes":400105,"maxTile":384,"moves":226,"seed":119,"snaps":{"10":"0,0,3,0,1,0,3,0,0,1,3,3,0,0,0,2","30":"1,0,0,0,2,2,4,2,0,0,2,2,0,1,4,6","60":"2,0,0,1,2,1,3,4,0,1,5,6,0,2,3,7"}},{"avgNodes":294836,"dirs":"leftrightleftleftrightupupleftleftupupleftleftuprightleftupupleftleftupleftleftleftleftupleftleftleftdownleftleftdownleftupleftdownupdownupleftupupdownleftupupdowndownleftupupleftupupupdowndowndownleftupdownupleftleftdownleftupleftleftdowndownrightdowndowndownrightdowndownleftleftrightrightdowndownrightrightrightleftdownleftleftrightleftleftdowndownleftleftrightdowndowndowndownrightrightrightrightdownleftdownleftdownleftdowndowndownleftdowndownleftdowndownleftupdowndownleftdownleftrightdowndownleftdowndownleftdownrightupleftrightupdownupdownleftupleftupdownleftupdownleftdownupuprightuprightdowndown","maxNodes":400131,"maxTile":192,"moves":163,"seed":120,"snaps":{"10":"4,3,0,1,2,0,0,0,2,0,2,0,1,0,0,0","30":"6,2,3,0,1,4,0,0,4,2,3,0,3,1,1,2","60":"7,1,0,3,5,5,1,2,6,1,1,4,4,3,3,1"}}]"""

ENGINE_GOLDEN = json.loads(_ENGINE_GOLDEN_JSON)

if __name__ == "__main__":
    main()
