import json
import os
import unittest
from pathlib import Path

import git

from aider.coders import Coder
from aider.dump import dump  # noqa: F401
from aider.io import InputOutput
from aider.models import Model
from aider.utils import GitTemporaryDirectory

SERVICE_PY = """\
from storage import save_record


def normalize(raw_value):
    return raw_value.strip()


def handle_request(raw_value):
    payload = normalize(raw_value)
    save_record(payload)
    return payload
"""

STORAGE_PY = """\
def save_record(payload):
    return payload
"""

API_PY = """\
from service import handle_request


def post(raw_value):
    return handle_request(raw_value)
"""

TEST_SERVICE_PY = """\
from service import handle_request


def test_handle_request_strips():
    assert handle_request(" x ") == "x"
"""

EDIT_AND_TRACE = """\
Here is the change, and I need to see the callers too.

service.py
<<<<<<< SEARCH
    return raw_value.strip()
=======
    return raw_value.strip().lower()
>>>>>>> REPLACE

```trace
handle_request
```
"""


class TestCoderTracing(unittest.TestCase):
    def setUp(self):
        self.GPT35 = Model("gpt-3.5-turbo")

    def make_repo(self):
        files = {
            "service.py": SERVICE_PY,
            "storage.py": STORAGE_PY,
            "api.py": API_PY,
            "tests/test_service.py": TEST_SERVICE_PY,
        }
        for fname, content in files.items():
            path = Path(fname)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

        repo = git.Repo.init(os.getcwd())
        repo.git.add(A=True)
        repo.git.commit("-m", "init")

        return files

    def make_coder(self, **kwargs):
        io = InputOutput(yes=True)
        kwargs.setdefault("llm_log", False)
        return Coder.create(self.GPT35, "diff", io=io, use_git=True, **kwargs)

    def reply_with(self, coder, content):
        """Run one message, with the LLM replying `content`."""

        replies = [content]

        def mock_send(*args, **kwargs):
            coder.partial_response_content = replies.pop(0) if replies else "ok"
            coder.partial_response_function_call = dict()
            return []

        coder.send = mock_send
        coder.run(with_message="please look at this", preproc=False)

    def test_tracer_is_created_with_the_repo_map(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)
            self.assertIsNotNone(coder.tracer)

    def test_tracer_is_disabled_by_flag(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024, trace=False)
            self.assertIsNone(coder.tracer)

    def test_tracer_is_disabled_without_a_repo_map(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=0)
            self.assertIsNone(coder.tracer)

    def test_trace_instructions_only_appear_when_enabled(self):
        with GitTemporaryDirectory():
            self.make_repo()

            coder = self.make_coder(map_tokens=1024)
            system = coder.format_chat_chunks().system[0]["content"]
            self.assertIn("trace", system)

            coder = self.make_coder(map_tokens=1024, trace=False)
            system = coder.format_chat_chunks().system[0]["content"]
            self.assertNotIn("```trace", system)

    def test_trace_request_in_reply_is_answered(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            self.reply_with(coder, "I need context first.\n\n```trace\nhandle_request\n```\n")

            history = "\n".join(msg["content"] for msg in coder.cur_messages + coder.done_messages)
            self.assertIn("api.py", history)
            self.assertIn("handle_request", history)

    def test_trace_results_are_not_kept_in_the_chat_history(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            self.reply_with(coder, "```trace\nhandle_request\n```\n")

            coder.move_back_cur_messages("done")
            history = "\n".join(
                msg["content"] for msg in coder.done_messages if isinstance(msg["content"], str)
            )
            self.assertIn("results omitted", history)
            self.assertNotIn("Callers of", history)

    def test_trace_results_expire_after_one_message(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            self.reply_with(coder, "```trace\nhandle_request\n```\n")
            self.assertIn("Callers of", "\n".join(m["content"] for m in coder.cur_messages))

            # No edits were made, so nothing moved to done_messages, but the
            # next message must not still be paying for the snippets
            self.reply_with(coder, "ok")
            current = "\n".join(
                m["content"] for m in coder.cur_messages if isinstance(m["content"], str)
            )
            self.assertNotIn("Callers of", current)
            self.assertIn("results omitted", current)

    def history(self, coder):
        return "\n".join(
            msg["content"]
            for msg in coder.done_messages + coder.cur_messages
            if isinstance(msg["content"], str)
        )

    def test_edits_are_still_linted_when_the_reply_also_traces(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024, fnames=["service.py"], auto_lint=True)

            linted = []
            coder.lint_edited = lambda fnames: linted.append(set(fnames))

            self.reply_with(coder, EDIT_AND_TRACE)

            self.assertEqual(linted, [{"service.py"}])
            self.assertIn("lower()", Path("service.py").read_text())
            self.assertIn("Callers of", self.history(coder))

    def test_lint_errors_and_trace_results_are_sent_together(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024, fnames=["service.py"], auto_lint=True)
            coder.lint_edited = lambda fnames: "service.py:1 is not to my taste"

            self.reply_with(coder, EDIT_AND_TRACE)

            history = self.history(coder)
            self.assertIn("not to my taste", history)
            self.assertIn("Callers of", history)

    def test_shell_commands_run_when_the_reply_also_traces(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024, fnames=["service.py"])

            events = []
            answer_trace = coder.get_trace_reply

            coder.run_shell_commands = lambda: events.append("shell") or ""
            coder.get_trace_reply = lambda content: (
                events.append("trace"),
                answer_trace(content),
            )[1]

            self.reply_with(coder, EDIT_AND_TRACE)

            # The reply's own shell commands run before the trace sends it back
            self.assertEqual(events[:2], ["shell", "trace"])
            self.assertIn("Callers of", self.history(coder))

    def test_trace_rounds_are_capped(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)
            coder.num_trace_rounds = coder.max_trace_rounds

            reply = coder.get_trace_reply("```trace\nhandle_request\n```\n")
            self.assertIn("can't run any more traces", reply.lower())

    def test_repeated_trace_request_is_refused(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            first = coder.get_trace_reply("```trace\nhandle_request\n```\n")
            self.assertIn("Callers of", first)

            second = coder.get_trace_reply("```trace\nhandle_request\n```\n")
            self.assertIn("already traced", second)

    def test_unknown_symbol_gets_an_actionable_reply(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            reply = coder.get_trace_reply("```trace\nhandle_requst\n```\n")
            self.assertIn("No uses", reply)
            self.assertIn("handle_request", reply)

    def test_loose_trace_request_needs_a_real_symbol(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            self.assertIsNone(coder.get_trace_reply("Let me trace the logic through the code."))
            self.assertIsNotNone(coder.get_trace_reply("Can you trace handle_request for me?"))

    def test_no_trace_request_means_no_reply(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            self.assertIsNone(coder.get_trace_reply("Here is my answer, no tracing needed."))

    def test_auto_trace_chunk_is_built_from_the_user_message(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            coder.cur_messages = [dict(role="user", content="rework handle_request please")]
            chunks = coder.format_chat_chunks()

            self.assertTrue(chunks.trace)
            self.assertIn("handle_request", chunks.trace[0]["content"])
            # It is a transient chunk, it must not be in the chat messages
            self.assertNotIn(chunks.trace[0], coder.cur_messages)

    def test_auto_trace_follows_the_models_reply_too(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            coder.cur_messages = [
                dict(role="user", content="something is wrong with the lowercasing"),
                dict(role="assistant", content="I think save_record is the culprit."),
            ]
            chunks = coder.format_chat_chunks()

            self.assertTrue(chunks.trace)
            self.assertIn("save_record", chunks.trace[0]["content"])

    def test_auto_trace_ignores_its_own_results(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            trace_text = coder.get_trace_reply("```trace\nhandle_request\n```\n")
            coder.cur_messages = [
                dict(role="user", content="fix the lowercasing"),
                dict(role="assistant", content="```trace\nhandle_request\n```"),
                dict(role="user", content=trace_text),
            ]

            # Neither the trace output nor the symbol it already traced
            self.assertEqual(coder.get_traceable_idents(coder.get_trace_topic_text()), [])

    def test_auto_trace_can_be_turned_off(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024, auto_trace=False)

            coder.cur_messages = [dict(role="user", content="rework handle_request please")]
            self.assertEqual(coder.format_chat_chunks().trace, [])

    def test_auto_trace_skips_symbols_already_in_the_chat(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024, fnames=["service.py"])

            idents = coder.get_traceable_idents("rework handle_request please")
            self.assertNotIn("handle_request", idents)

    def test_auto_traced_symbol_is_not_traced_again_on_request(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            coder.cur_messages = [dict(role="user", content="rework handle_request please")]
            chunks = coder.format_chat_chunks()
            self.assertIn("handle_request", chunks.trace[0]["content"])

            reply = coder.get_trace_reply("```trace\nhandle_request\n```\n")
            self.assertIn("already traced", reply)

    def test_slash_trace_results_last_exactly_one_message(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            coder.commands.cmd_trace("handle_request")
            self.assertIn("Callers of", self.history(coder))

            # The model has to see them once before they are stubbed out
            self.reply_with(coder, "ok")
            self.assertIn("Callers of", self.history(coder))

            self.reply_with(coder, "ok")
            history = self.history(coder)
            self.assertNotIn("Callers of", history)
            self.assertIn("results omitted", history)

    def test_trace_instructions_teach_the_backtick_fence(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            # A chat file full of backticks makes aider pick another fence, but
            # the trace block still has to be one the parser understands
            coder.fence = ("<source>", "</source>")
            instructions = coder.fmt_system_prompt(coder.gpt_prompts.trace_instructions)

            self.assertIn("```trace", instructions)
            self.assertNotIn("<source>trace", instructions)

    def test_focus_idents_reach_the_repo_map(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024, focus_idents=["save_record"])

            seen = {}
            original = coder.repo_map.get_repo_map

            def spy(*args, **kwargs):
                seen.update(kwargs)
                return original(*args, **kwargs)

            coder.repo_map.get_repo_map = spy
            coder.get_repo_map()

            self.assertIn("save_record", seen["mentioned_idents"])

    def test_snippets_are_sent_as_read_only_context(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            coder.add_snippet("storage.py", "save_record", 0, 1)
            messages = coder.get_readonly_files_messages()
            content = "\n".join(msg["content"] for msg in messages)

            self.assertIn("def save_record(payload):", content)
            self.assertIn("storage.py lines 1-2", content)
            # The rest of the repo is not pulled in
            self.assertNotIn("handle_request", content)

    def test_snippets_follow_the_file_on_disk(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            coder.add_snippet("storage.py", "save_record", 0, 1)
            Path("storage.py").write_text("def save_record(payload):\n    return None\n")

            content = coder.get_snippets_content()
            self.assertIn("return None", content)

    def test_snippets_are_not_sent_when_the_whole_file_is_in_the_chat(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024, fnames=["storage.py"])

            coder.add_snippet("storage.py", "save_record", 0, 1)

            self.assertEqual(coder.get_snippets_content(), "")

    def test_dropping_a_file_drops_its_snippets(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            coder.add_snippet("storage.py", "save_record", 0, 1)
            coder.commands.cmd_drop("storage.py")

            self.assertEqual(coder.snippets, {})

    def test_snippet_is_dropped_when_the_file_is_gone(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            coder.add_snippet("storage.py", "save_record", 0, 1)
            Path("storage.py").unlink()

            self.assertEqual(coder.get_snippets_content(), "")
            self.assertEqual(coder.snippets, {})

    def test_context_coder_answers_traces_without_dropping_files(self):
        with GitTemporaryDirectory():
            self.make_repo()
            io = InputOutput(yes=True)
            coder = Coder.create(self.GPT35, "context", io=io, use_git=True, fnames=["service.py"])

            coder.partial_response_content = "```trace\nhandle_request\n```\n"
            coder.reply_completed()

            self.assertIn("Callers of", coder.reflected_message)
            # The file set is untouched, the reply named no files
            self.assertEqual(coder.get_inchat_relative_files(), ["service.py"])

    def test_architect_coder_traces_before_calling_the_editor(self):
        with GitTemporaryDirectory():
            self.make_repo()
            io = InputOutput(yes=True)
            coder = Coder.create(self.GPT35, "architect", io=io, use_git=True)

            coder.partial_response_content = "```trace\nhandle_request\n```\n"
            coder.reply_completed()

            self.assertIn("Callers of", coder.reflected_message)

    def test_tracing_state_survives_a_mode_switch(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024, focus_idents=["save_record"])
            coder.add_snippet("storage.py", "save_record", 0, 1)

            ask_coder = Coder.create(from_coder=coder, edit_format="ask")

            self.assertIn("save_record", ask_coder.focus_idents)
            self.assertIn(("storage.py", "save_record"), ask_coder.snippets)

    def test_file_add_prompt_says_why_the_file_was_suggested(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            coder.get_trace_reply("```trace\nhandle_request\n```\n")
            self.assertTrue(coder.file_reason_text("api.py"))

            asked = []

            def capture(question, subject=None, **kwargs):
                asked.append(subject)
                return True

            coder.io.confirm_ask = capture
            coder.check_for_file_mentions("Please add api.py")

            self.assertTrue(asked)
            self.assertIn("api.py", asked[0])
            self.assertIn("handle_request", asked[0])

    def test_tests_command_adds_the_test_as_a_snippet(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            coder.commands.cmd_tests("handle_request")

            self.assertTrue(
                any("test_service.py" in key[0] for key in coder.snippets),
                coder.snippets,
            )

    def test_limitation_log_records_unknown_and_tool_xml(self):
        with GitTemporaryDirectory():
            self.make_repo()
            log_path = Path(".aider.llm-limitations.jsonl")
            coder = self.make_coder(map_tokens=1024, llm_log=True, llm_log_file=str(log_path))

            coder.get_trace_reply("```trace\nzzzz_not_here\n```\n")
            coder.get_trace_reply(
                "<tool_call>\n<function=trace>\n<parameter=name>handle_request</parameter>\n"
                "</function>\n</tool_call>\n"
            )

            kinds = [json.loads(line)["kind"] for line in log_path.read_text().splitlines() if line]
            self.assertIn("trace.unknown", kinds)
            self.assertIn("trace.tool_xml", kinds)


if __name__ == "__main__":
    unittest.main()
