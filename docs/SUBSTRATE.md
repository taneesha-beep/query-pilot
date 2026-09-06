# The substrate

Spider dev: 1,034 natural-language questions over 20 SQLite databases, each question paired
with a reference SQL query. This project executes both the agent's query and the reference
query and compares rows, so the database files — not the question and SQL pairs — are what
the project actually needs.

The databases are **not committed**. `data/` is ignored. This document plus
[`substrate-fingerprint.json`](substrate-fingerprint.json) are what make an acquisition
reproducible and a substitution detectable.

## Provenance

| | |
|---|---|
| Archive | `spider.zip`, 99,736,136 bytes |
| SHA256 | `5ddff97bb1d421282c593e8d30ce0ce107270f4dd4a21d60eba4bf287d5956b1` |
| Acquired from | `https://huggingface.co/datasets/HusnaManakkot/new-spider-HM/resolve/main/spider.zip` |
| Acquisition date | 2026-09-06 |
| Release | Spider 1.0, the 2020-08-03 corrected release (per the archive's own `README.txt`) |

### Which kind of check this got

A checksum computed over a file you just downloaded proves only that the download did not
corrupt. It becomes provenance only when someone else published the same value first. This
archive got **both kinds, and they agree**:

1. **Publisher-declared, verified before download.** HuggingFace publishes the git-LFS
   SHA256 of every file through its API. The value above was read from
   `https://huggingface.co/api/datasets/HusnaManakkot/new-spider-HM/tree/main?recursive=true`
   *before* fetching the archive, and the downloaded bytes hash to it.
2. **Independent re-host, same digest.** A second, unrelated HuggingFace account publishes
   the same archive under a different filename — `Sreenath/spider`, `spider_v1.zip` — with
   an identical size and an identical declared SHA256.
3. **The original distribution, byte-identical.** The Yale LILY Google Drive distribution
   linked from the Spider project
   (`https://drive.usercontent.google.com/download?id=1TqleXec_OykOYFREKKtschzY29dUcVAQ&export=download`,
   served as `spider.zip`) was downloaded separately on 2026-09-06 and hashes to the same
   value. Google Drive publishes no checksum of its own, so this is a byte-comparison
   against the canonical source rather than a declared-digest check.

So the mirror is not a trusted-by-assertion source: it is byte-identical to the
distribution the dataset's authors publish, and that was confirmed by downloading both.

`dev.json` SHA256: `30d64a3fccde493226df79687aed9e4a1c0129525baf44f29c0573d914d758a4`
`tables.json` SHA256: `61bb20aa401f03164e2d7f3b16509b7b5f79cc9c943ca7bd159046df1159e2ed`

## Fingerprint

Checksums go stale when an archive is re-zipped or a mirror rebuilds. The shape of the
data does not. A substrate swap that preserved every number below would be a different
kind of event from the one this section is here to catch.

**20 databases · 80 tables · 539,860 rows · 105,684,992 bytes of SQLite**

| db_id | tables | rows |
|---|---|---|
| battle_death | 3 | 28 |
| car_1 | 6 | 890 |
| concert_singer | 4 | 31 |
| course_teach | 3 | 23 |
| cre_Doc_Template_Mgt | 4 | 55 |
| dog_kennels | 8 | 72 |
| employee_hire_evaluation | 4 | 32 |
| flight_2 | 3 | 1,312 |
| museum_visit | 3 | 20 |
| network_1 | 3 | 46 |
| orchestra | 4 | 40 |
| pets_1 | 3 | 40 |
| poker_player | 2 | 12 |
| real_estate_properties | 5 | 40 |
| singer | 2 | 16 |
| student_transcripts_tracking | 11 | 165 |
| tvshow | 3 | 39 |
| voter_1 | 3 | 320 |
| world_1 | 3 | 5,302 |
| wta_1 | 3 | 531,377 |

Per-table row counts are in `substrate-fingerprint.json`. Note the shape: 19 of the 20
databases are tiny, and `wta_1` holds 98% of all rows in the substrate. That matters for
Phase 2.2's row cap and for anything timing-related.

## Task IDs

`dev.json` carries no identifier field. A task ID here is the **zero-based position of the
question in `dev.json`**, formatted `dev-0000` … `dev-1033`. That is stable only because
`dev.json` is pinned by the checksum above, which is a further reason the checksum is
recorded rather than assumed.

## Gold-query execution

Every one of the 1,034 reference queries was executed against its own database by
`scripts/substrate_check.py` on 2026-09-06.

| | |
|---|---|
| Executed without raising | **1,034 / 1,034 — 100.00%** |
| Failed | 0 |
| Dropped from the sampling frame | **0** |
| Reference queries returning zero rows | 49 |

**The roadmap predicted this would not be 100%.** It is. The stop-gate threshold was 90%,
so the gate passes, but the prediction was wrong and that is recorded rather than quietly
absorbed.

### What "executes" means here, exactly

**Executes = does not raise.** An empty result is a legitimate reference result, not a
failure: the equivalence rule in Phase 2.1 scores an empty result against an empty
reference as a solve, so excluding the 49 zero-row tasks would delete the one case where
the instrument is most easily fooled. They stay in the frame.

Connections are opened read-only and configured with
`text_factory = lambda b: b.decode("utf-8", errors="replace")`. This is load-bearing and
the cost of it is measured, not assumed: **under Python's strict default text factory, 2
of the 1,034 reference queries fail** — `dev-0455` and `dev-0456`, both against `wta_1`,
both raising `OperationalError: Could not decode to UTF-8 column 'last_name' with text
'Treyes Albarrac��N'`. That is a defect in the stored bytes surfacing through the
client's codec, not a defect in the query, and the Spider evaluation scripts tolerate it
the same way. Without the tolerance the recorded rate would be 99.81%.

### What this number is not

100% execution means every reference query **runs**. It does not mean every reference
query is **right**. A query that runs and returns the wrong rows for the question asked is
invisible here and will only surface in Phase 2.5's read of 30 failures by hand. Until
that read happens, no claim is made about reference correctness, and if that read finds a
meaningful share of wrong references, **every accuracy figure in this project is a lower
bound** and will say so.

## The sampling frame

The working set and the reserve set are drawn from **the 1,034 tasks whose reference query
executes** — which, here, is all of them. The rule is fixed as of 2026-09-06 and is not
revisited: re-deriving the frame after seeing results would invalidate every number taken
on it. Had any task been dropped, its ID would be listed here.

## Reproducing this

```
curl -L -o data/raw/spider.zip \
  https://huggingface.co/datasets/HusnaManakkot/new-spider-HM/resolve/main/spider.zip
shasum -a 256 data/raw/spider.zip   # must print 5ddff97b...56b1
unzip -q data/raw/spider.zip -d data/
uv run python scripts/substrate_check.py
```

The script rewrites `docs/substrate-fingerprint.json`. If that file changes, the substrate
changed.
