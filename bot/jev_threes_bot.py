#!/usr/bin/env python3
"""Jev Threes bot — driver (plan D1, D7, D8, D11).

Plays complete games of the Threes clone served by `node server.js` on
localhost:8321 (start it yourself: `cd ../game && node server.js`).

Live mode is jev-only: each turn the compiled state facts are put to the
TypeSafe Jev (System One) decision model as ONE closed-set choice question
over the server's validMoves, and the answer is accepted whenever it is a
legal direction — no confidence gate, no greedy fallback. If Jev's answer is
unusable (illegal, missing, or the call raised) it is re-asked once; if the
re-ask is also unusable, the first legal direction in up,left,down,right
order is played as an audited `lastresort` stub (run-record source
"lastresort"). The prompt instructions come from a versioned file
(--instructions PATH; the default bot/prompts/iter-09-rank1.txt pairs with
the expectimax plan facts — it instructs Jev to play "plan rank 1 of N",
the winning benchmark configuration, 13/20 games reaching a 96 tile).
`--policy-only` skips Jev entirely (zero network to api.typesafe.ai) and
plays the deterministic greedy policy from policy.py instead.

Examples:
  python3 jev_threes_bot.py --policy-only --games 20 --seeds 101-120
  python3 jev_threes_bot.py --policy-only --seed 42
  python3 jev_threes_bot.py --games 1 --seed 42
  python3 jev_threes_bot.py --games 5 --planner-depth 3   # faster, lower win rate
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

import compiler
from compiler import compile_state, criterion_line
from policy import choose_fallback

BASE = "http://localhost:8321"
WIN_TILE_DEFAULT = 96           # D9
MAX_FAILS = 50                  # D7 spin guard
LASTRESORT_PRIORITY = ("up", "left", "down", "right")
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INSTRUCTIONS_PATH = os.path.join(HERE, "prompts", "iter-09-rank1.txt")


def load_instructions(path=None):
    """Read the Jev prompt instructions from a versioned prompt file
    (bot/prompts/iter-XX-*.txt; one plain-text block per bench iteration).
    Raises RuntimeError with a clear message when the file is missing."""
    p = DEFAULT_INSTRUCTIONS_PATH if path is None else path
    try:
        with open(p, encoding="utf-8") as f:
            text = f.read().strip()
    except OSError as e:
        raise RuntimeError("instructions file not readable: %s (%s)" % (p, e))
    if not text:
        raise RuntimeError("instructions file is empty: %s" % p)
    return text


def lastresort_dir(valid):
    """First direction in up,left,down,right priority order that the server
    lists as legal. A legal-move stub, not a strategy: only played when Jev
    was unusable twice in a row (live mode never consults the greedy policy)."""
    for d in LASTRESORT_PRIORITY:
        if d in valid:
            return d
    return None


def api(path, body=None, base=BASE, timeout=10):
    req = urllib.request.Request(
        base + path,
        data=(json.dumps(body).encode() if body is not None else None),
        method=("POST" if body is not None else "GET"),
        headers={"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def jev_choose(state_facts, dirs, client, instructions):
    """One closed-set choice question. Returns (choice, confidence) or
    (None, None) on transport error (never trusts model output beyond id)."""
    criteria = {}
    for d in dirs:
        f = state_facts["options"].get(d)
        criteria[d] = criterion_line(d, f) if f else (d + ": no facts")
    qs = {"decision": {"type": "choice",
                       "instructions": instructions,
                       "criteria": criteria}}
    # state sent to Jev is ONLY compiled facts — never the raw grid (D5)
    try:
        resp = client.system_one(state_facts, qs)
    except Exception:
        return None, None
    a = (resp or {}).get("answers", {}).get("decision", {}) or {}
    return a.get("choice"), a.get("confidence")


def play_game(seed, policy_only=False, client=None, delay_ms=0,
              max_moves=3000, base=BASE, jev_fn=None, instructions=None,
              planner_depth=None):
    """Play one seeded game. Returns the run-record dict (D7).

    jev_fn: optional (facts, dirs) -> (choice, confidence) override used by
    the offline tests to inject a fake Jev responder.
    instructions: Jev prompt text; None loads load_instructions(None)
    (DEFAULT_INSTRUCTIONS_PATH). Live mode is jev-only — no confidence gate,
    no greedy fallback: any legal answer is accepted, an unusable answer is
    re-asked once, then the first legal direction (up,left,down,right) is
    played as an audited `lastresort` stub.
    planner_depth: expectimax depth for the compiled plan facts; None keeps
    the compiler's current setting (--planner-depth; 0 disables them).
    """
    _saved = compiler.get_planner_depth()
    if policy_only:
        # greedy fallback ignores plan facts; keep --policy-only fast/offline
        compiler.set_planner_depth(0)
    elif planner_depth is not None:
        compiler.set_planner_depth(planner_depth)
    if instructions is None:
        instructions = load_instructions(None)
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
        src = None
        conf = None
        chosen = None
        retries = 0
        jev_choice = None
        jev_conf = None
        jev_outcome = None
        if policy_only:
            # --policy-only: pure greedy fallback, zero Jev calls. Live mode
            # NEVER reaches choose_fallback (jev-only, no fallback policy).
            chosen = choose_fallback(valid, facts["options"])
            src = "fallback"
        else:
            if jev_fn is not None:
                ask = jev_fn
            else:
                def ask(f, d):
                    return jev_choose(f, d, client, instructions)
            transport_attempts = 0
            for attempt in (0, 1):        # first answer + ONE re-ask
                try:
                    choice, confidence = ask(facts, valid)
                    raised = False
                except Exception:
                    choice, confidence, raised = None, None, True
                retries = attempt
                jev_choice, jev_conf = choice, confidence
                if choice is not None and choice in valid:
                    # accept regardless of the confidence value/type
                    chosen, conf, src = choice, confidence, "jev"
                    jev_outcome = None
                    break
                jev_outcome = "illegal"   # None choice or not in validMoves
                if raised or (choice is None and confidence is None):
                    transport_attempts += 1
            if chosen is None:            # both attempts unusable
                if transport_attempts == 2:
                    jev_outcome = "transport_error"
                chosen = lastresort_dir(valid)
                src = "lastresort"
        decision = {"turn": turn, "dir": chosen, "source": src,
                    "confidence": conf, "validMoves": valid,
                    "retries": retries}
        if jev_outcome is not None:       # lastresort telemetry (auditable)
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
    if policy_only:
        compiler.set_planner_depth(_saved)
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
                    help="deterministic greedy policy only; zero Jev/network calls")
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
    ap.add_argument("--instructions", default=DEFAULT_INSTRUCTIONS_PATH,
                    help="Jev prompt instructions file (default: %(default)s)")
    ap.add_argument("--planner-depth", type=int, default=5,
                    help="expectimax depth of the plan facts compiled into "
                         "every turn's options; the winning benchmark config "
                         "is 5 (~2 s/move). 3 = fast, 0 = no planner facts "
                         "(pair 0 with a non-rank prompt via --instructions)")
    args = ap.parse_args(argv)
    compiler.set_planner_depth(args.planner_depth)

    seeds = parse_seeds(args)
    if args.seeds:
        seeds = seeds[:args.games]
    else:
        seeds = [int(args.seed)] * args.games

    try:
        instructions = load_instructions(args.instructions)
    except RuntimeError as e:
        sys.stderr.write("error: %s\n" % e)
        return 1
    client = None
    if not args.policy_only:
        from jev_client import JevClient
        try:
            client = JevClient()
        except Exception as e:
            # live mode is jev-only: NO silent policy-only downgrade
            sys.stderr.write("error: live Jev mode needs a usable "
                             "TYPESAFE_API_KEY (JevClient: %s) — rerun with "
                             "--policy-only for the deterministic run\n" % e)
            return 1

    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs"),
                exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    run_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "runs", "run-%s%s.jsonl" % (ts, "-policy" if args.policy_only else ""))

    records = []
    for i, seed in enumerate(seeds):
        rec = play_game(seed, policy_only=args.policy_only, client=client,
                        delay_ms=args.delay, max_moves=args.max_moves,
                        base=args.url, instructions=instructions,
                        planner_depth=args.planner_depth)
        rec["game_num"] = i + 1
        rec["win"] = rec["max_tile"] >= args.win_tile
        records.append(rec)
        with open(run_path, "a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")

    wins = sum(1 for r in records if r["win"])
    losses = len(records) - wins
    best = max(records, key=lambda r: r["score"])
    jev_n = sum(1 for r in records for d in r["decisions"] if d["source"] == "jev")
    lr_n = sum(1 for r in records for d in r["decisions"] if d["source"] == "lastresort")
    fb_n = sum(1 for r in records for d in r["decisions"] if d["source"] == "fallback")
    print("games=%d wins=%d losses=%d (win-tile=%d)" % (len(records), wins, losses,
                                                         args.win_tile))
    print("best score=%d (seed %s, max_tile %s)" % (best["score"], best["seed"],
                                                    best["max_tile"]))
    print("decisions: jev=%d lastresort=%d fallback=%d" % (jev_n, lr_n, fb_n))
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
