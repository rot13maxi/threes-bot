# threes-bot

A [Threes!](https://threesgame.com)-style puzzle game with a JSON API, plus a bot
that plays it using [TypeSafe System One ("Jev")](https://api.typesafe.ai) — a
decision model that returns typed choices with calibrated confidences instead of
text. Two self-contained modules:

```
game/   Node.js (zero dependencies): game engine, JSON API server, browser client
        with a live "Watch AI" spectator mode.
bot/    Python 3.9+ (stdlib only): the Jev bot (jev-only live play with
        versioned prompts), deterministic greedy policy for --policy-only
        runs, state compiler, offline test suite, benchmark/report tooling.
```

## How it fits together

```
┌──────────────┐   HTTP JSON    ┌──────────────────┐   POST /v1/systemone   ┌──────┐
│  bot module   │ ────────────▶ │   game module     │                        │ Jev  │
│ (python)      │ /api/state    │ server.js :8321   │      browser client    │ API  │
└──────┬───────┘ /api/move     └──────────────────┘  ◀── watches API moves ──┤      │
       │        ◀───────────────  /api/new /api/undo     (👀 Watch AI mode)  └──────┘
       └── closed-set decision: enumerate legal moves in code → ask Jev for
          ONE choice over compiled board facts (prompt from a versioned
          prompts/ file) → accept any legal answer, whatever the confidence;
          re-ask once if unusable, else play the first legal direction as an
          audited `lastresort` stub. Jev plays every live move.
```

Design invariants (why it's built this way):
- **Jev can never play an illegal move** — legal directions are enumerated in code
  from the server's `validMoves`; Jev only picks among verified-legal options.
- **Jev never sees the raw grid** — it can't count, so the compiler
  (`bot/compiler.py`) sends only precomputed per-direction facts.
- **Everything is seeded & deterministic** — `--seed N` replays are byte-identical,
  which is what makes the offline test suite and benchmark regressions meaningful.
- **No key material in the repo** — the Jev API key is read from the
  `TYPESAFE_API_KEY` env var, a git-ignored `bot/.env`, or
  `~/.hermes/credentials/typesafe-api-key.txt` if you use Hermes credential storage.

## Requirements

- Node.js ≥ 16 (game module) — no `npm install` needed, zero dependencies.
- Python ≥ 3.9 (bot module) — stdlib only, no `pip install` needed.
- A TypeSafe API key for live-Jev mode (`--policy-only` needs no network):
  put `TYPESAFE_API_KEY=...` in `bot/.env`, or `export TYPESAFE_API_KEY=...`.
  Live Jev mode hard-errors without a usable key — no silent policy-only downgrade.

## Run everything

**1. Start the game server** (from the repo root):

```bash
cd game
node server.js          # http://localhost:8321
# optionally pin to loopback:  HOST=127.0.0.1 node server.js
# PORT=9000 node server.js    to change the port
```

**2. Play it yourself:** open http://localhost:8321 (arrow keys / WASD, or swipe on
a phone on the same network — `PORT`/`HOST` env vars override the bind address).

**3. Run the engine's own tests** (in a second terminal, from `game/`):

```bash
node test/engine.test.js          # 30 engine tests
node examples/ai-play.js --delay 700   # example greedy bot drives the game via API
```

**4. Run the Jev bot** (from `bot/`):

```bash
python3 test_offline.py                    # offline suite (28 tests, no network)

# spectate one game:  in the browser, open http://localhost:8321 and enable
# 👀 Watch AI, then:
python3 jev_threes_bot.py --games 1 --seed 42 --delay 600

# batch + reporting:
python3 jev_threes_bot.py --games 20 --seeds 101-120 --policy-only   # deterministic, free
python3 jev_threes_bot.py --games 5 --seeds 21-25                    # live Jev (~$0.001/game)
python3 report.py runs/run-<timestamp>.jsonl    # scores, max tiles, decision-source split
```

Flags: `--policy-only` (deterministic greedy policy only, zero network), `--seed N` /
`--seeds A-B`, `--delay MS` (per-move sleep for spectating), `--win-tile N`
(default 96 = "win"), `--max-moves N`, `--instructions PATH` (Jev prompt file,
default `bot/prompts/iter-00-base.txt`; prompts are versioned per bench iteration).

The bot drives the game purely through the HTTP API (`/api/new` with its own seed,
`/api/state`, `/api/move`), so the browser's Watch-AI view follows its moves live.

## Module map

| Path | What |
|---|---|
| `game/server.js` | HTTP server: `/api/state`, `/api/move`, `/api/new`, `/api/undo`, `/api/health` |
| `game/lib/engine.js` | Pure game logic (board, merges, scoring) |
| `game/public/` | Mobile-friendly browser client (Watch-AI spectator mode) |
| `game/examples/ai-play.js` | Reference external-AI loop over the same API |
| `game/test/engine.test.js` | Engine test suite |
| `bot/jev_threes_bot.py` | Bot loop: legal-move enumeration, jev-only decisions, lastresort stub, run log |
| `bot/compiler.py` | Board → Jev-facing computed facts (never the raw grid) |
| `bot/policy.py` | Deterministic greedy policy (`--policy-only` mode only) |
| `bot/prompts/` | Versioned Jev prompt instructions (baseline: `iter-00-base.txt`) |
| `bot/bench.py` | Parallel benchmark: seed set × prompt file → per-tag summary |
| `bot/jev_client.py` | Minimal stdlib client for `POST /v1/systemone` (+429 backoff) |
| `bot/report.py` | Summarize a run `.jsonl` (scores, decisions, costs) |
| `bot/test_offline.py` | Offline suite: compiler/policy/jev-only-live/determinism, no network |

## API notes

- `POST /api/new {"seed": 42}` — seeded games make runs reproducible; omit `seed` for random.
- `POST /api/move {"dir": "up|down|left|right"}` — illegal directions return `{ok:false,reason}`.
- The browser client polls state; any external process moving via the API is visible live in Watch-AI mode.
- Bot run logs record every turn: `validMoves`, `source` (`jev`/`lastresort`/`fallback`), `retries`, and — when Jev was unusable twice — `jevChoice`/`jevConfidence`/`jevOutcome`.
