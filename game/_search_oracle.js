'use strict';
/* Throwaway experiment: expectimax + MC-hybrid policies for the Threes clone,
 * evaluated over eval seeds 101-120. Fully deterministic: no Math.random;
 * all sampling goes through seeded LCGs. The engine (lib/engine.js) is used
 * only as the game referee; search runs on a fast mirrored slide on code grids.
 *
 * Usage (from game/):
 *   node _search_oracle.js check               — verify slide mirror vs engine
 *   node _search_oracle.js list                — list configs
 *   node _search_oracle.js run <a,b|all|TOP>   — run configs (SEEDS=101-103 override)
 *   node _search_oracle.js final               — top configs: per-seed + union tables
 *   node _search_oracle.js signals [name]      — decision-signal diff vs greedy
 */
const { Threes } = require('./lib/engine');

/* ---------------- tile codes ---------------- */
const VALS = [0, 1, 2, 3, 6, 12, 24, 48, 96, 192, 384, 768, 1536, 3072];
const CODE_OF = {};
VALS.forEach((v, i) => { CODE_OF[v] = i; });
const MERGE = new Uint8Array(256);
for (let a = 1; a < 16; a++) {
  for (let b = 1; b < 16; b++) {
    if ((a === 1 && b === 2) || (a === 2 && b === 1)) MERGE[(a << 4) | b] = 3;
    else if (a === b && a >= 3) MERGE[(a << 4) | b] = a + 1;
  }
}

/* ---------------- geometry ---------------- */
const NAMES = ['up', 'down', 'left', 'right'];  // dir index order
const GREEDY_PRI = [0, 2, 1, 3];                // policy.py priority up,left,down,right
const CELLS = [[], [], [], []];                 // CELLS[d][lane] = 4 grid idx, dest-edge first
for (let k = 0; k < 4; k++) {
  CELLS[0].push([k, k + 4, k + 8, k + 12]);     // up   (lane = col)
  CELLS[1].push([k + 12, k + 8, k + 4, k]);     // down (lane = col)
  CELLS[2].push([4 * k, 4 * k + 1, 4 * k + 2, 4 * k + 3]);         // left (lane = row)
  CELLS[3].push([4 * k + 3, 4 * k + 2, 4 * k + 1, 4 * k]);         // right(lane = row)
}
const EDGE = [[], [], [], []];   // spawn edge (opposite) cells per dir
const EDGE_LANE = [[], [], [], []]; // lane id of each spawn-edge cell
for (let j = 0; j < 4; j++) {
  EDGE[0].push(12 + j); EDGE_LANE[0].push(j);        // up   -> bottom row
  EDGE[1].push(j);      EDGE_LANE[1].push(j);        // down -> top row
  EDGE[2].push(4 * j + 3); EDGE_LANE[2].push(j);     // left -> right col
  EDGE[3].push(4 * j);    EDGE_LANE[3].push(j);      // right-> left col
}
const ADJ = [];
for (let r = 0; r < 4; r++) for (let c = 0; c < 4; c++) {
  const a = [];
  if (r > 0) a.push((r - 1) * 4 + c);
  if (r < 3) a.push((r + 1) * 4 + c);
  if (c > 0) a.push(r * 4 + c - 1);
  if (c < 3) a.push(r * 4 + c + 1);
  ADJ.push(a);
}
const IS_CORNER = [1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 1];
const EDGE_NONCORNER = [0, 1, 1, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, 1, 1, 0];

/* 8 snake paths: serpentine rows + serpentine cols from each of the 4 corners */
const SNAKES = [
  [0, 1, 2, 3, 7, 6, 5, 4, 8, 9, 10, 11, 15, 14, 13, 12],
  [3, 2, 1, 0, 4, 5, 6, 7, 11, 10, 9, 8, 12, 13, 14, 15],
  [12, 13, 14, 15, 11, 10, 9, 8, 4, 5, 6, 7, 0, 1, 2, 3],
  [15, 14, 13, 12, 8, 9, 10, 11, 7, 6, 5, 4, 0, 1, 2, 3],
  [0, 4, 8, 12, 13, 9, 5, 1, 2, 6, 10, 14, 15, 11, 7, 3],
  [3, 7, 11, 15, 14, 10, 6, 2, 1, 5, 9, 13, 12, 8, 4, 0],
  [12, 8, 4, 0, 1, 5, 9, 13, 14, 10, 6, 2, 3, 7, 11, 15],
  [15, 11, 7, 3, 2, 6, 10, 14, 13, 9, 5, 1, 0, 4, 8, 12],
];

/* ---------------- slide mirror (must match engine._simulate) ---------------- */
function slide(g, d) {
  const next = g.slice();
  let any = false, laneMask = 0;
  const m = [0, 0, 0, 0];
  const steps = [];
  for (let k = 0; k < 4; k++) {
    const cs = CELLS[d][k];
    m[0] = m[1] = m[2] = m[3] = 0;
    let lm = false;
    for (let i = 1; i < 4; i++) {
      const fi = cs[i], f = g[fi];
      if (!f) continue;
      const ti = cs[i - 1];
      if (m[i - 1]) {
        next[fi] = 0; next[ti] = f; m[i] = 1; lm = true; any = true;
        steps.push([fi, ti, 0, f]);
      } else {
        const t = g[ti];
        if (t === 0) {
          next[fi] = 0; next[ti] = f; m[i] = 1; lm = true; any = true;
          steps.push([fi, ti, 0, f]);
        } else {
          const mg = MERGE[(t << 4) | f];
          if (mg) {
            next[fi] = 0; next[ti] = mg; m[i] = 1; lm = true; any = true;
            steps.push([fi, ti, 1, mg]);
          }
        }
      }
    }
    if (lm) laneMask |= 1 << k;
  }
  if (!any) return null;
  const holes = [];
  for (let j = 0; j < 4; j++) {
    const cell = EDGE[d][j];
    if (next[cell] === 0 && (laneMask & (1 << EDGE_LANE[d][j]))) holes.push(cell);
  }
  if (!holes.length) return null;
  return { next, holes, steps };
}
function maxCodeOf(g) { let mt = 0; for (let i = 0; i < 16; i++) if (g[i] > mt) mt = g[i]; return mt; }
function codesOf(grid) {
  const g = new Uint8Array(16);
  for (let r = 0; r < 4; r++) for (let c = 0; c < 4; c++) g[r * 4 + c] = CODE_OF[grid[r][c]];
  return g;
}
function validDirs(g) {
  const out = [];
  for (let d = 0; d < 4; d++) if (slide(g, d)) out.push(d);
  return out;
}

/* spawn distribution (engine _weightedSpawn) keyed by current max CODE */
const DIST = [];
function buildDist() {
  for (let mc = 0; mc < 16; mc++) {
    const v = VALS[mc] || 0;
    const e = [];
    let lo = 0;
    if (v >= 96) { e.push([CODE_OF[12], 0.01]); lo = 0.01; }
    if (v >= 24) { e.push([CODE_OF[6], 0.05 - lo]); lo = 0.05; }
    if (v >= 6) { e.push([CODE_OF[3], 0.12 - lo]); lo = 0.12; }
    if (0.5 - lo > 0) e.push([1, 0.5 - lo]);
    e.push([2, 0.5]);
    DIST[mc] = e;
  }
}
buildDist();
function lcg(seed) {
  let s = seed >>> 0;
  return function () { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 4294967296; };
}

/* ---------------- evaluation ---------------- */
/* parts: 0 snake,1 mono,2 empty,3 cramp,4 chain,5 jr,6 isoJr,7 isoBig,
 *        8 corner,9 sub,10 edgeBig,11 jrCorner */
const PNAMES = ['snake', 'mono', 'empty', 'cramp', 'chain', 'jr', 'isoJr',
  'isoBig', 'corner', 'sub', 'edgeBig', 'jrCorner'];
const SCRATCH = new Float64Array(12);
let POWA = null;
let WV = null;
let VALW = null;

function evalGrid(g, wantParts) {
  const S = SCRATCH;
  for (let k = 0; k < 12; k++) S[k] = 0;
  let empties = 0, mt = 0, mtCnt = 0;
  for (let i = 0; i < 16; i++) {
    const c = g[i];
    if (!c) empties++;
    else if (c > mt) { mt = c; mtCnt = 1; }
    else if (c === mt) mtCnt++;
  }
  // snake + monotonicity (best of 8 paths)
  let bp = 0, bpos = -1e18;
  for (let p = 0; p < 8; p++) {
    const pa = SNAKES[p];
    let s = 0;
    for (let i = 0; i < 16; i++) { const c = g[pa[i]]; if (c) s += POWA[i] * VALW[c]; }
    if (s > bpos) { bpos = s; bp = p; }
  }
  S[0] = bpos;
  {
    const pa = SNAKES[bp];
    let mono = 0, prev = g[pa[0]];
    for (let i = 1; i < 16; i++) {
      const c = g[pa[i]];
      const d = prev - c;
      mono += d >= 0 ? d : 2.2 * d;
      prev = c;
    }
    S[1] = mono;
  }
  S[2] = empties;
  S[3] = -(empties < 3 ? 3 - empties : 0);
  // per-cell: isolation, corners, stranded edge tiles
  let isoBig = 0, isoJr = 0, jrCorner = 0, edgeBig = 0, corner = 0;
  for (let i = 0; i < 16; i++) {
    const v = g[i];
    if (!v) continue;
    if (v >= 3) {
      let eq = false;
      const a = ADJ[i];
      for (let x = 0; x < a.length; x++) if (g[a[x]] === v) { eq = true; break; }
      if (!eq) isoBig += -(v >= 5 ? 2 : 1);
      if (mt >= 3 && v === mt && IS_CORNER[i]) corner = 1;
    } else {
      const need = v === 1 ? 2 : 1;
      let ok = false;
      const a = ADJ[i];
      for (let x = 0; x < a.length; x++) if (g[a[x]] === need) { ok = true; break; }
      if (!ok) isoJr--;
      if (IS_CORNER[i]) jrCorner--;
    }
    if (EDGE_NONCORNER[i] && v >= 5) edgeBig -= (v - 3);
  }
  S[6] = isoJr; S[7] = isoBig; S[8] = corner; S[10] = edgeBig; S[11] = jrCorner;
  // pairs: right + down neighbours
  let chain = 0, jr = 0, sub = 0;
  if (mt >= 3 && mtCnt >= 2) sub += 1.5;
  for (let i = 0; i < 16; i++) {
    const a = g[i];
    if (!a) continue;
    const r = i >> 2, c = i & 3;
    for (let x = 0; x < 2; x++) {
      if (x === 0 && c === 3) continue;
      if (x === 1 && r === 3) continue;
      const j = x === 0 ? i + 1 : i + 4;
      const b = g[j];
      if (!b) continue;
      if (a === b) {
        if (a >= 3) {
          chain += a >= 5 ? a * a : a;
          if (a === mt) sub += 0.75;
          else if (mt >= 4 && a === mt - 1) sub += 1.0;
        }
      } else if ((a === 1 && b === 2) || (a === 2 && b === 1)) jr++;
    }
  }
  S[4] = chain; S[5] = jr; S[9] = sub;
  let total = 0;
  for (let k = 0; k < 12; k++) total += WV[k] * S[k];
  if (!wantParts) return total;
  const copy = new Float64Array(12);
  for (let k = 0; k < 12; k++) copy[k] = WV[k] * S[k];
  return { total, parts: copy };
}
function evalFast(g) { return evalGrid(g, false); }

/* quick value of a slide: avg eval over holes with known/expected spawn */
function quickVal(sim, q) {
  const spawns = q.length ? [[q[0], 1]] : DIST[maxCodeOf(sim.next)];
  let tot = 0, wn = 0;
  for (const h of sim.holes) {
    for (const [sc, w] of spawns) {
      const ch = sim.next.slice();
      ch[h] = sc;
      tot += w * evalFast(ch); wn += w;
    }
  }
  return wn ? tot / wn : -1e9;
}

/* ---------------- MC rollout tail ---------------- */
function rollout(g, q, rng, cap, noise) {
  let cur = g.slice();
  let qq = q.slice();
  for (let m = 0; m < cap; m++) {
    let best = -1e18, bestS = null, bestV = 0;
    for (let d = 0; d < 4; d++) {
      const s = slide(cur, d);
      if (!s) continue;
      const v = quickVal(s, qq) + rng() * noise;
      if (v > best) { best = v; bestS = s; bestV = d; }
    }
    if (bestS === null) break;
    cur = bestS.next.slice();
    let tile;
    if (qq.length) tile = qq.shift();
    tile = tile !== undefined ? tile : pickDist(DIST[maxCodeOf(cur)], rng);
    cur[bestS.holes[Math.floor(rng() * bestS.holes.length)]] = tile;
  }
  return evalFast(cur);
}
function pickDist(dist, rng) {
  const r = rng();
  let acc = 0;
  for (const [v, w] of dist) { acc += w; if (r < acc) return v; }
  return 2;
}

/* ---------------- expectimax ---------------- */
const BIG = 1e6;
function packKey(g, depth, q) {
  let lo = 0, hi = 0;
  for (let i = 0; i < 8; i++) lo = (lo << 4) | g[i];
  for (let i = 8; i < 16; i++) hi = (hi << 4) | g[i];
  return (lo >>> 0) + ':' + (hi >>> 0) + ':' + depth + ':' + q.join(',');
}
function bestC(g, depth, q, ctx) {
  ctx.nodes++;
  if (depth <= 0 || ctx.nodes > ctx.budget) {
    if (ctx.tail) {
      const base = evalFast(g);
      let tot = 0;
      for (let i = 0; i < ctx.tail.n; i++) {
        let seed = (ctx.nodes * 2654435761 + i * 40503) >>> 0;
        for (let k = 0; k < 16; k++) seed = (seed * 31 + g[k]) >>> 0;
        tot += rollout(g, q, lcg(seed), ctx.tail.cap, ctx.tail.noise);
      }
      const rAvg = tot / ctx.tail.n;
      return base * (1 - ctx.tail.w) + rAvg * ctx.tail.w;
    }
    return evalFast(g);
  }
  let key = null;
  if (ctx.memoOn && depth >= 2) {
    key = packKey(g, depth, q);
    const hit = ctx.memo.get(key);
    if (hit !== undefined) return hit;
  }
  const cand = [];
  for (let d = 0; d < 4; d++) {
    const s = slide(g, d);
    if (s) cand.push({ d, s, qv: quickVal(s, q) });
  }
  if (!cand.length) return -BIG + depth;
  if (cand.length > 2) cand.sort((a, b) => b.qv - a.qv);
  let best = -Infinity;
  if (ctx.worst) {
    for (const c of cand) {
      const v = valueDir(c.s, depth - 1, q, ctx);
      if (v < best) best = v;
    }
  } else {
    const top = cand[0].qv;
    let floorQ = Infinity;
    for (const c of cand) if (c.qv < floorQ) floorQ = c.qv;
    const thr = ctx.pruneDelta > 0 ? top - ctx.pruneDelta * (top - floorQ) : -Infinity;
    for (let i = 0; i < cand.length; i++) {
      const c = cand[i];
      if (i >= 2 && c.qv < thr) continue;
      const v = valueDir(c.s, depth - 1, q, ctx);
      if (v > best) best = v;
    }
  }
  if (key) ctx.memo.set(key, best);
  return best;
}
function valueDir(sim, depth, q, ctx) {
  let tot = 0, wn = 0;
  const spawns = q.length ? [[q[0], 1]] : DIST[maxCodeOf(sim.next)];
  const qn = q.length ? q.slice(1) : q;
  for (const h of sim.holes) {
    for (let i = 0; i < spawns.length; i++) {
      const sc = spawns[i][0], w = spawns[i][1];
      if (w <= 0) continue;
      const ch = sim.next.slice();
      ch[h] = sc;
      tot += w * bestC(ch, depth, qn, ctx);
      wn += w;
    }
  }
  return wn ? tot / wn : -BIG;
}
function searchMove(gc, qv, cfg) {
  const ctx = {
    nodes: 0, budget: cfg.nodes, memo: new Map(), memoOn: cfg.memo !== false,
    worst: !!cfg.worst, pruneDelta: cfg.pruneDelta === undefined ? 0.25 : cfg.pruneDelta,
    tail: cfg.tail || null,
  };
  const cand = [];
  for (let d = 0; d < 4; d++) {
    const s = slide(gc, d);
    if (s) cand.push({ d, s, qv: quickVal(s, qv) });
  }
  if (!cand.length) return null;
  if (cand.length === 1) return cand[0].d;
  // order: max-tile merges first, then quick value
  for (const c of cand) {
    c.mergesMax = false;
    for (const [fi, ti, mg, v] of c.s.steps) if (mg && v === maxCodeOf(gc) + 1) c.mergesMax = true;
  }
  cand.sort((a, b) => (b.mergesMax - a.mergesMax) || (b.qv - a.qv));
  const top = cand[0].qv;
  let floorQ = Infinity;
  for (const c of cand) if (c.qv < floorQ) floorQ = c.qv;
  const thr = (ctx.pruneDelta > 0 && cand.length > 2) ? top - ctx.pruneDelta * (top - floorQ) : -Infinity;
  const vals = [];
  let best = -Infinity;
  for (let i = 0; i < cand.length; i++) {
    const c = cand[i];
    if (!c.mergesMax && i >= 2 && c.qv < thr) continue;
    const v = valueDir(c.s, cfg.depth - 1, qv, ctx);
    vals.push({ d: c.d, v });
    if (v > best) best = v;
  }
  const tied = vals.filter((x) => x.v >= best - 1e-9);
  if (cfg.tieEps > 0 && cfg.rng) {
    const near = vals.filter((x) => x.v >= best - cfg.tieEps);
    if (near.length > 1) return near[Math.floor(cfg.rng() * near.length)].d;
  }
  if (tied.length === 1) return tied[0].d;
  return greedyPick(gc, qv, tied.map((x) => x.d));
}

/* ---------------- policy.py greedy baseline ---------------- */
function factsFor(gc, sim) {
  const mt = maxCodeOf(gc);
  let merges = 0, maxMerge = false, worstRemoved = 0, worstComp = false, maxDisp = false, chainAfter = 0;
  const v = []; for (let i = 0; i < 16; i++) if (gc[i]) v.push(gc[i]);
  const worst = v.length ? Math.min.apply(null, v) : null;
  for (const [fi, ti, mg, val] of sim.steps) {
    if (gc[fi] === mt) maxDisp = true; // any step that MOVES the max tile
    if (mg) {
      merges++;
      if (val === mt + 1 || ((mt === 1 || mt === 2) && val === 3)) maxMerge = true;
      if (gc[fi] === 1 || gc[fi] === 2) worstRemoved++;
      if (gc[ti] === 1 || gc[ti] === 2) worstRemoved++;
      if (worst !== null && (gc[fi] === worst || gc[ti] === worst)) worstComp = true;
    }
  }
  let emptyAfter = -1;
  for (let i = 0; i < 16; i++) if (!sim.next[i]) emptyAfter++;
  // chain pairs in next
  for (let i = 0; i < 16; i++) {
    const a = sim.next[i];
    if (!a || a < 3) continue;
    const r = i >> 2, c = i & 3;
    if (c < 3 && sim.next[i + 1] === a) chainAfter++;
    if (r < 3 && sim.next[i + 4] === a) chainAfter++;
  }
  return { merges, maxMerge, worstRemoved, worstComp, maxDisp, chainAfter, emptyAfter };
}
function greedyScore(gc, sim, edgeWorst) {
  const f = factsFor(gc, sim);
  return 100 * f.merges + 40 * (f.maxMerge ? 1 : 0) + 15 * f.emptyAfter
    - 10 * edgeWorst + 30 * f.worstRemoved + 20 * (f.worstComp ? 1 : 0)
    - 40 * (f.maxDisp ? 1 : 0) + 5 * f.chainAfter;
}
function edgeWorstCount(sim, d) {
  let n = 0;
  for (let j = 0; j < 4; j++) {
    const cv = sim.next[EDGE[d][j]];
    if (cv === 1 || cv === 2) n++;
  }
  return n;
}
function greedyPick(gc, qv, onlyDirs) {
  const dirs = onlyDirs || validDirs(gc);
  if (!dirs.length) return null;
  let best = -Infinity, bestD = null, bestPri = 99;
  for (const d of dirs) {
    const s = slide(gc, d);
    if (!s) continue;
    const sc = greedyScore(gc, s, edgeWorstCount(s, d));
    const pri = GREEDY_PRI.indexOf(d);
    if (sc > best || (sc === best && pri < bestPri)) { best = sc; bestD = d; bestPri = pri; }
  }
  return bestD;
}

/* ---------------- game runner ---------------- */
function play(seed, policyFactory, hooks) {
  const g = new Threes({ seed });
  let guard = 0;
  while (!g.gameOver && guard++ < 3000) {
    const gc = codesOf(g.grid);
    const qv = (g.queue || []).map((x) => CODE_OF[x]);
    if (hooks && hooks.onMove) hooks.onMove(g, gc, qv, guard);
    const d = policyFactory(g, gc, qv);
    if (d === null || d === undefined) break;
    const r = g.applyMove(NAMES[d]);
    if (!r || !r.ok) break;
  }
  return { seed, maxTile: g._maxTile(), moves: g.moves, score: g.score };
}
function seedRange() {
  const s = process.env.SEEDS;
  if (!s) { const a = []; for (let x = 101; x <= 120; x++) a.push(x); return a; }
  const a = [];
  for (const part of s.split(',')) {
    if (part.includes('-')) { const [x, y] = part.split('-').map(Number); for (let k = x; k <= y; k++) a.push(k); }
    else a.push(Number(part));
  }
  return a;
}

/* ---------------- configs ---------------- */
function W(o) {
  const base = {
    snake: 10, mono: 6, empty: 30, cramp: 120, chain: 9, jr: 14,
    isoJr: 18, isoBig: 25, corner: 40, sub: 60, edgeBig: 20, jrCorner: 12,
  };
  return Object.assign(base, o || {});
}
function wvec(o) {
  const w = W(o);
  return PNAMES.map((n) => w[n]);
}
function mkW(o, alpha, sv) {
  POWA = new Float64Array(16);
  for (let i = 0; i < 16; i++) POWA[i] = Math.pow(alpha === undefined ? 0.82 : alpha, i);
  WV = wvec(o);
  VALW = new Float64Array(16);
  const p = sv === undefined ? 1 : sv;
  for (let c = 1; c < 16; c++) VALW[c] = Math.pow(VALS[c] || 0, p);
}

const TW = { empty: 90, cramp: 240, isoJr: 25.2, jr: 9.8 }; // from coordinate-descent tune of ex-d3
const CONFIGS = {
  'greedy': { greedy: true },
  'ex-d3': { depth: 3, nodes: 40000 },
  'ex-d4': { depth: 4, nodes: 80000 },
  'ex-d5': { depth: 5, nodes: 150000 },
  'ex-d6': { depth: 6, nodes: 260000 },
  'ex-d7': { depth: 7, nodes: 400000 },
  'ex-d4-empty': { depth: 4, nodes: 80000, w: { empty: 55, cramp: 220, snake: 8 } },
  'ex-d5-empty': { depth: 5, nodes: 150000, w: { empty: 55, cramp: 220, snake: 8 } },
  'ex-d5-chain': { depth: 5, nodes: 150000, w: { chain: 24, sub: 95, mono: 4, snake: 8 } },
  'ex-d5-junk': { depth: 5, nodes: 150000, w: { jr: 30, isoJr: 34, jrCorner: 30, edgeBig: 34 } },
  'ex-d5-flat': { depth: 5, nodes: 150000, w: { empty: 45, cramp: 170, chain: 14, sub: 75, mono: 8 } },
  'ex-d5-worst': { depth: 5, nodes: 150000, worst: true },
  'ex-d4-worst': { depth: 4, nodes: 80000, worst: true },
  'hyb-d2-roll': {
    depth: 2, nodes: 30000, tail: { n: 4, cap: 45, noise: 30, w: 0.35 },
    w: { empty: 40, cramp: 160 },
  },
  'hyb-d3-roll': {
    depth: 3, nodes: 120000, tail: { n: 3, cap: 40, noise: 30, w: 0.3 },
    w: { empty: 40, cramp: 160 },
  },
  'mc-roll': {
    depth: 1, nodes: 5000, tail: { n: 25, cap: 120, noise: 30, w: 1.0 },
  },
  'ex-t-d3': { depth: 3, nodes: 40000, w: TW },
  'ex-t-d4': { depth: 4, nodes: 80000, w: TW },
  'ex-t-d5': { depth: 5, nodes: 150000, w: TW },
  'ex-t-d6': { depth: 6, nodes: 260000, w: TW },
  'ex-t-d7': { depth: 7, nodes: 400000, w: TW },
  'ex-t-d5-b2': { depth: 5, nodes: 600000, w: TW, pruneDelta: 0.15 },
  'hyb-t-d3': {
    depth: 3, nodes: 120000, tail: { n: 3, cap: 40, noise: 30, w: 0.3 }, w: TW,
  },
  'hyb-t-d4': {
    depth: 4, nodes: 250000, tail: { n: 3, cap: 40, noise: 30, w: 0.3 }, w: TW,
  },
  'ex-t-d5-np': { depth: 5, nodes: 400000, w: TW, pruneDelta: 0 },
  'ex-t-d4-np': { depth: 4, nodes: 250000, w: TW, pruneDelta: 0 },
  'ex-t-d4-chain': { depth: 4, nodes: 120000, w: Object.assign({}, TW, { chain: 27, sub: 120, snake: 14 }), pruneDelta: 0.15 },
  'ex-t-d4-space': { depth: 4, nodes: 120000, w: Object.assign({}, TW, { empty: 140, cramp: 320, snake: 7 }), pruneDelta: 0.15 },
  'ex-hard-d3': { depth: 3, nodes: 40000, w: { empty: 63, cramp: 168, isoJr: 17.6, jr: 9.8 } },
  'ex-hard-d5': { depth: 5, nodes: 400000, w: { empty: 63, cramp: 168, isoJr: 17.6, jr: 9.8 }, pruneDelta: 0 },
  'ex-t-d8': { depth: 8, nodes: 400000, w: TW, pruneDelta: 0.15 },
  'ex-t-d9': { depth: 9, nodes: 700000, w: TW, pruneDelta: 0.15 },
  'mc-roll-big': { depth: 1, nodes: 5000, tail: { n: 60, cap: 220, noise: 30, w: 1.0 } },
};

// best-of-K over tie-randomized near-optimal lines (deterministic LCG per sample)
function runBestOf(name, k, eps) {
  const base = CONFIGS[name];
  mkW(base.w, base.alpha, base.sv);
  const seeds = seedRange();
  const out = {};
  const t0 = Date.now();
  for (const s of seeds) {
    let bt = 0;
    for (let k2 = 0; k2 < k; k2++) {
      const cfg = Object.assign({}, base, { tieEps: eps, rng: lcg(s * 100003 + k2 * 7919 + 11) });
      const r = play(s, makePolicy(cfg));
      if (r.maxTile > bt) bt = r.maxTile;
    }
    out[s] = bt;
    console.log('bestof ' + name + ' seed ' + s + ' -> ' + bt + ' (' + ((Date.now() - t0) / 1000).toFixed(0) + 's)');
  }
  const w = seeds.filter((s) => out[s] >= 96).length;
  console.log('BESTOF ' + name + ' K=' + k + ' eps=' + eps + ' wins=' + w + '/' + seeds.length + ' ' + JSON.stringify(out));
}
const TOP3 = ['ex-t-d5-np', 'ex-t-d5-b2', 'ex-t-d3'];

function makePolicy(cfg) {
  if (cfg.greedy) return function (g, gc, qv) { return greedyPick(gc, qv); };
  return function (g, gc, qv) {
    const d = searchMove(gc, qv, cfg);
    if (d !== null) return d;
    return greedyPick(gc, qv);
  };
}
function runCfg(name, cfg, label) {
  mkW(cfg.w, cfg.alpha, cfg.sv);
  const policy = makePolicy(cfg);
  const seeds = seedRange();
  const rows = [];
  const t0 = Date.now();
  for (const s of seeds) rows.push(play(s, policy));
  const wins = rows.filter((r) => r.maxTile >= 96).length;
  const dist = {};
  rows.forEach((r) => { dist[r.maxTile] = (dist[r.maxTile] || 0) + 1; });
  const sec = ((Date.now() - t0) / 1000).toFixed(1);
  console.log(String(label || name).padEnd(16),
    'wins=' + wins + '/' + seeds.length,
    'best=' + Math.max.apply(null, rows.map((r) => r.maxTile)),
    'avgMoves=' + Math.round(rows.reduce((a, r) => a + r.moves, 0) / rows.length),
    sec + 's', JSON.stringify(dist));
  return { name, rows, wins };
}

/* ---------------- decision signals ---------------- */
function runSignals(name) {
  const cfg = CONFIGS[name];
  mkW(cfg.w, cfg.alpha, cfg.sv);
  const partPos = new Float64Array(12), partSum = new Float64Array(12);
  const factT = {
    corner_after: 0, no_displace: 0, empty_after: 0, edge_worst: 0,
    worst_removed: 0, chain: 0, spawn_edge: 0, spawn_edge_clear: 0, max_pin: 0,
    sub_merge: 0, merges: 0, wiggle: 0, second_pair: 0, iso_big: 0, junk_clear: 0,
    mono: 0, snake_keep: 0,
  };
  const factLoss = { corner_after: 0, no_displace: 0, empty_after: 0, edge_worst: 0,
    worst_removed: 0, chain: 0, spawn_edge: 0, max_pin: 0, sub_merge: 0, wiggle: 0,
    second_pair: 0, iso_big: 0 };
  let diffCount = 0, totalCount = 0;
  const dirsLog = {};
  const policy = function (g, gc, qv) {
    totalCount++;
    const gp = greedyPick(gc, qv);
    const ch = searchMove(gc, qv, cfg);
    if (ch !== null && gp !== null && ch !== gp) {
      diffCount++;
      const key = NAMES[gp] + '->' + NAMES[ch];
      dirsLog[key] = (dirsLog[key] || 0) + 1;
      const pa = avgParts(gc, gp, qv), pb = avgParts(gc, ch, qv);
      for (let k = 0; k < 12; k++) {
        const d = pb[k] - pa[k];
        if (d > 1e-6) { partPos[k]++; partSum[k] += d; }
      }
      const sa = slide(gc, gp), sb = slide(gc, ch);
      const fa = factsFor(gc, sa), fb = factsFor(gc, sb);
      if (fb.emptyAfter > fa.emptyAfter) factT.empty_after++;
      if (edgeWorstCount(sb, ch) < edgeWorstCount(sa, gp)) factT.edge_worst++;
      if (fb.worstRemoved > fa.worstRemoved) factT.worst_removed++;
      if (fb.chainAfter > fa.chainAfter) factT.chain++;
      if (fa.maxDisp && !fb.maxDisp) factT.no_displace++;
      if (!fa.maxDisp && fb.maxDisp) factLoss.no_displace++;
      const ea = spawnEdgeFacts(sa, gp), eb = spawnEdgeFacts(sb, ch);
      if (eb.empties > ea.empties) factT.spawn_edge++;
      if (!eb.hasBig && ea.hasBig) factT.spawn_edge_clear++;
      if (pinsMax(gc, ch) && !pinsMax(gc, gp)) factT.max_pin++;
      if (fb.maxMerge && !fa.maxMerge) factT.sub_merge++;
      if (fb.merges > fa.merges) factT.merges++;
      if (cornerAfter(sa, gp) && !cornerAfter(sb, ch)) factLoss.corner_after++;
      if (cornerAfter(sb, ch) && !cornerAfter(sa, gp)) factT.corner_after++;
      if (worstWiggle(sb, ch) > worstWiggle(sa, gp)) factT.wiggle++;
      if (worstWiggle(sb, ch) < worstWiggle(sa, gp)) factLoss.wiggle++;
      if (secondPair(sb) && !secondPair(sa)) factT.second_pair++;
      if (!secondPair(sb) && secondPair(sa)) factLoss.second_pair++;
      if (isoBigCount(sb) < isoBigCount(sa)) factT.iso_big++;
      if (isoBigCount(sb) > isoBigCount(sa)) factLoss.iso_big++;
    }
    return ch === null ? gp : ch;
  };
  for (const s of seedRange()) play(s, policy);
  const rows = [];
  for (let k = 0; k < 12; k++) {
    if (partPos[k]) rows.push({ part: PNAMES[k], freq: partPos[k] / diffCount, avg: partSum[k] / partPos[k] });
  }
  rows.sort((a, b) => b.freq - b.avg * 0.001 || b.avg - a.avg);
  console.log('signals config=' + name + ' diverged=' + diffCount + '/' + totalCount + ' moves');
  console.log('top eval-component diffs (chosen vs greedy):');
  rows.slice(0, 8).forEach((r) => console.log('  ' + r.part.padEnd(10),
    'favours chosen in ' + (100 * r.freq).toFixed(0) + '%, avg delta ' + r.avg.toFixed(1)));
  console.log('fact-level gains (chosen over greedy):', JSON.stringify(factT));
  console.log('fact-level losses (chosen vs greedy):', JSON.stringify(factLoss));
  const dk = Object.entries(dirsLog).sort((a, b) => b[1] - a[1]).slice(0, 8);
  console.log('top direction flips (greedy->search):', JSON.stringify(Object.fromEntries(dk)));
}
function avgParts(gc, d, qv) {
  const sim = slide(gc, d);
  const out = new Float64Array(12);
  if (!sim) return out;
  const spawns = qv.length ? [[qv[0], 1]] : DIST[maxCodeOf(sim.next)];
  let wn = 0;
  for (const h of sim.holes) {
    for (const [sc, w] of spawns) {
      if (w <= 0) continue;
      const ch = sim.next.slice();
      ch[h] = sc;
      const r = evalGrid(ch, true);
      for (let k = 0; k < 12; k++) out[k] += w * r.parts[k];
      wn += w;
    }
  }
  if (wn) for (let k = 0; k < 12; k++) out[k] /= wn;
  return out;
}
function spawnEdgeFacts(sim, d) {
  let empties = 0, hasBig = false;
  for (let j = 0; j < 4; j++) {
    const cv = sim.next[EDGE[d][j]];
    if (cv === 0) empties++;
    else if (cv >= 4) hasBig = true; // compiler: value > 6
  }
  return { empties, hasBig };
}
function cornerAfter(sim, d) {
  // max tile's post-slide cell is a corner (mirror of compiler max_tile_corner_after)
  if (!sim) return false;
  let mt = 0;
  for (const v of sim.next) if (v > mt) mt = v;
  if (mt < 3) return true;
  for (let i = 0; i < 16; i++) if (sim.next[i] === mt && IS_CORNER[i]) return true;
  return false;
}
function worstWiggle(sim, d) {
  // min over spawn holes (spawn value 1 or 2) of number of still-valid dirs
  if (!sim) return -1;
  let worst = 99;
  for (const h of sim.holes) {
    for (const sc of [1, 2]) {
      const ch = sim.next.slice();
      ch[h] = sc;
      const n = validDirs(ch).length;
      if (n < worst) worst = n;
    }
  }
  return worst;
}
function secondPair(sim) {
  if (!sim) return false;
  const seen = {};
  for (const v of sim.next) if (v) seen[v] = (seen[v] || 0) + 1;
  const vals = Object.keys(seen).map(Number).sort((a, b) => b - a);
  return vals.length >= 2 && seen[vals[1]] >= 2;
}
function isoBigCount(sim) {
  if (!sim) return 99;
  let n = 0;
  for (let i = 0; i < 16; i++) {
    const v = sim.next[i];
    if (v < 3) continue;
    let eq = false;
    for (const j of ADJ[i]) if (sim.next[j] === v) eq = true;
    if (!eq) n++;
  }
  return n;
}
function pinsMax(gc, d) {
  const mt = maxCodeOf(gc);
  let pos = -1;
  for (let i = 0; i < 16; i++) if (gc[i] === mt) { pos = i; break; }
  if (pos < 0) return false;
  const r = pos >> 2, c = pos & 3;
  // pin dirs: up can't move a tile in row 0; down row 3; left col 0; right col 3
  const pins = [];
  if (r === 0) pins.push(0);
  if (r === 3) pins.push(1);
  if (c === 0) pins.push(2);
  if (c === 3) pins.push(3);
  return pins.includes(d);
}

/* ---------------- mirror check ---------------- */
function check() {
  let states = 0, mismatches = 0;
  const rng = lcg(12345);
  const policy = function (g, gc) {
    states++;
    for (let d = 0; d < 4; d++) {
      const mine = slide(gc, d);
      const theirs = g._simulate(NAMES[d]);
      const ok = (!!mine) === (!!theirs);
      if (!ok) { mismatches++; console.log('MISMATCH seed', g.seed, 'dir', NAMES[d]); }
      if (mine && theirs && mine.holes.length !== theirs.holes.length) {
        mismatches++; console.log('HOLE MISMATCH', g.seed, NAMES[d], mine.holes.length, theirs.holes.length);
      }
    }
    const v = validDirs(gc);
    return v.length ? v[Math.floor(rng() * v.length)] : null;
  };
  for (let s = 101; s <= 108; s++) play(s, policy);
  console.log('mirror check: states=' + states + ' mismatches=' + mismatches);
  if (mismatches) process.exit(1);
}

/* ---------------- weight tuning (coordinate descent) ---------------- */
function scoreCfg(cfg, seeds) {
  mkW(cfg.w, cfg.alpha, cfg.sv);
  const policy = makePolicy(cfg);
  let wins = 0, sum = 0;
  for (const s of seeds) {
    const r = play(s, policy);
    if (r.maxTile >= 96) wins++;
    sum += Math.log2(r.maxTile || 2);
  }
  return { wins, sum };
}
function tune(baseName, rounds) {
  const base = CONFIGS[baseName];
  const seeds = seedRange();
  let cur = { depth: base.depth, nodes: base.nodes, worst: base.worst, alpha: base.alpha, sv: base.sv, w: Object.assign({}, base.w || {}) };
  let best = scoreCfg(cur, seeds);
  console.log('tune from ' + baseName + ' wins=' + best.wins + '/' + seeds.length + ' sum=' + best.sum.toFixed(1));
  const TUNE_PARTS = ['empty', 'cramp', 'corner', 'sub', 'chain', 'snake', 'mono', 'isoJr', 'isoBig', 'edgeBig', 'jr', 'jrCorner'];
  for (let round = 0; round < rounds; round++) {
    for (const part of TUNE_PARTS) {
      const w0 = W(cur.w)[part];
      for (const mult of [0.5, 0.7, 1.4, 2, 3]) {
        const cand = Object.assign({}, cur, { w: Object.assign({}, cur.w, { [part]: w0 * mult }) });
        const sc = scoreCfg(cand, seeds);
        if (sc.wins > best.wins || (sc.wins === best.wins && sc.sum > best.sum + 1e-6)) {
          best = sc; cur = cand;
          console.log('  +' + part + '*' + mult + ' -> wins=' + best.wins + ' sum=' + best.sum.toFixed(1));
        }
      }
    }
    for (const [a, sp] of [[0.7, 1], [0.9, 1], [0.75, 1], [0.88, 1], [0.82, 0.6], [0.82, 0.8], [0.82, 1.5], [0.82, 2]]) {
      const cand = Object.assign({}, cur, { alpha: a, sv: sp });
      const sc = scoreCfg(cand, seeds);
      if (sc.wins > best.wins || (sc.wins === best.wins && sc.sum > best.sum + 1e-6)) {
        best = sc; cur = cand;
        console.log('  alpha/sv ' + a + '/' + sp + ' -> wins=' + best.wins + ' sum=' + best.sum.toFixed(1));
      }
    }
    console.log('round ' + round + ' done: ' + JSON.stringify(cur.w) + ' a=' + (cur.alpha || 0.82) + ' sv=' + (cur.sv === undefined ? 1 : cur.sv));
  }
  console.log('TUNED ' + baseName + ' w=' + JSON.stringify(cur.w) + ' alpha=' + (cur.alpha || 0.82) + ' wins=' + best.wins + '/' + seeds.length);
}

/* ---------------- commands ---------------- */
const cmd = process.argv[2] || 'list';
const arg = process.argv[3];
const t0 = Date.now();
if (cmd === 'check') {
  check();
} else if (cmd === 'list') {
  console.log(Object.keys(CONFIGS).join('\n'));
  console.log('TOP3:', TOP3.join(','));
} else if (cmd === 'run') {
  const names = arg === 'all' ? Object.keys(CONFIGS) : (arg ? arg.split(',') : TOP3);
  mkW(); // ensure defaults exist before first use
  for (const n of names) {
    if (!CONFIGS[n]) { console.log('no config ' + n); continue; }
    runCfg(n, CONFIGS[n]);
  }
} else if (cmd === 'final') {
  const names = (arg ? arg.split(',') : TOP3).filter((n) => CONFIGS[n]);
  const res = names.map((n) => runCfg(n, CONFIGS[n]));
  const seeds = seedRange();
  console.log('\nper-seed max_tile (rows=seeds, cols=' + names.join(', ') + '):');
  console.log('seed'.padEnd(6) + names.map((n) => n.padStart(14)).join('') + '   union');
  let unionWins = 0;
  const union = [];
  for (let i = 0; i < seeds.length; i++) {
    const vals = res.map((r) => r.rows[i].maxTile);
    const u = Math.max.apply(null, vals);
    union.push(u);
    if (u >= 96) unionWins++;
    console.log(String(seeds[i]).padEnd(6) + vals.map((v) => String(v).padStart(14)).join('') +
      String(u).padStart(9) + (u >= 96 ? '  WIN' : ''));
  }
  console.log('union wins: ' + unionWins + '/' + seeds.length);
  res.forEach((r, i) => console.log('config ' + names[i] + ': wins ' + r.wins + '/' + seeds.length +
    ' best ' + Math.max.apply(null, r.rows.map((x) => x.maxTile))));
  const never = seeds.filter((s, i) => union[i] < 96);
  console.log('never-won seeds (candidate hard): ' + JSON.stringify(never));
} else if (cmd === 'tune') {
  const parts = (arg || 'ex-d5:1').split(':');
  tune(parts[0], Number(parts[1] || 1));
} else if (cmd === 'dump') {
  const cfg = CONFIGS[arg];
  if (!cfg) { console.log('no config ' + arg); process.exit(1); }
  mkW(cfg.w, cfg.alpha, cfg.sv);
  const policy = makePolicy(cfg);
  const out = {};
  for (const s of seedRange()) out[s] = play(s, policy).maxTile;
  console.log('DUMP ' + arg + ' ' + JSON.stringify(out));
} else if (cmd === 'bestof') {
  const parts = (arg || 'ex-t-d3:8:10').split(':');
  runBestOf(parts[0], Number(parts[1] || 8), Number(parts[2] || 10));
} else if (cmd === 'signals') {
  runSignals(arg || TOP3[0]);
} else {
  console.log('unknown cmd ' + cmd);
  process.exit(1);
}
console.log('# total wall ' + ((Date.now() - t0) / 1000).toFixed(1) + 's');
