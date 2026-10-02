"""State compiler for the Jev Threes bot (plan D5).

Jev cannot count or do arithmetic, so ALL facts handed to it are precomputed
here in code. The raw `grid` is NEVER included in the output (AC 2b: no key
named "grid" anywhere in the compiled dict).

Python 3.9 stdlib only.
"""

SIZE = 4
DIRS = ("up", "left", "down", "right")

_CORNERS = ((0, 0), (0, SIZE - 1), (SIZE - 1, 0), (SIZE - 1, SIZE - 1))


def merge_result(a, b):
    """Mirror of lib/engine.js mergeResult: 1+2->3, v+v->2v for v = 3*2^k."""
    if a == 0 or b == 0:
        return None
    if (a == 1 and b == 2) or (a == 2 and b == 1):
        return 3
    if a == b and a >= 3 and _is_three_tile(a):
        return 2 * a
    return None


def _is_three_tile(v):
    if v < 3 or v % 3 != 0:
        return False
    v = v // 3
    return (v & (v - 1)) == 0


def _lane_cells(grid, dir):
    """Cells ordered from the destination edge inward (mirror of engine)."""
    cells = []
    for k in range(SIZE):
        if dir == "left":
            lane = [[k, c] for c in range(SIZE)]
        elif dir == "right":
            lane = [[k, SIZE - 1 - c] for c in range(SIZE)]
        elif dir == "up":
            lane = [[r, k] for r in range(SIZE)]
        else:  # down
            lane = [[SIZE - 1 - r, k] for r in range(SIZE)]
        cells.append(lane)
    return cells


def simulate_slide(grid, dir):
    """Trial-apply a slide. Returns None if the move is illegal, else
    {steps, moved_lanes, next} mirroring the server engine's _simulate().
    """
    steps = []
    moved_lanes = set()
    for lane in _lane_cells(grid, dir):
        moved = [False] * SIZE
        lane_moved = False
        for i in range(1, SIZE):
            fr, fc = lane[i]
            f = grid[fr][fc]
            if f == 0:
                continue
            tr, tc = lane[i - 1]
            t = grid[tr][tc]
            if moved[i - 1]:
                steps.append((fr, fc, tr, tc, False, f))
                moved[i] = True
                lane_moved = True
            elif t == 0:
                steps.append((fr, fc, tr, tc, False, f))
                moved[i] = True
                lane_moved = True
            else:
                m = merge_result(t, f)
                if m is not None:
                    steps.append((fr, fc, tr, tc, True, m))
                    moved[i] = True
                    lane_moved = True
        if lane_moved:
            # lane index k is the column for up/down, the row for left/right
            moved_lanes.add(lane[0][1] if dir in ("up", "down") else lane[0][0])
    if not steps:
        return None
    next_grid = [row[:] for row in grid]
    for (fr, fc, tr, tc, _mg, val) in steps:
        next_grid[fr][fc] = 0
        next_grid[tr][tc] = val
    # spawn must land on the trailing edge WITHIN A MOVED LANE (engine rule)
    holes = []
    for i in range(SIZE):
        if dir == "up":
            r, c = SIZE - 1, i
        elif dir == "down":
            r, c = 0, i
        elif dir == "left":
            r, c = i, SIZE - 1
        else:
            r, c = i, 0
        if next_grid[r][c] == 0:
            lane_idx = c if dir in ("up", "down") else r
            if lane_idx in moved_lanes:
                holes.append((r, c))
    if not holes:
        return None
    return {"steps": steps, "moved_lanes": moved_lanes, "next": next_grid,
            "holes": holes}


def _opposite_edge_cells(dir):
    """Cells of the edge where the new tile spawns (opposite the swipe)."""
    cells = []
    for i in range(SIZE):
        if dir == "up":
            cells.append((SIZE - 1, i))     # bottom row
        elif dir == "down":
            cells.append((0, i))             # top row
        elif dir == "left":
            cells.append((i, SIZE - 1))      # right col
        else:
            cells.append((i, 0))             # left col
    return cells


def _worst_cell(grid):
    """(row, col) of the lowest non-zero tile, row-major first; None if empty."""
    vals = [v for row in grid for v in row if v]
    if not vals:
        return None
    lo = min(vals)
    for r in range(SIZE):
        for c in range(SIZE):
            if grid[r][c] == lo:
                return (r, c)
    return None


def _chain_pairs_after(next_grid):
    """Adjacent equal 3*2^k pairs in the post-slide grid (pre-spawn).

    Pairs merged this slide are inherently excluded: the merged tile is a
    single cell in next_grid. Horizontal + vertical adjacency.
    """
    n = 0
    for r in range(SIZE):
        for c in range(SIZE):
            v = next_grid[r][c]
            if v >= 3 and _is_three_tile(v):
                if c + 1 < SIZE and next_grid[r][c + 1] == v:
                    n += 1
                if r + 1 < SIZE and next_grid[r + 1][c] == v:
                    n += 1
    return n


def _max_tile_cell(grid, max_tile):
    """(row, col) of the max tile, first in row-major order; None if empty."""
    if not max_tile:
        return None
    for r in range(SIZE):
        for c in range(SIZE):
            if grid[r][c] == max_tile:
                return (r, c)
    return None


def _pin_dirs(pos):
    """Directions whose swipe can never displace a tile sitting at ``pos``:
    swiping toward an edge the tile already occupies leaves that tile in
    place (corner (0,0) -> up,left; middle -> []). Order: up,left,down,right.
    """
    if pos is None:
        return []
    r, c = pos
    dirs = []
    if r == 0:
        dirs.append("up")
    if c == 0:
        dirs.append("left")
    if r == SIZE - 1:
        dirs.append("down")
    if c == SIZE - 1:
        dirs.append("right")
    return dirs


def _isolated_big_after(next_grid):
    """Tiles v >= 3 with NO orthogonally adjacent merge partner in the
    post-slide grid (pre-spawn). For v >= 3 the only partner is an adjacent
    equal value (equal double, lib/engine.js); 1s and 2s are never counted.
    """
    n = 0
    for r in range(SIZE):
        for c in range(SIZE):
            v = next_grid[r][c]
            if v < 3:
                continue
            if ((r > 0 and next_grid[r - 1][c] == v) or
                    (r + 1 < SIZE and next_grid[r + 1][c] == v) or
                    (c > 0 and next_grid[r][c - 1] == v) or
                    (c + 1 < SIZE and next_grid[r][c + 1] == v)):
                continue
            n += 1
    return n


def _second_max_pair_exists(grid, next_grid):
    """True when the post-slide layout carries >=2 cells of the board's
    second-largest distinct tile value — the feed for the next big merge.

    The value is picked from the PRE-slide board (the slide itself can
    change what the max is); when the board holds only one distinct value,
    that same value is used and we ask for a fresh pair of it post-slide.
    """
    vals = set(v for row in grid for v in row if v)
    if not vals:
        return False
    top = max(vals)
    below = [v for v in vals if v < top]
    second = max(below) if below else top
    return sum(1 for row in next_grid for v in row if v == second) >= 2


def _advance_possible(grid, dir):
    """True when at least one tile can ADVANCE in ``dir``: slide into an
    empty cell or merge. Pure lane-geometry mirror of lib/engine.js
    _laneSteps minus the spawn-edge-empty gate (that gate is already
    satisfied once the forced spawn has landed, so a post-spawn board's
    survival is exactly "some lane can advance").
    """
    for lane in _lane_cells(grid, dir):
        moved = [False] * SIZE
        for i in range(1, SIZE):
            fr, fc = lane[i]
            f = grid[fr][fc]
            if f == 0:
                continue
            tr, tc = lane[i - 1]
            t = grid[tr][tc]
            if moved[i - 1] or t == 0 or merge_result(t, f) is not None:
                return True
    return False


def _min_wiggles_after_spawn(next_grid, dir, spawn_value):
    """Worst-case survival count after the forced spawn of this swipe.

    Enumerates every cell the engine COULD drop the next tile on (each
    empty cell of the edge opposite the swipe — the same cell set as
    ``_opposite_edge_cells(dir)`` intersected with empties of the
    post-slide board), places ``spawn_value`` there, and counts how many
    of the four directions still have at least one tile able to advance
    (a "wiggle"). Returns the MINIMUM over placements — the worst-case
    random spawn: 0 = next-turn death trap, 1 = knife edge, 3+ = safe.
    None when no placement cell exists (cannot happen for a legal move).
    """
    worst = None
    for (r, c) in _opposite_edge_cells(dir):
        if next_grid[r][c] != 0:
            continue
        g = [row[:] for row in next_grid]
        g[r][c] = spawn_value
        wiggles = sum(1 for d in DIRS if _advance_possible(g, d))
        worst = wiggles if worst is None else min(worst, wiggles)
    return worst


def _second_max_count(grid, next_grid):
    """Count of cells carrying the board's second-largest DISTINCT tile
    value on the post-slide layout (pre-spawn).

    The value is picked from the PRE-slide board exactly like
    ``_second_max_pair_exists``; when the board holds only one distinct
    value, that value's own post-slide count is returned. 1 = a lone
    runner-up waiting for its partner, 2+ = merge-ready.
    """
    vals = set(v for row in grid for v in row if v)
    if not vals:
        return 0
    top = max(vals)
    below = [v for v in vals if v < top]
    second = max(below) if below else top
    return sum(1 for row in next_grid for v in row if v == second)


def compile_state(state):
    """compile_state(state) -> facts dict (pure, deterministic, no `grid`).

    Board facts: max_tile, max_tile_pos, max_tile_pin_dirs, score,
    empty_count, corner_values, worst_tile_count, worst_tile_value.
    Per legal direction (only from state["validMoves"]): moves, merges,
    merge_counts_by_value, max_tile_mergeable, empty_before, empty_after,
    new_worst_row, new_worst_col, sum_of_edges_opposite, edge_worst_count,
    next_spawn_value, spawn_edge_empties, spawn_edge_has_big,
    isolated_big_after, second_max_pair_exists, pins_max_tile, anchor_kept,
    min_wiggles_after_spawn, second_max_count.

    min_wiggles_after_spawn: worst case over every possible landing cell
    of the forced next tile (post-slide, empty cells of the spawn edge),
    of how many directions can still advance a tile on the spawned board
    — the congestion signal: 0 = death trap next turn, 1 = knife edge,
    3+ = safe. second_max_count: cells of the second-largest distinct
    tile value left after the slide (count of that value itself when the
    board carries only one distinct value).

    pins_max_tile: True when this swipe is one of the board-level
    max_tile_pin_dirs (computed from the PRE-slide max-tile position), i.e.
    it can never drag the max tile off its cell. Empty board -> False.
    anchor_kept: True when the max tile ends the slide anchored: a merge
    step involves a max-tile cell, or the tile does not move at all and
    already sits on a corner cell. A board whose max tile is below 3 has
    nothing to anchor yet, so anchor_kept is trivially True there.
    max_tile_corner_after: True when the max tile's POST-slide cell (its
    merge destination if it merged — the merged tile counts as the max
    tile's cell even though its value grew) is a corner. Max tile < 3 ->
    True (nothing to anchor yet).
    Returns {board, options, board_text}. `board_text` is a plain ASCII
    rendering of the board (4 rows of 4 cell values, left-to-right, single
    spaces, zeros as `.`) so the Jev fast path can SEE the spatial layout. It
    is a visual aid only: Jev still cannot count or do arithmetic, so every
    number it needs stays precomputed above.
    """
    grid = state["grid"]
    board_text = "\n".join(" ".join("." if v == 0 else str(v) for v in row)
                           for row in grid)
    max_tile = max([v for row in grid for v in row] or [0])
    empty_before = sum(1 for row in grid for v in row if v == 0)
    worst_vals = [v for row in grid for v in row if v]
    worst_val = min(worst_vals) if worst_vals else None
    mt_pos = _max_tile_cell(grid, max_tile)
    board = {
        "max_tile": max_tile,
        "max_tile_pos": (list(mt_pos) if mt_pos else None),
        "max_tile_pin_dirs": _pin_dirs(mt_pos),
        "score": state["score"],
        "empty_count": empty_before,
        "corner_values": [grid[0][0], grid[0][SIZE - 1],
                          grid[SIZE - 1][0], grid[SIZE - 1][SIZE - 1]],
        "worst_tile_count": (worst_vals.count(worst_val) if worst_val else 0),
        "worst_tile_value": worst_val,
    }
    queue = state.get("queue") or []
    next_spawn = queue[0] if queue else None
    options = {}
    for d in state.get("validMoves", []):
        sim = simulate_slide(grid, d)
        if sim is None:
            # server says legal but our mirror disagrees — omit facts; the
            # fallback chooser still handles the direction safely
            continue
        steps = sim["steps"]
        next_grid = sim["next"]
        merge_counts = {}
        max_mergeable = False
        for (fr, fc, tr, tc, mg, val) in steps:
            if mg:
                merge_counts[val] = merge_counts.get(val, 0) + 1
                if val == 2 * max_tile or (max_tile in (1, 2) and val == 3):
                    max_mergeable = True
        empty_after = sum(1 for row in next_grid for v in row if v == 0) - 1  # minus spawn
        edge = _opposite_edge_cells(d)
        edge_sum = sum(next_grid[r][c] for (r, c) in edge)
        edge_worst = sum(1 for (r, c) in edge if next_grid[r][c] in (1, 2))
        wc = _worst_cell(next_grid)
        # --- C2 facts -----------------------------------------------------
        # worst tiles removed: count of 1/2 tiles consumed by merges in the
        # slide (origins: the moving cell AND the destination cell values)
        worst_removed = 0
        worst_companion = False
        max_displaced = False
        for (fr, fc, tr, tc, mg, val) in steps:
            if grid[fr][fc] == max_tile:
                max_displaced = True
            if mg:
                if grid[fr][fc] in (1, 2):
                    worst_removed += 1
                if grid[tr][tc] in (1, 2):
                    worst_removed += 1
                if worst_val is not None and (grid[fr][fc] == worst_val
                                              or grid[tr][tc] == worst_val):
                    worst_companion = True
        # --- C3 facts -----------------------------------------------------
        # spawn-edge flexibility + stranded big tiles, judged on the post-slide
        # layout (pre-spawn), where the engine forces the next tile in
        spawn_empties = sum(1 for (r, c) in edge if next_grid[r][c] == 0)
        spawn_big = any(next_grid[r][c] > 6 for (r, c) in edge)
        # --- anchor facts (per-option max-tile pinning) --------------------
        # pins_max_tile: this swipe is one of the board-level pin dirs for
        # the CURRENT (pre-slide) max-tile position, so it can never drag
        # the max tile off its cell. Empty board -> no pin dirs -> False.
        pins_max = d in board["max_tile_pin_dirs"]
        # --- anchor_kept / max_tile_corner_after ---------------------------
        # follow the max tile (first row-major copy) through the slide:
        # a tile moves at most one cell, and a merge step lands it on the
        # merged cell (the stationary destination when it merges INTO it).
        mt_after = mt_pos
        mt_merged = False
        for (fr, fc, tr, tc, mg, _v) in steps:
            if (fr, fc) == mt_pos:
                mt_after = (tr, tc)
                if mg:
                    mt_merged = True
            elif mg and (tr, tc) == mt_pos:
                mt_merged = True
        # anchor_kept: the max tile ends the slide anchored — it merged, or
        # it never moved at all and already sat on a corner. Max tile < 3
        # means there is nothing to anchor yet, so it is trivially True.
        # max_tile_corner_after: the max tile's post-slide cell is a corner
        # (the merged cell counts even though the merged value is bigger);
        # also trivially True for a max tile < 3.
        if max_tile < 3:
            anchor_kept = True
            corner_after = True
        else:
            anchor_kept = mt_merged or (mt_after == mt_pos
                                        and mt_pos in _CORNERS)
            corner_after = mt_after in _CORNERS
        options[d] = {
            "moves": len(steps),
            "merges": len([s for s in steps if s[4]]),
            "merge_counts_by_value": merge_counts,
            "max_tile_mergeable": max_mergeable,
            "empty_before": empty_before,
            "empty_after": empty_after,
            "new_worst_row": (wc[0] if wc else None),
            "new_worst_col": (wc[1] if wc else None),
            "sum_of_edges_opposite": edge_sum,
            "edge_worst_count": edge_worst,
            "next_spawn_value": next_spawn,
            "worst_tiles_removed": worst_removed,
            "worst_tile_companion": worst_companion,
            "max_tile_displaced": max_displaced,
            "chain_pairs_after": _chain_pairs_after(next_grid),
            "spawn_edge_empties": spawn_empties,
            "spawn_edge_has_big": spawn_big,
            "isolated_big_after": _isolated_big_after(next_grid),
            "second_max_pair_exists": _second_max_pair_exists(grid, next_grid),
            "pins_max_tile": pins_max,
            "anchor_kept": anchor_kept,
            "max_tile_corner_after": corner_after,
            "min_wiggles_after_spawn": _min_wiggles_after_spawn(
                next_grid, d, next_spawn),
            "second_max_count": _second_max_count(grid, next_grid),
        }
    return {"board": board, "options": options, "board_text": board_text}


def criterion_line(dir, facts):
    """One-line factual summary for the Jev choice question (D10)."""
    parts = []
    # Decisive facts FIRST: the fast model weighs the start of the line
    # most, so the max-tile anchoring block leads.
    parts.append("max-tile merge %s" % ("YES" if facts["max_tile_mergeable"] else "no"))
    pm = facts["pins_max_tile"]
    parts.append("pins max tile %s" %
                 ("n/a" if pm is None else ("YES" if pm else "no")))
    md = facts["max_tile_displaced"]
    parts.append("max tile displaced %s" %
                 ("n/a" if md is None else ("YES" if md else "no")))
    ak = facts["anchor_kept"]
    parts.append("anchor kept %s" %
                 ("n/a" if ak is None else ("YES" if ak else "no")))
    bc = facts["max_tile_corner_after"]
    parts.append("big tile in corner after %s" %
                 ("n/a" if bc is None else ("YES" if bc else "no")))
    parts.append("%d low tiles (1/2) removed, worst tile has partner %s" %
                 (facts["worst_tiles_removed"],
                  "YES" if facts["worst_tile_companion"] else "no"))
    if facts["merges"]:
        mc = ", ".join("%d x%d" % (v, c)
                       for (v, c) in sorted(facts["merge_counts_by_value"].items()))
        parts.append("%d merges (%s)" % (facts["merges"], mc))
    else:
        parts.append("0 merges")
    parts.append("empties %d->%d" % (facts["empty_before"], facts["empty_after"]))
    # --- C3 lines (spawn flexibility, stranding, next-big feed) ----------
    se = facts["spawn_edge_empties"]
    parts.append("spawn-edge empties %s" % ("n/a" if se is None else se))
    parts.append("spawn-edge sum %d, low tiles there %d" %
                 (facts["sum_of_edges_opposite"], facts["edge_worst_count"]))
    sb = facts["spawn_edge_has_big"]
    parts.append("big tile stranded on spawn edge %s" %
                 ("n/a" if sb is None else ("YES" if sb else "no")))
    parts.append("next spawn %s" % facts["next_spawn_value"])
    mw = facts["min_wiggles_after_spawn"]
    parts.append("worst-case spawn wiggles %s" %
                 ("n/a" if mw is None else mw))
    parts.append("%d mergeable equal pairs left after" % facts["chain_pairs_after"])
    sp = facts["second_max_pair_exists"]
    smc = facts["second_max_count"]
    parts.append("second-biggest tile %s on board %s" %
                 ("n/a" if smc is None else "x%d" % smc,
                  "n/a" if sp is None else ("YES" if sp else "no")))
    ib = facts["isolated_big_after"]
    parts.append("%s isolated big tiles left after" % ("n/a" if ib is None else ib))
    parts.append("%d tiles move" % facts["moves"])
    if facts["new_worst_row"] is not None:
        parts.append("worst tile lands r%d c%d" % (facts["new_worst_row"], facts["new_worst_col"]))
    return "%s: %s" % (dir, ", ".join(parts))
