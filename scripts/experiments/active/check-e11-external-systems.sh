#!/usr/bin/env bash
set -euo pipefail

cd /data/WorkSpace/lsmgraph-rs

OUTPUT="${OUTPUT:-remote-logs/e11-external-systems-check.txt}"

check_command() {
  local name="$1"
  shift
  local path
  if path=$(command -v "$1" 2>/dev/null); then
    echo "FOUND ${name}: ${path}"
    return 0
  else
    echo "MISSING ${name}"
    return 1
  fi
}

check_repository() {
  local name="$1"
  shift
  local dir="$1"
  if [[ -d "$dir" ]]; then
    echo "FOUND ${name} repo: ${dir}"
    return 0
  else
    echo "MISSING ${name} repo: ${dir}"
    return 1
  fi
}

mkdir -p "$(dirname "$OUTPUT")"
{
  echo "E11 External Systems Check"
  echo "date=$(date -Is)"
  echo "pwd=$(pwd)"

  echo ""
  echo "## Toolchain"
  cargo --version 2>&1
  rustc --version 2>&1

  echo ""
  echo "## External Systems"

  echo ""
  echo "### LiveGraph"
  check_command "LiveGraph binary" livegraph || true
  check_repository "LiveGraph" "/data/WorkSpace/LiveGraph" || true
  if [[ -d "/data/WorkSpace/LiveGraph" ]]; then
    git -C "/data/WorkSpace/LiveGraph" rev-parse HEAD 2>&1 || true
  fi

  echo ""
  echo "### Teseo"
  check_command "Teseo binary" teseo || true
  check_repository "Teseo" "/data/WorkSpace/teseo" || true
  if [[ -d "/data/WorkSpace/teseo" ]]; then
    git -C "/data/WorkSpace/teseo" rev-parse HEAD 2>&1 || true
  fi

  echo ""
  echo "### GraphOne"
  check_command "GraphOne binary" graphone || true
  check_repository "GraphOne" "/data/WorkSpace/GraphOne" || true
  if [[ -d "/data/WorkSpace/GraphOne" ]]; then
    git -C "/data/WorkSpace/GraphOne" rev-parse HEAD 2>&1 || true
  fi

  echo ""
  echo "### LLAMA"
  check_command "LLAMA binary" llama || true
  check_repository "LLAMA" "/data/WorkSpace/LLAMA" || true
  if [[ -d "/data/WorkSpace/LLAMA" ]]; then
    git -C "/data/WorkSpace/LLAMA" rev-parse HEAD 2>&1 || true
  fi

  echo ""
  echo "## Data Availability"
  DATA="${DATA:-/data/WorkSpace/dgs/data/social_network_tugraph}"
  if [[ -d "$DATA" ]]; then
    echo "FOUND data: ${DATA}"
    if [[ -d "$DATA/static" && -d "$DATA/dynamic" ]]; then
      echo "LDBC structure: complete"
    else
      echo "LDBC structure: incomplete"
    fi
  else
    echo "MISSING data: ${DATA}"
  fi

} > "$OUTPUT" 2>&1

cat "$OUTPUT"
echo ""
echo "Check written to: ${OUTPUT}"
