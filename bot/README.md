# Jev Threes Bot

Unattended bot that plays complete games of the Threes clone in `../game/`
by asking the TypeSafe Jev (System One) decision model which swipe to make
each turn. Live mode is jev-only: Jev plays every move — no confidence gate,
no greedy fallback. A legal-direction `lastresort` stub fires only when Jev
is unusable twice in a row (audited in the run record). Plan:
`~/plans/jev-threes-bot.md`.

## Prerequisites

1. Start the game server (the bot does NOT start it):
   `cd ../game && node server.js`   →  API on http://localhost:8321
2. Check: `curl http://localhost:8321/api/health` → `{"ok":true,...}`
3. Jev API key: put `TYPESAFE_API_KEY=...` in `bot/.env` (git-ignored,
   chmod 600), or export the env var, or use
   `~/.hermes/credentials/typesafe-api-key.txt`. Required for live games —
   live mode with no usable key exits 1 with an error (no silent
   policy-only downgrade); only `--policy-only` needs no key.

## Running

All commands from this directory. Python 3.9+ stdlib only.

```
# deterministic offline batch (no Jev, no network to api.typesafe.ai)
python3 jev_threes_bot.py --policy-only --games 20 --seeds 101-120

# one seeded policy-only game
python3 jev_threes_bot.py --policy-only --seed 42

# live Jev game (default --games 3)
python3 jev_threes_bot.py --games 1 --seed 42

# live game with a versioned prompt (bench iterations add prompts/iter-NN-*.txt)
python3 jev_threes_bot.py --games 5 --seeds 21-25 --instructions prompts/iter-01-aggressive.txt

# parallel benchmark across seeds (own servers/ports; see bench.py --help)
python3 bench.py --instructions prompts/iter-00-base.txt --tag iter-00 --seeds 101-120

# watch-AI mode: open http://localhost:8321, click "👀 Watch AI", then
python3 jev_threes_bot.py --policy-only --delay 700 --seed 5
```

Flags: `--policy-only` `--seed N` `--games N` `--seeds A-B` `--delay MS`
`--win-tile T` (default 96) `--max-moves` (default 3000)
`--instructions PATH` (Jev prompt file; default `prompts/iter-00-base.txt`).

Each run writes `runs/run-<ts>[-policy].jsonl` — one line per game:
`{seed, score, max_tile, moves, game_over, aborted, decisions:
[{turn, dir, source: jev|lastresort|fallback, confidence, validMoves, retries}]}`.
`retries` = extra Jev attempts used that turn (0 when the first answer was
accepted). `fallback` appears ONLY in `--policy-only` runs; live turns are
`jev` (legal Jev choice, whatever the confidence) or `lastresort` (Jev
unusable twice in a row → first legal direction in up,left,down,right order).
`validMoves` is logged per decision so every posted direction can be
audited against legality (AC 8).

Lastresort telemetry (additive): a stub turn also carries `jevChoice` (the
last rejected pick), `jevConfidence` (null on transport error) and
`jevOutcome` in `illegal|transport_error`. `jev` and `--policy-only` turns
leave these keys absent.

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
python3 test_offline.py     # 28 cases; spawns its own server on :8399
```

## Design notes

- `compiler.py` precomputes every fact (Jev cannot count); the raw grid is
  never sent to Jev.
- Live mode = Jev plays every move: any choice in `validMoves` is accepted
  regardless of the reported confidence value/type. An unusable answer
  (illegal, missing, or the call raised) triggers ONE re-ask; when that is
  also unusable the first legal direction in up,left,down,right order is
  played as an audited `lastresort` stub (`jevOutcome` records why Jev was
  rejected). Live mode NEVER calls the greedy policy.
- Prompts are versioned per benchmark iteration: `--instructions PATH`
  selects the file (default `bot/prompts/iter-00-base.txt`) whose plain-text
  block is sent to Jev as the choice instructions. `bot/bench.py` replays
  seed sets in parallel against a chosen prompt file
  (`python3 bench.py --instructions prompts/FILE --tag NAME`).
- `--policy-only` (unchanged): `policy.py` deterministic greedy policy only,
  zero Jev calls — `100*merges + 40*max_tile_merge + 15*empty_after
  − 10*edge_worst_penalty`, tie-break up, left, down, right.
- Seeded games replay identically (server-side PRNG).
