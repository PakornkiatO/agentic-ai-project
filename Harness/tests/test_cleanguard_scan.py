"""CleanGuard offline tests: platform profiles, scanner, signals, triage."""
import os
import sys
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _cg_util import make_file, make_run, profile, record, settings, tmpdir  # noqa: E402

from cleanguard.platform import load_profile  # noqa: E402
from cleanguard.scanner import mark_duplicates, scan  # noqa: E402
from cleanguard.signals import find_signals, luhn_valid, signal_type, thai_id_valid  # noqa: E402
from cleanguard.triage import triage  # noqa: E402


class TestProfiles(unittest.TestCase):
    def test_profile_matching(self):
        win = load_profile("windows")
        self.assertTrue(win.is_protected(Path("C:/Windows/System32/x.dll")))
        self.assertTrue(win.is_protected(Path("c:/program files/App/a.exe")))      # case-insensitive
        self.assertTrue(win.is_junk_name("thumbs.db"))
        self.assertTrue(win.is_junk_name("~$report.docx"))
        self.assertFalse(win.is_protected(Path("C:/Users/me/Downloads/a.exe")))

        mac = load_profile("macos")
        self.assertTrue(mac.is_protected(Path("/Applications/Zoom.app/Contents/x")))
        self.assertTrue(mac.is_protected(Path("/Users/me/Projects/Foo.app/Contents/x")))
        self.assertTrue(mac.is_protected(Path("/Users/me/code/repo/.git/HEAD")))
        self.assertTrue(mac.is_junk_name(".DS_Store") and mac.is_junk_name("._IMG.jpg"))
        self.assertTrue(mac.is_junk_name(".ds_store"))
        self.assertTrue(mac.in_sync_root(Path(os.path.expanduser("~/Library/CloudStorage/Drive/a"))))

        lin = load_profile("linux")
        self.assertTrue(lin.case_sensitive)
        self.assertTrue(lin.is_protected(Path("/etc/passwd")))
        self.assertFalse(lin.is_protected(Path("/ETC/passwd")))                  # case-sensitive
        self.assertTrue(lin.is_junk_name("notes.txt~"))
        self.assertTrue(lin.is_junk_name(".~lock.report.odt#"))
        self.assertFalse(lin.is_junk_name("notes.txt"))
        self.assertIn(".crdownload", lin.partial_download_ext)

    def test_folder_itself_is_covered(self):
        lin = load_profile("linux")
        self.assertTrue(lin.covers_folder(Path("/usr")))       # "/usr/**" alone misses "/usr"
        self.assertFalse(lin.covers_folder(Path("/home/me/Downloads")))


class TestScanner(unittest.TestCase):
    def test_scan_refuses_protected_root(self):
        tmp = tmpdir(self)
        prof = profile(tmp).with_extra_protected([tmp.as_posix() + "/**"])
        with self.assertRaises(PermissionError):
            scan(tmp, prof, include_hidden=False, max_files=100)
        sync = replace(profile(tmp), sync_roots=(tmp.resolve().as_posix() + "/**",))
        with self.assertRaises(PermissionError):
            scan(tmp, sync, include_hidden=False, max_files=100)

    def test_scanner_skips_links(self):
        tmp = tmpdir(self)
        root, outside = tmp / "root", tmp / "outside"
        make_file(outside, "secret_dir/private.txt")
        make_file(outside, "file.txt")
        make_file(root, "real.txt")
        try:
            os.symlink(outside / "secret_dir", root / "link_dir", target_is_directory=True)
            os.symlink(outside / "file.txt", root / "link_file.txt")
        except (OSError, NotImplementedError):
            self.skipTest("no symlink rights (Windows without developer mode)")
        recs, _ = scan(root, profile(tmp), include_hidden=False, max_files=100)
        self.assertEqual([r.rel_path for r in recs], ["real.txt"])

    def test_hidden_protected_and_limits(self):
        tmp = tmpdir(self)
        make_file(tmp, "a.txt")
        make_file(tmp, ".hidden.txt")
        make_file(tmp, "app_config/settings.json")
        prof = profile(tmp).with_extra_protected(["**/app_config/**"])
        recs, _ = scan(tmp, prof, include_hidden=False, max_files=100)
        by = {r.rel_path: r for r in recs}
        self.assertNotIn(".hidden.txt", by)
        self.assertTrue(by["app_config/settings.json"].protected)
        self.assertFalse(by["a.txt"].protected)
        recs, _ = scan(tmp, prof, include_hidden=True, max_files=100)
        self.assertIn(".hidden.txt", {r.rel_path for r in recs})
        recs, skipped = scan(tmp, prof, include_hidden=True, max_files=2)
        self.assertEqual(len(recs), 2)
        self.assertIn("max_files reached", skipped)

    def test_duplicates_keep_oldest(self):
        tmp = tmpdir(self)
        make_file(tmp, "a/original.pdf", b"same bytes", age_days=200)
        make_file(tmp, "b/copy (1).pdf", b"same bytes", age_days=50)
        make_file(tmp, "c/other.pdf", b"diff bytes", age_days=50)        # same size, other hash
        recs, _ = scan(tmp, profile(tmp), False, 100)
        self.assertEqual(mark_duplicates(recs), 1)
        by = {r.rel_path: r for r in recs}
        self.assertIsNone(by["a/original.pdf"].dup_of)
        self.assertEqual(by["b/copy (1).pdf"].dup_of, by["a/original.pdf"].file_id)
        self.assertIsNone(by["c/other.pdf"].dup_of)


class TestSignals(unittest.TestCase):
    def test_thai_id_and_luhn(self):
        self.assertTrue(thai_id_valid("1-1037-02071-81-1"))
        self.assertFalse(thai_id_valid("1-1037-02071-81-2"))     # wrong check digit
        self.assertFalse(thai_id_valid("1103702071"))            # too short
        self.assertTrue(luhn_valid("4111 1111 1111 1111"))
        self.assertFalse(luhn_valid("4111 1111 1111 1112"))
        self.assertFalse(luhn_valid("4111"))
        self.assertIn("thai_id", find_signals("scan_001.pdf", "ID: 1-1037-02071-81-1"))
        self.assertNotIn("thai_id", find_signals("scan_001.pdf", "ID: 1-1037-02071-81-2"))
        self.assertIn("card_number", find_signals("x.txt", "card 4111-1111-1111-1111 exp"))
        self.assertEqual(find_signals("setup_zoom_5.17.exe"), [])

    def test_keywords_and_types(self):
        self.assertEqual(signal_type(find_signals("bank_statement_may.pdf")), "financial")
        self.assertEqual(signal_type(find_signals("สลิปเงินเดือน.pdf")), "financial")
        self.assertEqual(signal_type(find_signals("my passport.jpg")), "id_doc")
        self.assertEqual(signal_type(["kw:financial", "thai_id"]), "id_doc")
        self.assertEqual(signal_type([]), "none")


class TestTriage(unittest.TestCase):
    def test_triage_order(self):
        tmp = tmpdir(self)
        recs = [
            record("f0001", "app_config/x.exe", protected=True),
            record("f0002", "Downloads/setup_new.exe", days=3),
            record("f0003", "Downloads/passport_scan.pdf"),
            record("f0004", "Downloads/Thumbs.db" if load_profile().name == "windows" else
                   "Downloads/.DS_Store" if load_profile().name == "macos" else "Downloads/a.txt~"),
            record("f0005", "Downloads/big.zip.crdownload", days=40),
            record("f0006", "Downloads/lecture (1).pdf", dup_of="f0009"),
            record("f0007", "Downloads/bank_statement (1).pdf", dup_of="f0010"),  # dup + signal
            record("f0008", "Downloads/setup_zoom.exe", days=400),
            record("f0009", "Downloads/fresh.zip.crdownload", days=31),
        ]
        prof = profile(tmp)
        if ".crdownload" not in prof.partial_download_ext:
            prof = replace(prof, partial_download_ext=prof.partial_download_ext + (".crdownload",))
        run = make_run(tmp, recs, prof)
        counts = Counter()
        pending = triage(run, settings(tmp), counts)
        d = run.decisions
        self.assertEqual((d["f0001"].label, d["f0001"].reason), ("keep", "inside a protected folder"))
        self.assertEqual(d["f0002"].label, "keep")
        self.assertEqual(d["f0004"].reason, "system junk file")
        self.assertEqual(d["f0005"].reason, "unfinished download")
        self.assertEqual(d["f0006"].reason, "exact copy of f0009")
        self.assertEqual(sorted(pending), ["f0003", "f0007", "f0008"])
        self.assertNotIn("f0007", d)                 # duplicate with a privacy signal -> LLM
        self.assertTrue(all(x.decided_by == "rule" for x in d.values()))
        self.assertEqual(counts["privacy_signal"], 2)
        self.assertEqual(counts["to_llm"], 1)

    def test_rule_decided_files_carry_sensitivity(self):
        tmp = tmpdir(self)
        run = make_run(tmp, [record("f0001", "Documents/payslip_may.pdf", days=2)])
        triage(run, settings(tmp))
        d = run.decisions["f0001"]
        self.assertEqual((d.label, d.sensitive, d.sensitive_type), ("keep", True, "financial"))


if __name__ == "__main__":
    unittest.main()
