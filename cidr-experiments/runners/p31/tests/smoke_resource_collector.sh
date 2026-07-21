#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(git -C "$RUNNER_DIR" rev-parse --show-toplevel)"
TMP_ROOT="$(mktemp -d /tmp/cidr-p31-smoke.XXXXXX)"
SMOKE_OK=0

cleanup() {
  case "$TMP_ROOT" in
    /tmp/cidr-p31-smoke.*)
      if [[ "$SMOKE_OK" == "1" ]]; then
        [[ -d "$TMP_ROOT" ]] && rm -rf -- "$TMP_ROOT"
      else
        echo "P31 smoke failed; retained artifacts at $TMP_ROOT" >&2
      fi
      ;;
    *)
      echo "refusing to remove unexpected smoke path: $TMP_ROOT" >&2
      ;;
  esac
}
trap cleanup EXIT

PASS_RUN="${TMP_ROOT}/pass"
"${RUNNER_DIR}/run_with_resources.sh" \
  --run-dir "$PASS_RUN" \
  --task-id P31-smoke-pass \
  --performance-eligible false \
  --repo-root "$REPO_ROOT" \
  --device nvme1n1 \
  --data-mount /tmp \
  --interval 0.5 \
  --disk-interval 0.5 \
  --min-samples 3 \
  --store "fixture-store=${TMP_ROOT}/pass-store" \
  --temp "fixture-temp=${TMP_ROOT}/pass-temp" \
  -- python3 "${SCRIPT_DIR}/fixture_process_tree.py" \
    --store "${TMP_ROOT}/pass-store" \
    --temp "${TMP_ROOT}/pass-temp" \
    --seconds 4

python3 "${SCRIPT_DIR}/assert_smoke_artifacts.py" --run-dir "$PASS_RUN" --expect pass

printf '# intentional smoke tamper\n' >> "${PASS_RUN}/command.txt"
set +e
python3 -B "${RUNNER_DIR}/validate_resource_run.py" --run-dir "$PASS_RUN" --min-samples 3 \
  > "${TMP_ROOT}/tamper-validator.stdout" 2> "${TMP_ROOT}/tamper-validator.stderr"
TAMPER_RC=$?
set -e
[[ "$TAMPER_RC" -ne 0 ]] || { echo "tampered command unexpectedly passed validation" >&2; exit 1; }
python3 "${SCRIPT_DIR}/assert_smoke_artifacts.py" --run-dir "$PASS_RUN" --expect failed

FAIL_RUN="${TMP_ROOT}/command-failure"
set +e
"${RUNNER_DIR}/run_with_resources.sh" \
  --run-dir "$FAIL_RUN" \
  --task-id P31-smoke-command-failure \
  --performance-eligible false \
  --repo-root "$REPO_ROOT" \
  --device nvme1n1 \
  --data-mount /tmp \
  --interval 0.5 \
  --disk-interval 0.5 \
  --min-samples 3 \
  --store "fixture-store=${TMP_ROOT}/fail-store" \
  --temp "fixture-temp=${TMP_ROOT}/fail-temp" \
  -- python3 "${SCRIPT_DIR}/fixture_process_tree.py" \
    --store "${TMP_ROOT}/fail-store" \
    --temp "${TMP_ROOT}/fail-temp" \
    --seconds 3 \
    --exit-code 7
FAIL_RC=$?
set -e
[[ "$FAIL_RC" -eq 7 ]] || { echo "expected command exit 7, got $FAIL_RC" >&2; exit 1; }
python3 "${SCRIPT_DIR}/assert_smoke_artifacts.py" --run-dir "$FAIL_RUN" --expect failed

MANIFEST_FAIL_RUN="${TMP_ROOT}/manifest-failure"
set +e
"${RUNNER_DIR}/run_with_resources.sh" \
  --run-dir "$MANIFEST_FAIL_RUN" \
  --task-id P31-smoke-manifest-failure \
  --performance-eligible false \
  --repo-root "$REPO_ROOT" \
  --data-mount /tmp \
  --store "fixture-store=${TMP_ROOT}/overlap" \
  --temp "fixture-temp=${TMP_ROOT}/overlap" \
  -- python3 "${SCRIPT_DIR}/fixture_process_tree.py" \
    --store "${TMP_ROOT}/overlap" \
    --temp "${TMP_ROOT}/overlap" \
    --seconds 1
MANIFEST_FAIL_RC=$?
set -e
[[ "$MANIFEST_FAIL_RC" -ne 0 ]] || { echo "overlapping roots unexpectedly passed manifest creation" >&2; exit 1; }
[[ -f "${MANIFEST_FAIL_RUN}/FAILED" ]] || { echo "manifest failure did not leave FAILED" >&2; exit 1; }
[[ ! -e "${MANIFEST_FAIL_RUN}/DONE" ]] || { echo "manifest failure left DONE" >&2; exit 1; }

SIGNAL_RUN="${TMP_ROOT}/signal-failure"
set +e
"${RUNNER_DIR}/run_with_resources.sh" \
  --run-dir "$SIGNAL_RUN" \
  --task-id P31-smoke-signal-failure \
  --performance-eligible false \
  --repo-root "$REPO_ROOT" \
  --device nvme1n1 \
  --data-mount /tmp \
  --interval 0.5 \
  --disk-interval 0.5 \
  --min-samples 2 \
  --store "fixture-store=${TMP_ROOT}/signal-store" \
  --temp "fixture-temp=${TMP_ROOT}/signal-temp" \
  -- python3 "${SCRIPT_DIR}/fixture_process_tree.py" \
    --store "${TMP_ROOT}/signal-store" \
    --temp "${TMP_ROOT}/signal-temp" \
    --seconds 10 &
SIGNAL_WRAPPER_PID=$!
sleep 1.5
kill -TERM "$SIGNAL_WRAPPER_PID"
wait "$SIGNAL_WRAPPER_PID"
SIGNAL_RC=$?
set -e
[[ "$SIGNAL_RC" -ne 0 ]] || { echo "signaled wrapper unexpectedly returned success" >&2; exit 1; }
python3 "${SCRIPT_DIR}/assert_smoke_artifacts.py" --run-dir "$SIGNAL_RUN" --expect failed
grep -q "wrapper was interrupted by signal 15" "${SIGNAL_RUN}/validation.json" || {
  echo "signal failure reason was not preserved" >&2
  exit 1
}

SMOKE_OK=1
echo "P31 fixture smoke passed (success plus tamper, command, manifest, and signal rejection)."
