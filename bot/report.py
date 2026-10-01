#!/usr/bin/env python3
"""Summarize run jsonl file(s) produced by jev_threes_bot.py (plan D9).

Usage: python3 report.py runs/run-....jsonl [more.jsonl ...] [--win-tile 96]
Prints games, wins, losses, best score, best max_tile, mean score, and
jev/fallback decision counts.

Python 3.9 stdlib only.
"""
import argparse
import json
import sys


def load_games(paths):
    games = []
    for p in paths:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    games.append(json.loads(line))
    return games


def summarize(games, win_tile=96):
    n = len(games)
    wins = sum(1 for g in games if g.get("max_tile", 0) >= win_tile)
    losses = n - wins
    best_score = max((g.get("score", 0) for g in games), default=0)
    best_tile = max((g.get("max_tile", 0) for g in games), default=0)
    mean_score = (sum(g.get("score", 0) for g in games) / n) if n else 0.0
    jev = sum(1 for g in games for d in g.get("decisions", [])
              if d.get("source") == "jev")
    fb = sum(1 for g in games for d in g.get("decisions", [])
             if d.get("source") == "fallback")
    returns = sum(1 for g in games if not g.get("game_over"))
    return {
        "games": n, "wins": wins, "losses": losses,
        "best_score": best_score, "best_max_tile": best_tile,
        "mean_score": round(mean_score, 1),
        "jev_decisions": jev, "fallback_decisions": fb,
        "not_game_over": returns,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Report for jev-threes-bot runs")
    ap.add_argument("runs", nargs="+", help="run jsonl file(s)")
    ap.add_argument("--win-tile", type=int, default=96)
    args = ap.parse_args(argv)

    games = load_games(args.runs)
    s = summarize(games, args.win_tile)
    print("games=%d" % s["games"])
    print("wins=%d losses=%d (win-tile=%d)" % (s["wins"], s["losses"], args.win_tile))
    print("best_score=%d best_max_tile=%d mean_score=%s"
          % (s["best_score"], s["best_max_tile"], s["mean_score"]))
    print("jev_decisions=%d fallback_decisions=%d"
          % (s["jev_decisions"], s["fallback_decisions"]))
    if s["not_game_over"]:
        print("WARNING games not game_over: %d" % s["not_game_over"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
