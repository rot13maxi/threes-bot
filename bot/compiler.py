"""State compiler for the Jev Threes bot (plan D5).

Jev cannot count or do arithmetic, so ALL facts handed to it are precomputed
here in code. The raw `grid` is NEVER included in the output (AC 2b: no key
named "grid" anywhere in the compiled dict).

Python 3.9 stdlib only.
"""

SIZE = 4
DIRS = ("up", "left", "down", "right")


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


def compile_state(state):
    """compile_state(state) -> facts dict (pure, deterministic, no `grid`).

    Board facts: max_tile, score, empty_count, corner_values, worst_tile_count.
    Per legal direction (only from state["validMoves"]): moves, merges,
    merge_counts_by_value, max_tile_mergeable, empty_before, empty_after,
    new_worst_row, new_worst_col, sum_of_edges_opposite, edge_worst_count,
    next_spawn_value.
    """
    grid = state["grid"]
    max_tile = max([v for row in grid for v in row] or [0])
    empty_before = sum(1 for row in grid for v in row if v == 0)
    worst_vals = [v for row in grid for v in row if v]
    worst_val = min(worst_vals) if worst_vals else None
    board = {
        "max_tile": max_tile,
        "score": state["score"],
        "empty_count": empty_before,
        "corner_values": [grid[0][0], grid[0][SIZE - 1],
                          grid[SIZE - 1][0], grid[SIZE - 1][SIZE - 1]],
        "worst_tile_count": (worst_vals.count(worst_val) if worst_val else 0),
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
        }
    return {"board": board, "options": options}


def criterion_line(dir, facts):
    """One-line factual summary for the Jev choice question (D10)."""
    parts = []
    parts.append("%d tiles move" % facts["moves"])
    if facts["merges"]:
        mc = ", ".join("%d x%d" % (v, c)
                       for (v, c) in sorted(facts["merge_counts_by_value"].items()))
        parts.append("%d merges (%s)" % (facts["merges"], mc))
    else:
        parts.append("0 merges")
    parts.append("empties %d->%d" % (facts["empty_before"], facts["empty_after"]))
    parts.append("max-tile merge %s" % ("YES" if facts["max_tile_mergeable"] else "no"))
    if facts["new_worst_row"] is not None:
        parts.append("worst tile lands r%d c%d" % (facts["new_worst_row"], facts["new_worst_col"]))
    parts.append("spawn-edge sum %d, low tiles there %d" %
                 (facts["sum_of_edges_opposite"], facts["edge_worst_count"]))
    parts.append("next spawn %s" % facts["next_spawn_value"])
    parts.append("%d low tiles (1/2) removed, worst tile has partner %s" %
                 (facts["worst_tiles_removed"],
                  "YES" if facts["worst_tile_companion"] else "no"))
    parts.append("max tile displaced %s" %
                 ("YES" if facts["max_tile_displaced"] else "no"))
    parts.append("%d mergeable equal pairs left after" % facts["chain_pairs_after"])
    return "%s: %s" % (dir, ", ".join(parts))
