#!/usr/bin/env python3
"""Parallel benchmark runner for the Jev Threes bot.

Plays a batch of seeded games across N worker threads. Each worker spawns
its own `node server.js` (ports 8400+), plays its assigned seeds against
it, then shuts the server down. Results are appended as JSON lines to
bot/runs/bench-<tag>-<ts>.jsonl plus a .summary.json beside it.

Usage:
  python3 bench.py --instructions prompts/iter-00-base.txt \
      --seeds 101-120 --workers 8 --tag iter-00 [--policy-only] \
      [--win-tile 96] [--max-moves 3000] [--games N] [--delay 0]

Always exits 0 — this is a benchmark, not a gate.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from jev_threes_bot import play_game, load_instructions, DEFAULT_INSTRUCTIONS_PATH  # noqa: E402

RUNS_DIR = os.path.join(HERE, "runs")
GAME_DIR = os.path.normpath(os.path.join(HERE, "..", "game"))
BASE_PORT = 8400
SOURCE_KEYS = ("jev", "lastresort", "fallback")


def parse_seeds(spec):
    """'101-120' -> [101..120]; '5,7,9' -> [5,7,9]; single '42' -> [42]."""
    seeds = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            seeds.extend(range(int(a), int(b) + 1))
        else:
            seeds.append(int(part))
    return seeds


class Worker:
    """Owns one game-server subprocess and plays a chunk of seeds on it."""

    def __init__(self, index, args, instructions):
        self.index = index
        self.args = args
        self.instructions = instructions
        self.port = BASE_PORT + index
        self.base = "http://127.0.0.1:%d" % self.port
        self.proc = None
        self.client = None
        self.records = []

    def start_server(self):
        env = dict(os.environ)
        env["PORT"] = str(self.port)
        env["HOST"] = "127.0.0.1"
        self.proc = subprocess.Popen(
            ["node", "server.js"], cwd=GAME_DIR, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited early on port %d" % self.port)
            try:
                with urllib.request.urlopen(self.base + "/api/health", timeout=2) as resp:
                    if resp.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.25)
        raise RuntimeError("server did not become healthy on port %d" % self.port)

    def stop_server(self):
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.proc = None

    def run(self, seeds):
        """Play assigned seeds sequentially; returns list of run records."""
        try:
            self.start_server()
            if not self.args.policy_only:
                from jev_client import JevClient  # lazy: policy-only needs no key
                self.client = JevClient()
            for seed in seeds:
                self.records.append(self.play_seed(seed))
        except Exception as e:  # server failed to start: record all seeds as aborted
            err = repr(e)
            for seed in seeds:
                if not any(r.get("seed") == seed for r in self.records):
                    self.records.append(self.error_record(seed, err))
        finally:
            self.stop_server()
        return self.records

    def error_record(self, seed, err):
        rec = {"seed": seed, "error": repr(err), "aborted": "exception",
               "max_tile": 0, "score": 0, "game_over": False, "decisions": []}
        rec["worker"] = self.index
        rec["tag"] = self.args.tag
        return rec

    def play_seed(self, seed):
        try:
            rec = play_game(seed, policy_only=self.args.policy_only,
                            client=self.client, delay_ms=self.args.delay,
                            max_moves=self.args.max_moves, base=self.base,
                            instructions=self.instructions,
                            planner_depth=self.args.planner_depth)
        except Exception as e:
            return self.error_record(seed, e)
        rec["worker"] = self.index
        rec["tag"] = self.args.tag
        return rec


def chunk_round_robin(seeds, nworkers):
    """Round-robin seeds across worker buckets."""
    buckets = [[] for _ in range(nworkers)]
    for i, seed in enumerate(seeds):
        buckets[i % nworkers].append(seed)
    return buckets


def shard_worker(shard_index, nshards, workers_per, args_dict, instructions,
                queue):
    """Runs in its OWN process (the expectimax planner is CPU-bound and the
    GIL serializes threads otherwise). Plays its seed slice sequentially on
    workers_per local servers; returns (records, costs, plan_drops) via the
    queue so CPU-bound planner work escapes the GIL."""
    import argparse
    args = argparse.Namespace(**args_dict)
    my_seeds = [s for i, s in enumerate(args.seeds_list) if i % nshards == shard_index]
    base_index = shard_index * workers_per
    workers = [Worker(base_index + i, args, instructions)
               for i in range(workers_per)]
    buckets = chunk_round_robin(my_seeds, workers_per)
    import threading as _th
    threads = [_th.Thread(target=w.run, args=(bucket,))
               for w, bucket in zip(workers, buckets) if bucket]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    records = [r for w in workers for r in w.records]
    costs = [w.client.cost_usd() for w in workers if w.client is not None]
    import compiler as _comp
    queue.put((records, costs, getattr(_comp, "PLANNER_DROP_COUNT", 0)))


def summarize(records, args, seeds, instructions_path, costs):
    games = len(records)
    wins = sum(1 for r in records if (r.get("max_tile") or 0) >= args.win_tile)
    scores = [r.get("score") or 0 for r in records]
    tiles = [r.get("max_tile") or 0 for r in records]
    sources = dict((k, 0) for k in SOURCE_KEYS)
    for r in records:
        for d in r.get("decisions") or []:
            src = d.get("source")
            if src in sources:
                sources[src] += 1
    aborted = dict((r["seed"], r.get("aborted")) for r in records if r.get("aborted"))
    return {
        "tag": args.tag,
        "instructions": instructions_path,
        "policy_only": bool(args.policy_only),
        "seeds": seeds,
        "games": games,
        "wins": wins,
        "losses": games - wins,
        "win_rate": (wins / games) if games else 0.0,
        "best_score": max(scores) if scores else 0,
        "mean_score": (sum(scores) / games) if games else 0.0,
        "best_max_tile": max(tiles) if tiles else 0,
        "sources": sources,
        "jev_cost_usd": sum(costs),
        "aborted": aborted,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Parallel benchmark for the Jev Threes bot")
    ap.add_argument("--instructions", default=DEFAULT_INSTRUCTIONS_PATH,
                    help="prompt instructions file (default: %(default)s)")
    ap.add_argument("--seeds", default=None, help="seed spec: '101-120' or '5,7,9'")
    ap.add_argument("--seed", type=int, default=None, help="single seed (alternative to --seeds)")
    ap.add_argument("--games", type=int, default=None,
                    help="play this many games starting at --seed (default single seed)")
    ap.add_argument("--workers", type=int, default=8, help="worker threads/servers (default 8)")
    ap.add_argument("--tag", default="bench", help="run tag for output filenames/summary")
    ap.add_argument("--win-tile", type=int, default=96, help="max_tile counting as a win (default 96)")
    ap.add_argument("--max-moves", type=int, default=3000, help="per-game move cap (default 3000)")
    ap.add_argument("--policy-only", action="store_true",
                    help="greedy fallback policy only; no Jev calls, no key needed")
    ap.add_argument("--delay", type=int, default=0, metavar="MS",
                    help="ms delay between moves (spectating sanity; default 0)")
    ap.add_argument("--planner-depth", type=int, default=5,
                    help="expectimax planner depth per move (0 disables; "
                         "default 5 = the winning benchmark recipe)")
    ap.add_argument("--procs", type=int, default=1,
                    help="shard the seed set across this many PROCESSES "
                         "(recommended with the CPU-bound planner; default "
                         "1 = threads only)")
    args = ap.parse_args(argv)

    if args.seeds:
        seeds = parse_seeds(args.seeds)
    elif args.seed is not None:
        start = args.seed
        seeds = list(range(start, start + args.games)) if args.games else [start]
    else:
        ap.error("need --seeds or --seed")
    if args.games:
        seeds = seeds[:args.games]

    workers_n = max(1, min(args.workers, len(seeds)))
    instructions = load_instructions(args.instructions)

    ts = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(RUNS_DIR, exist_ok=True)
    jsonl_path = os.path.join(RUNS_DIR, "bench-%s-%s.jsonl" % (args.tag, ts))

    if args.procs > 1:
        import multiprocessing as mp
        nprocs = max(1, min(args.procs, len(seeds)))
        workers_per = max(1, workers_n // nprocs)
        args_dict = dict(vars(args))
        args_dict["seeds_list"] = seeds
        ctx = mp.get_context("spawn")
        queue = ctx.Queue()
        procs = [ctx.Process(target=shard_worker,
                             args=(i, nprocs, workers_per, args_dict,
                                   instructions, queue))
                 for i in range(nprocs)]
        for p in procs:
            p.start()
        collected = [queue.get() for _ in procs]
        for p in procs:
            p.join()
        records = [r for recs, _c, _d in collected for r in recs]
        costs = [c for _r, cs, _d in collected for c in cs]
        plan_drops = sum(d for _r, _c, d in collected)
    else:
        workers = [Worker(i, args, instructions) for i in range(workers_n)]
        buckets = chunk_round_robin(seeds, workers_n)
        threads = []
        for w, bucket in zip(workers, buckets):
            t = threading.Thread(target=w.run, args=(bucket,), name="bench-w%d" % w.index)
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        records = [r for w in workers for r in w.records]
        costs = [w.client.cost_usd() for w in workers if w.client is not None]
        plan_drops = 0
    records.sort(key=lambda r: r.get("seed", 0))

    with open(jsonl_path, "a") as f:
        for rec in records:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    summary = summarize(records, args, seeds, args.instructions, costs)
    summary_path = jsonl_path[:-len(".jsonl")] + ".summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, sort_keys=True)
        f.write("\n")

    print("tag=%s games=%d wins=%d win_rate=%.1f%% (win-tile=%d)"
          % (args.tag, summary["games"], summary["wins"],
             100.0 * summary["win_rate"], args.win_tile))
    print("best_score=%d mean_score=%.1f best_max_tile=%d"
          % (summary["best_score"], summary["mean_score"], summary["best_max_tile"]))
    print("sources: " + " ".join("%s=%d" % (k, summary["sources"][k]) for k in SOURCE_KEYS))
    print("jev_cost_usd=%.6f plan_drops=%d" % (summary["jev_cost_usd"], plan_drops))
    if summary["aborted"]:
        print("aborted=%d %s" % (len(summary["aborted"]),
                                 json.dumps(summary["aborted"], sort_keys=True)))
    for rec in records:
        won = (rec.get("max_tile") or 0) >= args.win_tile
        print("seed=%s max_tile=%s score=%s moves=%s %s"
              % (rec.get("seed"), rec.get("max_tile"), rec.get("score"),
                 rec.get("moves"), "win" if won else "LOSS"))
    print("wrote %s" % jsonl_path)
    print("wrote %s" % summary_path)
    return 0


if __name__ == "__main__":
    main()
