#!/usr/bin/env node
'use strict';
/*
 * Example AI driver for the 3's API — plays a full game through HTTP.
 * Swap `chooseMove()` for your own model (LLM, expectimax, whatever).
 *
 *   node examples/ai-play.js [--url http://localhost:8321] [--seed 42] [--games 1] [--delay 350]
 *
 * Tip: open the browser page, hit "👀 Watch AI", then run this — you'll see
 * the AI's moves animate live.
 */

const args = process.argv.slice(2);
const getArg = (name, dflt) => {
  const i = args.indexOf('--' + name);
  return i >= 0 && args[i + 1] ? args[i + 1] : dflt;
};
const BASE = getArg('url', 'http://localhost:8321');
const SEED = getArg('seed', undefined);
const GAMES = Number(getArg('games', 1));
const DELAY = Number(getArg('delay', 350)); // ms between moves, so humans can watch

async function api(path, body) {
  const res = await fetch(BASE + path, body ? {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  } : undefined);
  return res.json();
}

/* ---------- the pluggable policy ---------- */
function chooseMove(state) {
  // Replace this function with your AI. Inputs: full state (grid, score,
  // validMoves, queue). Output: one of state.validMoves.
  //
  // Baseline heuristic below: 1-ply greedy on merge value, penalize
  // occupied corners with no escape, prefer keeping big tiles adjacent.
  let best = null, bestScore = -Infinity;
  for (const dir of state.validMoves) {
    const sim = simulate(state, dir);
    if (!sim) continue;
    const s = evaluate(sim.grid) + Math.random() * 1e-6;
    if (s > bestScore) { bestScore = s; best = dir; }
  }
  return best;
}

/* cheap client-side sim mirror (server validates authoritatively anyway) */
function simulate(state, dir) {
  const g = state.grid.map(r => r.slice());
  const steps = laneSteps(g, dir);
  if (!steps.length) return null;
  for (const s of steps) {
    g[s.from[0]][s.from[1]] = 0;
    g[s.to[0]][s.to[1]] = s.value;
  }
  return { grid: g, steps };
}
function laneSteps(g, dir) {
  const N = 4, steps = [];
  const merge = (a, b) => {
    if (!a || !b) return null;
    if ((a === 1 && b === 2) || (a === 2 && b === 1)) return 3;
    if (a === b && a >= 3 && ((a / 3) & ((a / 3) - 1)) === 0 && (a / 3) > 0) return 2 * a;
    return null;
  };
  for (let k = 0; k < N; k++) {
    let cells;
    if (dir === 'left') cells = [...Array(N).keys()].map(c => [k, c]);
    else if (dir === 'right') cells = [...Array(N).keys()].map(c => [k, N - 1 - c]);
    else if (dir === 'up') cells = [...Array(N).keys()].map(r => [r, k]);
    else cells = [...Array(N).keys()].map(r => [N - 1 - r, k]);
    const moved = new Array(N).fill(false);
    for (let i = 1; i < N; i++) {
      const f = g[cells[i][0]][cells[i][1]];
      if (!f) continue;
      const t = g[cells[i - 1][0]][cells[i - 1][1]];
      if (moved[i - 1]) { steps.push({ from: cells[i], to: cells[i - 1], merge: false, value: f }); moved[i] = true; }
      else if (t === 0) { steps.push({ from: cells[i], to: cells[i - 1], merge: false, value: f }); moved[i] = true; }
      else { const m = merge(t, f); if (m !== null) { steps.push({ from: cells[i], to: cells[i - 1], merge: true, value: m }); moved[i] = true; } }
    }
  }
  return steps;
}
function evaluate(grid) {
  let score = 0;
  for (let r = 0; r < 4; r++) for (let c = 0; c < 4; c++) {
    const v = grid[r][c];
    if (!v) continue;
    score += Math.log2(v + 1) * 10;
    const isCorner = (r === 0 || r === 3) && (c === 0 || c === 3);
    if (isCorner && v >= 3) {
      const nb = [];
      if (r > 0) nb.push(grid[r - 1][c]); if (r < 3) nb.push(grid[r + 1][c]);
      if (c > 0) nb.push(grid[r][c - 1]); if (c < 3) nb.push(grid[r][c + 1]);
      if (nb.every(x => x !== 0)) score -= 25;
    }
    if (r < 3 && grid[r + 1][c] === v && v >= 3) score += 8;   // big tiles adjacent
    if (c < 3 && grid[r][c + 1] === v && v >= 3) score += 8;
    const opp = v === 1 ? 2 : v === 2 ? 1 : v;                  // mergeable neighbor
    if (r < 3 && grid[r + 1][c] === opp) score += 4;
    if (c < 3 && grid[r][c + 1] === opp) score += 4;
  }
  return score;
}

/* ---------- main loop ---------- */
(async () => {
  const results = [];
  for (let game = 1; game <= GAMES; game++) {
    const body = SEED !== undefined ? { seed: Number(SEED) + game - 1 } : {};
    let s = await api('/api/new', body);
    console.log(`game ${game}: seed=${s.seed}`);
    while (!s.gameOver) {
      const dir = chooseMove(s);
      if (!dir) break;
      if (DELAY) await new Promise(r => setTimeout(r, DELAY));
      const next = await api('/api/move', { dir });
      if (!next.ok) { console.error('rejected', dir, next.reason); break; }
      s = next;
      const line = s.grid.map(r => r.map(v => String(v).padStart(4)).join('')).join('\n');
      console.log(`\n[${dir}]  score ${s.score}  moves ${s.moves}\n` + line + '\n');
    }
    console.log(`game ${game} OVER — score ${s.score}, best tile ${s.maxTile}, ${s.moves} moves\n`);
    results.push({ seed: s.seed, score: s.score, maxTile: s.maxTile, moves: s.moves });
  }
  if (GAMES > 1) {
    const scores = results.map(r => r.score).sort((a, b) => a - b);
    console.log(`${GAMES} games — avg ${(scores.reduce((a, b) => a + b, 0) / GAMES).toFixed(1)}, median ${scores[scores.length >> 1]}, max ${scores[scores.length - 1]}`);
  }
})();
