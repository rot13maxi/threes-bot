"""Deterministic fallback policy (plan D6).

fallback_score(dir) = 100*merges + 40*max_tile_merge + 15*empty_after
                      - 10*edge_worst_penalty
                      + 30*worst_tiles_removed + 20*worst_tile_companion
                      - 40*max_tile_displaced + 5*chain_pairs_after   (C2)

where edge_worst_penalty = number of 1/2-tiles on the spawn edge opposite the
swipe (compiler fact ``edge_worst_count``). Tie-break by fixed direction
priority: up, left, down, right. Identical board => identical choice, always.

Python 3.9 stdlib only.
"""

# fixed tie-break priority (D6)
DIR_PRIORITY = ("up", "left", "down", "right")


def fallback_score(dir, facts):
    """Score one legal direction from its compiled facts dict."""
    return (100 * facts["merges"]
            + 40 * (1 if facts["max_tile_mergeable"] else 0)
            + 15 * facts["empty_after"]
            - 10 * facts["edge_worst_count"]
            + 30 * facts.get("worst_tiles_removed", 0)
            + 20 * (1 if facts.get("worst_tile_companion") else 0)
            - 40 * (1 if facts.get("max_tile_displaced") else 0)
            + 5 * facts.get("chain_pairs_after", 0))


def choose_fallback(dirs, facts_by_dir):
    """Pick the best-scoring direction among ``dirs`` (must be legal moves).

    ``facts_by_dir`` maps direction -> compiled option facts. Directions
    without facts (compiler disagreed with server validity) sort last among
    the provided dirs but remain eligible (server is authoritative).
    Tie-break: DIR_PRIORITY order, independent of dict iteration order.
    """
    ranked = []
    for idx, d in enumerate(DIR_PRIORITY):
        if d not in dirs:
            continue
        facts = facts_by_dir.get(d)
        if facts is None:
            score = float("-inf")
        else:
            score = fallback_score(d, facts)
        ranked.append((score, DIR_PRIORITY.index(d), d))
    if not ranked:
        raise ValueError("no legal directions to choose from")
    ranked.sort(key=lambda t: (-t[0], t[1]))
    return ranked[0][2]
