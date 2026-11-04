"""CleanGuard offline tests: approval tokens, quarantine and restore, the approve flow."""
import json
import os
import re
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _cg_util import ROOT, auto_reply, make_file, make_harness, profile, settings, tmpdir  # noqa: E402

from harness import execute  # noqa: E402

from cleanguard import quarantine  # noqa: E402
from cleanguard.cli import approve, parse_selection  # noqa: E402
from cleanguard.safety import GATE, SafetyGate, ensure_inside  # noqa: E402
from cleanguard.scanner import scan  # noqa: E402
from cleanguard.state_machine import CleanGuardRun, load_run, new_run  # noqa: E402
from cleanguard import run_context  # noqa: E402


def call(name, args):
    return {"function": {"name": name, "arguments": args}}


class TestTokens(unittest.TestCase):
    def test_token_rules(self):
        g = SafetyGate()
        with self.assertRaises(PermissionError):                       # no token
            g.consume("r1", "", ["f0001"])
        t = g.issue("r1", ["f0001", "f0002"], ttl_min=30)
        with self.assertRaises(PermissionError):                       # wrong set
            g.consume("r1", t, ["f0001"])
        t = g.issue("r1", ["f0001"], ttl_min=30)
        with self.assertRaises(PermissionError):                       # wrong run
            g.consume("r2", t, ["f0001"])
        t = g.issue("r1", ["f0001"], ttl_min=0)
        time.sleep(0.01)
        with self.assertRaises(PermissionError):                       # expired
            g.consume("r1", t, ["f0001"])
        t = g.issue("r1", ["f0002", "f0001"], ttl_min=30)
        g.consume("r1", t, ["f0001", "f0002"])                         # order does not matter
        with self.assertRaises(PermissionError):                       # reused
            g.consume("r1", t, ["f0001", "f0002"])

    def test_gate_issued_only_in_cli(self):
        callers = []
        for p in (ROOT / "cleanguard").rglob("*.py"):
            if re.search(r"GATE\.issue\(", p.read_text(encoding="utf-8")):
                callers.append(p.name)
        self.assertEqual(callers, ["cli.py"])
        for p in list((ROOT / "cleanguard").rglob("*.py")):
            self.assertNotRegex(p.read_text(encoding="utf-8"),
                                r"(?m)^\s*(import harness\.sample_tools|from harness(\.sample_tools)? import"
                                r".*sample_tools|from harness\.sample_tools)", p.name)

    def test_ensure_inside(self):
        tmp = tmpdir(self)
        inside = make_file(tmp, "root/a.txt")
        outside = make_file(tmp, "b.txt")
        self.assertEqual(ensure_inside(tmp / "root", inside), inside.resolve())
        with self.assertRaises(PermissionError):
            ensure_inside(tmp / "root", tmp / "root" / ".." / "b.txt")
        del outside


class QuarantineBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir(self)
        self.root = self.tmp / "folder"
        self.a = make_file(self.root, "Downloads/setup_zoom.exe", os.urandom(5000), 400)
        self.b = make_file(self.root, "Downloads/sub/old.zip", os.urandom(300), 300)
        self.prof = profile(self.tmp)
        self.cfg = settings(self.tmp)
        self.h = make_harness(self, auto=auto_reply)
        self.run = new_run(self.root, self.cfg, profile=self.prof)
        self.runner = CleanGuardRun(self.h, self.run, self.cfg)        # sets the active run
        recs, _ = scan(self.root, self.prof, False, 100)
        self.run.records = {r.file_id: r for r in recs}
        self.ids = {r.rel_path: r.file_id for r in recs}

    def quarantine(self, ids, token=None):
        token = token or GATE.issue(self.run.run_id, ids, 30)
        return execute(call("quarantine_files", {"file_ids": ids, "approval_token": token}),
                       allowed={"read", "write"}, logger=self.h.log, run_id=self.run.run_id)


class TestQuarantine(QuarantineBase):
    def test_quarantine_restore_roundtrip(self):
        before = {p: p.read_bytes() for p in (self.a, self.b)}
        res = self.quarantine(list(self.ids.values()))
        self.assertTrue(res["ok"], res)
        q = res["result"]
        self.assertEqual(q["status"], "quarantined")
        self.assertEqual(len(q["moved"]), 2)
        self.assertFalse(self.a.exists() or self.b.exists())
        manifest = json.loads((self.prof.quarantine_dir / q["batch_id"] / "manifest.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual([e["status"] for e in manifest["entries"]], ["moved", "moved"])
        self.assertTrue(all(e["sha256"] for e in manifest["entries"]))
        self.assertRegex(q["batch_id"], r"^q-\d{8}-\d{6}-[0-9a-f]{4}$")

        self.b.parent.rmdir()                                         # parent folder gone
        self.a.write_bytes(b"a new file took the name")               # name taken
        r = execute(call("restore_batch", {"batch_id": q["batch_id"]}), allowed={"read", "write"})
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["result"]["restored"], r["result"]["renamed"]), (2, 1))
        self.assertEqual(self.b.read_bytes(), before[self.b])
        restored = self.a.with_name("setup_zoom (restored).exe")
        self.assertEqual(restored.read_bytes(), before[self.a])
        self.assertEqual(self.a.read_bytes(), b"a new file took the name")
        self.assertEqual(quarantine.list_batches(self.prof.quarantine_dir)[0]["status"], "restored")

    def test_changed_since_scan(self):
        fid_a = self.ids["Downloads/setup_zoom.exe"]
        fid_b = self.ids["Downloads/sub/old.zip"]
        self.a.write_bytes(b"edited after the scan")
        res = self.quarantine([fid_a, fid_b])["result"]
        self.assertEqual(res["moved"], [fid_b])
        self.assertEqual(res["skipped"], [{"file_id": fid_a, "status": "changed_since_scan"}])
        self.assertTrue(self.a.exists())

    def test_symlink_swapped_in_after_scan_is_skipped(self):
        fid_a = self.ids["Downloads/setup_zoom.exe"]
        st = self.a.stat()
        target = make_file(self.tmp, "outside.exe", self.a.read_bytes())
        os.utime(target, (st.st_mtime, st.st_mtime))
        self.a.unlink()
        try:
            os.symlink(target, self.a)
        except (OSError, NotImplementedError):
            self.skipTest("no symlink rights")
        res = self.quarantine([fid_a])["result"]
        self.assertEqual(res["skipped"][0]["status"], "changed_since_scan")
        self.assertTrue(target.exists())

    def test_quarantine_needs_valid_token(self):
        fid = self.ids["Downloads/setup_zoom.exe"]
        r = self.quarantine([fid], token="made-up")
        self.assertFalse(r["ok"])
        self.assertIn("PermissionError", r["error"])
        self.assertTrue(self.a.exists())


class TestApproveFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir(self)
        self.root = self.tmp / "folder"
        make_file(self.root, "Downloads/setup_zoom_5.17.exe", os.urandom(4000), 400)
        make_file(self.root, "Downloads/vlc-3.0.20.dmg", os.urandom(3000), 300)
        make_file(self.root, "Downloads/bank_statement_may.txt", "statement", 200)
        make_file(self.root, "Downloads/fresh.zip", b"new", 2)
        self.cfg = settings(self.tmp)
        self.h = make_harness(self, auto=auto_reply)
        run = new_run(self.root, self.cfg, profile=profile(self.tmp))
        self.assertEqual(CleanGuardRun(self.h, run, self.cfg).until_pause(), "APPROVE")
        self.run_id = run.run_id
        run_context.set_active(None)

    def answers(self, *items):
        it = iter(items)

        def ask(prompt):
            try:
                return next(it)
            except StopIteration:
                raise EOFError from None
        return ask

    def test_parse_selection(self):
        self.assertEqual(parse_selection("1,3,7-9", 10), [1, 3, 7, 8, 9])
        self.assertEqual(parse_selection("all", 3), [1, 2, 3])
        self.assertEqual(parse_selection(" none ", 3), [])
        for bad in ("0", "4", "3-1", "x"):
            with self.assertRaises(ValueError):
                parse_selection(bad, 3)

    def test_approve_moves_and_restores(self):
        run = load_run(self.run_id, self.cfg)
        out = []
        # deletable: 1, 2 ; review: 3 (sensitive) -> added, but SENSITIVE not typed
        res = approve(self.h, run, self.cfg, ask=self.answers("1", "3", "no", "MOVE"), say=out.append)
        self.assertEqual(len(res["moved"]), 1)
        self.assertEqual(load_run(self.run_id, self.cfg).state, "DONE")
        self.assertTrue((self.root / "Downloads/bank_statement_may.txt").exists())
        self.assertEqual(len(list(self.root.rglob("*"))), 4)          # 3 files + Downloads/
        r = execute(call("restore_batch", {"batch_id": res["batch_id"]}), allowed={"read", "write"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(list(self.root.rglob("*"))), 5)

    def test_cancel_moves_nothing(self):
        for answers in [("all",), ("all", "", "nope"), ("none", "")]:
            with self.subTest(answers):
                h = make_harness(self, auto=auto_reply)
                run = new_run(self.root, self.cfg, profile=profile(self.tmp))
                CleanGuardRun(h, run, self.cfg).until_pause()
                self.assertIsNone(approve(h, run, self.cfg, ask=self.answers(*answers),
                                          say=lambda *_: None))
                self.assertEqual(load_run(run.run_id, self.cfg).state, "DONE")
                self.assertEqual(len(list(self.root.rglob("*"))), 5)

    def test_approve_requires_approve_state(self):
        run = load_run(self.run_id, self.cfg)
        approve(self.h, run, self.cfg, ask=self.answers(), say=lambda *_: None)   # EOF -> DONE
        with self.assertRaises(RuntimeError):
            approve(self.h, load_run(self.run_id, self.cfg), self.cfg, ask=self.answers("all", "MOVE"))


if __name__ == "__main__":
    unittest.main()
