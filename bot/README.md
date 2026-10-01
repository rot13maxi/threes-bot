# Jev Threes Bot

Unattended bot that plays complete games of the Threes clone in `~/threes/`
by asking the TypeSafe Jev (System One) decision model which swipe to make
each turn, gated by a closed-set harness, with a deterministic fallback
policy. Plan: `~/plans/jev-threes-bot.md`.

## Prerequisites

1. Start the game server (the bot does NOT start it):
   `cd ~/threes && node server.js`   →  API on http://localhost:8321
2. Check: `curl http://localhost:8321/api/health` → `{"ok":true,...}`
3. Jev API key at `~/.hermes/credentials/typesafe-api-key.txt`
   (or env `TYPESAFE_API_KEY`). Only needed for live games, not
   `--policy-only`.

## Running

All commands from this directory. Python 3.9+ stdlib only.

```
# deterministic offline batch (no Jev, no network to api.typesafe.ai)
python3 jev_threes_bot.py --policy-only --games 20 --seeds 101-120

# one seeded policy-only game
python3 jev_threes_bot.py --policy-only --seed 42

# live Jev game (default --games 3)
python3 jev_threes_bot.py --games 1 --seed 42

# watch-AI mode: open http://localhost:8321, click "👀 Watch AI", then
python3 jev_threes_bot.py --policy-only --delay 700 --seed 5
```

Flags: `--policy-only` `--seed N` `--games N` `--seeds A-B` `--delay MS`
`--win-tile T` (default 96) `--max-moves` (default 3000).

Each run writes `runs/run-<ts>[-policy].jsonl` — one line per game:
`{seed, score, max_tile, moves, game_over, aborted, decisions:
[{turn, dir, source: jev|fallback, confidence, validMoves}]}`.
`validMoves` is logged per decision so every posted direction can be
audited against legality (AC 8).

C1 gate-out telemetry (additive): a fallback turn where Jev was consulted
also carries `jevChoice` (rejected pick), `jevConfidence` (null on transport
error) and `jevOutcome` in `gated_out|illegal|transport_error`. Pure
fallback / `--policy-only` turns leave these keys absent.

C2 compiled facts (additive, per legal direction in `options`):
`worst_tiles_removed`, `worst_tile_companion`, `max_tile_displaced`,
`chain_pairs_after`, all mirrored into `policy.fallback_score`.

## Report

```
python3 report.py runs/run-....jsonl
```
Prints games, wins/losses (win := max_tile ≥ --win-tile, default 96),
best score, best max_tile, mean score, and jev/fallback decision counts.

## Tests

```
python3 test_offline.py     # 18 cases; spawns its own server on :8399
```

## Design notes

- `compiler.py` precomputes every fact (Jev cannot count); the raw grid is
  never sent to Jev.
- `policy.py` fallback: `100*merges + 40*max_tile_merge + 15*empty_after
  − 10*edge_worst_penalty`, tie-break up, left, down, right. Deterministic.
- Gate: accept Jev's choice only if it is in `validMoves` and
  confidence ≥ 0.55; anything else (malformed, illegal, low-confidence,
  transport error) routes to the fallback.
- Seeded games replay identically (server-side PRNG).
