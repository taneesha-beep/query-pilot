# The working set, the reserve set and the smoke set

Three committed ID lists, one seed, drawn from the 1,034-task frame fixed in
[SUBSTRATE.md](SUBSTRATE.md). The files hold task IDs only; everything else here is
regenerable by `uv run python scripts/make_splits.py`.

| File | Size | What it is for |
|---|---|---|
| [`splits/working.json`](../splits/working.json) | 150 | Every intermediate number in this project. |
| [`splits/reserve.json`](../splits/reserve.json) | 150 | Read **once**, after Phase 7, and never before — by run `20260913-100923-4a20e9`, 2026-09-13. |
| [`splits/smoke.json`](../splits/smoke.json) | 15 | Development iteration. **No number from it is ever reported.** |
| [`splits/frame.json`](../splits/frame.json) | 1,034 | The frame itself: task ID to database and difficulty. |

Seed: **20260906**, committed in `src/query_pilot/splits.py`.

## The smoke set exists to protect the quota

Free tiers refuse requests rather than charge money, so a broken agent loop discovered on
task 90 of 150 costs a day of measurement. The smoke set is 15 tasks drawn from inside the
working set — inside, so that iterating on it can never touch the reserve set — carrying
the working set's difficulty mix in miniature. Debug against those 15. Report nothing from
them.

## Difficulty is the stratum

Spider ships no difficulty field. The label is computed from the parsed SQL already in
`dev.json` by [`src/query_pilot/difficulty.py`](../src/query_pilot/difficulty.py), which
implements the rule the dataset's authors apply in their evaluation script.

**That implementation was differential-tested against the canonical one on all 1,034 dev
items and disagreed on zero of them**, after which the temporarily vendored copy was
deleted. Two oddities in the reference rule are reproduced deliberately and named in
comments; the labels have to be the canonical ones or this project's by-difficulty numbers
would not be comparable to anyone else's.

The frame's difficulty distribution: **easy 248, medium 446, hard 174, extra 166**.

Difficulty is the stratum because difficulty is a **reported dimension** — accuracy is
broken out by it in the README — and a set that misrepresents the difficulty mix would
misreport the headline.

## Database is a balance constraint, not a stratum

Twenty databases crossed with four difficulties gives eighty cells over a 300-task draw.
Cells that small are mostly rounding, and stratifying on them would buy precision the
sample size cannot support. So each difficulty's allocation is spread across databases in
proportion to what that difficulty offers, and the result is reported rather than assumed.

Allocation is largest-remainder throughout, with ties broken by key order so the draw
depends on nothing but its arguments. Three rules shape it:

1. **The two sets share one difficulty allocation**, so their difficulty mixes are
   identical by construction rather than close by luck.
2. **Cells are dealt alternately**, and the leftover task in an odd cell goes to each set
   in turn. Always handing it to the working set left that set well over its allocation,
   and correcting that afterwards skewed the database spread.
3. **A database drawn from at all gets at least two tasks in that cell**, so it can reach
   both sets. `real_estate_properties` has four questions in all of dev; at this sample
   size it is apportioned one, which would put it in one set and not the other and leave
   the two sets measuring a different number of databases. It costs one task from the
   largest cell.

## Achieved difficulty mix

Identical by construction, and asserted by a test.

| Set | easy | medium | hard | extra |
|---|---|---|---|---|
| working | 36 | 65 | 25 | 24 |
| reserve | 36 | 65 | 25 | 24 |
| smoke | 4 | 7 | 2 | 2 |

## Achieved database spread

Not identical, and not claimed to be. **Eleven of twenty databases land on exactly the same
count in both sets; the largest gap is four tasks, on `car_1`.** All twenty databases
appear in both sets.

| db_id | working | reserve | difference |
|---|---|---|---|
| battle_death | 3 | 3 | 0 |
| car_1 | 14 | 10 | 4 |
| concert_singer | 7 | 8 | 1 |
| course_teach | 4 | 4 | 0 |
| cre_Doc_Template_Mgt | 11 | 12 | 1 |
| dog_kennels | 11 | 11 | 0 |
| employee_hire_evaluation | 6 | 6 | 0 |
| flight_2 | 11 | 11 | 0 |
| museum_visit | 4 | 4 | 0 |
| network_1 | 8 | 8 | 0 |
| orchestra | 6 | 7 | 1 |
| pets_1 | 6 | 7 | 1 |
| poker_player | 7 | 5 | 2 |
| real_estate_properties | 1 | 1 | 0 |
| singer | 4 | 5 | 1 |
| student_transcripts_tracking | 11 | 10 | 1 |
| tvshow | 10 | 10 | 0 |
| voter_1 | 3 | 3 | 0 |
| world_1 | 15 | 15 | 0 |
| wta_1 | 8 | 10 | 2 |

The residue comes from evening the difficulty counts after dealing: a moved task takes its
database with it. The move always takes from the database where the two sets are furthest
apart, which bounds the damage but does not erase it.

## Limitation: the reserve set measures task generalisation, not schema generalisation

**The same twenty databases appear in both sets.** So the reserve number answers "does this
agent hold up on questions it has not been tuned against?" and not "does this agent hold up
on databases it has never seen?" The second is the stronger claim and this project does not
make it.

The alternative was a schema-disjoint reserve — ten databases in the working set, ten held
back. That was considered and rejected: twenty databases split ten and ten leaves both sets
small, differently shaped, and dominated by whichever schemas happened to fall on each
side, which makes the two numbers noisy and not comparable to each other. A weaker claim
measured cleanly beats a stronger claim measured badly.

This limitation is repeated next to the reserve figure wherever it is reported:
[RESULTS.md](RESULTS.md#a0-on-the-reserve-set--the-reserve-run), the README and the results page.
*(This line said "when that figure is reported" until the reserve run, 2026-09-13.)*

## What the tests hold

[`tests/test_splits.py`](../tests/test_splits.py), all running with no substrate and no
network:

- The working set and the reserve set do not intersect.
- The smoke set lies inside the working set.
- Every drawn task is in the frame.
- The two sets carry the same difficulty mix.
- Every database in the frame reaches both sets.
- **The committed files regenerate exactly from the seed** — the file cannot be edited by
  hand, and the draw cannot change, without the test failing.
- A different seed draws different sets, so the seed is doing work.
