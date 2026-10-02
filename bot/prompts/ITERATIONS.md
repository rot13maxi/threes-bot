# Prompt iteration log — Jev-only Threes bot

Eval protocol: `python3 bench.py --instructions prompts/<file> --seeds 101-120 --workers 8 --tag <tag>`
(20 fixed seeded games, win := max_tile ≥ 96, target > 60%). Jev-only: no
confidence gate, no fallback policy (lastresort legal-direction stub only on
two consecutive unusable Jev answers; every run so far had 0 lastresorts).

| iter | prompt file | hypothesis | wins/20 | best tile | run file |
|---|---|---|---|---|---|
| 00 | iter-00-base.txt | verbatim legacy in-code instructions (greedy-flavored rules) | 0 | 48 | bench-iter-00-20261001-125132 |
| 01 | iter-01-rules.txt | explicit rules + fact names, win-path first | 2 | 96 | bench-iter-01-20261001-125903 |
| 01b | iter-01-rules.txt | repeatability rerun | 2 | 96 | bench-iter-01b-20261001-140656 |
| 02 | iter-02-survival.txt | survival-first rule ladder (v1 of position facts) | 0 | 48 | bench-iter-02-20261001-140258 |
| 03 | iter-03-pin.txt | compact pin/anchor rules (new per-option pin facts) | 2 | 96 | bench-iter-03-20261001-153732 |
| 04 | iter-04-corner.txt | corner-after priority + displaced-eliminate | 0 | 48 | bench-iter-04-20261001-153815 |
| 05 | iter-05-commit.txt | commit to the two directions pinning the max tile (board_text) | 2 | 96 | bench-iter-05-20261001-154435 |
| 06 | iter-06-minimal.txt | minimal 5-fact strict ladder | 0 | 48 | bench-iter-06-20261001-155034 |
| 08b | iter-08b-noladder.txt | search-distilled recipe as strict fact ladder (chain pairs rule 3, no planner) | 2 | 96 | bench-iter-08b-20261001-234154 |
| 07 | iter-07-wiggle.txt | strict ladder incl. worst-case spawn wiggles survival rule | 2 (first 192 tile) | 192 | bench-iter-07-20261002-030923 |

Reference points (same seeds, offline engines not using Jev): deterministic
greedy policy 3/20; scripted pair-locked policies 0/20; JS expectimax
depth-3 tuned (ex-t-d3) 12/20, depth-5 tuned (ex-t-d5-np) 13/20 (best tile
384); MC rollouts 11/20; top-3 union 14/20; all-configs union 15/20 — seeds
102, 103, 107, 110, 114 never won by any strategy (hard/capped at 48 under
deep search), 115 soft-hard. Practical ceiling for a single non-search policy
with per-move facts: ~6/20; a rule-following Jev needs planner-derived facts.
