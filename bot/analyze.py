#!/usr/bin/env python3
"""Offline diagnostic replay for Jev Threes bot runs (permanent tool).

Replays recorded games from bench/bot run files against a LIVE game server
(default http://localhost:8321): for every logged decision we recompile the
facts Jev saw, compute the greedy fallback pick (policy.choose_fallback),
compare it with the direction Jev actually played, then POST the recorded
move so the replay tracks the original game.

Usage:
    python3 analyze.py runs/bench-iter-01-*.jsonl [--base URL] [--top 5]

Stdlib + project modules only. Deterministic given the same run files and a
server that honours seeds.
"""
import argparse
import glob
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from compiler import compile_state, criterion_line  # noqa: E402
from policy import choose_fallback  # noqa: E402

DEFAULT_BASE = "http://localhost:8321"


def api(path, body=None, base=DEFAULT_BASE, timeout=15):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def compress_turn_diffs(rows):
    """rows: list of (dir_jev, dir_greedy, agree). Returns RLE tuples
    (dir_jev, dir_greedy, agree, run_length) for compact printing."""
    out = []
    for (dj, dg, agree) in rows:
        if out and out[-1][0] == dj and out[-1][1] == dg and out[-1][2] == agree:
            out[-1] = (dj, dg, agree, out[-1][3] + 1)
        else:
            out.append((dj, dg, agree, 1))
    return out


def replay_game(rec, base):
    """Replay one recorded game on the live server.

    Returns a dict: {seed, replayed, aborted, divergence, turns: [...]}
    Each turn: {turn, dir_jev, source, confidence, greedy, agree,
                max_tile, empty_count, lines: [(dir, criterion_line)]}
    """
    seed = rec.get("seed")
    out = {"seed": seed, "replayed": 0, "aborted": None, "divergence": None,
           "turns": []}
    try:
        state = api("/api/new", {"seed": seed}, base=base)
    except (urllib.error.URLError, OSError, ValueError) as e:
        out["aborted"] = "new_failed: %s" % e
        return out
    if not state.get("ok", True) or "grid" not in state:
        out["aborted"] = "bad_new_state"
        return out

    decisions = rec.get("decisions") or []
    prev_moves = state.get("moves")
    for d in decisions:
        turn = d.get("turn")
        dir_jev = d.get("dir")
        valid = list(state.get("validMoves") or [])
        if turn != out["replayed"]:
            out["divergence"] = ("turn_index %s != replayed %d"
                                 % (turn, out["replayed"]))
            break
        if dir_jev not in valid:
            # recorded move disagrees with the server: server is
            # authoritative, stop this game.
            out["divergence"] = ("turn %d: recorded dir %r not in server "
                                 "validMoves %r" % (turn, dir_jev, valid))
            break
        # BEFORE posting the move: capture state, compile facts, greedy pick.
        try:
            facts = compile_state(state)
        except Exception as e:  # mirror disagreement must not crash replay
            out["divergence"] = "turn %d: compile_state failed: %r" % (turn, e)
            break
        greedy = None
        try:
            greedy = choose_fallback(valid, facts["options"])
        except ValueError:
            greedy = None
        lines = [(dd, criterion_line(dd, facts["options"][dd]))
                 for dd in valid if dd in facts["options"]]
        out["turns"].append({
            "turn": turn,
            "dir_jev": dir_jev,
            "source": d.get("source"),
            "confidence": d.get("confidence"),
            "greedy": greedy,
            "agree": greedy is not None and dir_jev == greedy,
            "max_tile": facts["board"]["max_tile"],
            "empty_count": facts["board"]["empty_count"],
            "lines": lines,
        })
        out["replayed"] += 1
        try:
            new_state = api("/api/move", {"dir": dir_jev}, base=base)
        except (urllib.error.URLError, OSError, ValueError) as e:
            out["aborted"] = "move_failed turn %d: %s" % (turn, e)
            return out
        # tracking guard: server must advance version/moves by one
        if (new_state.get("version") == state.get("version")
                and new_state.get("moves") == state.get("moves")):
            out["divergence"] = ("turn %d: server did not advance "
                                 "(version/moves unchanged)" % turn)
            return out
        if new_state.get("moves") != (prev_moves or 0) + 1:
            out["divergence"] = ("turn %d: moves jumped %s -> %s"
                                 % (turn, prev_moves, new_state.get("moves")))
            return out
        prev_moves = new_state.get("moves")
        state = new_state
    return out


def fmt_turn_diffs(rows):
    """rows: list of (dir_jev, dir_greedy, agree) -> compressed string.
    'up/left= x3' = Jev up, greedy left, agreed 3 times in a row (! = not)."""
    return " ".join("%s/%s%s x%d" % (dj, dg, "=" if agree else "!", n)
                    for (dj, dg, agree, n) in compress_turn_diffs(rows))


def print_examples(top, examples):
    """Print the --top N most instructive deviations (latest turns first)."""
    if not examples:
        print("\n=== no deviations found ===")
        return
    ranked = sorted(examples, key=lambda t: (-t["turn"], t["seed"]))
    print("\n=== top %d instructive deviations ===" % min(top, len(ranked)))
    for (i, ex) in enumerate(ranked[:top], 1):
        print("\n[%d] seed %s turn %d  (max tile %s, %d empties)"
              % (i, ex["seed"], ex["turn"], ex["max_tile"], ex["empty_count"]))
        print("    Jev: %s  (source=%s, confidence=%s)"
              % (ex["dir_jev"], ex["source"], ex["confidence"]))
        print("    greedy pick: %s" % ex["greedy"])
        for (dd, line) in ex["lines"]:
            if dd == ex["dir_jev"]:
                mark = ">>"
            elif dd == ex["greedy"]:
                mark = "> "
            else:
                mark = "  "
            print("      %s %s" % (mark, line))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Replay recorded Jev games against a live server and "
                    "report where greedy and Jev disagree.")
    ap.add_argument("runs", nargs="+",
                    help="run .jsonl file(s) (glob patterns allowed)")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args(argv)

    paths = []
    for pat in args.runs:
        hits = sorted(glob.glob(pat)) if any(c in pat for c in "*?[") else [pat]
        paths.extend(hits)

    all_turns = []          # every replayed (game, turn) record
    late_turns = []         # last-10-turns slice per game
    losing_games = []       # games whose final recorded max_tile < 96
    tile_dist = Counter()   # recorded max_tile -> game count (all run files)
    moves_list = []         # recorded move counts (all run files)
    n_games = n_replayed = n_failed = 0
    examples = []

    for path in paths:
        try:
            with open(path) as fh:
                recs = [json.loads(l) for l in fh if l.strip()]
        except (OSError, ValueError) as e:
            print("SKIP %s: %s" % (path, e))
            continue
        print("\n### run file: %s (%d games)"
              % (os.path.basename(path), len(recs)))
        for rec in recs:
            n_games += 1
            res = replay_game(rec, args.base)
            if res["divergence"] or res["aborted"]:
                n_failed += 1
                why = res["divergence"] or res["aborted"]
                print("  seed %s: REPLAY FAILED after %d turns -- %s"
                      % (res["seed"], res["replayed"], why))
            else:
                n_replayed += 1
            moves_list.append(rec.get("moves", res["replayed"]))
            tile_dist[rec.get("max_tile", 0)] += 1
            if not res["turns"]:
                continue
            late = res["turns"][-10:]
            all_turns.extend(res["turns"])
            late_turns.extend(late)
            mt = rec.get("max_tile", 0)
            if mt < 96:
                losing_games.append({
                    "seed": res["seed"], "max_tile": mt,
                    "last10": fmt_turn_diffs([(t["dir_jev"], t["greedy"],
                                               t["agree"]) for t in late]),
                })
            # most instructive = deviations in the closing stages
            for t in late:
                if not t["agree"]:
                    ex = {"seed": res["seed"]}
                    ex.update(t)
                    examples.append(ex)

    print("\n=== per-run aggregate ===")
    print("run files: %d   games: %d   replayed clean: %d   failed/divergent: %d"
          % (len(paths), n_games, n_replayed, n_failed))
    if moves_list:
        print("mean moves: %.1f (over %d games)"
              % (sum(moves_list) / len(moves_list), len(moves_list)))
    if tile_dist:
        print("tile distribution (max_tile): %s" % ", ".join(
            "%s:%d" % (v, tile_dist[v]) for v in sorted(tile_dist)))

    print("\n=== agreement Jev vs greedy ===")
    if all_turns:
        agree = sum(t["agree"] for t in all_turns)
        print("overall: %.1f%% (%d/%d turns)"
              % (100 * agree / len(all_turns), agree, len(all_turns)))
    if late_turns:
        la = sum(t["agree"] for t in late_turns)
        print("last 10 turns per game: %.1f%% (%d/%d turns)"
              % (100 * la / len(late_turns), la, len(late_turns)))

    print("\n=== losing games (max_tile < 96): greedy-vs-jev last-10 diffs ===")
    print("format: JEV/greedy =agree !disagree x<run length>")
    if losing_games:
        for g in losing_games:
            print("  seed %s (max %s): %s" % (g["seed"], g["max_tile"],
                                              g["last10"]))
    else:
        print("  none")

    print_examples(args.top, examples)
    return 0


if __name__ == "__main__":
    sys.exit(main())
