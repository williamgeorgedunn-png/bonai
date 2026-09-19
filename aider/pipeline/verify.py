import re
from pathlib import Path

from aider.run_cmd import run_cmd

FAILURE_MARKERS = re.compile(
    r"(FAILED|FAIL:|=+ FAILURES|Traceback \(most recent call last\)|"
    r"^E\s+\w|AssertionError|\berror\b:)",
    re.IGNORECASE | re.MULTILINE,
)
SUMMARY_MARKERS = re.compile(
    r"(\d+ (passed|failed|error)|Ran \d+ tests?|OK\b|FAILED \(|=+ short test summary)",
    re.IGNORECASE,
)


def clamp(text, token_count, max_tokens):
    """Drop trailing lines until the text fits the budget."""
    if not text:
        return ""
    if not max_tokens or token_count(text) <= max_tokens:
        return text
    lines = text.splitlines()
    while lines and token_count("\n".join(lines)) > max_tokens:
        drop = max(1, len(lines) // 10)
        lines = lines[:-drop]
    return "\n".join(lines) + "\n(truncated)"


def lint_file(coder, abs_fname):
    """Lint one file, returning error text or ''."""
    try:
        return coder.linter.lint(abs_fname) or ""
    except Exception as err:
        return f"Linter failed: {err}"


def is_tracked(repo, rel_fname):
    if not repo:
        return False
    try:
        return rel_fname in repo.get_tracked_files()
    except Exception:
        return False


def file_diff(repo, root, rel_fname, read_text=None):
    """Uncommitted diff for one file, or the full body of a new file."""
    abs_fname = str(Path(root) / rel_fname)

    if repo:
        try:
            diff = repo.repo.git.diff("HEAD", "--", rel_fname)
        except Exception:
            diff = ""
        if diff.strip():
            return diff

    reader = read_text or (lambda path: Path(path).read_text(encoding="utf-8", errors="replace"))
    try:
        body = reader(abs_fname) or ""
    except OSError:
        body = ""

    if not body.strip():
        return ""

    numbered = "\n".join(f"{n + 1:>4} {line}" for n, line in enumerate(body.splitlines()))
    return f"(new or untracked file {rel_fname}, full contents)\n{numbered}"


def revert_files(repo, rel_fnames, io=None):
    """Undo worker edits to files outside the task's scope."""
    reverted = []
    for rel_fname in rel_fnames:
        try:
            repo.repo.git.checkout("--", rel_fname)
            reverted.append(rel_fname)
        except Exception as err:
            if io:
                io.tool_warning(f"Could not revert {rel_fname}: {err}")
    return reverted


def run_tests(test_cmd, root, verbose=False, error_print=None):
    """Run the test command, returning (exit_code, output)."""
    if not test_cmd:
        return 0, ""
    if callable(test_cmd):
        output = test_cmd() or ""
        return (1 if output else 0), output
    exit_status, output = run_cmd(
        test_cmd, verbose=verbose, error_print=error_print, cwd=str(root)
    )
    return exit_status, output or ""


def trim_test_output(text, token_count, max_tokens):
    """Keep the summary and the first failure; drop the rest.

    Test runners emit far more than the architect needs, and the whole point
    of the pipeline is that no step floods its context.
    """
    if not text:
        return ""

    lines = text.splitlines()
    if token_count(text) <= max_tokens:
        return text

    summary = [line for line in lines[-30:] if line.strip()]
    summary_block = [line for line in summary if SUMMARY_MARKERS.search(line)]
    if not summary_block:
        summary_block = summary[-8:]

    first_failure = None
    for index, line in enumerate(lines):
        if FAILURE_MARKERS.search(line):
            first_failure = index
            break

    # The summary is the most useful part, so it is kept whole and the failure
    # detail gets whatever budget is left.
    head = f"(output trimmed from {len(lines)} lines)\n# Summary\n" + "\n".join(summary_block)

    if first_failure is not None:
        start = max(0, first_failure - 3)
        detail = "# First failure\n" + "\n".join(lines[start : start + 45])
    else:
        detail = "# Output head\n" + "\n".join(lines[:30])

    remaining = max_tokens - token_count(head)
    if remaining < 20:
        return clamp(head, token_count, max_tokens)
    return head + "\n" + clamp(detail, token_count, remaining)
