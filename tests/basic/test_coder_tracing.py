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


class TestCoderTracing(unittest.TestCase):
    def setUp(self):
        self.GPT35 = Model("gpt-3.5-turbo")

    def make_repo(self):
        files = {
            "service.py": SERVICE_PY,
            "storage.py": STORAGE_PY,
            "api.py": API_PY,
        }
        for fname, content in files.items():
            Path(fname).write_text(content)

        repo = git.Repo.init(os.getcwd())
        repo.git.add(A=True)
        repo.git.commit("-m", "init")

        return files

    def make_coder(self, **kwargs):
        io = InputOutput(yes=True)
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

            history = "\n".join(
                msg["content"] for msg in coder.cur_messages + coder.done_messages
            )
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

    def test_snippet_is_dropped_when_the_file_is_gone(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024)

            coder.add_snippet("storage.py", "save_record", 0, 1)
            Path("storage.py").unlink()

            self.assertEqual(coder.get_snippets_content(), "")
            self.assertEqual(coder.snippets, {})

    def test_tracing_state_survives_a_mode_switch(self):
        with GitTemporaryDirectory():
            self.make_repo()
            coder = self.make_coder(map_tokens=1024, focus_idents=["save_record"])
            coder.add_snippet("storage.py", "save_record", 0, 1)

            ask_coder = Coder.create(from_coder=coder, edit_format="ask")

            self.assertIn("save_record", ask_coder.focus_idents)
            self.assertIn(("storage.py", "save_record"), ask_coder.snippets)


if __name__ == "__main__":
    unittest.main()
