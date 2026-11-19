import os
import subprocess
import sys
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge import events, research


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.env = patch.dict(os.environ, {"DEEPSEEK_BRIDGE_EVENTS": str(Path(self.root.name) / "events.jsonl")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_start_generates_id_and_rejects_invalid_budget(self):
        with self.assertRaises(ValueError):
            research.start("公开问题", "一手来源", 59)
        result = research.start("公开问题", "一手来源", 120)
        self.assertEqual(result["question_public"], "公开问题")
        self.assertEqual(len(result["research_id"]), 32)
        self.assertEqual(events.read_all_events()[0]["kind"], "research_start")

    def test_record_idempotence_references_and_conflict(self):
        rid = research.start("公开问题", "", 120)["research_id"]
        claim = {"claim_id": "C1", "text": "事实", "author": "codex"}
        result = research.record(rid, [claim], [])
        self.assertEqual(result["written_claim_ids"], ["C1"])
        research.record(rid, [claim], [])
        self.assertEqual(len([x for x in events.read_all_events() if x["kind"] == "research_claim"]), 1)
        with self.assertRaises(ValueError):
            research.record(rid, [{**claim, "text": "不同"}], [])
        with self.assertRaises(ValueError):
            research.record(rid, [], [{"claim_id": "missing", "evidence_id": "E1"}])

    def test_signed_url_redaction_does_not_preserve_verified_label(self):
        rid = research.start("公开问题", "", 120)["research_id"]
        research.record(rid, [{"claim_id": "C1", "text": "事实", "author": "codex"}], [])
        evidence = {"claim_id": "C1", "evidence_id": "E1", "url": "https://alice:pw@example.org/a?token=secret&x=1",
                    "source_type": "primary", "retrieval_state": "opened", "support": "supports",
                    "reported_by": "codex", "opened_by": "codex", "assessed_by": "codex",
                    "quote": "原文", "quote_location": "第1段"}
        research.record(rid, [], [evidence])
        stored = next(x for x in events.read_all_events() if x["kind"] == "research_evidence")
        self.assertEqual(stored["url"], "https://example.org/a")
        self.assertNotIn("secret", str(stored))
        self.assertEqual(stored["retrieval_state"], "search_only")
        self.assertEqual(stored["support"], "not_assessed")
        self.assertEqual(stored["opened_by"], "none")
        self.assertEqual(research.snapshot(rid)["claims"][0]["labels"], ["未核验"])

    def test_unknown_query_is_not_logged(self):
        rid = research.start("公开问题", "", 120)["research_id"]
        research.record(rid, [{"claim_id": "C1", "text": "事实", "author": "codex"}], [])
        research.record(rid, [], [{"claim_id": "C1", "evidence_id": "E1", "url": "https://example.org/a?opaque=abc123"}])
        stored = next(x for x in events.read_all_events() if x["kind"] == "research_evidence")
        self.assertEqual(stored["url"], "https://example.org/a")

    def test_finish_is_idempotent_and_terminal(self):
        rid = research.start("公开问题", "", 120)["research_id"]
        research.record(rid, [{"claim_id": "C1", "text": "事实", "author": "codex"}], [])
        args = (rid, "partial", "ok", "failed", ["C1"], ["无法复核"])
        first = research.finish(*args)
        self.assertEqual(first["coverage"], "0/1")
        self.assertFalse(first["already_finished"])
        self.assertTrue(research.finish(*args)["already_finished"])
        with self.assertRaises(ValueError):
            research.finish(rid, "completed", "ok", "ok", ["C1"], [])
        with self.assertRaises(ValueError):
            research.record(rid, [{"claim_id": "C2", "text": "晚到", "author": "deepseek"}], [])
        with self.assertRaises(ValueError):
            research.record(rid, [{"claim_id": "C1", "text": "事实", "author": "codex"}], [])

    def test_deepseek_phase_actor_and_challenge_prerequisite(self):
        rid = research.start("公开问题", "", 120)["research_id"]
        research.record(rid, [{"claim_id": "C1", "text": "事实", "author": "codex"}], [])
        with self.assertRaisesRegex(ValueError, "independent 结束事件"):
            research.validate_deepseek_call(rid, "independent")
        research.record_phase(rid, "independent", "finished")
        research.validate_deepseek_call(rid, "independent")
        with self.assertRaises(ValueError):
            research.validate_deepseek_call(rid, "challenge")
        research.record_deepseek_phase(rid, "independent", "started", "call1")
        research.record_deepseek_phase(rid, "independent", "finished", "call1")
        research.record_phase(rid, "challenge", "started", disagreements=[{"claim_id": "C1", "note": "证据较弱"}])
        research.validate_deepseek_call(rid, "challenge")
        phases = [x for x in events.read_all_events() if x["kind"] == "research_phase"]
        self.assertEqual([x["actor"] for x in phases], ["codex", "deepseek", "deepseek", "codex"])

    def test_cannot_claim_completed_without_both_real_deepseek_phases(self):
        rid = research.start("公开问题", "", 120)["research_id"]
        research.record(rid, [{"claim_id": "C1", "text": "事实", "author": "codex"}], [])
        research.record_phase(rid, "independent", "finished")
        with self.assertRaises(ValueError):
            research.finish(rid, "completed", "ok", "ok", ["C1"], [])

    def test_independent_finished_requires_prior_codex_claim(self):
        rid = research.start("公开问题", "", 120)["research_id"]
        research.record_phase(rid, "independent", "finished")
        with self.assertRaisesRegex(ValueError, "Codex claim"):
            research.validate_deepseek_call(rid, "independent")

    def test_evidence_rejects_unopened_support_and_dangerous_url(self):
        rid = research.start("公开问题", "", 120)["research_id"]
        research.record(rid, [{"claim_id": "C1", "text": "事实", "author": "codex"}], [])
        for evidence in (
            {"claim_id": "C1", "evidence_id": "E1", "url": "https://example.org", "support": "supports", "retrieval_state": "search_only"},
            {"claim_id": "C1", "evidence_id": "E2", "url": "javascript:alert(1)"},
        ):
            with self.assertRaises(ValueError):
                research.record(rid, [], [evidence])

    def test_two_processes_same_claim_id_write_once(self):
        rid = research.start("公开问题", "", 120)["research_id"]
        code = (
            "from bridge import research; "
            f"research.record('{rid}', [{{'claim_id':'C1','text':'事实','author':'codex'}}], [])"
        )
        workers = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                   for _ in range(2)]
        for worker in workers:
            _, error = worker.communicate(timeout=10)
            self.assertEqual(worker.returncode, 0, error.decode())
        claims = [row for row in events.read_all_events() if row["kind"] == "research_claim"]
        self.assertEqual(len(claims), 1)

    def test_disk_failure_reports_claim_already_committed_in_batch(self):
        rid = research.start("公开问题", "", 120)["research_id"]
        real_write = os.write
        attempts = 0

        def failing_second_write(fd, data):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise OSError("disk full")
            return real_write(fd, data)

        with patch.object(events.os, "write", side_effect=failing_second_write):
            with self.assertRaisesRegex(OSError, "C1"):
                research.record(rid, [
                    {"claim_id": "C1", "text": "first", "author": "codex"},
                    {"claim_id": "C2", "text": "second", "author": "codex"},
                ], [])
        stored = [row["claim_id"] for row in events.read_all_events() if row["kind"] == "research_claim"]
        self.assertEqual(stored, ["C1"])

    def test_concurrent_identical_finish_reports_one_reuse(self):
        rid = research.start("公开问题", "", 120)["research_id"]
        research.record(rid, [{"claim_id": "C1", "text": "事实", "author": "codex"}], [])
        barrier = threading.Barrier(2)
        original = research._append
        flags = []

        def paced_append(*args, **kwargs):
            barrier.wait(timeout=3)
            return original(*args, **kwargs)

        def worker():
            result = research.finish(rid, "partial", "ok", "failed", ["C1"], [])
            flags.append(result["already_finished"])

        with patch.object(research, "_append", side_effect=paced_append):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        self.assertEqual(sorted(flags), [False, True])

    def test_deepseek_no_source_reports_no_source_warning(self):
        rid = research.start("公开问题", "", 120)["research_id"]
        research.record(rid, [{"claim_id": "D1", "text": "线索", "author": "deepseek"}], [
            {"claim_id": "D1", "evidence_id": "E1", "url": "", "retrieval_state": "no_source", "reported_by": "deepseek"}
        ])
        self.assertEqual(research.snapshot(rid)["warning"], "本轮大肥鱼未提供来源")
