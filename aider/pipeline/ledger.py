import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from .memory import WorkingMemory

LEDGER_VERSION = 1

# Terminal states never get scheduled again.
DONE_STATUSES = ("accepted", "skipped")
STATUSES = (
    "pending",
    "briefed",
    "editing",
    "review",
    "testing",
    "accepted",
    "failed",
    "skipped",
)
KINDS = ("edit", "test", "new_file")


class LedgerError(ValueError):
    pass


@dataclass
class Task:
    id: str
    title: str
    file: str
    kind: str = "edit"
    symbols: list = field(default_factory=list)
    depends_on: list = field(default_factory=list)
    status: str = "pending"
    attempts: int = 0
    test_rounds: int = 0
    commit: str = ""
    notes: str = ""
    brief_path: str = ""

    @property
    def done(self):
        return self.status in DONE_STATUSES

    def to_dict(self):
        return {k: v for k, v in asdict(self).items() if v not in ("", [], 0)}


class Ledger:
    """The pipeline's durable state: plan, tasks, working memory, history.

    The architect is stateless; everything it is allowed to remember between
    steps lives here, which is what keeps its context bounded.
    """

    def __init__(self, path, request="", config=None):
        self.path = Path(path)
        self.request = request
        self.clarifications = []
        self.plan_summary = ""
        self.tasks = []
        self.history = []
        self.memory = WorkingMemory(
            max_tokens=config.working_memory_tokens if config else 2000,
        )

    # ------------------------------------------------------------------ io

    @classmethod
    def load(cls, path, config=None):
        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        version = data.get("version", LEDGER_VERSION)
        if version != LEDGER_VERSION:
            raise LedgerError(f"Unsupported ledger version {version} in {path}")

        ledger = cls(path, request=data.get("request", ""), config=config)
        ledger.clarifications = list(data.get("clarifications") or [])
        ledger.plan_summary = data.get("plan_summary", "") or ""
        ledger.history = list(data.get("history") or [])
        for raw in data.get("tasks") or []:
            ledger.tasks.append(Task(**raw))
        ledger.memory.load(data.get("working_memory") or [])
        return ledger

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": LEDGER_VERSION,
            "request": self.request,
            "clarifications": self.clarifications,
            "plan_summary": self.plan_summary,
            "tasks": [t.to_dict() for t in self.tasks],
            "working_memory": self.memory.dump(),
            "history": self.history[-200:],
        }
        text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=88)
        self.path.write_text(text, encoding="utf-8")

    # --------------------------------------------------------------- tasks

    def get(self, task_id):
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def next_id(self, prefix="T"):
        used = set()
        for task in self.tasks:
            if task.id.startswith(prefix) and task.id[len(prefix) :].isdigit():
                used.add(int(task.id[len(prefix) :]))
        num = 1
        while num in used:
            num += 1
        return f"{prefix}{num}"

    def set_tasks(self, raw_tasks, max_tasks=None):
        """Replace the task list from architect output. Raises LedgerError."""
        tasks = [self._coerce(raw, index) for index, raw in enumerate(raw_tasks)]
        if not tasks:
            raise LedgerError("The plan contains no tasks.")
        if max_tasks and len(tasks) > max_tasks:
            raise LedgerError(
                f"The plan has {len(tasks)} tasks but the limit is {max_tasks}."
                " Produce a coarser plan with fewer, larger tasks."
            )
        self.tasks = tasks
        self.validate_dag()

    def add_task(self, raw, after=None):
        """Insert one task (used by REPLAN and test triage). Returns the Task."""
        task = self._coerce(raw, len(self.tasks))
        if self.get(task.id):
            task.id = self.next_id()
        if after is None:
            self.tasks.append(task)
        else:
            index = next((i for i, t in enumerate(self.tasks) if t.id == after), None)
            if index is None:
                self.tasks.append(task)
            else:
                self.tasks.insert(index + 1, task)
        self.validate_dag()
        return task

    def _coerce(self, raw, index):
        if not isinstance(raw, dict):
            raise LedgerError(f"Task {index + 1} is not a mapping: {raw!r}")

        known = {f for f in Task.__dataclass_fields__}
        data = {k: v for k, v in raw.items() if k in known}

        for required in ("title", "file"):
            if not data.get(required):
                raise LedgerError(f"Task {index + 1} is missing '{required}'.")

        data.setdefault("id", f"T{index + 1}")
        data["id"] = str(data["id"]).strip()

        kind = data.get("kind") or "edit"
        if kind not in KINDS:
            raise LedgerError(f"Task {data['id']} has unknown kind {kind!r}.")
        data["kind"] = kind

        status = data.get("status") or "pending"
        if status not in STATUSES:
            raise LedgerError(f"Task {data['id']} has unknown status {status!r}.")
        data["status"] = status

        # Store paths POSIX-style so ledgers written on Windows stay portable.
        data["file"] = str(data["file"]).strip().replace("\\", "/")

        for name in ("symbols", "depends_on"):
            value = data.get(name) or []
            if isinstance(value, str):
                value = [v.strip() for v in value.split(",") if v.strip()]
            data[name] = [str(v).strip() for v in value]

        return Task(**data)

    def validate_dag(self):
        ids = {t.id for t in self.tasks}
        if len(ids) != len(self.tasks):
            raise LedgerError("Task ids are not unique.")

        for task in self.tasks:
            unknown = [d for d in task.depends_on if d not in ids]
            if unknown:
                raise LedgerError(
                    f"Task {task.id} depends on unknown task(s): {', '.join(unknown)}"
                )

        # Kahn's algorithm; anything left over is in a cycle.
        pending = {t.id: set(t.depends_on) for t in self.tasks}
        while True:
            ready = [tid for tid, deps in pending.items() if not deps]
            if not ready:
                break
            for tid in ready:
                del pending[tid]
                for deps in pending.values():
                    deps.discard(tid)
        if pending:
            raise LedgerError(
                "Task dependencies contain a cycle: " + ", ".join(sorted(pending))
            )

    def next_task(self):
        """First schedulable task in ledger order, or None."""
        for task in self.tasks:
            if task.status in DONE_STATUSES or task.status == "failed":
                continue
            if all(self._dep_satisfied(dep) for dep in task.depends_on):
                return task
        return None

    def _dep_satisfied(self, task_id):
        dep = self.get(task_id)
        return dep is None or dep.status in DONE_STATUSES

    def blocked_tasks(self):
        """Tasks that can never run because a dependency failed."""
        blocked = []
        for task in self.tasks:
            if task.done or task.status == "failed":
                continue
            for dep_id in task.depends_on:
                dep = self.get(dep_id)
                if dep is not None and dep.status == "failed":
                    blocked.append(task)
                    break
        return blocked

    def is_complete(self):
        return all(t.status in DONE_STATUSES or t.status == "failed" for t in self.tasks)

    def counts(self):
        counts = {}
        for task in self.tasks:
            counts[task.status] = counts.get(task.status, 0) + 1
        return counts

    # -------------------------------------------------------------- render

    def render_view(self, token_count=None, max_tokens=None, current_id=None):
        """The compact ledger the architect sees. Never includes briefs."""
        for detail in ("full", "collapse_accepted", "count_accepted"):
            text = self._render(detail, current_id)
            if not token_count or not max_tokens:
                return text
            if token_count(text) <= max_tokens:
                return text
        return text

    def _render(self, detail, current_id):
        lines = ["# Request", self.request.strip() or "(none)"]

        if self.clarifications:
            lines.append("")
            lines.append("# Clarifications from the user")
            lines += [f"- {c}" for c in self.clarifications]

        if self.plan_summary:
            lines.append("")
            lines.append("# Plan")
            lines.append(self.plan_summary.strip())

        lines.append("")
        lines.append("# Tasks")
        if not self.tasks:
            lines.append("(no tasks yet)")
            return "\n".join(lines)

        accepted = [t for t in self.tasks if t.status == "accepted"]
        if detail == "count_accepted" and accepted:
            ids = ", ".join(t.id for t in accepted)
            lines.append(f"- {len(accepted)} task(s) accepted and committed: {ids}")

        for task in self.tasks:
            if task.status == "accepted" and detail == "count_accepted":
                continue
            marker = " <- current step" if task.id == current_id else ""
            head = f"- {task.id} [{task.status}] {task.file}: {task.title}{marker}"
            lines.append(head)
            if task.status == "accepted" and detail != "full":
                continue
            if task.depends_on:
                lines.append(f"    needs: {', '.join(task.depends_on)}")
            if task.notes:
                lines.append(f"    note: {task.notes}")

        return "\n".join(lines)

    # ------------------------------------------------------------- history

    def log(self, step, task=None, **kwargs):
        entry = {"ts": int(time.time()), "step": step}
        if task:
            entry["task"] = task
        entry.update({k: v for k, v in kwargs.items() if v is not None})
        self.history.append(entry)
        return entry

    def totals(self):
        tokens_in = sum(e.get("tokens_in", 0) for e in self.history)
        tokens_out = sum(e.get("tokens_out", 0) for e in self.history)
        seconds = sum(e.get("seconds", 0) for e in self.history)
        calls = sum(1 for e in self.history if "tokens_in" in e)
        worker_calls = sum(
            1 for e in self.history if "tokens_in" in e and e.get("role") == "worker"
        )
        return dict(
            calls=calls,
            worker_calls=worker_calls,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            seconds=seconds,
        )
