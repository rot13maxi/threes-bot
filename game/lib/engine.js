'use strict';
/*
 * Threes-clone engine — pure logic, zero deps.
 *
 * Rules:
 *  - 4x4 grid. Tiles slide ONE space toward the pressed direction.
 *  - A direction is valid iff, in some lane, a tile can advance into the
 *    neighbor toward the pressed edge (empty slide, or a merge) AND at least
 *    one cell on the opposite edge is empty (the new tile must fit).
 *  - Merges: (1,2) -> 3, and (v, 2v) -> 3v for v in {3,6,12,...} (3*2^k).
 *  - After a valid move, a new tile spawns in a random empty cell of the
 *    edge opposite the direction (3-tile lookahead queue, like the original).
 *  - Score = sum of every merged tile's result.
 *  - Game over when no direction is valid.
 */

const SIZE = 4;
const MOVES = ['up', 'down', 'left', 'right'];

// deterministic PRNG so AI harnesses can replay games by seed
function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// resulting tile of merging a,b, or null.
// Real Threes rules: 1+2 -> 3, and equal "threes" tiles double: 3+3 -> 6,
// 6+6 -> 12, 12+12 -> 24, ... (tiles are 3 * 2^k).
function mergeResult(a, b) {
  if (a === 0 || b === 0) return null;
  if ((a === 1 && b === 2) || (a === 2 && b === 1)) return 3;
  if (a === b && a >= 3 && isThreeTile(a)) return 2 * a;
  return null;
}

function isThreeTile(v) {
  // v must be 3 * 2^k
  if (v < 3 || v % 3 !== 0) return false;
  v /= 3;
  return (v & (v - 1)) === 0;
}

class Threes {
  constructor(opts = {}) {
    const seed = opts.seed ?? ((Math.random() * 0xffffffff) >>> 0);
    this.seed = seed >>> 0;
    this.rand = mulberry32(this.seed);
    this.score = 0;
    this.moves = 0;
    this.version = 0;
    this.lastMove = null;
    this.gameOver = false;
    this.history = [];
    this.queue = [];
    this.grid = Array.from({ length: SIZE }, () => Array(SIZE).fill(0));
    this._spawnQueue();
    this._placeInitial();
  }

  _spawnQueue() {
    while (this.queue.length < 3) {
      this.queue.push(this._weightedSpawn());
    }
  }

  _weightedSpawn() {
    const max = this._maxTile();
    const r = this.rand();
    if (max >= 96 && r < 0.01) return 12;
    if (max >= 24 && r < 0.05) return 6;
    if (max >= 6 && r < 0.12) return 3;
    return r < 0.5 ? 1 : 2;
  }

  _placeInitial() {
    // two random tiles, retry with fresh randomness until the board isn't
    // already game-over (astronomically unlikely to loop, but deterministic-safe)
    for (let attempt = 0; attempt < 50; attempt++) {
      this.grid = Array.from({ length: SIZE }, () => Array(SIZE).fill(0));
      this.score = 0;
      this.queue = [];
      this._spawnQueue();
      const cells = [];
      for (let r = 0; r < SIZE; r++) for (let c = 0; c < SIZE; c++) cells.push([r, c]);
      for (let i = cells.length - 1; i > 0; i--) {
        const j = Math.floor(this.rand() * (i + 1));
        [cells[i], cells[j]] = [cells[j], cells[i]];
      }
      const [[r0, c0], [r1, c1]] = cells;
      this.grid[r0][c0] = this.queue[0];
      this.grid[r1][c1] = this.queue[1];
      this.queue = this.queue.slice(2);
      this._spawnQueue();
      if (this._anyValid()) return;
    }
  }

  _laneSteps(dir) {
    // cells ordered from the destination edge inward; at each i we test
    // whether the tile at cells[i+1] can advance into cells[i].
    const steps = [];
    const movedLanes = new Set();
    for (let k = 0; k < SIZE; k++) {
      let cells;
      if (dir === 'left') cells = Array.from({ length: SIZE }, (_, c) => [k, c]);
      else if (dir === 'right') cells = Array.from({ length: SIZE }, (_, c) => [k, SIZE - 1 - c]);
      else if (dir === 'up') cells = Array.from({ length: SIZE }, (_, r) => [r, k]);
      else cells = Array.from({ length: SIZE }, (_, r) => [SIZE - 1 - r, k]);

      // Walk inward from the destination edge. A tile at lane index i advances
      // into i-1 iff the tile that was at i-1 moved this turn (block chain),
      // or cell i-1 is empty (slide), or the pair is mergeable (merge).
      // Each tile moves at most one space.
      const moved = new Array(SIZE).fill(false);
      let laneMoved = false;
      for (let i = 1; i < SIZE; i++) {
        const [fr, fc] = cells[i];
        const f = this.grid[fr][fc];
        if (f === 0) continue;
        const [tr, tc] = cells[i - 1];
        const t = this.grid[tr][tc];
        if (moved[i - 1]) {
          steps.push({ from: [fr, fc], to: [tr, tc], merge: false, value: f });
          moved[i] = true; laneMoved = true;
        } else if (t === 0) {
          steps.push({ from: [fr, fc], to: [tr, tc], merge: false, value: f });
          moved[i] = true; laneMoved = true;
        } else {
          const m = mergeResult(t, f);
          if (m !== null) {
            steps.push({ from: [fr, fc], to: [tr, tc], merge: true, value: m });
            moved[i] = true; laneMoved = true;
          }
        }
      }
      if (laneMoved) movedLanes.add(k);
    }
    return { steps, movedLanes };
  }

  _spawnCellsOn(grid, dir) {
    // empty cells on the edge opposite the pressed direction, in `grid`
    const cells = [];
    for (let i = 0; i < SIZE; i++) {
      let r, c;
      if (dir === 'up') { r = SIZE - 1; c = i; }
      else if (dir === 'down') { r = 0; c = i; }
      else if (dir === 'left') { r = i; c = SIZE - 1; }
      else { r = i; c = 0; }
      if (grid[r][c] === 0) cells.push([r, c]);
    }
    return cells;
  }

  /** Trial-apply: steps + spawn availability, evaluated on the post-slide grid.
   *  Spawn must land on the trailing edge WITHIN A MOVED LANE (real Threes
   *  rule: the new tile enters a lane that actually moved), falling back to
   *  any empty trailing-edge cell if no moved lane left room there. */
  _simulate(dir) {
    const laneInfo = this._laneSteps(dir);
    const steps = laneInfo.steps;
    if (steps.length === 0) return null;
    const next = this.grid.map((r) => r.slice());
    for (const s of steps) {
      const [fr, fc] = s.from, [tr, tc] = s.to;
      next[fr][fc] = 0;
      next[tr][tc] = s.value;
    }
    // the new tile must fit on the trailing edge of one of the lanes that
    // actually moved — this gates move validity too (a slide that leaves no
    // room for its own spawn is not a legal move in real Threes)
    const holes = this._spawnCellsOn(next, dir).filter(([r, c]) =>
      laneInfo.movedLanes.has((dir === 'up' || dir === 'down') ? c : r));
    if (holes.length === 0) return null;
    return { steps, holes, next };
  }

  _valid(dir) {
    return this._simulate(dir) !== null;
  }

  _anyValid() {
    return MOVES.some((d) => this._valid(d));
  }

  /* ---- public API ---- */

  _maxTile() {
    let m = 0;
    for (const row of this.grid) for (const v of row) if (v > m) m = v;
    return m;
  }

  maxTile() { return this._maxTile(); }

  validMoves() {
    return this.gameOver ? [] : MOVES.filter((d) => this._valid(d));
  }

  /**
   * Apply a move. Returns {ok:false, reason} if illegal; otherwise move detail
   * incl. merges (for animation) and spawn (value + landing cell).
   */
  applyMove(dir) {
    if (this.gameOver) return { ok: false, reason: 'game_over' };
    if (!MOVES.includes(dir)) return { ok: false, reason: 'bad_direction' };
    const sim = this._simulate(dir);
    if (!sim) return { ok: false, reason: 'invalid_move' };
    const { steps, holes } = sim;

    this._pushHistory();
    const scoreBefore = this.score;
    const merges = steps.filter((s) => s.merge).map((s) => ({
      from: s.from, to: s.to, result: s.value,
    }));

    for (const s of steps) {
      const [fr, fc] = s.from, [tr, tc] = s.to;
      this.grid[fr][fc] = 0;
      this.grid[tr][tc] = s.value;
      if (s.merge) this.score += s.value;
    }

    this._spawnQueue();
    const tile = this.queue.shift();
    this._spawnQueue();
    const [sr, sc] = holes[Math.floor(this.rand() * holes.length)];
    this.grid[sr][sc] = tile;
    const spawn = { value: tile, row: sr, col: sc };

    this.moves += 1;
    this.version += 1;
    this.lastMove = {
      direction: dir, merges, spawn,
      steps: steps.map((s) => ({ from: s.from, to: s.to, merge: s.merge })),
      scoreDelta: this.score - scoreBefore, at: Date.now(),
    };
    if (!this._anyValid()) this.gameOver = true;

    return {
      ok: true, direction: dir, merges, spawn,
      score: this.score, scoreDelta: this.score - scoreBefore,
      moves: this.moves, gameOver: this.gameOver,
      next: this.queue.slice(),
    };
  }

  _pushHistory() {
    this.history.push({
      grid: this.grid.map((r) => r.slice()),
      score: this.score,
      queue: this.queue.slice(),
      lastMove: this.lastMove,
      gameOver: this.gameOver,
      moves: this.moves,
    });
    if (this.history.length > 200) this.history.shift();
  }

  undo() {
    const snap = this.history.pop();
    if (!snap) return { ok: false, reason: 'no_history' };
    this.grid = snap.grid;
    this.score = snap.score;
    this.queue = snap.queue;
    this.lastMove = snap.lastMove;
    this.gameOver = snap.gameOver;
    this.moves = snap.moves;
    this.version += 1;
    return { ok: true };
  }

  /** Serializable state for the API + renderer. */
  state() {
    return {
      ok: true,
      seed: this.seed,
      grid: this.grid.map((r) => r.slice()),
      score: this.score,
      maxTile: this._maxTile(),
      moves: this.moves,
      version: this.version,
      gameOver: this.gameOver,
      validMoves: this.validMoves(),
      queue: this.queue.slice(),
      lastMove: this.lastMove,
      canUndo: this.history.length > 0,
    };
  }
}

module.exports = { Threes, MOVES, mergeResult, SIZE };
