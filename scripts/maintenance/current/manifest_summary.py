import json
import sys
from collections import Counter


def summarize(store: str) -> None:
    by_type = Counter()
    by_type_bytes = Counter()
    by_key = Counter()
    by_key_bytes = Counter()
    total = 0
    total_bytes = 0
    manifest = f"{store}/MANIFEST"
    with open(manifest, "r", encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            meta = rec.get("meta")
            if not meta:
                continue
            total += 1
            seg_bytes = int(meta.get("segment_bytes", 0))
            total_bytes += seg_bytes
            edge_type = meta.get("edge_type_partition")
            degree_class = meta.get("degree_class")
            key = (meta.get("src_label"), edge_type, degree_class)
            by_type[edge_type] += 1
            by_type_bytes[edge_type] += seg_bytes
            by_key[key] += 1
            by_key_bytes[key] += seg_bytes

    print(f"STORE {store}")
    print(f"files {total} segment_bytes {total_bytes}")
    print("top edge_type by files")
    for edge_type, count in by_type.most_common(12):
        print(f"  et {edge_type} files {count} bytes {by_type_bytes[edge_type]}")
    print("top key by files")
    for key, count in by_key.most_common(16):
        print(f"  key {key} files {count} bytes {by_key_bytes[key]}")
    print()


for arg in sys.argv[1:]:
    summarize(arg)
