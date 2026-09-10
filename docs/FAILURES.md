# A0's failures, read by hand

**Every failure of the measured working-set run, read one at a time under a protocol fixed
before any of them was read.**

The protocol below was written and approved on **2026-09-08, before the run of 2.4 had
started** and therefore before this project's first accuracy figure existed. That ordering
is the whole point of the item: how many failures to read, how to choose them, and what the
categories are all decide what the counts mean, and deciding any of them after seeing the
counts is how a taxonomy is talked into a more comfortable shape.

Drawn by [`scripts/failure_sample.py`](../scripts/failure_sample.py) into
[`docs/failure-sample.json`](failure-sample.json).

| | |
|---|---|
| Run | `20260908-133316-faecd5` |
| Ledger | `runs/20260908-133316-faecd5/ledger.jsonl` |
| Provider · model · date | Groq · `openai/gpt-oss-120b` · 2026-09-08 |
| Accuracy this reads the failures of | [124 of 150 — 82.6667%](RESULTS.md) |

---

## The protocol, as it was fixed

**What counts as a failure.** A task the run recorded **complete** — the model answered and
the answer was scored — whose `solved` is false. Every non-solve is in the population,
including `no_sql` (the answer contained no statement) and `candidate_error` (the statement
did not execute).

Tasks recorded **failed** are excluded. A failed task has no model answer to read: the run
never got one, and a resume retries it. **This run recorded none**, so the exclusion cost
nothing here.

**How many.** Thirty. If the run produced fewer than thirty non-solves, **all** of them are
read and every count is reported out of that number, named as such. Nothing tops the sample
up — not the smoke set, not a second run, and never `splits/reserve.json`, which stays
untouched until the reserve run.

> **This is the branch that was taken.** The run produced **26** non-solves, so all 26 were
> read and every count below is **out of 26**, not out of 30. The clause existed before the
> accuracy was known, which is why the sample size is not something this run's result chose.

**These are counts out of the sample. They are never re-weighted into a rate**, and no
figure here is multiplied back up to the working set.

**How the thirty are chosen** — unused here, recorded because it was fixed in advance and a
larger population would have used it:

    random.Random(20260908).sample(sorted(non_solves_by_task_id), 30)

Random rather than the alternatives, and each alternative answers a different question:

- *The first thirty* would be ordered by task ID, and task IDs are positional in Spider's
  `dev.json`, which groups by database. The first thirty would over-represent a handful of
  schemas — measured on this split, up to fifteen consecutive tasks share one database.
- *Stratified by difficulty or by reason slug* buys equal precision per stratum and gives up
  being representative of the run. Since these counts are never re-weighted, a stratified
  sample's counts would describe the strata and not A0.

**The categories are a second taxonomy over the reason slugs, not the slugs themselves.** A
slug says *how the comparison failed* — `row_count`, `value_mismatch`, `column_count`. A
category says *why the query was different*. One slug spans many categories and one category
appears under many slugs, so each failure carries both: the slug is read from the ledger and
never re-decided, and the category is assigned by hand. No slug is renamed and none is
added; [`docs/EQUIVALENCE.md`](EQUIVALENCE.md)'s set may be extended but never renamed
underneath a committed result, and this item extends nothing.

**One category each, under a precedence fixed in advance.** "Reference query is wrong or
ambiguous" outranks everything: if the reference is wrong, the model's query is not the
failure. Otherwise the category is the single fault which, corrected, would make the query
return the reference's rows; where two are needed, the one nearer the root of the query wins
— table or column, then join, then aggregation, then literal.

**"Reference query is wrong or ambiguous" has to be argued, not felt.** Each one states what
the question asks, what the reference does, why they differ, and why the model's reading is
the better one. And **nothing in this item re-scores anything.** No task's verdict moves, no
accuracy figure is adjusted, and the finding is reported as a count and as a statement about
what the accuracy figure is a lower bound on.

**What is read.** The question, the reference query, the model's query, the slug, the row
counts and the comparison's detail string — all from the run's ledger. Re-executing either
query read-only to understand it is allowed, and every claim below about what the data
contains was checked that way. Changing anything is not.

**Three tasks whose output had already been seen.** `dev-0126`, `dev-0388` and `dev-0489`
were read during 2.3's ten-task acceptance run, before this protocol was fixed, and two were
provisionally described there as candidates for the category that matters. Fixed in advance:
they stay in the population, get no special treatment, and are categorised under the same
precedence as everything else. Excluding them would remove exactly the tasks that motivate
the category and bias its count down. **Two of the three are in this sample** — `dev-0126`
and `dev-0489`; `dev-0388` **solved** this time and is not a failure of this run at all.

### One clarification of the precedence, stated because it decided nine of the twenty-six

A failure that is **only** a difference of column order, column count or row order is
`correct-but-different result shape`, never `reference query is wrong or ambiguous` — even
where the reference's column order contradicts the wording of its own question. What
rejected those answers is [decision 1 and decision 2 of the equivalence rule](EQUIVALENCE.md),
which are positional and ordered by design and were committed before any result existed, and
whose costs that document already states. `reference query is wrong or ambiguous` is reserved
for a reference that returns **wrong data**, or a question that genuinely admits two readings
of what the data should be. Folding shape differences into it would let the rule's own
documented cost be reported as a fault in the substrate.

---

## The counts, out of 26

| Category | Count of 26 |
|---|---|
| **reference query is wrong or ambiguous** | **9** |
| correct-but-different result shape | 9 |
| wrong table or column | 3 |
| wrong value literal | 3 |
| wrong aggregation | 2 |
| wrong join | 0 |
| syntactically invalid SQL | 0 |
| other | 0 |

By reason slug, which is the ledger's own verdict and not re-decided here:
`value_mismatch` 13 · `row_count` 11 · `column_count` 2 · `no_sql` 0 · `candidate_error` 0 ·
`truncated` 0 · `reference_error` 0.

**The nine references split into two kinds, and they are not equally strong.**

- **Five where the reference returns demonstrably wrong data**, each verified by running a
  query: `dev-0125`, `dev-0126`, `dev-0133`, `dev-0820`, `dev-0937`.
- **Four where the question admits both readings** and the reference took one:
  `dev-0106`, `dev-0883`, `dev-0897`, `dev-0980`. `dev-0980` is the weakest of these and is
  marked as such below.

**Zero syntactically invalid queries and zero wrong joins.** Every one of the 150 answers
held a statement the extractor found, every statement executed, and no failure in this
sample was a join written wrongly. Whatever A0 gets wrong here, it is not the mechanics.

---

## What this means for every accuracy figure in this project

**Execution accuracy is a lower bound, and this is the measurement that says by how much it
could matter.** Nine of 26 failures are the reference rather than the model, and five of
those are references that do not answer their own question — not a matter of taste. Nine
more are the equivalence rule's own documented costs: positional columns and ordered rows,
both chosen before any result existed and both stated in `docs/EQUIVALENCE.md` as making the
number stricter rather than kinder.

**Nothing here is re-scored.** The figure stands at 124 of 150. What changes is how it must
be read, and every figure derived from it: as a floor, with 26 failures of which 8 —
`wrong table or column`, `wrong value literal` and `wrong aggregation` — are the model
getting the data wrong.

**The consequence for Phase 3 — and this paragraph was wrong, corrected 2026-09-10 after
3.6 measured A1.** It said: *a defect in a reference costs both agents the same task, so the
A0-against-A1 difference is unaffected by all nine.* **The first half does not hold in
general, and A1's run produced the counter-example.**

A defect costs both agents the same task only where both agents answer the same way. Where
the defect makes the **reference return no rows**, an agent that can inspect the data may
decline to return nothing — and then the same defect costs one agent and rewards the other.
Measured on `dev-0186`, `flight_2`: the stored city is `'Anthony '` with a trailing space, so
`City = "Anthony"` returns nothing. **A0 wrote that query, returned nothing, matched the
reference's nothing and scored a solve. A1 found the trailing space, disbelieved the empty
result, and ran out of tool calls before committing to an answer — `no_sql`, task lost.**
Across the split: 7 of these 150 reference queries return no rows, **A0 solved 7 of 7 and A1
solved 2 of 7**, which is five of the eight tasks separating them.

**What still holds** is the part that matters for the absolute figures: the nine references
identified above are defects, execution accuracy in this project is a lower bound, and no
task's verdict was moved on any of it. What does **not** hold is the claim that a defective
reference is neutral between two agents. It is neutral only between agents with the same
visibility into the data, and giving A1 tools is precisely the difference in visibility.
[`docs/RESULTS.md`](RESULTS.md) reads the whole comparison. It is the absolute figures that
are floors, and the *difference* between them carries this correction with it.

---

## The twenty-six, one at a time

Slug is the ledger's. Category is assigned by hand under the precedence above.

### reference query is wrong or ambiguous — 9

**`dev-0125` · `car_1` · `value_mismatch` · reference returns wrong data**
*"What is the number of the cars with horsepower more than 150?"* The reference is
`WHERE horsepower > 150`, and `Horsepower` is a **TEXT** column, so SQLite applies text
affinity to the literal and compares strings: `'90' > '150'` is true. The reference counts
**281** of 406 cars, including every 90-horsepower car. The model wrote
`CAST(Horsepower AS REAL) > 150` and counted **49**. Verified: the numeric comparison
returns 49, the text comparison 281, and all 406 rows are non-empty.

**`dev-0126` · `car_1` · `value_mismatch` · reference returns wrong data**
The same question in different words, the same reference, the same fault. The model wrote
`CAST(Horsepower AS INTEGER)`. *One of the two tasks seen before the protocol was fixed.*

**`dev-0133` · `car_1` · `value_mismatch` · reference returns wrong data**
*"Which model saves the most gasoline? … maximum miles per gallon."* The reference is
`ORDER BY T2.mpg DESC LIMIT 1` on a **TEXT** `MPG` column. Verified: that ordering puts the
literal string `'null'` first, so the reference's answer is the top of a lexicographic sort,
not the highest mileage. Ordering on `CAST(MPG AS REAL)` gives `46.6`. The model cast and
returned `mazda`; the reference returns `citroen`.

**`dev-0820` · `world_1` · `row_count` · reference returns wrong data**
*"What are the codes of countries where Spanish is spoken by the largest percentage of
people?"* The reference is
`SELECT CountryCode, max(Percentage) … WHERE LANGUAGE = "Spanish" GROUP BY CountryCode` —
which groups by country and so returns **every** country that speaks Spanish, each with its
own percentage. Verified: 28 countries speak Spanish and the reference returns 28 rows. The
model returned the 2 countries at the maximum, 100.0% (`CUB`, `SLV`).

**`dev-0937` · `dog_kennels` · `value_mismatch` · reference returns wrong data**
*"…the owner who **spent the most** on treatments of his or her dogs."* The reference orders
by `count(*)` — the number of treatments, not the money. Verified: by treatment count the
top owner is `(14, 'Funk')` with 4 treatments costing 1,282; by `sum(cost_of_treatment)` it
is `(3, 'Stoltenberg')` with 2 treatments costing 1,601. The model summed the cost and
returned Stoltenberg.

**`dev-0106` · `car_1` · `row_count` · question admits both readings**
*"What is the name of **each** continent and how many car makers are there in each one?"*
The reference inner-joins, so continents with no car maker vanish: 3 rows. The model used
`LEFT JOIN` and returned all 5 with zeroes. Verified: 5 continents exist, 3 have a maker.
"Each continent" reads more naturally as all five.

**`dev-0883` · `network_1` · `row_count` · question admits both readings**
*"How many friends does **each** student have?"* The reference groups the `Friend` table, so
only students who appear in it are counted: 14 rows. The model left-joined from
`Highschooler` and returned all 16 with zeroes. Verified: 16 highschoolers, 14 appearing as
`student_id`.

**`dev-0897` · `network_1` · `row_count` · question admits both readings**
*"What are the names of students who have no friends?"* The reference treats friendship as
the `student_id` column only and returns `Brittany` and `John`. The model treated it as
either column and returned none. Verified: **both** of those students appear as someone
else's `friend_id`, and 20 rows of `Friend` have no reciprocal row — so the table is stored
directed, and whether being named as someone's friend makes you not friendless is exactly
what the question does not say.

**`dev-0980` · `dog_kennels` · `value_mismatch` · question admits both readings — the
weakest of the nine**
*"How many owners **temporarily** do not have any dogs?"* The reference is
`count(*) FROM Owners WHERE owner_id NOT IN (SELECT owner_id FROM Dogs)` — owners with no
dog at all, 3 of 15 — which ignores the word *temporarily* entirely. The model read the
arrival and departure dates and returned 12. The reference's reading is the likelier intent
and the model's is elaborate; it is here because the reference answers a question with a
word removed from it, and it is marked weak rather than dropped.

### correct-but-different result shape — 9

Each of these returned the right data. What rejected them is the equivalence rule's
positional column comparison (decision 2) or its ordered comparison (decision 1), both fixed
before any result existed.

| Task | Slug | What differed |
|---|---|---|
| `dev-0110` | `value_mismatch` | Column order. The question asks for *"the id and full name"*; the reference returns full name then id, and the model matched the question. |
| `dev-0263` | `value_mismatch` | Column order: `(city, count)` against the reference's `(count, city)`. |
| `dev-0489` | `value_mismatch` | Column order: `(hand, count)` against `(count, hand)`. *Seen before the protocol was fixed.* |
| `dev-0490` | `value_mismatch` | The same question in different words, the same swap. |
| `dev-0571` | `value_mismatch` | Column order: `(id, count)` against `(count, id)`. |
| `dev-0613` | `value_mismatch` | Row order. *"sorted by rating"* names no direction; the reference used SQL's ascending default and the model wrote `DESC`. |
| `dev-0617` | `value_mismatch` | Column order. The question asks for *"minimum and maximum"*; the reference returns `max, min` and the model returned `min, max`. |
| `dev-0760` | `column_count` | *"Find the city…"* — the model returned the name, the reference returns name **and** population. |
| `dev-0851` | `column_count` | The model added a `frequency` column the question did not ask for, and sorted descending where the reference sorts ascending. |

### wrong table or column — 3

**`dev-0150` · `car_1` · `value_mismatch`** — *"names and ids of all makers"*: the model
returned `car_makers.Maker`, the short name, where the reference returns `FullName`.
Verified: maker 4 is `('General Motors', 'gm')`, so the model returned `gm`.

**`dev-0152` · `car_1` · `row_count`** — the model filtered on `car_names.Make = 'General
Motors'` instead of joining to `car_makers.FullName`. Verified: no row of `car_names.Make`
matches, so that half of the `OR` contributed nothing and the answer came from the weight
clause alone.

**`dev-0354` · `cre_Doc_Template_Mgt` · `row_count`** — the model returned
`Templates.Template_Details` where the question asks for the template *description*.
Verified: `Template_Details` is the empty string in every row, which is why one distinct
value came back against the reference's four.

### wrong value literal — 3

**`dev-0404` · `course_teach` · `row_count`** — `WHERE course.Course = 'math'` against the
stored `Math`. SQLite's comparison is case-sensitive, so it matched nothing.

**`dev-0549` · `student_transcripts_tracking` · `row_count`** — `'North Carolina'` against
the stored `'NorthCarolina'`. **The prompt holds no row values by design**, so the spelling
of a stored literal is not something A0 can see; 3.1's tools are where an agent gets to look.

**`dev-0758` · `world_1` · `row_count`** — `GovernmentForm LIKE '%Republic%'` against the
reference's `= "Republic"`, which pulls in *Federal Republic*, *Islamic Republic* and the
rest: 306 rows against 258.

### wrong aggregation — 2

**`dev-0816` · `world_1` · `row_count`** — the model joined against a per-country maximum,
which returns **every** language tied at that maximum; the reference's bare-column `max()`
returns one row per country. 241 rows against 233, the difference being ties.

**`dev-0999` · `dog_kennels` · `row_count`** — the reference uses `DISTINCT` and the model
does not: 15 rows against 12. The question does not say whether duplicates are wanted, and
[decision 3](EQUIVALENCE.md) compares multisets, which is the stricter choice and is
documented as such.
