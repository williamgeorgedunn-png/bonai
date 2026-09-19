import importlib.util
import unittest
from pathlib import Path

SMOKE_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "pipeline" / "smoke-two-endpoints.py"
)


def load_smoke():
    spec = importlib.util.spec_from_file_location("pipeline_smoke_script", SMOKE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTwoEndpointSmoke(unittest.TestCase):
    def test_two_fake_endpoints_plan_edit_review_and_commit(self):
        """CLI plumbing: two api_bases, one commit per accepted task."""
        smoke = load_smoke()
        failures, seen = smoke.run_smoke()
        self.assertEqual(failures, [], "\n".join(failures))
        roles = {role for role, _step in seen}
        self.assertEqual(roles, {"architect", "worker"})
        steps = [step for role, step in seen if role == "architect"]
        for expected in ("PLAN", "BRIEF", "REVIEW"):
            self.assertIn(expected, steps)


if __name__ == "__main__":
    unittest.main()
