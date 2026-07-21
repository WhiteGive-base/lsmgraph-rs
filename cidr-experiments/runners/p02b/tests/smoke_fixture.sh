#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
P02B_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(git -C "$P02B_DIR" rev-parse --show-toplevel)"
FIXTURE_BINARY="$SCRIPT_DIR/fixture_binary.py"
TMP_ROOT="$(mktemp -d /data/WorkSpace/p02b-fixture.XXXXXX)"

cleanup() {
  case "$TMP_ROOT" in
    /data/WorkSpace/p02b-fixture.*) rm -rf -- "$TMP_ROOT" ;;
    *) printf 'refusing to remove unexpected fixture path: %s\n' "$TMP_ROOT" >&2 ;;
  esac
}
trap cleanup EXIT

python3 -B "$SCRIPT_DIR/prepare_fixture.py" --root "$TMP_ROOT" --repo-root "$REPO_ROOT"

"$FIXTURE_BINARY" --io-backend blocking shared-truth-verify \
  --data-dir "$TMP_ROOT/store" \
  --truth-tsv "$TMP_ROOT/truth.tsv" \
  --id-map-dir "$TMP_ROOT/id-map" \
  --expected-queries 6 \
  --l0-layout schema \
  --semantic-degree-hint \
  --sample-plan-out "$TMP_ROOT/query-plan.json" \
  --output "$TMP_ROOT/preflight.json"

python3 -B "$P02B_DIR/run_sf10_sentinel.py" \
  --run-dir "$TMP_ROOT/run" \
  --clean-ready "$TMP_ROOT/P03-CLEAN-WINDOW-MONITOR/raw/fixture-clean/READY" \
  --repo-root "$REPO_ROOT" \
  --binary "$FIXTURE_BINARY" \
  --dataset-manifest "$TMP_ROOT/dataset-manifest.json" \
  --store "$TMP_ROOT/store" \
  --store-manifest "$TMP_ROOT/store-manifest.json" \
  --truth "$TMP_ROOT/truth.tsv" \
  --query-plan "$TMP_ROOT/query-plan.json" \
  --id-map-dir "$TMP_ROOT/id-map" \
  --config "$TMP_ROOT/config.json" \
  > "$TMP_ROOT/runner.stdout.log" 2> "$TMP_ROOT/runner.stderr.log"

test -f "$TMP_ROOT/run/FIXTURE-PASS"
test ! -e "$TMP_ROOT/run/PASS"
test ! -e "$TMP_ROOT/run/FAILED"

python3 -B "$P02B_DIR/validate_sentinel_result.py" \
  --result "$TMP_ROOT/run/sentinel-result.json" --consumer P10 \
  > "$TMP_ROOT/consumer-pass.json"

if python3 -B "$P02B_DIR/validate_sentinel_result.py" \
  --result "$TMP_ROOT/run/sentinel-result.json" --consumer P20 --require-formal \
  > "$TMP_ROOT/formal-consumer.stdout.log" 2> "$TMP_ROOT/formal-consumer.stderr.log"; then
  printf 'fixture result incorrectly released a formal consumer\n' >&2
  exit 1
fi

cp "$TMP_ROOT/run/sentinel-result.json" "$TMP_ROOT/sentinel-result.original.json"
printf ' ' >> "$TMP_ROOT/run/sentinel-result.json"
if python3 -B "$P02B_DIR/validate_sentinel_result.py" \
  --result "$TMP_ROOT/run/sentinel-result.json" --consumer P10 \
  > "$TMP_ROOT/tamper.stdout.log" 2> "$TMP_ROOT/tamper.stderr.log"; then
  printf 'tampered sentinel result was accepted\n' >&2
  exit 1
fi
mv "$TMP_ROOT/sentinel-result.original.json" "$TMP_ROOT/run/sentinel-result.json"

python3 - "$TMP_ROOT/run/sentinel-result.json" <<'PY'
import json
import sys

result = json.load(open(sys.argv[1], encoding="utf-8"))
assert result["state"] == "PASS"
assert result["fixture_only"] is True
assert result["formal_gate_eligible"] is False
assert result["correctness"]["checked"] == 6
assert result["correctness"]["mismatches"] == 0
assert result["stability"]["qps"]["cv"] <= 0.03
assert result["stability"]["p99_us"]["cv"] <= 0.05
assert len(result["repeats"]) == 3
for repeat in result["repeats"]:
    assert repeat["p31"]["manifest"]["sha256"]
PY

printf 'P02B fixture smoke PASS (3 fake independent runs + P31 + fail-closed consumer checks)\n'
