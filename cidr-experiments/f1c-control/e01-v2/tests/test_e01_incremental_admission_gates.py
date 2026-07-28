#!/usr/bin/env python3

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import seal_e01_incremental_admission_gates as gates


class AdmissionGateTests(unittest.TestCase):
    def test_thresholds_are_frozen(self) -> None:
        self.assertEqual(gates.MIN_DATA_FREE_BYTES, 200 * 1024**3)
        self.assertEqual(gates.MIN_MEM_AVAILABLE_KIB, 400 * 1024**2)

    def test_false_eligibility_is_fail_closed(self) -> None:
        with self.assertRaises(gates.GateSealError):
            gates.false_eligibility({"formal_eligible": True}, "fixture")

    def test_iso_utc(self) -> None:
        self.assertEqual(gates.iso("2026-07-28T00:00:00Z").utcoffset().total_seconds(), 0)


if __name__ == "__main__":
    unittest.main()
