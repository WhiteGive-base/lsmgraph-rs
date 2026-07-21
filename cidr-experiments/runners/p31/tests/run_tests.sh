#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

bash -n "${RUNNER_DIR}/run_with_resources.sh" "${SCRIPT_DIR}/smoke_resource_collector.sh" "${SCRIPT_DIR}/run_tests.sh"
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s "$SCRIPT_DIR" -p 'test_*.py' -v
"${SCRIPT_DIR}/smoke_resource_collector.sh"
