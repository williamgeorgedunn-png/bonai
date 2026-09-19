"""Architect/worker pipeline orchestration.

The pipeline splits a request into single-file tasks. A strong "architect"
model plans, briefs and reviews; a small "worker" model performs each edit
with a fresh context. See docs/design/dual-gpu-pipeline-mode.md.
"""

from .config import PipelineConfig
from .ledger import Ledger, Task

__all__ = ["PipelineConfig", "Ledger", "Task"]
