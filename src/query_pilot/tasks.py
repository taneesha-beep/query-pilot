"""One task: a question, the database it is asked of, and the query that answers it.

Beside `splits.py` and `difficulty.py` rather than inside `agents/`, because a task is a
fact about the substrate and not about any agent. A0, A1 and A2 all read the same ones.

**The reference query is carried, and it is never given to a model.** It is here because
scoring needs it and because a task without it cannot be scored at all; every prompt in
this project is built from :attr:`Task.question` and the live schema, and nothing else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Task", "load_split", "load_tasks"]

#: How a position in Spider's `dev.json` becomes a task ID. Fixed at 0.2 and depended on by
#: `splits/frame.json`, every split beside it, and `docs/equivalence-check.json`.
TASK_ID = "dev-{:04d}"


@dataclass(frozen=True, slots=True)
class Task:
    """A question to answer, and what answering it correctly looks like."""

    task_id: str
    db_id: str
    question: str
    reference_sql: str


def load_split(path: Path | str) -> tuple[str, ...]:
    """Task IDs from a split file, **in the order the file lists them**.

    The order matters and is not incidental: a run's fingerprint covers its ordered task
    list, so re-sorting here would refuse every resume of a run started before the change.

    Handles both committed shapes — `splits/frame.json` calls the list `tasks` and the
    three splits beside it call it `task_ids` — rather than making a caller know which.
    """
    document = json.loads(Path(path).read_text())
    for key in ("task_ids", "tasks"):
        if key in document:
            return tuple(document[key])
    raise ValueError(f"{path} has neither 'task_ids' nor 'tasks'")


def load_tasks(spider_root: Path | str, split: Path | str) -> tuple[Task, ...]:
    """Every task named by ``split``, read out of Spider's `dev.json`.

    Raises if the split names a task the substrate does not have, rather than returning a
    shorter list: a run that quietly measured 149 of 150 tasks would report a rate over the
    wrong denominator.
    """
    dev = json.loads((Path(spider_root) / "dev.json").read_text())
    by_id = {
        TASK_ID.format(index): Task(
            task_id=TASK_ID.format(index),
            db_id=item["db_id"],
            question=item["question"],
            reference_sql=item["query"],
        )
        for index, item in enumerate(dev)
    }
    tasks = []
    for task_id in load_split(split):
        if task_id not in by_id:
            raise KeyError(f"{task_id} is not in {spider_root}/dev.json")
        tasks.append(by_id[task_id])
    return tuple(tasks)
