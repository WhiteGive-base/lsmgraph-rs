#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
P10_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

bash -n "$P10_DIR/run_adapter_with_p31.sh" "$SCRIPT_DIR/fixture_p31.sh"
for json_file in \
  "$SCRIPT_DIR/fixture-suite.json" \
  "$P10_DIR/adapters/aster/formal-system.template.json" \
  "$P10_DIR/schemas/suite-manifest.schema.json" \
  "$P10_DIR/schemas/adapter-request.schema.json" \
  "$P10_DIR/schemas/adapter-result.schema.json" \
  "$P10_DIR/schemas/observation-row.schema.json"; do
  python3 -B -m json.tool "$json_file" >/dev/null
done
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s "$SCRIPT_DIR" -p 'test_*.py' -v
