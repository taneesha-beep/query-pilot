# Failures, read by hand, and the catalog

Three parts. **A0's failures** (2.5) — all 26 non-solves of A0's measured run, read one at a
time. **A1's failures** (4.4) — all 34 non-solves of A1's measured run, read the same way under
the same categories. And **[the failure catalog](#the-failure-catalog--44)** (4.4) — every
failure mode this project has a number for, each with its frequency, its denominator and the
artifact it came from, kept apart from the modes that are real but uncounted.

---

# A0's failures, read by hand — 2.5

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

**"A floor" was itself incomplete — corrected 2026-09-12, when 4.4 read A1's failures.** The
two paragraphs above, and the one headed *Execution accuracy is a lower bound*, treat a
defective reference as something that only rejects correct answers. **It also accepts wrong
ones** — any answer that reproduces the defect. Measured: of the ten tasks A1 lost and A0 won,
**eight are A0 solves whose rows equal a reference verified to return wrong data**. Seven are on
`flight_2`, where all 1,200 rows of `flights` store both airport codes with a leading space and
all 100 cities in `airports` end with one, so A0's natural query and the reference return the same nothing (or the
same count of 0) where the data holds 1 to 47 rows or flights. The eighth is `dev-0388`: A0
copied the question's quote marks into its literal, `' Little Lever Urban District '`, matched
no hometown, and returned all 7 teachers — exactly as the reference does, whose lowercase
literal also matches nothing. Every one is re-run read-only in
[`docs/a1-failure-counts.json`](a1-failure-counts.json).

**So 124 of 150 is not a floor on correct answers.** It is agreement with the reference
queries, and the references err in both directions: 9 of A0's 26 failures are the reference
(the figure reads low), and **at least 8 of its 124 solves** are wrong answers the reference
agrees with (the figure reads high). "At least", because those 8 were found without reading
A0's solves — only the ten tasks A1 lost were examined, and the other 116 solves were never
read. The equivalence rule's own documented costs still push only downward, and nothing is
re-scored: 124 of 150 stands as taken, and so does 116 of 150.

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

---

# A1's failures, read by hand — 4.4

**Every non-solve of A1's measured run, read one at a time under a protocol fixed before any
of them was read.** The census was agreed with the author on 2026-09-11; the categories and
precedence below were written into this file on 2026-09-12, session 11, before one of the 34
trajectories was opened.

| | |
|---|---|
| Run | `20260910-024454-1f69bc` |
| Ledger | `runs/20260910-024454-1f69bc/ledger.jsonl` |
| Provider · model · date | Groq · `openai/gpt-oss-120b` · 2026-09-10 |
| Accuracy this reads the failures of | [116 of 150 — 77.3333%](RESULTS.md) |
| Population | [`docs/failure-sample-a1.json`](failure-sample-a1.json) |

## The protocol, as it was fixed

**A census, not a draw.** The run produced **34** non-solves. 2.5's rule taken literally would
draw 30 at random and leave 4 unread; reading all 34 costs four more tasks and leaves nothing to
choose, so every count below is **out of 34** and is a rate over A1's non-solves, never
re-weighted to the working set. `scripts/failure_sample.py --census` wrote the list; there is no
seed because nothing was drawn. The run recorded no task **failed** at its end — `dev-0044`
and `dev-0758` failed once and completed on resume, and a task's last row stands.

**The categories are 2.5's eight, unchanged, plus one.** A1 can end a trajectory without a
statement — all 8 of its `no_sql` are `tool_call_limit` trajectories that wrote nothing — and
2.5's categories each name a fault in a query. So: **`no answer`** — the trajectory ended
without a statement to score. Nothing else is added, no category is renamed, and the reason
slugs are the ledger's and never re-decided (constraint 27).

**The precedence is 2.5's, unchanged, with one clause for the new category.** "Reference query
is wrong or ambiguous" outranks everything; otherwise the single fault which, corrected, would
make the query return the reference's rows, nearest the root first — table or column, then
join, then aggregation, then literal — and 2.5's shape clarification holds word for word. **A
reference that returns demonstrably wrong data outranks `no answer`**: no answer could have
solved it except one that reproduced the defect. **A reference that merely admits two readings
does not**: that category needs the model's reading to be the better one, and a trajectory
with no answer has no reading, so it is `no answer`.

**What is read.** The question, the reference, A1's final SQL, the slug, the termination — from
the ledger — and **the whole transcript**: every tool call, every result, in order. Re-executing
a query read-only to check a claim is allowed; changing anything is not.

**Seen before the protocol was fixed, and named so a reader can discount them.** `dev-0186` was
read in full in session 9 and written up in `docs/RESULTS.md`. Session 9 also read at least two
more of the eight `no_sql` trajectories — `docs/RESULTS.md` records that "three of the eight
`no_sql` tasks are this shape" — **without naming them**. And 24 of the 34 are tasks A0 also failed, whose references
and A0's answers 2.5 read and categorised above: nine of those references are already known
defects. All stay in the population and are categorised under the same precedence.

## The counts, out of 34

| Category | A1 — count of 34 | A0 — count of 26 |
|---|---|---|
| **reference query is wrong or ambiguous** | **17** | 9 |
| correct-but-different result shape | 10 | 9 |
| **no answer** | **3** | — |
| wrong table or column | 2 | 3 |
| wrong value literal | 1 | 3 |
| wrong aggregation | 1 | 2 |
| wrong join | 0 | 0 |
| syntactically invalid SQL | 0 | 0 |
| other | 0 | 0 |

By reason slug, the ledger's own verdict and not re-decided here: `value_mismatch` 14 ·
`row_count` 10 · `no_sql` 8 · `column_count` 2 · `candidate_error` 0 · `truncated` 0 ·
`reference_error` 0.

**Half of A1's failures are the reference, and four of 34 are the model getting the data
wrong.** Of the 17 references, **13 return demonstrably wrong data** — A0's five, seven on
`flight_2`, and `dev-0388` — and **4 admit two readings**: A0's three and `dev-0484`. The model
getting the data wrong — wrong table or column, wrong literal, wrong aggregation — is **4 of
34**, against A0's 8 of 26. `no answer` is 3 more.

**On the 24 tasks both agents failed, A1 failed the same way A0 did 20 times.** The four that
differ are three `no answer` — `dev-0152` and `dev-0354`, where A0 chose the wrong column, and
`dev-0980`, A0's weakest reference reading — and `dev-0816`, which the precedence moved (below).

**The ten tasks A1 lost and A0 won are nine references and one sort order.** Seven on `flight_2`,
`dev-0388` (wrong data), `dev-0484` (two readings) and `dev-0614` (row order). **In none of the
ten did A1 get the data wrong where A0 got it right** — and in eight of them, A0's solve is a
wrong answer that reproduces the reference's defect, which is the correction written above.

**12 of the 34 had already run a query the rule scores as a solve** — every `execute_sql` the
trajectory ran, re-executed read-only and compared with the same rule, by
`scripts/a1_failure_counts.py` into [`docs/a1-failure-counts.json`](a1-failure-counts.json). No
verdict moves; the count asks only whether the answer had been in hand. **Seven then ran out of
tool calls** — 7 of the 8 `no_sql`, every one but `dev-0354` — and **five answered with something
else**: `dev-0248`, `dev-0484`, `dev-0883`, `dev-0897`, `dev-0999`. Six of the twelve are
`flight_2`, where the "solving" query is the one that reproduces the defect; the other six had a
reading the reference shares and moved off it.

**Five of the eight `no_sql` are `dev-0186`'s shape, not three.** An empty or zero result that
was the stored data's padding, disbelieved, investigated, and never answered: `dev-0186`,
`dev-0207`, `dev-0238`, `dev-0254`, `dev-0256`. `docs/RESULTS.md` said three, from a partial
reading in session 9 that named only one.

**One call the precedence made, stated because it moved a task between categories.**
`dev-0816`'s answer differs from the reference in two ways: it returns every language tied at
a country's maximum (241 rows against 233), and it identifies the country by `Name` where the
reference returns `CountryCode`. Correcting either alone does not return the reference's rows,
so the one nearer the root wins — **wrong table or column**. A0's answer to the same task kept
`CountryCode` and differed only in shape and ties, which is why 2.5 filed it as wrong
aggregation. Read the identifier as representation instead and A1's moves there too: wrong
table or column 1, wrong aggregation 2. No other task depends on this call.

## The thirty-four, one at a time

Slug is the ledger's. Category is assigned by hand under the precedence above. "As A0" means
2.5 filed the same task under the same category, for the same reason, and the argument there
applies unchanged.

### reference query is wrong or ambiguous — 17

**Returns demonstrably wrong data — 13.**

| Task | Slug | What A1 did, against what the reference does |
|---|---|---|
| `dev-0125` | `value_mismatch` | `CAST(Horsepower AS INTEGER) > 150` → 49; the reference compares a TEXT column and counts 281. As A0. |
| `dev-0126` | `value_mismatch` | The same question, the same cast, the same fault. As A0. |
| `dev-0133` | `value_mismatch` | `ORDER BY CAST(MPG AS REAL)` → `mazda`; the reference sorts text and puts `'null'` first. As A0. |
| `dev-0820` | `row_count` | The 2 countries at the maximum Spanish share; the reference returns all 28. As A0. |
| `dev-0937` | `value_mismatch` | Summed treatment cost → Stoltenberg; the reference counts treatments. As A0. |
| `dev-0388` | `row_count` | `Hometown <> 'Little Lever Urban District'` → 6 teachers. The reference's literal is lowercase, matches nothing, and returns all 7 — including Anne Walker, the one teacher the question excludes. **A1 is right.** |
| `dev-0186` | `no_sql` | Found `'Anthony '` with a trailing space after twelve calls and never answered. The reference returns nothing; the data holds `ANY`. |
| `dev-0207` | `no_sql` | Its first query counted 0 — matching the reference — disbelieved it, and chased the padding until the limit. The data holds 21 flights. |
| `dev-0227` | `row_count` | Counted codes straight from `flights` and answered `' AID'`; the reference's join never matches a padded code and returns nothing. |
| `dev-0238` | `no_sql` | Its first query returned nothing — matching the reference — and it never answered. The data holds 7 airlines. |
| `dev-0248` | `row_count` | Found the leading space with `hex()` and answered with `trim()`: the **correct 11 flights**. The reference returns none. |
| `dev-0254` | `no_sql` | The same shape as `dev-0238`; the data holds 21 flights. |
| `dev-0256` | `no_sql` | The same shape as `dev-0207`; the reference counts 0, the data 47. |

**Every `flight_2` claim above is one fact about the stored data**: all 1,200 `flights` rows
store both airport codes with a leading space (`' APG'`), and all 100 cities in `airports` end
with one (`'Aberdeen '`, `'Anthony '`), so any equality against a clean literal or across the join
matches nothing. The reference, its result, and a query that strips the padding are recorded
per task in `docs/a1-failure-counts.json`.

**Admits two readings — 4.**

| Task | Slug | The two readings |
|---|---|---|
| `dev-0106` | `row_count` | *"each continent"*: A1 left-joined and returned all 5, as A0 did; the reference inner-joins to 3. As A0. |
| `dev-0883` | `row_count` | *"each student"*: A1 first ran the reference's reading (14 rows), then left-joined from `Highschooler` and returned all 16. As A0. |
| `dev-0897` | `row_count` | *"no friends"*: A1 first ran the reference's reading (Brittany, John), then counted either column of `Friend` and returned none. As A0. |
| `dev-0484` | `value_mismatch` | *"the three youngest winners"*: the reference returns Madison Keys three times — three matches, one player — and A1's first query did too; A1 then returned three different players. The reference's own `DISTINCT` suggests it meant distinct winners and deduplicated `(name, rank)` instead. A1's reading is the more natural one. |

### correct-but-different result shape — 10

Each returned the right data; what rejected it is the rule's positional column comparison or
its ordered comparison, both fixed before any result existed.

| Task | Slug | What differed |
|---|---|---|
| `dev-0110` | `value_mismatch` | Column order, `(id, name, count)` against `(count, name, id)`. As A0. |
| `dev-0263` | `value_mismatch` | Column order, `(city, count)`. As A0. |
| `dev-0489` | `value_mismatch` | Column order, `(hand, count)`. As A0. |
| `dev-0490` | `value_mismatch` | The same question in other words. As A0. |
| `dev-0571` | `value_mismatch` | Column order, `(id, count)`. As A0. |
| `dev-0613` | `value_mismatch` | Row order: `CAST(Rating AS REAL) DESC` where the reference sorts the text ascending. As A0. |
| `dev-0614` | `value_mismatch` | The same question in other words, the same order. **A0 solved it** with `ORDER BY Rating`. |
| `dev-0617` | `value_mismatch` | Column order, `min, max` against `max, min`. As A0. |
| `dev-0760` | `column_count` | The city's name without its population. As A0. |
| `dev-0851` | `column_count` | A `Frequency` column added, and sorted descending. As A0. |

### no answer — 3

**`dev-0152` · `car_1` · `no_sql`** — ran the right query at its ninth call (13 rows, scored as
a solve on re-execution), ran it again twice more, sampled a table, and was cut off by
`TOOL_CALL_LIMIT` asking to run it a fourth time. A0 failed this task with a wrong column.

**`dev-0354` · `cre_Doc_Template_Mgt` · `no_sql`** — wrote A0's wrong column,
`Templates.Template_Details`, saw it was empty in every row, and spent its last four calls on
**the same `sample_rows` call with the same arguments**. It never found `Ref_Template_Types`,
where the descriptions are. The one `no_sql` with no solving query in hand.

**`dev-0980` · `dog_kennels` · `no_sql`** — ran the reference's own query at its eighth call
(3 owners), then kept working the word *temporarily* through the arrival and departure dates
until the limit. 2.5 filed A0's answer as the weakest of its nine reference readings; A1 gave
no reading at all.

### wrong table or column — 2

**`dev-0150` · `car_1` · `value_mismatch`** — returned `car_makers.Maker`, the short name, where
the question asks for the maker's name and the reference returns `FullName`. As A0.

**`dev-0816` · `world_1` · `row_count`** — every language tied at the maximum, identified by
country `Name` rather than `CountryCode`. The precedence call above.

### wrong value literal — 1

**`dev-0758` · `world_1` · `row_count`** — `GovernmentForm LIKE '%Republic%'` against the
reference's `= "Republic"`: 306 rows against 258. As A0 — and A1, which could have looked at the
stored government forms, never did.

### wrong aggregation — 1

**`dev-0999` · `dog_kennels` · `row_count`** — ran the reference's `DISTINCT` query first (12
rows), then dropped `DISTINCT` and answered with 15. As A0.

---

# The failure catalog — 4.4

**Every failure mode this project has measured, with a frequency, a denominator and the
artifact it came from. No entry without a number.** Three kinds, never mixed: rates over a
population, counts with no rate available, and — in a separate list, deliberately not in either
table — modes that are real but uncounted.

Runs referred to below: **A0** — Groq, `openai/gpt-oss-120b`, 2026-09-08, run
`20260908-133316-faecd5`, `runs/20260908-133316-faecd5/ledger.jsonl`. **A1** — Groq,
`openai/gpt-oss-120b`, 2026-09-10, run `20260910-024454-1f69bc`,
`runs/20260910-024454-1f69bc/ledger.jsonl`. **Attack** — Groq, `openai/gpt-oss-120b`,
2026-09-11, run `20260911-113246-1a97c9`, `runs/20260911-113246-1a97c9/ledger.jsonl`, lifted to
`tests/transcripts/attacks-lifted/`.

## Rates over a population

| Mode | Frequency | Denominator | Artifact |
|---|---|---|---|
| **Answers — A0, by hand (2.5)** | | | |
| Reference query wrong or ambiguous | 9 | A0's 26 non-solves | this file; `docs/failure-sample.json` |
| — of which the reference returns demonstrably wrong data | 5 | A0's 26 non-solves | this file |
| Correct-but-different result shape (rule decisions 1–2) | 9 | A0's 26 non-solves | this file |
| Wrong table or column | 3 | A0's 26 non-solves | this file |
| Wrong value literal | 3 | A0's 26 non-solves | this file |
| Wrong aggregation | 2 | A0's 26 non-solves | this file |
| Wrong join · syntactically invalid SQL | 0 · 0 | A0's 26 non-solves | this file |
| **Answers — A1, by hand (4.4)** | | | |
| Reference query wrong or ambiguous | 17 | A1's 34 non-solves | this file; `docs/failure-sample-a1.json` |
| — of which the reference returns demonstrably wrong data | 13 | A1's 34 non-solves | this file; `docs/a1-failure-counts.json` |
| Correct-but-different result shape | 10 | A1's 34 non-solves | this file |
| No answer | 3 | A1's 34 non-solves | this file |
| Wrong table or column | 2 | A1's 34 non-solves | this file |
| Wrong value literal · wrong aggregation | 1 · 1 | A1's 34 non-solves | this file |
| Wrong join · syntactically invalid SQL | 0 · 0 | A1's 34 non-solves | this file |
| Failed the same way as A0 did | 20 | 24 tasks both agents failed | this file |
| **Trajectories — A1, mechanical** | | | |
| A solving query already run, then not answered with | 12 | A1's 34 non-solves | `docs/a1-failure-counts.json` |
| Ended at `TOOL_CALL_LIMIT` | 8 | 150 trajectories | `results/a1-working.json` |
| — of which solved | 0 | 8 | `results/a1-working.json` |
| — of which a solving query was already in hand | 7 | 8 | `docs/a1-failure-counts.json` |
| No statement in the final reply (`no_sql`, all `no_statement`) | 8 | 150 trajectories | `results/a1-working.json` |
| A repeated identical tool call | 7 | 150 trajectories | `docs/a1-failure-counts.json` |
| — among non-solves · among solves | 6 · 1 | 34 · 116 | `docs/a1-failure-counts.json` |
| Repeated identical tool calls | 9 | 652 executed tool calls | `docs/a1-failure-counts.json` |
| Wasted tool calls (an upper bound on waste) | 17 | 556 calls in 142 trajectories with a final query | `results/a1-trajectory-metrics.json` |
| Recovered after an empty first `execute_sql` | 1 | 6 | `results/a1-trajectory-metrics.json` |
| A repair was needed (and it succeeded) | 1 (1) | 150 trajectories | `results/a1-trajectory-metrics.json` |
| **Substrate and rule** | | | |
| Reference returns no rows, so answering nothing solves | 49 | 1,034 frame tasks | `docs/EQUIVALENCE.md` |
| — the same, on the working set | 7 | 150 tasks | `results/a1-working.json` |
| Empty-reference tasks solved — A0 · A1 | 7 · 2 | 7 | `docs/RESULTS.md` |
| A0 solves whose rows equal a reference verified wrong | 8 | 10 tasks A1 lost and A0 won | `docs/a1-failure-counts.json` |
| **Attack corpus — A1 (4.3)** | | | |
| Compliance — the trajectory attempted the injected instruction | 8 | 45 attack cases | `results/attacks.json` |
| Containment — every compliant attempt refused before executing | 5 | 5 compliant containable cases | `results/attacks.json` |
| Task-damage — resisted, and answered wrongly anyway | 0 | 37 resisted cases | `results/attacks.json` |
| The injected instruction never reached a prompt | 15 | 45 attack cases | `results/attacks.json` |
| — row-values cases never exposed | 15 | 15 row-values cases | `results/attacks.json` |
| Compliance among exposed cases | 8 | 30 exposed cases | `results/attacks.json` |
| A repair was needed (all succeeded) | 6 | 45 trajectories | `runs/20260911-113246-1a97c9/ledger.jsonl`; `docs/ATTACKS.md` |
| **Trajectories — the cheap model, `openai/gpt-oss-20b` (5.1's preflights, rows added 2026-09-12)** | | | |
| Groq refused the model's own output with HTTP 400 — first run, under 3.6's retry rule | 6 | 19 trajectories not cut off by a request ceiling | `runs/20260912-052224-4f8be8/ledger.jsonl`; `docs/ESCALATION.md` |
| — refused again when the task was retried | 2 | 4 tasks retried after a refusal | same |
| Ended `provider_rejected` — second run, which scores the refusal | 3 | 15 trajectories | `docs/escalation-a2-cheap-smoke.json` |
| Ended at `TOOL_CALL_LIMIT` — second run | 1 | 15 trajectories | `docs/escalation-a2-cheap-smoke.json` |
| A repair was needed · it succeeded · the provider refused it | 5 · 4 · 1 | 15 trajectories | `tests/transcripts/a2-cheap-smoke-lifted/` |
| **Trajectories — the cheap model on the working set (5.2's always-cheap run, run `20260912-055938-9712c8`, rows added 2026-09-12)** | | | |
| Not solved | 61 — `no_sql` 42, `row_count` 11, `value_mismatch` 7, `column_count` 1 | 150 tasks | `results/a2-cheap-working.json` |
| Ended `provider_rejected` — Groq refused the model's own output | 28 | 150 trajectories | `results/a2-cheap-working.json` |
| Ended at `TOOL_CALL_LIMIT` | 9 | 150 trajectories | same |
| Answered a value, not a statement, and Groq refused the one repair | 5 | 150 trajectories | same (`repair_blocked`) |
| A repair was needed · it succeeded · the provider refused it | 35 · 30 · 5 | 150 trajectories | `results/a2-cheap-trajectory-metrics.json` |
| Ran no `execute_sql` · of those, solved | 42 · 9 | 150 trajectories | `docs/escalation-a2-cheap-working.json` |
| First `execute_sql` came back empty · of those, recovered | 8 · 0 | 150 trajectories | `results/a2-cheap-trajectory-metrics.json` |
| Failures the escalation rule catches (recall) | 46 | 61 failures | `docs/escalation-a2-cheap-working.json` |

## Counts with no rate available

Each has a number and no denominator that would make it a rate — either the population is
empty, or it was never defined, or it was never read.

| Mode | Count | Why no rate | Artifact |
|---|---|---|---|
| `execute_sql` errors on a trajectory's first call — recovery rate's headline | 0 | Its denominator is 0; the rate is `TBD` by a rule fixed before the run | `results/a1-trajectory-metrics.json` |
| Containment of a sandbox-escape attempt | 0 attempts | 0 of 9 cases attempted one; the rate is `TBD` | `results/attacks.json` |
| A0 solves that are wrong answers matching a wrong reference | **at least 8** | Found among 10 examined tasks; A0's other 116 solves were never read, so no rate over 124 exists | `docs/a1-failure-counts.json` |
| Groq HTTP 400 `tool_use_failed` — tool-call arguments not valid JSON | 1 — `dev-0758` | The ledger's 827 attempt rows are the requests that were *answered*, every one `ok`; a refused request is not an attempt row, so there is no per-request denominator. On the cheap model the same refusal is common enough to count per trajectory — see the 5.1 rows above | `runs/20260910-024454-1f69bc/ledger.jsonl`; `docs/PROVIDERS.md` |
| Quota walls during the A1 run | 12 — 4 day-scope on `groq#1`, 8 minute-scope on `groq#2` | How often a refilling bucket runs dry depends on how fast the operator spends, not on the agent. *Until 2026-09-12 this said the window Groq's daily counter runs over was `TBD`; it is a bucket that refills continuously — `docs/PROVIDERS.md`* | `runs/quota-walls.jsonl` |
| Quota walls during the always-cheap run (5.2, `openai/gpt-oss-20b`, run `20260912-055938-9712c8`) | 84 — 4 day-scope (`groq#2` 06:45:00Z and `groq#1` 06:46:55Z; both again at 10:53Z) and 80 minute-scope, 2026-09-12 | Same reason. The first pair stopped the run as `pools_exhausted` at 105 of 150; after a third pool was added the second pair did not. *This row said "2, none minute-scope" at 105 of 150* | `runs/quota-walls.jsonl` |
| A session ended `pools_exhausted` with no provider refusal in its window — the client's modelled bucket, not Groq | 1 of the A1 run's 6 sessions | A per-session rate over sessions the operator staged means nothing | `runs/20260910-024454-1f69bc/ledger.jsonl` |
| Trajectories cut off mid-task and retried on resume | 6 in the A1 run (1 operator stop, 4 `AllPoolsExhausted`, 1 HTTP 400) · 1 in the attack run (operator stop) · 3 in the always-cheap run (2 request-ceiling stops, `dev-0044` and `dev-0999`; 1 `AllPoolsExhausted`, `dev-0710`), every one retried and complete | Staging is the operator's choice, so the count describes the staging, not the agent | the runs' ledgers |
| Full test-suite runs with one intermittent failure | 1 in session 9 · 1 in session 11 (not captured) · 1 in session 13 — **identified**: `test_run_ledger.py::test_a_process_killed_with_sigkill_leaves_a_ledger_that_resumes`, the assertion that `t-3`'s attempt row survived | Suite runs were never counted as a population. **A race in the test, not a ledger defect**: it waits for three task rows before SIGKILL, and nothing guarantees `t-3`'s attempt row is written by then. *Until 2026-09-12 this row said "unexplained"* | `AGENT-ROADMAP.md`, sessions 9, 11 and 13 |

## Observed but uncounted

Real, seen, and deliberately **not** put into either table above, because no count of them
exists.

- **Wrong answers that agree with a wrong reference, among the solves nobody read.** The 8
  above were found by looking at the tasks A1 lost. Neither agent's other solves were read — 116
  for A0, 116 for A1 — so how many more exist, and whether they favour either agent, is
  unknown. This is the largest unmeasured term in both accuracy figures.
- **Why the cheap model's 61 non-solves failed, beyond their slugs.** 5.2's always-cheap run
  (`results/a2-cheap-working.json`) is counted above only mechanically — 42 with no statement, 19
  wrong results. None of its trajectories was read under this document's categories the way
  4.4 read A1's 34, and its 89 solves were not read either, so its share of reference defects is
  unknown in both directions.
- ~~**The window Groq's daily token counter runs over.**~~ **Established 2026-09-12 and moved
  out of this list**: it is not a window but a bucket of 200,000 refilling continuously at
  200,000 per 86,400 s — every one of the project's eight day-scope refusals gives a retry hint
  equal to its shortfall at that rate, to the millisecond (`docs/PROVIDERS.md`). This bullet
  said it "was never established and costs a day to find out"; the refusals already held it.
- **A0 under prompt injection.** A0's prompt carries every table name, column name and column
  type, so 30 of the 45 attack cases reach it. It was not run against them; its compliance is
  `TBD` (`docs/ATTACKS.md`).
- **Other stored-value defects beyond `flight_2`, `car_1`'s TEXT numbers and `course_teach`'s
  capitalisation.** Each was found because a failure pointed at it; the substrate was never
  swept for padding or type defects as a whole.
