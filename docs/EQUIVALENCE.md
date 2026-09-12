# The equivalence rule

**When two result sets count as the same answer.**

This is the measurement instrument. Every accuracy figure this project reports is this
rule's opinion, so it is written and committed **before a single query is generated** —
deciding these rules after seeing results is how a project talks itself into a better
number.

Implemented in [`src/query_pilot/equivalence.py`](../src/query_pilot/equivalence.py), tested
rule by rule in [`tests/test_equivalence.py`](../tests/test_equivalence.py), and applied to
the gold queries against themselves by
[`scripts/equivalence_check.py`](../scripts/equivalence_check.py).

The rule compares **rows, never SQL**. It executes nothing, opens no database, and imports
neither `query_pilot.client` nor `query_pilot.run`; it takes two lists of tuples and returns
a `Comparison`. That is what keeps the run package from ever learning what a solve is.

---

## Sanity check

Run on **2026-09-08** over all 1,034 tasks in `splits/frame.json`, recorded in
[`docs/equivalence-check.json`](equivalence-check.json).

| | |
|---|---|
| Gold against itself | **1,034 / 1,034 — 100.0000%** |
| Reference queries that fix a row order | 231 |
| Reference queries that fix none | 803 |
| Unordered results permuted, still solved | **344 / 344** |
| Ordered results permuted, now rejected | **57 / 57** |
| Failures of any kind | 0 |

**What the 100% does and does not prove.** On its own it is close to a tautology — the same
query run twice against the same database returns the same rows. Two things make it worth
running anyway. It puts the rule in front of **real data**: NULLs, mixed types in one
column, aggregates that came back as reals, empty results, and the `wta_1` bytes that are
not valid UTF-8. And it **permutes the rows**: every unordered comparison is re-run with the
candidate shuffled and must still solve, every ordered one is re-run shuffled and must now
fail. That second control is the part with teeth, and it tests the `ORDER BY` detection
against 1,034 real reference queries rather than the dozen in the unit tests.

A result with one row, or with rows that are all identical, cannot be permuted at all. Those
are left out of both control denominators rather than counted as passes, which is why 344
and 57 are smaller than 803 and 231.

**What it cannot prove:** the check cannot detect a column rule that is too lenient or a
tolerance that is too wide, because both sides are the same query. Those are argued for
below and tested against hand-built pairs instead.

---

## The seven decisions

### 1. Row order — unordered by default, ordered when the reference says `ORDER BY`

The question fixes the order, not the answer. A reference query with no `ORDER BY` has not
asked for one, and SQLite's row order in that case is an artifact of the query plan.

```
Reference   SELECT name FROM singer
            [("Joe",), ("Rose",), ("Tribal King",)]
Candidate   [("Tribal King",), ("Joe",), ("Rose",)]          -> SOLVE

Reference   SELECT name FROM singer ORDER BY age DESC
            [("Joe",), ("Rose",), ("Tribal King",)]
Candidate   [("Rose",), ("Joe",), ("Tribal King",)]          -> non-solve, value_mismatch
```

"Says `ORDER BY`" means **at parenthesis depth zero** — one that governs the outermost
`SELECT`. An `ORDER BY` inside a subquery orders that subquery and says nothing about what
the caller sees:

```
SELECT name FROM (SELECT name FROM singer ORDER BY age)          -> unordered
SELECT a FROM t WHERE x IN (SELECT y FROM z ORDER BY w)          -> unordered
SELECT a FROM t WHERE x IN (SELECT y FROM z ORDER BY w) ORDER BY a  -> ordered
SELECT a FROM t UNION SELECT b FROM u ORDER BY 1                 -> ordered
```

**This is a lexer, not a parser.** It blanks string literals, quoted identifiers and
comments, then counts parentheses, so `SELECT 'order by age' FROM t` is correctly unordered.
What it does not do is understand the query.

**Costs.** Ordered comparison is strict sequence equality, so a **tie in the reference's
sort key** can produce a false non-solve: if two singers share an age and the reference
returns them in one order, a candidate returning the other order is marked wrong. The same
tie under `ORDER BY … LIMIT 1` picks a different row entirely, which no comparison rule can
repair. Both push the reported accuracy down, never up.

### 2. Column order — positional; names are ignored entirely

SQLite names an unaliased expression with the expression's own text, so `count(*)`,
`COUNT(*)` and `n` are three names for one answer. Matching on names would reject nearly
every aggregate a model writes.

```
Reference   SELECT name, age FROM singer   ->  [("Joe", 30)]
Candidate   SELECT name AS singer_name, age AS yrs FROM singer
                                           ->  [("Joe", 30)]   -> SOLVE  (names ignored)
Candidate   SELECT age, name FROM singer   ->  [(30, "Joe")]   -> non-solve, value_mismatch
```

Differing column counts is its own refusal, with **no subset matching**. A query returning
extra columns has not answered the question as asked.

```
Reference   [("Joe", 30)]
Candidate   [("Joe", 30, "France")]        -> non-solve, column_count
```

**Cost:** right values under wrong names score; right columns in the wrong order do not.

### 3. Duplicate rows — multiset, not set

`SELECT name` and `SELECT DISTINCT name` are different queries and different answers.

```
Reference   SELECT country FROM singer     ->  [("France",), ("France",), ("France",)]
Candidate   SELECT DISTINCT country ...    ->  [("France",)]   -> non-solve, row_count
```

**Cost, stated because it is the common case:** a candidate whose `GROUP BY` collapses rows
the reference keeps is a non-solve, and so is a stray `DISTINCT` on data that happens to
hold duplicates. Set comparison would forgive both. Multiset is the stricter choice and it
pushes the reported accuracy **down** rather than flattering it, which is the direction this
project leans everywhere else. (Down, not to a floor: see the correction at the end of this
document.)

### 4. NULL — equal to NULL, and to nothing else

SQL says `NULL != NULL`. SQL is right about rows and wrong about result sets: two queries
that both returned no value for a column have returned the same answer.

```
Reference   [("Joe", None)]
Candidate   [("Joe", None)]                -> SOLVE

Reference   [("Joe", 30), ("Rose", 41)]
Candidate   [("Joe", 30), ("Rose", None)]  -> non-solve, value_mismatch
```

NULL is **not** equal to `0`, `0.0`, `''`, `'NULL'`, `'None'`, `[]` or `False`. This matters
more than it looks: a `LEFT` join where the reference wrote an `INNER` one produces exactly
a NULL where the reference has a value, and coercing NULL to a falsy value would hide the
most common wrong-join failure there is.

### 5. Floats — both tolerances, at 1e-9

Two doubles are the same value when `|a − b| ≤ 1e-9 + 1e-9 · |b|`.

**A policy choice, like a budget ceiling — and here is what it was derived from.** SQLite
stores `REAL` as an IEEE-754 double, whose epsilon is 2.22e-16. The largest table in this
substrate is `wta_1.rankings` at **510,437 rows** (measured, `docs/substrate-fingerprint.json`),
so an aggregate summing a whole column accumulates at worst 510437 × 2.22e-16 = **1.13e-10**
of relative drift. That is the pathological sequential-summation case, and it is the only
kind of disagreement two *correct* formulations of the same aggregate can produce. **1e-9
sits 8.8× above that ceiling** and seven orders of magnitude below any difference a Spider
question could mean.

```
admits   AVG(x) = 41.333333333333336  against  SUM(x)/COUNT(x) = 41.33333333333333
rejects  41.333333                    against  41.334
admits   1e9                          against  1e9 + 0.5     (relative half)
rejects  1e9                          against  1e9 + 5.0
admits   0.0                          against  1e-10         (absolute half)
rejects  0.0                          against  1e-8
```

Both halves earn their place: relative alone collapses to zero at and around zero, and
absolute alone rejects real reformulations at large magnitudes.

**Cost:** two values differing by one part in a billion are called equal. No question in
this substrate has that resolution, so nothing true is lost — but the number is a choice and
it is written here rather than buried in the code.

**Types.** An integer and a real of the same value are the same answer — `2` and `2.0` —
because which one SQLite hands back depends on how the query was written, not on what the
answer is. **Text never coerces to a number**: `'5'` is not `5` and `'0.50'` is not `0.5`.
That is the strict direction on purpose; allowing it would make a wrong column type
invisible. Two infinities are equal (the tolerance cannot prove it, since `inf − inf` is
NaN), and two NaNs are the same outcome even though IEEE-754 says no value equals a NaN.

### 6. Empty results — an empty result matching an empty reference is a **solve**

Stated explicitly because it is the one case where doing nothing scores.

```
Reference   []      Candidate   []            -> SOLVE
Reference   [("Joe",)]   Candidate   []       -> non-solve, row_count
Reference   []      Candidate   [("Joe",)]    -> non-solve, row_count
```

**And here is what that is worth, measured: 49 of the 1,034 reference queries in this frame
return no rows.** A query that returns nothing — `WHERE 1=0`, or anything that silently
matches nothing — therefore solves **4.7389%** of the frame by doing nothing at all. That is
the floor a trivial agent reaches, and it belongs beside every accuracy number this project
reports rather than in a footnote.

### 7. Errors — any exception is a non-solve, never a skip

A query that does not execute did not answer the question. It stays in the denominator.

```
Reference   [("Joe",)]
Candidate   raised: no such column: singer.nmae   -> non-solve, candidate_error
```

The message is carried through so 2.5 can read thirty of them by hand. A **reference** query
that fails is filed separately, as `reference_error`: it should not arise, because the frame
is every task whose reference query executes, but if it ever does it is a fact about the
substrate and must not be charged to the model.

---

## Three more decisions the seven do not cover

### Truncation is its own reason, never a wrong answer

[`src/query_pilot/sandbox.py`](../src/query_pilot/sandbox.py) caps a result at **50,000
rows** and **1,048,576 bytes**. A capped candidate compared against an uncapped reference is
a **false non-solve**, so the rule refuses to compare rather than guess:

```
Reference   [(0,), (1,), (2,), (3,), (4,)]
Candidate   [(0,), (1,), (2,)]  truncated=True   -> non-solve, truncated
Reference   [(0,), (1,), (2,), (3,), (4,)]
Candidate   [(0,), (1,), (2,), (3,), (4,)]  truncated=True   -> non-solve, truncated
```

Even when the rows happen to agree, a capped result is not evidence that they do. Folding
this into `row_count` would quietly attribute a cap's effect to the model. Named separately,
**2.4 can count how many tasks the cap decided** — and if that count is not zero, the answer
is to raise the cap, not to accept a lower accuracy figure.

Both caps are set above the largest thing this substrate legitimately returns, measured over
the whole frame by [`scripts/sandbox_caps.py`](../scripts/sandbox_caps.py) and recorded in
[`docs/sandbox-caps.json`](sandbox-caps.json): the biggest reference result is **20,662 rows**
and **289,104 bytes**, so **no reference query in this frame is truncated by either cap**.
That is the requirement rather than a happy accident — a cap that can decide a legitimate
answer is a cap that silently moves the accuracy figure, and a test asserts it still holds.

**What the sandbox's statement timeout costs, and it is a cost the rule cannot see.** A
query stopped at 30 seconds arrives here as `candidate_error`. A semantically *correct*
reformulation can be pathologically slow — a correlated-subquery form of `dev-0471`'s
question was measured at over 60 seconds against that reference's 0.289 — so the timeout
necessarily rejects some right answers. It belongs in the list of known limits below for the
same reason as the others: it pushes the number **down**.

### The decode fault belongs to the connection, not to the rule

Two of this frame's 1,034 reference queries return bytes that are not valid UTF-8, in
`wta_1`. Strict decoding reports 99.81% at 0.2 rather than 100.00%, and it would be
reporting on the stored bytes rather than on the query.

Both sides of every comparison are read through a connection configured with
`text_factory = lambda b: b.decode("utf-8", errors="replace")`, so both carry U+FFFD and
compare equal. **A decode fault is never a non-solve.** The rule itself does no decoding; if
a connection ever hands back raw `bytes`, those compare as bytes and never equal a `str`.

### The rule returns a reason, not a bool

`Comparison(solved, reason, detail)`. Phase 2.5 reads thirty failures by hand under a
protocol fixed before reading, and Phase 4 measures containment over an attack corpus; both
need to know *which* way a comparison failed, and a bare bool makes both guesswork.

The reasons are a fixed set of slugs, counted in 2.5's taxonomy and Phase 4's figures, so
they may be added to but **must not be renamed underneath a committed result**:

`solved` · `row_count` · `column_count` · `value_mismatch` · `candidate_error` ·
`reference_error` · `truncated` · `no_sql`

**Eight, and the module defines a ninth that nothing produces.** `equivalence.ROW_ORDER`
exists in the code and is not in this list: an ordered comparison that disagrees reports
`value_mismatch` or `row_count` like any other, because a row in the wrong place is a row
that does not match. It is named here so a future session does not read it as a slug the
rule emits — and **it must not be started now**, because a committed result exists and a
ninth slug appearing underneath it would change what an already-published number counted.

`no_sql` was **added in 2.3, before this project's first result existed** — which is the only
safe moment to add one. It means the model answered and the answer contained no SQL
statement to run. It is deliberately not `candidate_error`: "the SQL raised" and "there was
no SQL" are different failures, 2.5 reads thirty of them by hand, and folding one into the
other would hide a broken prompt inside a model's SQL mistakes. It is deliberately not
`executor_error` either, which 1.3 reserves for this project's own bugs, and deliberately
not a failed task, because a failed task is retried on resume and a model that answered has
already been paid for.

`compare()` never produces it. The rule compares rows and a response with no SQL never
reaches a row, so the agent constructs that `Comparison` directly.

**And in 3.3 `no_sql` acquired a second meaning, for A1 only.** A1 validates its final reply
before executing it — exactly one statement, and that statement must open a read-only query —
and a reply that fails **is not executed at all**, so it reports `no_sql` too. That covers
three cases, not one: no statement, more than one statement, and a statement that is not a
query (`INSERT INTO other SELECT ...` and its relatives). **A ninth slug was deliberately not
added**, for the reason stated two paragraphs above: a committed result exists.

So which of the three fired is carried **beside** the slug rather than inside it, as
`validation_rule` in the task row's free-form `detail` — `no_statement`,
`multiple_statements` or `not_a_query`. Anything grouping A1's failures by `reason` alone
conflates "wrote prose" with "wrote two statements"; **group by `validation_rule` beside it.**

**A0 does not validate and its numbers are unaffected.** 124 of 150 was taken with an
extractor that counts extra statements and keeps the first, and it still does; a test asserts
A0 never imports the validator. The asymmetry is deliberate — repair is one of the things A1
has and A0 does not, and rejecting a multi-statement reply without the repair that answers it
would be a penalty rather than a capability.

`Comparison.as_detail()` is shaped to drop straight into a ledger task row's free-form
`detail` mapping, which nothing in `run/` looks inside. That is the seam that keeps the run
package general.

---

## Known limits

Stated here rather than discovered in 2.4.

**Unordered comparison sorts both sides and compares pairwise.** For a single numeric column
that pairing is provably optimal. For wider rows it is an approximation, and the only way it
can be wrong is a pair of near-tied floats interleaving differently on the two sides — which
would report a false non-solve, never a false solve. It does not occur anywhere in the
1,034-task frame.

**`ORDER BY` detection is lexical.** It cannot be fooled by a literal, an identifier or a
comment, but it does not understand the query.

**Ties in an ordered reference are not repairable.** See decision 1.

**Column order is positional**, so a correct answer with its columns transposed is a
non-solve. See decision 2.

**The sandbox's statement timeout rejects correct-but-slow queries.** No timeout value
avoids this; see above.

Every one of these pushes the reported number **down**. Taken together with 2.5's finding
about ambiguous reference queries, execution accuracy in this project is a **lower bound**,
and every figure derived from it should be read as one.

**2.5 has now made that finding, and it is larger than this section anticipated.** All 26
non-solves of the first measured run were read by hand under a protocol fixed before the run
started — [`docs/FAILURES.md`](FAILURES.md), run `20260908-133316-faecd5`. **Nine of the 26
are the reference query rather than the model.** Five of those return demonstrably wrong
data, each verified by executing a query rather than argued: two compare a `TEXT` horsepower
column against `150`, so SQLite applies text affinity and a 90-horsepower car counts as over
150; one orders a `TEXT` mpg column and returns the top of a lexicographic sort; one groups
by country and returns every Spanish-speaking country for a question asking which speak it
most; one orders by treatment *count* for a question asking who *spent* the most. Four more
are questions that admit two readings.

A further **nine of the 26 are the costs listed above** — positional columns and ordered
rows — working exactly as documented. So of 26 failures, **eight are the model getting the
data wrong.** None of this re-scores anything and none of it may: the rule is what it was
when it was committed, and the figure stands as taken. What it establishes is how much room
sits between the reported number and the true one.

**And that room runs in both directions, which this section did not anticipate — corrected
2026-09-12.** The costs listed above push the number only down, and that stands. A defective
reference does more: it also *accepts* any wrong answer that reproduces its defect. 4.4 read
all 34 of A1's failures and, among the ten tasks A1 lost and A0 won, found **eight A0 solves
whose rows equal a reference verified to return wrong data** — found without reading any of
A0's other solves, so eight is a floor on that count. Execution accuracy here is therefore
**agreement with the reference queries**, not a lower bound on correct answers, and the
paragraph above that called it one is superseded by this one. Both directions are counted in
[`docs/FAILURES.md`](FAILURES.md). The rule is unchanged, and nothing is re-scored.
