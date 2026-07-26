#!/usr/bin/env python3
"""Standalone validator for a generated E01 production command plan."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import build_e01_production_command_plan as command_plan


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    value = command_plan.validate_command_plan(args.plan, args.plan)
    print(
        json.dumps(
            {
                "state": "PASS",
                "execution_state": value["execution_state"],
                "cell_count": value["cell_count"],
                **command_plan.FALSE_ELIGIBILITY,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (command_plan.PlanError, OSError, ValueError) as exc:
        print(f"E01 COMMAND PLAN VALIDATION FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
