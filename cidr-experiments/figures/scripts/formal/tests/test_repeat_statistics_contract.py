from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


FORMAL_DIR = Path(__file__).resolve().parents[1]
if str(FORMAL_DIR) not in sys.path:
    sys.path.insert(0, str(FORMAL_DIR))

import plot_figure1_end_to_end as figure1  # noqa: E402
import plot_figure2_component_ablation as figure2  # noqa: E402
import plot_figure3_resource_pareto as figure3  # noqa: E402
import plot_figure5_workload_coverage as figure5  # noqa: E402
import plot_figure6_scalability as figure6  # noqa: E402
from plot_support import (  # noqa: E402
    DEFAULT_MINIMUM_RUNS,
    DataContractError,
    estimate_values,
    paired_ratio_estimate,
)


def values(count: int) -> list[tuple[str, float]]:
    return [(f"run-{index}", float(index)) for index in range(1, count + 1)]


def paired_rows(count: int, *, multiplier: float) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for index in range(1, count + 1):
        rows.append(
            {
                "run_id": f"run-{multiplier:g}-{index}",
                "repeat_index": str(index),
                "query_trace_sha256": f"trace-{index}",
                "dataset_id": "dataset",
                "input_sha256": "input",
                "workload_id": "workload",
                "seed": str(index),
                "cache_state": "warm",
                "concurrency": "1",
                "directed_edge_count": "100",
                "value": str(multiplier * index),
            }
        )
    return rows


class RepeatStatisticsContractTests(unittest.TestCase):
    def test_default_is_three_and_three_runs_use_full_range(self) -> None:
        estimate = estimate_values(
            [("r1", 7.0), ("r2", 1.0), ("r3", 4.0)],
            label="n3",
        )
        self.assertEqual(DEFAULT_MINIMUM_RUNS, 3)
        self.assertEqual(estimate.n, 3)
        self.assertEqual(estimate.center, 4.0)
        self.assertEqual((estimate.low, estimate.high), (1.0, 7.0))
        self.assertEqual(estimate.interval_kind, "range")

    def test_four_runs_fail_as_incomplete_adaptive_set(self) -> None:
        with self.assertRaisesRegex(DataContractError, "incomplete adaptive repeat set"):
            estimate_values(values(4), label="n4")

    def test_five_runs_keep_all_provenance_and_use_bootstrap_ci(self) -> None:
        estimate = estimate_values(values(5), label="n5", minimum_runs=3)
        self.assertEqual(estimate.n, 5)
        self.assertEqual(estimate.interval_kind, "bootstrap_95_ci")
        self.assertLessEqual(estimate.low, estimate.center)
        self.assertGreaterEqual(estimate.high, estimate.center)

    def test_explicit_stricter_override_is_preserved(self) -> None:
        with self.assertRaisesRegex(DataContractError, "requires at least 5"):
            estimate_values(values(3), label="override", minimum_runs=5)
        estimate = estimate_values(values(5), label="override", minimum_runs=5)
        self.assertEqual((estimate.n, estimate.interval_kind), (5, "bootstrap_95_ci"))

    def test_override_cannot_relax_frozen_three_run_floor(self) -> None:
        with self.assertRaisesRegex(DataContractError, "minimum_runs must be an integer >= 3"):
            estimate_values(values(3), label="too-low", minimum_runs=2)

    def test_duplicate_run_provenance_is_rejected(self) -> None:
        with self.assertRaisesRegex(DataContractError, "duplicate run_id"):
            estimate_values(
                [("r1", 1.0), ("r1", 2.0), ("r3", 3.0)],
                label="duplicate",
            )

    def test_paired_ratio_uses_same_range_rule(self) -> None:
        numerator = paired_rows(3, multiplier=2.0)
        denominator = paired_rows(3, multiplier=1.0)
        estimate = paired_ratio_estimate(
            numerator,
            denominator,
            numerator_getter=lambda row: float(row["value"]),
            denominator_getter=lambda row: float(row["value"]),
            label="paired",
        )
        self.assertEqual(estimate.n, 3)
        self.assertEqual(estimate.interval_kind, "range")
        self.assertEqual((estimate.center, estimate.low, estimate.high), (2.0, 2.0, 2.0))


class EntryPointDefaultTests(unittest.TestCase):
    def test_figure1_to_3_defaults_and_overrides(self) -> None:
        with patch.object(sys, "argv", ["f1", "--input", "dummy.tsv"]):
            self.assertEqual(figure1.parse_args().min_runs, 3)
        with patch.object(
            sys,
            "argv",
            ["f2", "--input", "dummy.tsv", "--min-runs", "5"],
        ):
            self.assertEqual(figure2.parse_args().min_runs, 5)
        with patch.object(sys, "argv", ["f3", "--input", "dummy.tsv"]):
            args = figure3.parse_args()
            self.assertEqual(args.performance_min_runs, 3)
            self.assertEqual(args.resource_min_runs, 3)

    def test_figure5_and_6_defaults_and_overrides(self) -> None:
        self.assertEqual(figure5._parse_args(["--out-dir", "out"]).min_runs, 3)
        self.assertEqual(
            figure6._parse_args(["--out-dir", "out", "--min-runs", "6"]).min_runs,
            6,
        )


if __name__ == "__main__":
    unittest.main()
