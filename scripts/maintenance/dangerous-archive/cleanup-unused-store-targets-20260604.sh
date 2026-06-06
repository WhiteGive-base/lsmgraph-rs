#!/usr/bin/env bash
set -euo pipefail

root=/data/WorkSpace/lsmgraph-rs
cd "$root"

echo "before:"
df -h /data

mapfile -t targets < <(
  find target -maxdepth 1 -type d -name '*store*' -print
  find logs -maxdepth 2 -type d -name 'run-store*' -print
)

if [ "${#targets[@]}" -eq 0 ]; then
  echo "no cleanup targets"
  exit 0
fi

printf "cleanup targets: %s\n" "${#targets[@]}"

for p in "${targets[@]}"; do
  abs=$(realpath -m -- "$p")
  case "$abs" in
    "$root"/target/*|"$root"/logs/*) ;;
    *)
      echo "REFUSE $p -> $abs"
      exit 2
      ;;
  esac
  du -sh -- "$p" 2>/dev/null || true
done

for p in "${targets[@]}"; do
  rm -rf -- "$p"
done

echo "after:"
df -h /data

echo "remaining store root:"
find store -mindepth 1 -maxdepth 1 -type d -print | sort

echo "remaining target store roots:"
find target -maxdepth 1 -type d -name '*store*' -print | sort

echo "remaining log run-store roots:"
find logs -maxdepth 2 -type d -name 'run-store*' -print | sort
