"""Expectimax planner for the Jev Threes bot (port of game/_search_oracle.js).

Ports the tuned expectimax configs ``ex-t-d3`` / ``ex-t-d5-np`` from the JS
search oracle: max nodes over the four slides, chance nodes = AVG over every
spawn-hole placement of queue[0] (DIST spawn distribution beyond the visible
queue), leaf = the tuned 12-part classic eval (corner snake alpha=0.82 etc).

Usage:
    plan(grid, queue, valid_dirs, depth=3, node_budget=200000) -> {dir: int}
    rank_options(valid_dirs, scores) -> [dir, ...]   # rank 1 == chosen dir

DETERMINISM GUARANTEES
======================
- Zero randomness: every traversal iterates directions in fixed engine order
  (up, down, left, right), holes in ascending spawn-edge order, and spawn
  outcomes in fixed DIST order. No RNG is consulted anywhere; plan() is a
  pure function of (grid, queue, valid_dirs, depth, node_budget, prune_delta,
  memo_on).
- Float accumulation mirrors the JS oracle fold order for every eval part
  (left folds, hardcoded V8 POWA constants); direction values match the JS
  reference bit-for-bit (validated by runs/_planner_validate.py against
  game/_search_oracle.js).
- Ties between direction values (within 1e-9, the JS oracle's epsilon) are
  broken with the policy.py greedy score among the tied directions; plan()
  biases the returned integer scores so plain argmax — and rank 1 of
  rank_options() — equals the JS oracle's tie-break pick. Remaining ties fall
  back to DIR_PRIORITY = (up, left, down, right).
- Node budget: a hard cap on expectimax node visits (counted like the JS
  oracle, memo hits included). Once tripped, every further node visit
  evaluates the position where it stands (plain leaf eval), so values may be
  shallower but every requested direction still gets a score;
  STATS["budget_exhausted"] reports whether the cap bound the search.
- Internal caches (lane tables, slide/eval/expansion memos and the
  transposition tables) are pure-value caches keyed on their inputs; only
  fully-expanded (never budget-truncated) values are stored, so clearing
  them can never change results, only speed.
- plan() NEVER raises: malformed/weird states return an all-zero dict.

Python 3.9+ stdlib only.
"""

import time

try:  # run from bot/ (like test_offline.py) ...
    from compiler import simulate_slide as _simulate_slide
except ImportError:  # ... or import as bot.planner
    from bot.compiler import simulate_slide as _simulate_slide

# --------------------------------------------------------------------------
# constants / geometry (mirrors of game/_search_oracle.js)
# --------------------------------------------------------------------------

DIR_PRIORITY = ("up", "left", "down", "right")  # tie-break priority (policy.py)

# search traversal dir order = engine NAMES order: up, down, left, right
_NAMES = ("up", "down", "left", "right")
_DIR_IDX = {"up": 0, "down": 1, "left": 2, "right": 3}

_VALS = (0, 1, 2, 3, 6, 12, 24, 48, 96, 192, 384, 768, 1536, 3072)
_CODE_OF = {v: i for i, v in enumerate(_VALS)}

# MERGE[(a << 4) | b] -> merged tile CODE (0 = not mergeable); a,b in 1..15
_MERGE = [0] * 256
for _a in range(1, 16):
    for _b in range(1, 16):
        if (_a == 1 and _b == 2) or (_a == 2 and _b == 1):
            _MERGE[(_a << 4) | _b] = 3
        elif _a == _b and _a >= 3:
            _MERGE[(_a << 4) | _b] = _a + 1

# CELLS[d][lane] = 4 grid indices in destination-edge-first order (JS CELLS)
_CELLS = [[None] * 4 for _ in range(4)]
for _k in range(4):
    _CELLS[0][_k] = (_k, _k + 4, _k + 8, _k + 12)                    # up
    _CELLS[1][_k] = (_k + 12, _k + 8, _k + 4, _k)                    # down
    _CELLS[2][_k] = (4 * _k, 4 * _k + 1, 4 * _k + 2, 4 * _k + 3)     # left
    _CELLS[3][_k] = (4 * _k + 3, 4 * _k + 2, 4 * _k + 1, 4 * _k)     # right

# spawn-edge (opposite) cells per dir, ascending (JS EDGE)
_EDGE = ((12, 13, 14, 15),   # up   -> bottom row
         (0, 1, 2, 3),       # down -> top row
         (3, 7, 11, 15),     # left -> right col
         (0, 4, 8, 12))      # right-> left col

_ADJ = ((1, 4), (0, 2, 5), (1, 3, 6), (2, 7),
        (0, 5, 8), (1, 4, 6, 9), (2, 5, 7, 10), (3, 6, 11),
        (4, 9, 12), (5, 8, 10, 13), (6, 9, 11, 14), (7, 10, 15),
        (8, 13), (9, 12, 14), (10, 13, 15), (11, 14))
_IS_CORNER = (1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 1)
_EDGE_NONCORNER = (0, 1, 1, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, 1, 1, 0)

# 8 serpentine snake paths, cell indices (JS SNAKES)
_SNAKES = (
    (0, 1, 2, 3, 7, 6, 5, 4, 8, 9, 10, 11, 15, 14, 13, 12),
    (3, 2, 1, 0, 4, 5, 6, 7, 11, 10, 9, 8, 12, 13, 14, 15),
    (12, 13, 14, 15, 11, 10, 9, 8, 4, 5, 6, 7, 0, 1, 2, 3),
    (15, 14, 13, 12, 8, 9, 10, 11, 7, 6, 5, 4, 0, 1, 2, 3),
    (0, 4, 8, 12, 13, 9, 5, 1, 2, 6, 10, 14, 15, 11, 7, 3),
    (3, 7, 11, 15, 14, 10, 6, 2, 1, 5, 9, 13, 12, 8, 4, 0),
    (12, 8, 4, 0, 1, 5, 9, 13, 14, 10, 6, 2, 3, 7, 11, 15),
    (15, 11, 7, 3, 2, 6, 10, 14, 13, 9, 5, 1, 0, 4, 8, 12),
)

# POWA[i] = Math.pow(0.82, i) exactly as emitted by V8 (hardcoded so float
# folds are IEEE-identical to the JS oracle; CPython pow() may differ 1 ulp).
_POWA = (
    1.0,
    0.82,
    0.6723999999999999,
    0.5513679999999999,
    0.4521217599999999,
    0.37073984319999986,
    0.3040066714239999,
    0.24928547056767988,
    0.2044140858654975,
    0.16761955040970794,
    0.1374480313359605,
    0.1127073856954876,
    0.09242005627029984,
    0.07578444614164585,
    0.0621432458361496,
    0.05095746158564267,
)
# VALW[c] = pow(VALS[c], sv=1) (mkW default sv=1)
_VALW = tuple(float(_VALS[c]) if c < len(_VALS) else 0.0 for c in range(16))

# weights: classic base W() with TW overrides (empty 90, cramp 240, isoJr
# 25.2, jr 9.8). parts order: snake,mono,empty,cramp,chain,jr,isoJr,isoBig,
# corner,sub,edgeBig,jrCorner
_WVEC = (10.0, 6.0, 90.0, 240.0, 9.0, 9.8, 25.2, 25.0, 40.0, 60.0, 20.0, 12.0)

_BIG = 1e6

# spawn distribution per max CODE (JS DIST <- engine _weightedSpawn); entries
# are (spawn CODE, weight) in fixed order.
_DIST = []
for _mc in range(16):
    _v = _VALS[_mc] if _mc < len(_VALS) else 0
    _e = []
    _lo = 0.0
    if _v >= 96:
        _e.append((_CODE_OF[12], 0.01))
        _lo = 0.01
    if _v >= 24:
        _e.append((_CODE_OF[6], 0.05 - _lo))
        _lo = 0.05
    if _v >= 6:
        _e.append((_CODE_OF[3], 0.12 - _lo))
        _lo = 0.12
    if 0.5 - _lo > 0:
        _e.append((1, 0.5 - _lo))
    _e.append((2, 0.5))
    _DIST.append(tuple(_e))

# --------------------------------------------------------------------------
# packed grid helpers. grid int: nibble i (row-major cell i) = tile CODE.
# --------------------------------------------------------------------------

_REV16 = {}


def _rev16(l16):
    v = _REV16.get(l16)
    if v is None:
        v = (((l16 & 0xF) << 12) | (((l16 >> 4) & 0xF) << 8) |
             (((l16 >> 8) & 0xF) << 4) | ((l16 >> 12) & 0xF))
        _REV16[l16] = v
    return v


_LANEINFO = {}   # l16 -> (zeros, maxcode, maxcount within lane)
_SLIDEL = {}     # l16 -> (out16, steps) | 0, lane slide toward index 0
_UNPACK = {}     # grid int -> tuple of 16 codes
_EVALC = {}      # grid int -> eval (pure fn; cross-move memo)
_SLIDEM = {}     # (grid int, dir) -> (next, holes) | None
_EXP = {}        # (grid<<5 | q0+1) -> sorted slide/chance children | None
_TMEMO = {}      # prune_delta -> {packed (grid,depth,queue) -> value}
_CACHE_CAP = 1 << 20
_TMEMO_CAP = 1 << 21


def _lane_info(l16):
    t = _LANEINFO.get(l16)
    if t is None:
        z = mx = cnt = 0
        for c in ((l16 & 0xF), (l16 >> 4) & 0xF, (l16 >> 8) & 0xF,
                  (l16 >> 12) & 0xF):
            if c == 0:
                z += 1
            elif c > mx:
                mx = c
                cnt = 1
            elif c == mx:
                cnt += 1
        t = (z, mx, cnt)
        _LANEINFO[l16] = t
    return t


def _slide_lane_forward(l16):
    """Slide a lane whose tiles advance toward index 0 (destination edge).
    Returns (out16, steps) with steps = ((i_from, i_to, merge, code)...),
    or 0 when nothing advances. Mirror of the engine lane loop."""
    t = _SLIDEL.get(l16)
    if t is None:
        c = [l16 & 0xF, (l16 >> 4) & 0xF, (l16 >> 8) & 0xF, (l16 >> 12) & 0xF]
        o = list(c)
        moved = [0, 0, 0, 0]
        steps = []
        any_m = 0
        for i in range(1, 4):
            f = c[i]
            if not f:
                continue
            if moved[i - 1]:
                o[i] = 0
                o[i - 1] = f
                moved[i] = 1
                any_m = 1
                steps.append((i, i - 1, 0, f))
            else:
                tt = c[i - 1]
                if tt == 0:
                    o[i] = 0
                    o[i - 1] = f
                    moved[i] = 1
                    any_m = 1
                    steps.append((i, i - 1, 0, f))
                else:
                    mg = _MERGE[(tt << 4) | f]
                    if mg:
                        o[i] = 0
                        o[i - 1] = mg
                        moved[i] = 1
                        any_m = 1
                        steps.append((i, i - 1, 1, mg))
        if not any_m:
            t = 0
        else:
            t = ((o[3] << 12) | (o[2] << 8) | (o[1] << 4) | o[0], tuple(steps))
        _SLIDEL[l16] = t
    return t


def _pack_grid(grid):
    """list-of-lists tile VALUES -> packed CODE int (unknown values -> 0)."""
    g = 0
    i = 0
    for row in grid:
        for v in row:
            g |= (_CODE_OF.get(v, 0) & 0xF) << (4 * i)
            i += 1
    return g


def _col16(g, k):
    """16-bit pattern of column k: nibble j = cell (j, k), j=0 top."""
    return ((g >> (4 * k)) & 0xF) | \
        (((g >> (16 + 4 * k)) & 0xF) << 4) | \
        (((g >> (32 + 4 * k)) & 0xF) << 8) | \
        (((g >> (48 + 4 * k)) & 0xF) << 12)


def _put_col_bits(l16, k):
    """Grid int carrying only column k = l16 (nibble j -> cell (j,k))."""
    return ((l16 & 0xF) << (4 * k)) | \
        ((((l16 >> 4) & 0xF) << (4 * k)) << 16) | \
        ((((l16 >> 8) & 0xF) << (4 * k)) << 32) | \
        ((((l16 >> 12) & 0xF) << (4 * k)) << 48)


def _lane_ef(g, d, k):
    """Lane for search-dir d, lane k, in engine edge-first (destination-edge
    first) order — identical to the JS CELLS[d][k] tile sequence."""
    if d == 0:                      # up: column k, top-first = grid order
        return _col16(g, k)
    if d == 1:                      # down: column k, bottom-first
        return _rev16(_col16(g, k))
    if d == 2:                      # left: row k, left-first = grid order
        return (g >> (16 * k)) & 0xFFFF
    return _rev16((g >> (16 * k)) & 0xFFFF)   # right: right-first


def _slide(g, d):
    """Engine-faithful packed slide; d in NAMES order (up,down,left,right).
    Returns (next_packed, holes) with holes ascending on the spawn edge, or
    None when illegal (no advance, or no spawn hole in a moved lane).
    Memoized on (g, d)."""
    key = (g, d)
    if key in _SLIDEM:
        return _SLIDEM[key]
    nxt = 0
    lane_mask = 0
    for k in range(4):
        l16 = _lane_ef(g, d, k)
        t = _slide_lane_forward(l16)
        if t:
            out = t[0]
            lane_mask |= 1 << k
        else:
            out = l16
        # convert edge-first output back to grid orientation
        if d == 0:
            nxt |= _put_col_bits(out, k)
        elif d == 1:
            nxt |= _put_col_bits(_rev16(out), k)
        elif d == 2:
            nxt |= out << (16 * k)
        else:
            nxt |= _rev16(out) << (16 * k)
    if not lane_mask:
        _SLIDEM[key] = None
        return None
    # holes: empty spawn-edge cells inside a MOVED lane (engine validity rule)
    if d == 0:      # spawn edge = bottom row; lane of cell 12+j is column j
        edge = (nxt >> 48) & 0xFFFF
        holes = tuple(12 + j for j in range(4)
                      if ((edge >> (4 * j)) & 0xF) == 0
                      and (lane_mask & (1 << j)))
    elif d == 1:    # top row; lane of cell j is column j
        edge = nxt & 0xFFFF
        holes = tuple(j for j in range(4)
                      if ((edge >> (4 * j)) & 0xF) == 0
                      and (lane_mask & (1 << j)))
    elif d == 2:    # right col (nibble 3 of each row); lane = row r
        holes = tuple(4 * r + 3 for r in range(4)
                      if (((nxt >> (16 * r + 12)) & 0xF) == 0
                          and (lane_mask & (1 << r))))
    else:           # left col (nibble 0 of each row); lane = row r
        holes = tuple(4 * r for r in range(4)
                      if (((nxt >> (16 * r)) & 0xF) == 0
                          and (lane_mask & (1 << r))))
    if not holes:
        _SLIDEM[key] = None
        return None
    res = (nxt, holes)
    _SLIDEM[key] = res
    return res


def _max_code(g):
    return max((_lane_info(g & 0xFFFF)[1], _lane_info((g >> 16) & 0xFFFF)[1],
                _lane_info((g >> 32) & 0xFFFF)[1],
                _lane_info((g >> 48) & 0xFFFF)[1]))


def _unpack16(g):
    c = _UNPACK.get(g)
    if c is None:
        c = tuple((g >> (4 * i)) & 0xF for i in range(16))
        _UNPACK[g] = c
    return c


# --------------------------------------------------------------------------
# evaluation (port of JS evalGrid/evalFast with TW weights)
#
# The hot evaluator is generated once at import time as one straight-line
# function over 16 nibble locals: same structure and EXACT float accumulation
# order as the JS evalGrid (left folds, precomputed per-term products), minus
# the interpretive loop overhead. _EVALC memoizes it across moves (pure fn).
# --------------------------------------------------------------------------

_T = tuple(tuple(_POWA[i] * _VALW[c] for c in range(16)) for i in range(16))
_CHA = tuple(float(a * a) if a >= 5 else float(a) for a in range(16))
_IBI = (0.0, 0.0, 0.0, -1.0, -1.0, -2.0, -2.0, -2.0, -2.0,
        -2.0, -2.0, -2.0, -2.0, -2.0, -2.0, -2.0)
_ZT = (1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)


def _build_eval_src():
    L = []
    A = L.append
    args = ", ".join(("T%d=_T[%d]" % (i, i)) for i in range(16))
    A("def _eval_raw(g, %s, CHA=_CHA, IBI=_IBI, ZT=_ZT, W=_WVEC):" % args)
    A("    x0 = g & 0xF")
    for i in range(1, 16):
        A("    x%d = (g >> %d) & 0xF" % (i, 4 * i))
    A("    em = ZT[x0] + ZT[x1] + ZT[x2] + ZT[x3] + ZT[x4] + ZT[x5]"
      " + ZT[x6] + ZT[x7] + ZT[x8] + ZT[x9] + ZT[x10] + ZT[x11]"
      " + ZT[x12] + ZT[x13] + ZT[x14] + ZT[x15]")
    A("    mt = x0")
    for i in range(1, 16):
        A("    if x%d > mt:" % i)
        A("        mt = x%d" % i)
    A("    mc = 0")
    for i in range(16):
        A("    if x%d == mt:" % i)
        A("        mc += 1")
    # snake: exact left fold 0.0 + t0 + t1 + ... (empty cells contribute 0.0)
    for p in range(8):
        path = _SNAKES[p]
        terms = " + ".join(("T%d[x%d]" % (i, path[i])) for i in range(16))
        A("    s%d = 0.0 + %s" % (p, terms))
    A("    bs = s0")
    A("    bp = 0")
    for p in range(1, 8):
        A("    if s%d > bs:" % p)
        A("        bs = s%d" % p)
        A("        bp = %d" % p)
    # monotonicity along the winning snake path
    for p in range(8):
        path = _SNAKES[p]
        A("    if bp == %d:" % p)
        A("        mo = 0.0")
        A("        pv = x%d" % path[0])
        for i in range(1, 16):
            A("        d_%d = pv - x%d" % (i, path[i]))
            A("        mo += d_%d if d_%d >= 0 else 2.2 * d_%d"
              % (i, i, i))
            A("        pv = x%d" % path[i])
    # per-cell parts in JS loop order (isolation/corners/stranded edge)
    A("    ib = 0.0")
    A("    ij = 0")
    A("    jc = 0")
    A("    eb = 0")
    A("    co = 0")
    for i in range(16):
        nb = _ADJ[i]
        A("    v = x%d" % i)
        A("    if v:")
        A("        if v >= 3:")
        eq = " or ".join(("(x%d == v)" % n) for n in nb) or "False"
        A("            if not (%s):" % eq)
        A("                ib += IBI[v]")
        if _IS_CORNER[i]:
            A("            if mt >= 3 and v == mt:")
            A("                co = 1")
        A("        else:")
        eq2 = " or ".join(("(x%d == 2)" % n) for n in nb) or "False"
        eq1 = " or ".join(("(x%d == 1)" % n) for n in nb) or "False"
        A("            if v == 1:")
        A("                if not (%s):" % eq2)
        A("                    ij -= 1")
        A("            else:")
        A("                if not (%s):" % eq1)
        A("                    ij -= 1")
        if _IS_CORNER[i]:
            A("            jc -= 1")
        if _EDGE_NONCORNER[i]:
            A("        if v >= 5:")
            A("            eb -= (v - 3)")
    # pairs (right + down neighbours) in JS order: chain / jr / sub
    A("    ch = 0.0")
    A("    jr = 0.0")
    A("    su = 1.5 if (mt >= 3 and mc >= 2) else 0.0")
    for i in range(16):
        for j in (i + 1, i + 4):
            if j >= 16 or (j == i + 1 and (i & 3) == 3):
                continue
            A("    a = x%d" % i)
            A("    b = x%d" % j)
            A("    if a and b:")
            A("        if a == b:")
            A("            if a >= 3:")
            A("                ch += CHA[a]")
            A("                if a == mt:")
            A("                    su += 0.75")
            A("                elif mt >= 4 and a == mt - 1:")
            A("                    su += 1.0")
            A("        elif (a == 1 and b == 2) or (a == 2 and b == 1):")
            A("            jr += 1.0")
    A("    return (W[0] * bs + W[1] * mo + W[2] * em"
      " + W[3] * (-float(3 - em) if em < 3 else 0.0)"
      " + W[4] * ch + W[5] * jr + W[6] * ij + W[7] * ib + W[8] * co"
      " + W[9] * su + W[10] * eb + W[11] * jc)")
    return "\n".join(L)


_NS = {"_T": _T, "_CHA": _CHA, "_IBI": _IBI, "_ZT": _ZT, "_WVEC": _WVEC}
exec(compile(_build_eval_src(), "<planner-eval-codegen>", "exec"), _NS)
_eval_raw = _NS["_eval_raw"]


def _eval_packed(g):
    v = _EVALC.get(g)
    if v is None:
        v = _eval_raw(g)
        _EVALC[g] = v
    return v


def evaluate(grid, queue=None):
    """Public eval (port of JS evalFast). `grid` = list-of-lists values.
    `queue` accepted for API parity; JS evalFast is board-only."""
    return _eval_packed(_pack_grid(grid))


# --------------------------------------------------------------------------
# expectimax core (port of bestC / valueDir / quickVal)
# --------------------------------------------------------------------------

class _Ctx:
    __slots__ = ("nodes", "budget", "memo", "memo_on", "prune_delta",
                 "evals", "memo_hits", "exhausted")

    def __init__(self, budget, memo_on, prune_delta):
        self.nodes = 0
        self.budget = budget
        self.memo = {}                  # JS: fresh Map per move (searchMove)
        self.memo_on = memo_on
        self.prune_delta = prune_delta
        self.evals = 0
        self.memo_hits = 0
        self.exhausted = 0


def _quick_val(next_packed, holes, q):
    """JS quickVal: avg leaf eval over hole placements (known/expected spawn)."""
    spawns = ((q[0], 1.0),) if q else _DIST[_max_code(next_packed)]
    tot = 0.0
    wn = 0.0
    ev = _eval_packed
    for h in holes:
        bit = 4 * h
        clear = ~(0xF << bit)
        for sc, w in spawns:
            tot += w * ev((next_packed & clear) | (sc << bit))
            wn += w
    return tot / wn if wn else -1e9


def _expansion(g, q):
    """All legal slides of g with their chance-node children + quickVal,
    pre-sorted by quick value descending (JS cand.sort). Pure function of
    (g, q[0]); memoized globally (slides/evals are pure)."""
    q0 = q[0] if q else -1
    key = (g << 5) | (q0 + 1)
    hit = _EXP.get(key, 0)
    if hit != 0:
        return hit
    out = []
    for d in range(4):
        s = _slide(g, d)
        if s is None:
            continue
        nxt, holes = s
        if q0 >= 0:
            spawns = ((q0, 1.0),)
        else:
            spawns = _DIST[_max_code(nxt)]
        kids = []
        tot = 0.0
        wn = 0.0
        for h in holes:
            bit = 4 * h
            clear = ~(0xF << bit)
            for sc, w in spawns:
                child = (nxt & clear) | (sc << bit)
                kids.append((child, w))
                tot += w * _eval_packed(child)
                wn += w
        out.append((tuple(kids), wn, tot / wn if wn else -1e9))
    if not out:
        hit = None
    else:
        if len(out) > 2:
            out.sort(key=lambda t: -t[2])
        hit = tuple(out)
    _EXP[key] = hit
    return hit


def _best_c(g, depth, q, ctx):
    """Max node: best over legal slides of the chance-node value (port of
    bestC; chance aggregation inlined as the AVG over spawn children).

    Memoization mirrors the JS exactly: a fresh dict per plan() call (JS
    builds a new Map in searchMove), consulted only for depth >= 2, and
    only AFTER the budget gate — so post-trip visits always leaf-evaluate,
    never read back a truncated value, and node accounting stays identical
    to the JS oracle (memo hits still count as one node)."""
    ctx.nodes += 1
    if depth <= 0 or ctx.nodes > ctx.budget:
        if ctx.nodes > ctx.budget:
            ctx.exhausted = 1
        ctx.evals += 1
        return _eval_packed(g)
    if ctx.memo_on and depth >= 2:      # JS memo gate: depth >= 2 only
        if len(q) <= 4:
            qq = 0
            for cc in q:
                qq = (qq << 4) | cc
            key = g | (depth << 80) | (qq << 84)
        else:  # oversized queue tail: exact tuple key, no packing collision
            key = (g, depth, q)
        memo = ctx.memo
        hit = memo.get(key)
        if hit is not None:
            ctx.memo_hits += 1
            return hit
    else:
        memo = None
        key = None
    exp = _expansion(g, q)
    if exp is None:
        best = -_BIG + depth
        if memo is not None and ctx.nodes <= ctx.budget:
            memo[key] = best
        return best
    if len(exp) > 2:
        pd = ctx.prune_delta
        if pd > 0:
            thr = exp[0][2] - pd * (exp[0][2] - exp[-1][2])
        else:
            thr = -1e18
    else:
        thr = -1e18
    best = -1e18
    i = 0
    if depth == 1:
        # children are leaves: inline bestC(child, 0) with the same node
        # accounting (one visit each; post-trip visits leaf-evaluate anyway)
        ep = _eval_packed
        nodes = ctx.nodes
        budget = ctx.budget
        ev_cnt = ctx.evals
        for kids, wn, qv_i in exp:
            if i >= 2 and qv_i < thr:
                i += 1
                continue
            tot = 0.0
            for child, w in kids:
                nodes += 1
                if nodes > budget:
                    ctx.exhausted = 1
                tot += w * ep(child)
                ev_cnt += 1
            v = tot / wn if wn else -_BIG
            if v > best:
                best = v
            i += 1
        ctx.nodes = nodes
        ctx.evals = ev_cnt
    else:
        bc = _best_c
        qn = q[1:] if q else q
        for kids, wn, qv_i in exp:
            if i >= 2 and qv_i < thr:
                i += 1
                continue
            tot = 0.0
            for child, w in kids:
                tot += w * bc(child, depth - 1, qn, ctx)
            v = tot / wn if wn else -_BIG
            if v > best:
                best = v
            i += 1
    if memo is not None and ctx.nodes <= ctx.budget:
        memo[key] = best
    return best


# --------------------------------------------------------------------------
# greedy tie-break (port of JS factsFor / greedyPick — policy.py fallback)
# --------------------------------------------------------------------------

def _steps_of(g, d):
    """Engine-style step list for slide (g, d): (cell_from, cell_to, merge,
    result_code). Lanes are edge-first; CELLS[d][k] indexes the same order."""
    steps = []
    for k in range(4):
        t = _slide_lane_forward(_lane_ef(g, d, k))
        if not t:
            continue
        cells = _CELLS[d][k]
        for (fi, ti, mg, val) in t[1]:
            steps.append((cells[fi], cells[ti], mg, val))
    return steps


def _greedy_score_for(gc, nxt, d):
    """Port of factsFor + greedyScore for one root slide (policy.py weights)."""
    c = _unpack16(gc)
    nc = _unpack16(nxt)
    mt = _max_code(gc)
    nz = [c[i] for i in range(16) if c[i]]
    worst = min(nz) if nz else None
    merges = 0
    max_merge = 0
    worst_removed = 0
    worst_comp = 0
    max_disp = 0
    for (fi, ti, mg, val) in _steps_of(gc, d):
        if c[fi] == mt:
            max_disp = 1
        if mg:
            merges += 1
            if val == mt + 1 or ((mt == 1 or mt == 2) and val == 3):
                max_merge = 1
            if c[fi] == 1 or c[fi] == 2:
                worst_removed += 1
            if c[ti] == 1 or c[ti] == 2:
                worst_removed += 1
            if worst is not None and (c[fi] == worst or c[ti] == worst):
                worst_comp = 1
    empty_after = -1
    for i in range(16):
        if not nc[i]:
            empty_after += 1
    chain_after = 0
    for i in range(16):
        a = nc[i]
        if not a or a < 3:
            continue
        if (i & 3) < 3 and nc[i + 1] == a:
            chain_after += 1
        if (i >> 2) < 3 and nc[i + 4] == a:
            chain_after += 1
    edge_worst = 0
    for cv in _EDGE[d]:
        v = nc[cv]
        if v == 1 or v == 2:
            edge_worst += 1
    return (100 * merges + 40 * max_merge + 15 * empty_after - 10 * edge_worst
            + 30 * worst_removed + 20 * worst_comp - 40 * max_disp
            + 5 * chain_after)


def _greedy_pick(gc, only_dirs, greedy_scores):
    """Port of JS greedyPick: best greedy score over dirs, DIR_PRIORITY ties."""
    best = -1e18
    best_d = None
    best_pri = 99
    for d in only_dirs:
        if _slide(gc, d) is None:
            continue
        sc = greedy_scores[d]
        pri = DIR_PRIORITY.index(_NAMES[d])
        if sc > best or (sc == best and pri < best_pri):
            best = sc
            best_d = d
            best_pri = pri
    return best_d


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

STATS = {}

_PRUNED = -(10 ** 9)        # score for root-pruned dirs (below any real line)
_ILLEGAL = -(10 ** 9) - 1   # score for dirs the compiler rejects


def _cache_guard():
    # pure caches: clearing never changes any value, only speed
    if len(_EXP) > _CACHE_CAP:
        _EXP.clear()
    if len(_EVALC) > _CACHE_CAP:
        _EVALC.clear()
    if len(_SLIDEM) > _CACHE_CAP:
        _SLIDEM.clear()
    for _tb in list(_TMEMO.values()):
        if len(_tb) > _TMEMO_CAP:
            _tb.clear()


def plan(grid, queue, valid_dirs, depth=3, node_budget=200000,
         prune_delta=0.25, memo_on=True):
    """Expectimax scores per direction (search-result facts for Jev).

    grid        : list-of-lists tile VALUES (0 = empty)
    queue       : upcoming spawn VALUES, front first (server 'next' queue)
    valid_dirs  : iterable of server-valid direction names
    depth       : expectimax plies of player moves (chance nodes between)
    node_budget : hard cap on node visits; once tripped, positions are leaf-
                  evaluated where they stand (STATS['budget_exhausted'] = 1)
    prune_delta : quick-value pruning band; 0.25 = ex-t-d3 recipe,
                  0 = full width (ex-t-d5-np recipe)
    memo_on     : transposition-table on/off

    Returns {dir: int(value * 100)} for every requested dir; comparable
    across the dirs of one call. Never raises (all-zero dict on malformed
    states). Ties within the JS 1e-9 epsilon are broken by the policy.py
    greedy score, and scores are biased so argmax == JS oracle pick; then
    rank_options(valid_dirs, scores) gives ranks 1..n.
    """
    try:
        dirs = [d for d in (valid_dirs or ()) if d in _DIR_IDX]
        out = {d: 0 for d in dirs}
        if not dirs:
            return out
        _cache_guard()
        gc = _pack_grid(grid)
        if gc == 0:                     # empty board: nothing to plan
            return out
        q = tuple(_CODE_OF.get(v, 0) for v in (queue or ()))
        ctx = _Ctx(node_budget, memo_on, prune_delta)
        t0 = time.perf_counter()

        # root slides through the validated compiler mirror (engine-faithful)
        cands = []
        for d in range(4):
            name = _NAMES[d]
            if name not in dirs:
                continue
            sim = _simulate_slide(grid, name)
            if sim is None:
                out[name] = _ILLEGAL
                continue
            nxt = _pack_grid(sim["next"])
            holes = tuple(int(r) * 4 + int(c) for (r, c) in sim["holes"])
            cands.append([d, nxt, holes, _quick_val(nxt, holes, q), False])
        if not cands:
            STATS.update(nodes=0, evals=0, memo_hits=0, budget_exhausted=0,
                         time_s=time.perf_counter() - t0, depth=depth)
            return out

        mt = _max_code(gc)
        for c in cands:                 # mergesMax root ordering bonus (JS)
            for (_fi, _ti, mg, val) in _steps_of(gc, c[0]):
                if mg and val == mt + 1:
                    c[4] = True
        cands.sort(key=lambda c: (0 if c[4] else 1, -c[3]))  # maxmerge, qv desc

        thr = -1e18
        if prune_delta > 0 and len(cands) > 2:
            top = cands[0][3]
            floor_q = min(c[3] for c in cands)
            thr = top - prune_delta * (top - floor_q)

        vals = []                       # (dir_idx, value | None if pruned)
        best = -1e18
        qn = q[1:] if q else q
        q0 = q[0] if q else None
        for i, c in enumerate(cands):
            d, nxt, holes, qv, mmax = c
            if (not mmax) and i >= 2 and qv < thr:
                vals.append((d, None))
                continue
            spawns = ((q0, 1.0),) if q0 is not None else _DIST[_max_code(nxt)]
            tot = 0.0
            wn = 0.0
            for h in holes:
                bit = 4 * h
                clear = ~(0xF << bit)
                for sc, w in spawns:
                    tot += w * _best_c((nxt & clear) | (sc << bit),
                                       depth - 1, qn, ctx)
                    wn += w
            v = tot / wn if wn else -_BIG
            vals.append((d, v))
            if v > best:
                best = v

        # int scores: value*100 floored, groups strictly ordered so plain
        # argmax == float argmax; search ties (<= 1e-9 below best, the JS
        # epsilon) additionally biased to the greedyPick winner.
        for d, v in vals:
            if v is None:
                out[_NAMES[d]] = _PRUNED
        order = sorted(((v, d) for d, v in vals if v is not None),
                       key=lambda t: -t[0])
        groups = []
        grp = []
        for v, d in order:
            if grp and v < grp[0][0] - 1e-9:
                groups.append(grp)
                grp = []
            grp.append((v, d))
        if grp:
            groups.append(grp)
        nxt_of = {c[0]: c[1] for c in cands}
        prev_floor = None
        for grp in groups:
            nat = int(grp[0][0] * 100)
            if prev_floor is not None and nat > prev_floor - 1:
                nat = prev_floor - 1
            if len(grp) == 1:
                out[_NAMES[grp[0][1]]] = nat
                prev_floor = nat
            else:
                td = [d for _v, d in grp]
                greedy_scores = {d: _greedy_score_for(gc, nxt_of[d], d)
                                 for d in td}
                pick = _greedy_pick(gc, td, greedy_scores)
                rank = [d for d in td if d != pick]
                rank.sort(key=lambda d: (-greedy_scores[d],
                                         DIR_PRIORITY.index(_NAMES[d])))
                out[_NAMES[pick]] = nat
                for pos, d in enumerate(rank, start=1):
                    out[_NAMES[d]] = nat - pos
                prev_floor = nat - (len(grp) - 1)

        dt = time.perf_counter() - t0
        STATS.update(nodes=ctx.nodes, evals=ctx.evals,
                     memo_hits=ctx.memo_hits, budget_exhausted=ctx.exhausted,
                     time_s=dt, depth=depth)
        return out
    except Exception:
        return {d: 0 for d in (valid_dirs or ()) if d in _DIR_IDX}


def rank_options(valid_dirs, scores):
    """Rank directions 1..n: score desc, ties DIR_PRIORITY (up,left,down,right).

    Index 0 (rank 1) is the planner's chosen direction: plan() biases tied
    scores so argmax here equals the JS oracle tie-break. Dirs missing from
    `scores` rank last (DIR_PRIORITY order among themselves).
    """
    def key(d):
        pri = DIR_PRIORITY.index(d) if d in DIR_PRIORITY else 99
        if d in scores:
            return (0, -scores[d], pri)
        return (1, 0, pri)

    return sorted(valid_dirs, key=key)


def self_time():
    """Diagnostics of the most recent plan() call: time_s, nodes, evals,
    memo_hits, budget_exhausted, depth."""
    return dict(STATS)
