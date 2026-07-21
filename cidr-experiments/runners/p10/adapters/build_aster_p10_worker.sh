#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  echo "usage: $0 --aster-root DIR --output FILE [--boost-include DIR]" >&2
  exit 2
}

ASTER_ROOT=""
OUTPUT=""
BOOST_INCLUDE=""
while (($#)); do
  case "$1" in
    --aster-root) ASTER_ROOT="${2:-}"; shift 2 ;;
    --output) OUTPUT="${2:-}"; shift 2 ;;
    --boost-include) BOOST_INCLUDE="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done

[[ -n "$ASTER_ROOT" && -n "$OUTPUT" ]] || usage
ASTER_ROOT="$(realpath "$ASTER_ROOT")"
OUTPUT="$(realpath -m "$OUTPUT")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$SCRIPT_DIR/aster_p10_worker.cc"

[[ -f "$SOURCE" ]] || { echo "missing worker source: $SOURCE" >&2; exit 2; }
[[ -f "$ASTER_ROOT/include/rocksdb/graph.h" ]] || {
  echo "not an Aster/RocksGraph source tree: $ASTER_ROOT" >&2
  exit 2
}
[[ -f "$ASTER_ROOT/librocksdb.a" ]] || {
  echo "missing $ASTER_ROOT/librocksdb.a; build Aster static_lib first" >&2
  exit 2
}

if [[ -z "$BOOST_INCLUDE" && -f "$ASTER_ROOT/make_config.mk" ]]; then
  BOOST_INCLUDE="$(grep -oE -- '-I[^ ]*/boost-root/usr/include' "$ASTER_ROOT/make_config.mk" \
    | head -n 1 | sed 's/^-I//' || true)"
fi
if [[ -z "$BOOST_INCLUDE" && -f /usr/include/boost/functional/hash.hpp ]]; then
  BOOST_INCLUDE="/usr/include"
fi
[[ -n "$BOOST_INCLUDE" && -f "$BOOST_INCLUDE/boost/functional/hash.hpp" ]] || {
  echo "cannot locate boost/functional/hash.hpp; pass --boost-include" >&2
  exit 2
}

mkdir -p "$(dirname "$OUTPUT")"
g++ -std=c++17 -O2 -DNDEBUG \
  -I"$ASTER_ROOT/include" \
  -I"$BOOST_INCLUDE" \
  "$SOURCE" -o "$OUTPUT" "$ASTER_ROOT/librocksdb.a" \
  -lpthread -lrt -ldl -lz -lnuma -ltbb

"$OUTPUT" --capabilities
sha256sum "$SOURCE" "$OUTPUT"
