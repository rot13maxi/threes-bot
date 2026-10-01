#!/usr/bin/env python3
"""Jev Threes bot — driver (plan D1, D7, D8, D11).

Plays complete games of the Threes clone served by `node server.js` on
localhost:8321 (start it yourself: `cd ../game && node server.js`).

Each turn the compiled state facts are put to the TypeSafe Jev (System One)
decision model as ONE closed-set choice question over the server's
validMoves; the answer is accepted only if choice is legal AND
confidence >= 0.55, otherwise the deterministic fallback policy (policy.py)
moves. `--policy-only` skips Jev entirely (zero network to api.typesafe.ai).

Examples:
  python3 jev_threes_bot.py --policy-only --games 20 --seeds 101-120
  python3 jev_threes_bot.py --policy-only --seed 42
  python3 jev_threes_bot.py --games 1 --seed 42
  python3 jev_threes_bot.py --policy-only --delay 700 --seed 5   # watch-AI mode

Python 3.9 stdlib only.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from compiler import compile_state, criterion_line
from policy import choose_fallback

BASE = "http://localhost:8321"
CONF_THRESHOLD = 0.55           # D4 gate
WIN_TILE_DEFAULT = 96           # D9
MAX_FAILS = 50                  # D7 spin guard

PLAY_INSTRUCTIONS = (
    "You are choosing the next swipe (up, down, left, right) in a Threes-like "
    "4x4 sliding-tile game. Decision rule, applied in order: "
    "(a) do not move your largest tile off its cell if any other swipe is "
    "available (use the 'max tile displaced' fact); "
    "(b) among the remaining options, remove the most 1s and 2s from the "
    "board (use the 'worst tiles removed' and 'worst tile has partner' "
    "facts); "
    "(c) then pick the option with the most merges. If none of these "
    "distinguish the options, pick the one leaving the most empty cells. "
    "All numbers in the options are precomputed facts about what each swipe "
    "does; pick the single best option."
)


def api(path, body=None, base=BASE, timeout=10):
    req = urllib.request.Request(
        base + path,
        data=(json.dumps(body).encode() if body is not None else None),
        method=("POST" if body is not None else "GET"),
        headers={"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def jev_choose(state_facts, dirs, client):
    """One closed-set choice question. Returns (choice, confidence) or
    (None, None) on transport error (never trusts model output beyond id)."""
    criteria = {}
    for d in dirs:
        f = state_facts["options"].get(d)
        criteria[d] = criterion_line(d, f) if f else (d + ": no facts")
    qs = {"decision": {"type": "choice",
                       "instructions": PLAY_INSTRUCTIONS,
                       "criteria": criteria}}
    # state sent to Jev is ONLY compiled facts — never the raw grid (D5)
    try:
        resp = client.system_one(state_facts, qs)
    except Exception:
        return None, None
    a = (resp or {}).get("answers", {}).get("decision", {}) or {}
    return a.get("choice"), a.get("confidence")


def play_game(seed, policy_only=False, client=None, delay_ms=0,
              max_moves=3000, base=BASE, jev_fn=None):
    """Play one seeded game. Returns the run-record dict (D7).

    jev_fn: optional (facts, dirs) -> (choice, confidence) override used by
    the offline tests to inject a fake Jev responder.
    """
    state = api("/api/new", {"seed": seed} if seed is not None else {}, base=base)
    decisions = []
    fails = 0
    turn = 0
    aborted = None
    while True:
        if not state.get("ok", True) and "grid" not in state:
            aborted = "bad_state"
            break
        if state.get("gameOver"):
            break
        if turn >= max_moves:
            aborted = "max_moves"
            break
        valid = list(state.get("validMoves") or [])
        if not valid:
            break
        facts = compile_state(state)
        src = "fallback"
        conf = None
        chosen = None
        jev_choice = None
        jev_conf = None
        jev_outcome = None
        if not policy_only:
            try:
                if jev_fn is not None:
                    choice, confidence = jev_fn(facts, valid)
                else:
                    choice, confidence = jev_choose(facts, valid, client)
            except Exception:
                # transport failure / bad responder => fallback (D4)
                choice, confidence = None, None
            if (choice is not None and choice in valid
                    and isinstance(confidence, (int, float))
                    and confidence >= CONF_THRESHOLD):
                chosen, conf, src = choice, confidence, "jev"
            else:
                # C1 gate-out telemetry: record the rejected pick. Transport
                # errors surface as (None, None) from jev_choose; a model
                # answer of null/null is indistinguishable from the wire and
                # is classed as transport_error per C1 ("null on transport").
                jev_choice = choice
                jev_conf = confidence
                if choice is None and confidence is None:
                    jev_outcome = "transport_error"
                elif choice is None or choice not in valid:
                    jev_outcome = "illegal"
                else:
                    # legal choice, confidence below gate (or non-numeric)
                    jev_outcome = "gated_out"
        if chosen is None:
            chosen = choose_fallback(valid, facts["options"])
        decision = {"turn": turn, "dir": chosen, "source": src,
                    "confidence": conf, "validMoves": valid}
        if jev_outcome is not None:
            decision["jevChoice"] = jev_choice
            decision["jevConfidence"] = jev_conf
            decision["jevOutcome"] = jev_outcome
        decisions.append(decision)
        turn += 1
        if delay_ms:
            time.sleep(delay_ms / 1000.0)
        try:
            new_state = api("/api/move", {"dir": chosen}, base=base)
        except urllib.error.HTTPError as e:
            if e.code == 400:
                # server rejected the move — should never happen for
                # validMoves members; count as a failed move and re-read
                fails += 1
                state = api("/api/state", base=base)
                if fails >= MAX_FAILS:
                    aborted = "stuck"
                    break
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            aborted = "server_unreachable"
            break
        if new_state.get("ok") is False:
            fails += 1
            state = api("/api/state", base=base)
            if fails >= MAX_FAILS:
                aborted = "stuck"
                break
            continue
        # guard: state unchanged (version/moves didn't advance) counts as fail
        if (new_state.get("version") == state.get("version")
                and new_state.get("moves") == state.get("moves")):
            fails += 1
            if fails >= MAX_FAILS:
                aborted = "stuck"
                break
        else:
            fails = 0
        state = new_state
    return {
        "seed": state.get("seed", seed),
        "score": state.get("score", 0),
        "max_tile": state.get("maxTile", state.get("max_tile", 0)),
        "moves": state.get("moves", turn),
        "game_over": bool(state.get("gameOver")),
        "aborted": aborted,
        "decisions": decisions,
    }


def parse_seeds(args):
    if args.seeds:
        a, _, b = args.seeds.partition("-")
        return list(range(int(a), int(b) + 1))
    return [int(args.seed)]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Jev bot for the Threes clone")
    ap.add_argument("--policy-only", action="store_true",
                    help="deterministic fallback only; zero Jev/network calls")
    ap.add_argument("--seed", default="42", help="play this seed (default 42)")
    ap.add_argument("--games", type=int, default=3,
                    help="number of games (default 3, live smoke budget D11)")
    ap.add_argument("--seeds", help="range A-B of seeds, e.g. 101-120 "
                                    "(overrides --seed; --games caps the list)")
    ap.add_argument("--delay", type=int, default=0,
                    help="ms sleep between moves (watch-AI mode)")
    ap.add_argument("--win-tile", type=int, default=WIN_TILE_DEFAULT,
                    help="max_tile >= this counts as a win (default 96)")
    ap.add_argument("--max-moves", type=int, default=3000)
    ap.add_argument("--url", default=BASE, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    seeds = parse_seeds(args)
    if args.seeds:
        seeds = seeds[:args.games]
    else:
        seeds = [int(args.seed)] * args.games

    client = None
    if not args.policy_only:
        from jev_client import JevClient
        try:
            client = JevClient()
        except Exception as e:
            sys.stderr.write("Jev unavailable (%s) — falling back to policy-only "
                             "for this batch\n" % e)
            args.policy_only = True

    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs"),
                exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    run_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "runs", "run-%s%s.jsonl" % (ts, "-policy" if args.policy_only else ""))

    records = []
    for i, seed in enumerate(seeds):
        rec = play_game(seed, policy_only=args.policy_only, client=client,
                        delay_ms=args.delay, max_moves=args.max_moves,
                        base=args.url)
        rec["game_num"] = i + 1
        rec["win"] = rec["max_tile"] >= args.win_tile
        records.append(rec)
        with open(run_path, "a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")

    wins = sum(1 for r in records if r["win"])
    losses = len(records) - wins
    best = max(records, key=lambda r: r["score"])
    jev_n = sum(1 for r in records for d in r["decisions"] if d["source"] == "jev")
    fb_n = sum(1 for r in records for d in r["decisions"] if d["source"] == "fallback")
    print("games=%d wins=%d losses=%d (win-tile=%d)" % (len(records), wins, losses,
                                                         args.win_tile))
    print("best score=%d (seed %s, max_tile %s)" % (best["score"], best["seed"],
                                                    best["max_tile"]))
    print("decisions: jev=%d fallback=%d" % (jev_n, fb_n))
    if client is not None:
        s = client.summary()
        print("jev calls=%d input_tokens=%d cost_usd=%s avg_latency_s=%s"
              % (s["calls"], s["input_tokens"], s["cost_usd"], s["avg_latency_s"]))
    for r in records:
        if r.get("aborted"):
            print("WARNING seed %s aborted: %s" % (r["seed"], r["aborted"]))
    print("run file: %s" % run_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
