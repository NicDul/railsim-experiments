"""Regression tests for the UC1 capacity proofs (CP-SAT/arithmetic only, about 15 s)."""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

try:
    import ortools  # noqa: F401
except ImportError:  # pragma: no cover
    ortools = None

PROOFS = Path(__file__).resolve().parents[3] / 'proofs'


@unittest.skipIf(ortools is None, 'ortools is not installed')
class Bb3FvStopProofTest(unittest.TestCase):
    """34/hour on BB3 MAINLINE/FV_STOP: 137,280 cyclic type words, 150 survivors, all infeasible."""

    @unittest.skipIf(shutil.which('c++') is None, 'no C++ compiler')
    def test_enumeration_reproduces_survivors_and_certificates_verify(self):
        folder = PROOFS / 'bb3-fv-stop'
        subprocess.run(['c++', '-O3', '-std=c++17', 'enumerate_orders.cpp', '-o', 'enumerate_orders'],
                       cwd=folder, check=True)
        subprocess.run(['./enumerate_orders', 'survivors.tmp', 'order-certificates.tsv'], cwd=folder, check=True)
        try:
            self.assertEqual((folder / 'survivors.tmp').read_text(),
                             (folder / 'surviving-type-orders.txt').read_text())
        finally:
            (folder / 'survivors.tmp').unlink(missing_ok=True)
        subprocess.run([sys.executable, 'audit_certificates.py'], cwd=folder, check=True, capture_output=True)
        audit = json.loads((folder / 'certificate-audit.json').read_text())
        self.assertEqual((audit['status'], audit['canonical_words_covered'],
                          audit['positive_cycle_certificates'], audit['survivors']),
                         ('PASS', 137280, 137130, 150))

    def test_survivors_are_infeasible_with_all_platforms_free(self):
        folder = PROOFS / 'bb3-fv-stop'
        words = (folder / 'surviving-type-orders.txt').read_text().split()
        self.assertEqual(len(words), 150)
        sys.path.insert(0, str(folder))
        try:
            import check_survivors
            for word in words[:10]:
                self.assertEqual(check_survivors.check(word, seconds=20)['status'], 'INFEASIBLE', word)
        finally:
            sys.path.remove(str(folder))


@unittest.skipIf(ortools is None, 'ortools is not installed')
class WindowBoundTest(unittest.TestCase):
    def test_bb3_fv_pass_42_is_excluded_by_three_entry_windows(self):
        folder = PROOFS / 'window-bounds'
        done = subprocess.run([sys.executable, 'check_remaining.py', '--mode', 'fv_pass', '--rates', '42'],
                              cwd=folder, check=True, capture_output=True, text=True)
        results = [json.loads(line[len('RESULT '):]) for line in done.stdout.splitlines()
                   if line.startswith('RESULT ')]
        self.assertTrue(any(r['k'] == 3 and r['status'] == 'INFEASIBLE'
                            and r['excluded_in_fixed_profile_model'] for r in results))


if __name__ == '__main__':
    unittest.main()
