#!/usr/bin/env python
"""End-to-end smoke test for pipeline mode against two separate endpoints.

Stands up two fake OpenAI-compatible servers on two ports, one acting as the
architect and one as the worker, then runs aider against them exactly as you
would against two llama-server instances on two GPUs. Verifies that the file
was edited and that each accepted task produced its own commit.

    python scripts/pipeline/smoke-two-endpoints.py

This needs no GPU and no real model. It checks the plumbing: per-model
api_base, the orchestration loop, and one commit per task.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CALC = '''\
"""A tiny calculator."""


def scale(value, factor):
    return value * factor
'''

PLAN = """\
Clamp the factor.

```yaml
plan_summary: |
  Make scale() treat a negative factor as zero.
tasks:
  - id: T1
    title: Clamp the factor in scale
    file: calc.py
    kind: edit
    symbols: [scale]
    depends_on: []
```
"""

BRIEF = """\
# Task T1: Clamp the factor in scale
File to edit: calc.py   (this is the ONLY file you may change)

## Goal
scale() should treat a negative factor as zero.

## Changes
- `scale(value, factor)`: if factor < 0, use 0 instead.

## Acceptance criteria
- [ ] scale(5, -2) returns 0
"""

REVIEW = """\
VERDICT: ACCEPT
NOTE: scale clamps negative factors
The diff matches the brief.
REMEMBER: scale() clamps a negative factor to zero
"""

WORKER_EDIT = '''\
calc.py
```
"""A tiny calculator."""


def scale(value, factor):
    if factor < 0:
        factor = 0
    return value * factor
```
'''


class FakeModelServer(BaseHTTPRequestHandler):
    """Minimal /v1/chat/completions that replies based on the step it sees."""

    role = "architect"
    seen = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.endswith("/health") or self.path.rstrip("/").endswith("/models"):
            self.respond(200, {"status": "ok", "data": [{"id": self.role, "object": "model"}]})
        else:
            self.respond(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        prompt = "\n".join(m.get("content") or "" for m in body.get("messages", []))
        reply = self.pick_reply(prompt)
        self.seen.append((self.role, self.step_name(prompt)))
        self.respond(
            200,
            {
                "id": "smoke",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model", "fake"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": reply},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )

    def step_name(self, prompt):
        for step in ("PLAN", "BRIEF", "REVIEW", "TRIAGE", "COMPACT"):
            if f"# Step: {step}" in prompt or f"# Step: {step.replace('_', ' ')}" in prompt:
                return step
        return "other"

    def pick_reply(self, prompt):
        if self.role == "worker":
            return WORKER_EDIT
        step = self.step_name(prompt)
        return {"PLAN": PLAN, "BRIEF": BRIEF, "REVIEW": REVIEW}.get(step, "VERDICT: ACCEPT")

    def respond(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_server(role, seen, port=0):
    handler = type(f"{role.title()}Handler", (FakeModelServer,), {"role": role, "seen": seen})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run(cmd, cwd=None, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


def run_smoke():
    """Drive aider against two fake endpoints. Returns a list of failure strings."""
    seen = []
    architect = start_server("architect", seen)
    worker = start_server("worker", seen)
    architect_port = architect.server_address[1]
    worker_port = worker.server_address[1]
    print(f"Fake architect on :{architect_port}, fake worker on :{worker_port}")

    failures = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "calc.py").write_text(CALC)
            run(["git", "init", "-q"], cwd=repo)
            run(["git", "config", "user.email", "smoke@example.com"], cwd=repo)
            run(["git", "config", "user.name", "Smoke Test"], cwd=repo)
            run(["git", "add", "calc.py"], cwd=repo)
            run(["git", "commit", "-qm", "initial"], cwd=repo)

            env = dict(os.environ)
            env.update(
                OPENAI_API_KEY="dummy",
                AIDER_ANALYTICS="false",
                AIDER_CHECK_UPDATE="false",
            )

            result = run(
                [
                    sys.executable,
                    "-m",
                    "aider",
                    "--pipeline",
                    "--pipeline-architect-model",
                    "openai/architect",
                    "--pipeline-architect-api-base",
                    f"http://127.0.0.1:{architect_port}/v1",
                    "--pipeline-worker-model",
                    "openai/worker",
                    "--pipeline-worker-api-base",
                    f"http://127.0.0.1:{worker_port}/v1",
                    "--pipeline-approve",
                    "never",
                    "--no-pipeline-prewarm",
                    "--no-show-model-warnings",
                    "--no-check-update",
                    "--no-gitignore",
                    "--yes-always",
                    "--no-pretty",
                    "--no-stream",
                    "--map-tokens",
                    "512",
                    "--message",
                    "make scale clamp a negative factor to zero",
                ],
                cwd=repo,
                env=env,
            )
            print(result.stdout[-4000:])
            if result.stderr.strip():
                print("stderr:", result.stderr[-2000:], file=sys.stderr)
            if result.returncode not in (0, None):
                failures.append(f"aider exited {result.returncode}")

            source = (repo / "calc.py").read_text()
            if "if factor < 0" not in source:
                failures.append(f"calc.py was not edited:\n{source}")

            log = run(["git", "log", "--oneline"], cwd=repo).stdout.strip().splitlines()
            if len(log) != 2:
                failures.append(f"expected 2 commits (initial + T1), got {len(log)}: {log}")
            elif not re.search(r"pipeline T1", log[0]):
                failures.append(f"the commit is not the pipeline task: {log[0]}")

            ledger = repo / ".aider.pipeline" / "ledger.yml"
            if not ledger.exists():
                failures.append("no ledger was written")
            else:
                text = ledger.read_text()
                for expected in ("status: accepted", "clamps a negative factor"):
                    if expected not in text:
                        failures.append(f"ledger is missing {expected!r}:\n{text}")

            brief = repo / ".aider.pipeline" / "briefs" / "T1.md"
            if not brief.exists():
                failures.append("no brief was saved")

            roles = {role for role, _step in seen}
            if roles != {"architect", "worker"}:
                failures.append(f"both endpoints should have been called, saw: {sorted(roles)}")

            steps = [step for role, step in seen if role == "architect"]
            for expected in ("PLAN", "BRIEF", "REVIEW"):
                if expected not in steps:
                    failures.append(f"the architect was never asked to {expected}; saw {steps}")
    finally:
        architect.shutdown()
        worker.shutdown()

    return failures, seen


def main():
    failures, seen = run_smoke()
    print()
    if failures:
        print("FAILED")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("PASSED: two endpoints, one commit per task, ledger and brief written")
    print(f"Calls: {seen}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
