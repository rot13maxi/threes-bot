# 3's — a Threes! clone with an AI-playable API

Zero-dependency Node server: pure-logic game engine, JSON API, and a
mobile-friendly browser client that can **watch an external AI play live**.

## Run

```bash
node server.js            # http://localhost:8321  (PORT/HOST env override)
node test/engine.test.js  # 30 engine tests
node examples/ai-play.js --delay 700   # example AI drives the game via the API
```

Open http://localhost:8321 on desktop (arrows/WASD) or phone (swipe).

## API

| Endpoint | Method | Body | Returns |
|---|---|---|---|
| `/api/state` | GET | — | full game state |
| `/api/move`  | POST | `{"dir":"up\|down\|left\|right"}` | new state, or `{ok:false,reason}` |
| `/api/new`   | POST | `{"seed":42}` (optional) | state of a fresh game |
| `/api/undo`  | POST | — | state before last move |
| `/api/health`| GET | — | `{ok:true}` |

State shape:

```json
{
  "ok": true,
  "seed": 42,
  "grid": [[0,0,0,1],[2,2,3,3],[0,3,3,1],[0,3,3,1]],
  "score": 18, "maxTile": 3, "moves": 15, "version": 15,
  "gameOver": false,
  "validMoves": ["up","down","left","right"],
  "queue": [1,2,2],
  "lastMove": { "direction":"right", "merges":[{"from":[1,2],"to":[1,3],"result":6}],
                "spawn":{"value":2,"row":3,"col":1}, "scoreDelta":6 },
  "canUndo": true
}
```

Illegal moves return `{"ok":false,"reason":"invalid_move|game_over|bad_direction"}`
with HTTP 400 — the board never mutates on a rejected move.

### Hooking up your AI

- **Deterministic replay:** pass `{"seed":N}` to `/api/new`. Same seed ⇒ same
  spawn sequence, so runs are reproducible. Every game is appended to
  `games/games.jsonl` for training data.
- **Policy slot:** edit `chooseMove(state)` in `examples/ai-play.js` — it gets
  the full state and must return one of `state.validMoves`. Point it at your
  model server; keep `--delay 700` so humans can follow.
- **Watching:** press **👀 Watch AI** in the browser, then let the AI drive —
  the page polls `/api/state` (250 ms) and animates every move. **🤖 Demo bot**
  plays the same API path so you can preview without an AI.

## Rules implemented

- 4×4 grid; a swipe slides tiles **one space** toward the pressed edge.
- Valid move = at least one tile advances (slides into empty or merges) and the
  spawn tile fits on the opposite edge (evaluated after the slide).
- Merges: `1+2→3`, equal threes-tiles double: `3+3→6, 6+6→12, 12+12→24…`
- Spawns on the edge opposite the swipe direction, from a 3-deep preview queue;
  higher tiles (6, 12) start appearing as the board grows.
- Score = sum of every merge result. Game over when no direction is valid.

## Layout

```
server.js            HTTP API + static host, game event log
lib/engine.js        pure game logic (importable: require('./lib/engine'))
public/index.html    renderer (diff-animate tiles), swipe/keys, watch mode
examples/ai-play.js  AI driver skeleton (replace chooseMove with your model)
test/engine.test.js  unit tests: node test/engine.test.js
games/games.jsonl    move log per game (new/move/undo/gameover events)
```
