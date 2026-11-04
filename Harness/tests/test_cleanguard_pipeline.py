"""CleanGuard offline tests: domain rules, CLASSIFY loop, routing, privacy, invariants,
state machine and logging. A FakeClient stands in for Ollama."""
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _cg_util import (auto_reply, make_file, make_harness, make_run, profile, record,  # noqa: E402
                      settings, tmpdir, verdicts)

from harness import LLMConnectionError, execute  # noqa: E402
from harness.validation import DomainError  # noqa: E402

from cleanguard.agent import classify_batch  # noqa: E402
from cleanguard.privacy import merge_privacy  # noqa: E402
from cleanguard.records import Decision  # noqa: E402
from cleanguard.rules import enforce_invariants, make_domain_check  # noqa: E402
from cleanguard.schemas import PrivacyVerdict, VerdictBatch  # noqa: E402
from cleanguard.settings import ClassifyCfg, apply_config  # noqa: E402
from cleanguard.state_machine import CleanGuardRun, S, load_run, new_run  # noqa: E402

NO_SNIPPETS = ClassifyCfg(use_snippets=False)


def batch_of(*items):
    return VerdictBatch.model_validate_json(verdicts(*items))


class TestDomainRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir(self)
        self.recs = [record("f0001", days=100), record("f0002", days=5),
                     record("f0003", protected=True)]
        self.check = make_domain_check(self.recs, settings(self.tmp))

    def test_valid_batch_passes(self):
        self.check(batch_of(("f0001", "deletable"), ("f0002", "keep"), ("f0003", "keep")))

    def test_domain_rules(self):
        cases = {
            "exactly once": [("f0001", "deletable"), ("f0002", "keep")],
            "f0001 is sensitive": [("f0001", "deletable", True, "financial"), ("f0002", "keep"),
                                   ("f0003", "keep")],
            "sensitive_type must be none": [("f0001", "keep", False, "financial"),
                                            ("f0002", "keep"), ("f0003", "keep")],
            "modified 5 days ago": [("f0001", "keep"), ("f0002", "deletable"), ("f0003", "keep")],
            "protected folder": [("f0001", "keep"), ("f0002", "keep"), ("f0003", "review")],
        }
        for expected, items in cases.items():
            with self.subTest(expected), self.assertRaises(DomainError) as cm:
                self.check(batch_of(*items))
            self.assertIn(expected, str(cm.exception))
        with self.assertRaises(DomainError):                       # duplicated answer
            self.check(batch_of(("f0001", "keep"), ("f0001", "keep"), ("f0003", "keep")))

    def test_small_files_do_not_look_empty(self):
        from cleanguard.schemas import human_size, to_meta
        self.assertEqual(to_meta(record("f0001", size=180), "macos").size, "180 B")
        self.assertEqual(human_size(2_100), "2.1 KB")
        self.assertEqual(human_size(92_400_000), "92.4 MB")
        self.assertEqual(human_size(0), "0 B")

    def test_schema_has_no_refs(self):
        self.assertNotIn("$ref", json.dumps(VerdictBatch.model_json_schema()))


class ClassifyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir(self)
        self.recs = [record("f0001", "Downloads/setup_zoom.exe", days=400),
                     record("f0002", "Downloads/notes.txt", days=200)]
        self.run = make_run(self.tmp, self.recs)
        self.cfg = settings(self.tmp, classify=NO_SNIPPETS)


class TestClassify(ClassifyBase):
    def test_garbage_output_becomes_review(self):
        h = make_harness(self, ["not json", "{}", '{"verdicts": "nope"}'])
        out = classify_batch(h, self.recs, self.cfg, "r-test")
        self.assertEqual({d.label for d in out}, {"review"})
        self.assertEqual({d.decided_by for d in out}, {"fallback"})
        self.assertEqual(len(h.client.requests), 3)                 # 1 fast + 2 reasoning
        log = [json.loads(x) for x in h.log.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(log[-1]["event"], "safe_failure")

    def test_escalation_to_reasoning(self):
        bad = verdicts(("f0001", "deletable"))                       # misses f0002
        good = verdicts(("f0001", "deletable"), ("f0002", "keep"))
        h = make_harness(self, [bad, good])
        out = {d.file_id: d for d in classify_batch(h, self.recs, self.cfg, "r-test")}
        self.assertEqual(out["f0001"].label, "deletable")
        self.assertEqual(out["f0001"].decided_by, "llm_reasoning")
        models = [r["model"] for r in h.client.requests]
        self.assertEqual(models, [h.config.profile("fast").model, h.config.profile("reasoning").model])
        self.assertIn("previous answer was invalid", h.client.requests[1]["messages"][-1]["content"])

    def test_review_escalates_and_replaces_only_if_valid(self):
        first = verdicts(("f0001", "deletable"), ("f0002", "review"))
        h = make_harness(self, [first, verdicts(("f0002", "keep"))])
        out = {d.file_id: d for d in classify_batch(h, self.recs, self.cfg, "r-test")}
        self.assertEqual((out["f0002"].label, out["f0002"].decided_by), ("keep", "llm_reasoning"))
        self.assertEqual(out["f0001"].decided_by, "llm_fast")

        h = make_harness(self, [first, "garbage"])
        out = {d.file_id: d for d in classify_batch(h, self.recs, self.cfg, "r-test")}
        self.assertEqual((out["f0002"].label, out["f0002"].decided_by), ("review", "llm_fast"))

    def test_signals_force_sensitive(self):
        self.recs[1].signals = ["kw:financial"]
        h = make_harness(self, [verdicts(("f0001", "deletable"), ("f0002", "keep"))])
        out = {d.file_id: d for d in classify_batch(h, self.recs, self.cfg, "r-test")}
        self.assertEqual((out["f0002"].sensitive, out["f0002"].sensitive_type), (True, "financial"))

    def test_without_domain_rules_extra_and_missing_ids_are_safe(self):
        cfg = replace(self.cfg, domain_rules=False)
        self.run.records["f0009"] = record("f0009", days=1)          # outside the batch
        h = make_harness(self, [verdicts(("f0001", "deletable"), ("f0009", "deletable"))])
        out = {d.file_id: d for d in classify_batch(h, self.recs, cfg, "r-test")}
        self.assertEqual(set(out), {"f0001", "f0002"})
        self.assertEqual((out["f0002"].label, out["f0002"].decided_by), ("review", "fallback"))

    def test_connection_error_propagates(self):
        class Down:
            def chat(self, *a, **k):
                raise LLMConnectionError("down")
        h = make_harness(self)
        h.client = Down()
        with self.assertRaises(LLMConnectionError):
            classify_batch(h, self.recs, self.cfg, "r-test")


class TestSnippetTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir(self)
        make_file(self.tmp, "Downloads/scan_001.txt", "Name: X\nID: 1-1037-02071-81-1\n")
        make_file(self.tmp, "Downloads/other.txt", "hello")
        self.recs = [record("f0001", "Downloads/scan_001.txt", root=self.tmp),
                     record("f0002", "Downloads/other.txt", root=self.tmp)]
        self.run = make_run(self.tmp, self.recs)

    def call(self, name, args):
        return {"function": {"name": name, "arguments": args}}

    def test_read_snippet_scoped_to_batch(self):
        self.run.current_batch = {"f0001"}
        r = execute(self.call("read_snippet", {"file_id": "f0001"}))
        self.assertTrue(r["ok"])
        self.assertIn("1-1037", r["result"]["text"])
        r = execute(self.call("read_snippet", {"file_id": "f0002"}))
        self.assertFalse(r["ok"])
        self.assertIn("not in the current list", r["error"])
        r = execute(self.call("read_snippet", {"file_id": "../../etc/passwd"}))
        self.assertFalse(r["ok"])

    def test_snippet_phase_adds_signals(self):
        cfg = settings(self.tmp)
        tool_turn = {"tool_calls": [self.call("read_snippet", {"file_id": "f0001"})]}
        h = make_harness(self, [tool_turn, verdicts(("f0001", "keep", True, "id_doc"),
                                                    ("f0002", "deletable"))])
        out = {d.file_id: d for d in classify_batch(h, self.recs, cfg, "r-test")}
        self.assertIn("thai_id", self.run.records["f0001"].signals)
        self.assertTrue(out["f0001"].sensitive)
        self.assertEqual(h.client.requests[0]["tools"][0]["function"]["name"], "read_snippet")
        self.assertEqual(len(h.client.requests[0]["tools"]), 1)       # only read_snippet offered
        self.assertIn("1-1037", h.client.requests[1]["messages"][-1]["content"])  # snippet sent

    def test_snippet_budget(self):
        cfg = replace(settings(self.tmp), classify=ClassifyCfg(max_snippets_per_batch=1))
        calls = {"tool_calls": [self.call("read_snippet", {"file_id": "f0001"}),
                                self.call("read_snippet", {"file_id": "f0002"})]}
        h = make_harness(self, [calls, verdicts(("f0001", "keep", True, "id_doc"),
                                                ("f0002", "deletable"))])
        classify_batch(h, self.recs, cfg, "r-test")
        self.assertEqual(list(self.run.snippets), ["f0001"])

    def test_llm_cannot_write(self):
        self.run.current_batch = {"f0001"}
        for name, args in [("quarantine_files", {"file_ids": ["f0001"], "approval_token": "x"}),
                           ("restore_batch", {"batch_id": "q-20260101-000000-abcd"}),
                           ("delete_permanent", {"file_ids": ["f0001"]})]:
            r = execute(self.call(name, args), allowed={"read"})
            self.assertFalse(r["ok"])
            self.assertIn("Permission denied", r["error"])
        r = execute(self.call("delete_permanent", {"file_ids": ["f0001"]}), allowed={"read", "write"})
        self.assertIn("Permission denied", r["error"])
        self.assertTrue((self.tmp / "Downloads/scan_001.txt").exists())


class TestPrivacyAndInvariants(unittest.TestCase):
    def test_privacy_merge_is_monotonic(self):
        rec = record("f0001", signals=["kw:financial"])
        clear = PrivacyVerdict(file_id="f0001", sensitive=False, sensitive_type="none",
                               reason="Looks like a lecture.")
        d = Decision("f0001", "keep", True, "financial", "x", "llm_fast")
        m = merge_privacy(d, clear, rec)
        self.assertEqual((m.sensitive, m.sensitive_type), (True, "financial"))

        plain = record("f0002")
        d2 = Decision("f0002", "deletable", False, "none", "old file", "llm_fast")
        flag = PrivacyVerdict(file_id="f0002", sensitive=True, sensitive_type="medical",
                              reason="Medical letter.")
        m2 = merge_privacy(d2, flag, plain)
        self.assertEqual((m2.label, m2.sensitive, m2.sensitive_type), ("review", True, "medical"))

        rec3 = record("f0003", signals=["kw:medical"])
        d3 = Decision("f0003", "keep", False, "none", "x", "llm_fast")
        self.assertTrue(merge_privacy(d3, clear.model_copy(update={"file_id": "f0003"}), rec3).sensitive)

    def test_invariants_catch_bug(self):
        tmp = tmpdir(self)
        h = make_harness(self)
        recs = [record("f0001", signals=["thai_id"]), record("f0002", days=3),
                record("f0003", protected=True), record("f0004")]
        run = make_run(tmp, recs)
        run.decisions = {
            "f0001": Decision("f0001", "deletable", False, "none", "bug", "llm_fast"),
            "f0002": Decision("f0002", "deletable", False, "none", "bug", "llm_fast"),
            "f0003": Decision("f0003", "review", False, "none", "bug", "llm_fast"),
            "f0004": Decision("f0004", "deletable", False, "none", "fine", "llm_fast"),
        }
        self.assertEqual(enforce_invariants(run, settings(tmp), h.log), 3)
        self.assertEqual(run.decisions["f0001"].label, "review")
        self.assertEqual(run.decisions["f0002"].label, "review")
        self.assertEqual(run.decisions["f0003"].label, "keep")
        self.assertEqual(run.decisions["f0003"].decided_by, "invariant")
        self.assertEqual(run.decisions["f0004"].label, "deletable")
        log = [json.loads(x) for x in h.log.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sorted(e["rule"] for e in log if e["event"] == "invariant_violation"),
                         ["protected_not_keep", "recent_deletable", "sensitive_deletable"])


def build_tree(root: Path):
    """A small folder with one file per triage path."""
    make_file(root, "Downloads/setup_zoom_5.17.exe", b"\x00" * 3000, 400)
    make_file(root, "Downloads/setup_zoom_5.17 (1).exe", b"\x00" * 3000, 300)
    make_file(root, "Downloads/bank_statement_may.pdf", b"%PDF fake", 200)
    make_file(root, "Downloads/scan_001.txt", "Name: X\nID: 1-1037-02071-81-1\n", 120)
    make_file(root, "Downloads/lecture03_os.pdf", b"%PDF lecture", 150)
    make_file(root, "Downloads/fresh.zip", b"new", 2)
    make_file(root, "Documents/essay_draft1.docx", b"draft", 90)


class TestStateMachine(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir(self)
        self.root = self.tmp / "folder"
        build_tree(self.root)
        self.cfg = settings(self.tmp)

    def start(self, h, cfg=None, dry_run=False):
        run = new_run(self.root, cfg or self.cfg, profile=profile(self.tmp), dry_run=dry_run)
        return CleanGuardRun(h, run, cfg or self.cfg)

    def test_state_machine_happy_path(self):
        h = make_harness(self, auto=auto_reply)
        runner = self.start(h)
        self.assertEqual(runner.until_pause(), "APPROVE")
        log = [json.loads(x) for x in h.log.path.read_text(encoding="utf-8").splitlines()]
        path = [e["dst"] for e in log if e["event"] == "state_transition"]
        self.assertEqual(path, ["TRIAGE", "CLASSIFY", "PRIVACY", "VALIDATE", "REPORT", "APPROVE"])
        run = runner.run
        self.assertEqual(len(run.decisions), len(run.records), 7)
        by = {r.rel_path: run.decisions[f] for f, r in run.records.items()}
        self.assertEqual(by["Downloads/setup_zoom_5.17 (1).exe"].decided_by, "rule")
        self.assertEqual(by["Downloads/fresh.zip"].label, "keep")
        self.assertNotEqual(by["Downloads/bank_statement_may.pdf"].label, "deletable")
        self.assertTrue(by["Downloads/scan_001.txt"].sensitive)        # found via the snippet
        self.assertTrue((self.tmp / "runs" / run.run_id / "report.html").exists())
        with self.assertRaises(RuntimeError):
            runner.go(S.SCAN)                                         # illegal transition
        events = {e["event"] for e in log}
        self.assertTrue({"triage_summary", "decision", "run_summary", "tool_call"} <= events)

    def test_dry_run_stops_at_done_and_nothing_moves(self):
        h = make_harness(self, auto=auto_reply)
        self.assertEqual(self.start(h, dry_run=True).until_pause(), "DONE")
        self.assertFalse((self.tmp / "quarantine").exists())

    def test_rules_only_config_skips_llm(self):
        cfg = apply_config(self.cfg, "C0")
        h = make_harness(self)                                        # no outputs: any call fails
        runner = self.start(h, cfg, dry_run=True)
        self.assertEqual(runner.until_pause(), "DONE")
        self.assertEqual(h.client.requests, [])
        llm_files = [d for d in runner.run.decisions.values() if d.decided_by == "fallback"]
        self.assertTrue(llm_files and all(d.label == "review" for d in llm_files))

    def test_ollama_down_fails_then_resumes(self):
        h = make_harness(self)
        h.client.auto = lambda *a, **k: (_ for _ in ()).throw(LLMConnectionError("down"))
        runner = self.start(h)
        self.assertEqual(runner.until_pause(), "FAILED")
        self.assertEqual(runner.run.failed_from, "CLASSIFY")
        run = load_run(runner.run.run_id, self.cfg)                   # from state.json
        self.assertEqual(run.state, "FAILED")
        h.client.auto = auto_reply
        self.assertEqual(CleanGuardRun(h, run, self.cfg).resume(), "APPROVE")

    def test_scan_of_protected_root_fails_cleanly(self):
        h = make_harness(self)
        run = new_run(self.root, self.cfg, dry_run=True,
                      profile=profile(self.tmp).with_extra_protected([self.root.resolve().as_posix() + "/**"]))
        runner = CleanGuardRun(h, run, self.cfg)
        self.assertEqual(runner.until_pause(), "FAILED")
        self.assertIn("protected", run.error)

    def test_logs_have_no_names(self):
        """Run C4 on the generated testbed; no testbed file name may appear in calls.jsonl."""
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
        from make_testbed import EXTRA_PROTECTED, build
        files = self.tmp / "tb"
        files.mkdir()
        rows = build(files, seed=7, os_name=profile(self.tmp).name)
        cfg = replace(apply_config(self.cfg, "C4"), scan=replace(self.cfg.scan, include_hidden=True))
        h = make_harness(self, auto=auto_reply)
        run = new_run(files, cfg, dry_run=True, extra_protected=EXTRA_PROTECTED,
                      profile=profile(self.tmp).with_extra_protected(EXTRA_PROTECTED))
        self.assertEqual(CleanGuardRun(h, run, cfg).until_pause(), "DONE")
        self.assertEqual(len(run.records), 200)
        text = h.log.path.read_text(encoding="utf-8")
        self.assertIn('"tool": "read_snippet"', text)
        for r in rows:
            if r["category"] == "protected":
                continue                                              # HEAD, config, ...
            name = Path(r["rel_path"]).name
            self.assertNotIn(name, text, f"file name leaked into the log: {name}")
            self.assertNotIn(Path(r["rel_path"]).stem, text)
        self.assertNotIn(files.as_posix(), text)
        self.assertNotIn("SYNTHETIC", text)                           # no snippet text either


if __name__ == "__main__":
    unittest.main()
