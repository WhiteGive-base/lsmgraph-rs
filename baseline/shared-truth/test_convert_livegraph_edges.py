#!/usr/bin/env python3
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
CONVERTER = ROOT / "baseline" / "convert_livegraph_edges.py"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


class ConvertLiveGraphEdgesTest(unittest.TestCase):
    def test_converter_persists_bijective_maps_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = pathlib.Path(tmp_raw)
            dense = tmp / "dense.txt"
            summary = tmp / "summary.json"
            maps = tmp / "id-map"
            subprocess.run(
                [
                    sys.executable,
                    str(CONVERTER),
                    "--input",
                    str(FIXTURES / "raw-edges.tsv"),
                    "--output",
                    str(dense),
                    "--summary",
                    str(summary),
                    "--id-map-dir",
                    str(maps),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                dense.read_text(encoding="utf-8"),
                "3\n0 7 1\n0 7 2\n1 -7 0\n",
            )
            self.assertEqual(
                (maps / "dense-to-original.tsv").read_text(encoding="utf-8"),
                "dense_id\toriginal_id\n0\t100\n1\t400\n2\t900\n",
            )
            self.assertEqual(
                (maps / "original-to-dense.tsv").read_text(encoding="utf-8"),
                "original_id\tdense_id\n100\t0\n400\t1\n900\t2\n",
            )

            manifest = json.loads((maps / "id-map-manifest.json").read_text())
            self.assertEqual(manifest["format"], "seml0-shared-id-map")
            self.assertEqual(manifest["vertex_count"], 3)
            self.assertEqual(
                manifest["mapping_hash_algorithm"],
                "fnv1a64-le-dense-original-v1",
            )
            self.assertEqual(manifest["mapping_hash"], "e859143c2d2eeedc")
            for key in ("dense_to_original", "original_to_dense"):
                path = maps / manifest[key]["path"]
                self.assertEqual(
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    manifest[key]["sha256"],
                )

            checksum_lines = (maps / "SHA256SUMS").read_text().splitlines()
            self.assertEqual(len(checksum_lines), 2)
            result = json.loads(summary.read_text())
            self.assertEqual(result["vertex_count"], 3)
            self.assertEqual(result["edge_count"], 3)
            self.assertEqual(result["id_map"]["mapping_hash"], "e859143c2d2eeedc")


if __name__ == "__main__":
    unittest.main()
