#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
P02B_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$P02B_DIR"

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m py_compile \
  p02b_common.py validate_clean_ready.py extract_run_metrics.py calculate_cv.py \
  build_lineage_manifest.py \
  run_sf10_sentinel.py validate_sentinel_result.py \
  tests/fixture_binary.py tests/prepare_fixture.py tests/test_p02b.py
bash -n tests/run_tests.sh tests/smoke_fixture.sh
tests/smoke_fixture.sh
