import json
import unittest

from bridge import team_template as team


class TeamTemplateTests(unittest.TestCase):
    def setUp(self):
        self.template = team.default_template()
        self.source = {"brief": "合成样例需要去除标签两端空白。", "revision": {}}

    def run_nodes(self, scenario="success", lead="codex", round_number=1):
        outputs, results = {}, {}
        for node in self.template["nodes"]:
            inputs = team.bind_inputs(self.template, node, self.source, outputs)
            result = team.evaluate(node, inputs, scenario=scenario, round_number=round_number,
                                   attempt=1, choices={}, lead=lead)
            results[node] = result
            outputs[node] = result["output"]
            if result["help"]:
                break
        return results

    def test_single_wire_changes_the_context_actually_reviewed(self):
        first = self.run_nodes()
        alternate = json.loads(json.dumps(self.template))
        alternate["bindings"]["review"]["context"] = "source.brief"
        team.validate_template(alternate)
        self.template = alternate
        second = self.run_nodes()
        original = first["review"]["output"]["review"]
        rewired = second["review"]["output"]["review"]
        self.assertEqual(original["context"], first["lead"]["output"]["context"])
        self.assertEqual(rewired["context"], self.source["brief"])
        self.assertNotEqual(original["context"], rewired["context"])
        self.assertEqual(original["patch_id"], rewired["patch_id"])
        self.assertEqual(team.default_template()["bindings"]["review"]["context"], "lead.context")

    def test_required_port_type_origin_and_order_are_checked_before_dispatch(self):
        for node, port, reference in (
            ("verify", "patch", None), ("verify", "patch", "review.review"),
            ("review", "context", "source.typo"), ("lead", "brief", "lead.context"),
            ("opinion", "context", "source"), ("review", "typo", "source.brief"),
        ):
            with self.subTest(node=node, port=port, reference=reference):
                template = team.default_template()
                if reference is None:
                    del template["bindings"][node][port]
                else:
                    template["bindings"][node][port] = reference
                with self.assertRaisesRegex(ValueError, rf"{node}\.{port}"):
                    team.validate_template(template)
        self.template["nodes"] = ["lead", "review", "opinion", "verify"]
        with self.assertRaisesRegex(ValueError, r"review\.opinion"):
            team.validate_template(self.template)

    def test_missing_or_mistyped_runtime_value_identifies_downstream_port(self):
        for source in ({"brief": "原任务"}, {"brief": "原任务", "revision": []}):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, r"lead\.revision"):
                team.bind_inputs(self.template, "lead", source, {})
        with self.assertRaisesRegex(ValueError, r"opinion\.context"):
            team.bind_inputs(self.template, "opinion", self.source, {"lead": {}})

    def test_swapping_roles_and_skipping_opinion_keeps_a_verifiable_candidate(self):
        for lead, reviewer in (("codex", "claude"), ("claude", "codex")):
            with self.subTest(lead=lead):
                results = self.run_nodes(lead=lead)
                patch = results["lead"]["output"]["patch"]
                review = results["review"]["output"]["review"]
                checks = results["verify"]["output"]["checks"]
                self.assertEqual(patch["author"], lead)
                self.assertEqual(review["reviewer"], reviewer)
                self.assertEqual(results["opinion"]["status"], "skipped")
                self.assertEqual(review["opinion"], [])
                self.assertTrue(review["approved"])
                self.assertTrue(checks["passed"])
                self.assertEqual(checks["patch_id"], patch["id"])
                self.assertEqual(review["patch_id"], patch["id"])
                self.assertIn("+++ b/" + patch["path"], patch["diff"])
                self.assertTrue(checks["simulated"])
                self.assertTrue(all(case["passed"] for case in checks["cases"]))
                json.dumps(results, allow_nan=False)

    def test_verify_holds_if_review_refers_to_another_candidate(self):
        results = self.run_nodes()
        patch = results["lead"]["output"]["patch"]
        review = dict(results["review"]["output"]["review"], patch_id="other")
        result = team.evaluate("verify", {"patch": patch, "review": review}, scenario="success",
                               round_number=1, attempt=1, choices={}, lead="codex")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["output"].get("checks", {}).get("passed"))
        self.assertIsNotNone(result["help"])

    def test_rework_uses_previous_evidence_and_fails_again_at_requested_fixture(self):
        for scenario, node, key in (("review", "review", "review"),
                                    ("test_failure", "verify", "checks")):
            with self.subTest(scenario=scenario):
                self.source["revision"] = {}
                original = self.run_nodes(scenario)
                patch = original["lead"]["output"]["patch"]
                failure = original[node]
                evidence = failure["output"][key]
                self.assertEqual(failure["status"], "failed")
                self.assertEqual(failure["help"]["actions"], ["rework", "stop"])
                self.assertEqual(evidence["patch_id"], patch["id"])
                if node == "review":
                    self.assertFalse(evidence["approved"])
                    self.assertIn(patch["path"], evidence["findings"][0]["location"])
                    self.assertNotIn("verify", original)
                else:
                    self.assertFalse(evidence["passed"])
                    self.assertTrue(any(not case["passed"] for case in evidence["cases"]))
                self.source["revision"] = {"patch": patch, key: evidence}
                retry = self.run_nodes(scenario, round_number=2)
                proposal = retry["lead"]["output"]
                self.assertEqual(proposal["previous_patch_id"], patch["id"])
                detail = (evidence["findings"][0]["message"] if node == "review"
                          else next(case["name"] for case in evidence["cases"] if not case["passed"]))
                self.assertIn(detail, proposal["plan"])
                self.assertEqual(retry[node]["help"]["actions"], ["rework", "stop"])
                self.assertEqual(retry[node]["output"][key]["patch_id"], retry["lead"]["output"]["patch"]["id"])

    def test_timeout_needs_retry_and_choice_needs_the_selected_answer(self):
        inputs = team.bind_inputs(self.template, "lead", self.source, {})
        def evaluate(scenario, attempt=1, round_number=1, choices=None):
            return team.evaluate("lead", inputs, scenario=scenario, round_number=round_number,
                                 attempt=attempt, choices=choices or {}, lead="codex")
        timeout = evaluate("timeout")
        self.assertEqual(timeout["help"]["kind"], "error")
        self.assertEqual(timeout["help"]["actions"], ["retry", "stop"])
        self.assertEqual(evaluate("timeout", attempt=2)["status"], "success")
        self.assertEqual(evaluate("timeout", round_number=2)["status"], "success")
        self.assertEqual(evaluate("choice", choices={"supply": "continue"})["help"]["kind"], "choice")
        self.assertEqual(evaluate("choice", choices={"choose": "continue"})["status"], "success")
