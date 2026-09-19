import dataclasses
from dataclasses import dataclass, fields

APPROVE_CHOICES = ("plan", "task", "never")


@dataclass
class PipelineConfig:
    """Token budgets and limits for pipeline mode.

    Every prompt section the architect sees has a cap here, so its context
    stays bounded no matter how large the repo is or how long the run lasts.
    """

    # Architect prompt budgets
    architect_map_tokens: int = 2000
    ledger_view_tokens: int = 1500
    working_memory_tokens: int = 2000
    facts_tokens: int = 3000

    # Step input budgets
    review_diff_tokens: int = 2500
    lint_output_tokens: int = 800
    test_output_tokens: int = 1500
    source_slice_tokens: int = 1200

    # Worker budgets
    worker_snippet_tokens: int = 2000
    whole_file_max_tokens: int = 3000

    # Limits
    grep_hits: int = 40
    max_tasks: int = 25
    max_attempts: int = 2
    max_test_rounds: int = 3
    max_need_rounds: int = 3
    max_worker_calls: int = 200

    # Behaviour
    approve: str = "plan"
    tdd: bool = False
    prewarm: bool = True
    stream_worker: bool = False

    @classmethod
    def budget_fields(cls):
        """Names of the numeric knobs, used to generate CLI flags."""
        return [f.name for f in fields(cls) if f.type is int or f.type == "int"]

    @classmethod
    def from_args(cls, args):
        """Build a config from parsed CLI args, ignoring unset (None) values."""
        kwargs = {}
        for field in fields(cls):
            value = getattr(args, f"pipeline_{field.name}", None)
            if value is not None:
                kwargs[field.name] = value
        return cls(**kwargs)

    def replace(self, **kwargs):
        return dataclasses.replace(self, **kwargs)

    def validate(self):
        problems = []
        if self.approve not in APPROVE_CHOICES:
            problems.append(
                f"pipeline approve must be one of {', '.join(APPROVE_CHOICES)}, not {self.approve}"
            )
        for name in self.budget_fields():
            if getattr(self, name) < 0:
                problems.append(f"pipeline {name} must not be negative")
        return problems

    @property
    def architect_prompt_budget(self):
        """Rough ceiling on one architect prompt, excluding step inputs."""
        return (
            self.architect_map_tokens
            + self.ledger_view_tokens
            + self.working_memory_tokens
            + self.facts_tokens
        )
