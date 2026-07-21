#!/usr/bin/env python3
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
RECOVER = ROOT / "baseline" / "recover_dense_id_map.py"
CONVERTER = ROOT / "baseline" / "convert_livegraph_edges.py"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
RAW = FIXTURES / "raw-edges.tsv"
DENSE = FIXTURES / "dense-edges.txt"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RecoverDenseIdMapTest(unittest.TestCase):
    def command(self, output, raw=RAW, dense=DENSE, *extra):
        return [
            sys.executable,
            str(RECOVER),
            "--raw",
            str(raw),
            "--dense",
            str(dense),
            "--converter",
            str(CONVERTER),
            "--output-dir",
            str(output),
            "--expected-raw-sha256",
            sha256(raw),
            "--expected-dense-sha256",
            sha256(dense),
            "--expected-converter-sha256",
            sha256(CONVERTER),
            *extra,
        ]

    def test_formal_fixture_recovers_maps_and_publishes_pass_atomically(self):
        with tempfile.TemporaryDirectory() as tmp_raw:
            output = pathlib.Path(tmp_raw) / "id-map"
            result = subprocess.run(
                self.command(output, RAW, DENSE, "--expected-vertex-count", "3", "--expected-edge-count", "3"),
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((output / "FORMAL-PASS").is_file())
            self.assertFalse((output / "PARTIAL-NOT-FORMAL").exists())
            self.assertEqual(
                (output / "dense-to-original.tsv").read_text(encoding="ascii"),
                "dense_id\toriginal_id\n0\t100\n1\t400\n2\t900\n",
            )
            self.assertEqual(
                (output / "original-to-dense.tsv").read_text(encoding="ascii"),
                "original_id\tdense_id\n100\t0\n400\t1\n900\t2\n",
            )
            manifest = json.loads((output / "id-map-manifest.json").read_text())
            self.assertEqual(manifest["format"], "seml0-shared-id-map")
            self.assertEqual(manifest["status"], "PASS")
            self.assertTrue(manifest["formal_pass"])
            self.assertTrue(manifest["verification_complete"])
            self.assertEqual(manifest["vertex_count"], 3)
            self.assertEqual(manifest["mapping_hash"], "e859143c2d2eeedc")
            self.assertEqual(
                manifest["recovery"]["validation"]["verified_edge_rows"], 3
            )
            self.assertEqual(
                manifest["recovery"]["validation"]["input_sha256_during_lockstep"],
                "PASS",
            )
            for line in (output / "SHA256SUMS").read_text().splitlines():
                expected, filename = line.split("  ", 1)
                self.assertEqual(sha256(output / filename), expected)

    def test_max_rows_is_partial_unsupported_and_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp_raw:
            output = pathlib.Path(tmp_raw) / "partial-map"
            result = subprocess.run(
                self.command(output, RAW, DENSE, "--max-rows", "2"),
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 3, result.stderr)
            manifest = json.loads((output / "id-map-manifest.json").read_text())
            self.assertEqual(manifest["format"], "seml0-shared-id-map-partial")
            self.assertEqual(manifest["status"], "PARTIAL_NOT_FORMAL")
            self.assertFalse(manifest["formal_pass"])
            self.assertFalse(manifest["verification_complete"])
            self.assertEqual(
                manifest["recovery"]["validation"]["verified_edge_rows"], 2
            )
            self.assertEqual(
                manifest["recovery"]["validation"]["complete_edge_count"],
                "NOT_RUN_PARTIAL",
            )
            self.assertTrue((output / "PARTIAL-NOT-FORMAL").is_file())
            self.assertFalse((output / "FORMAL-PASS").exists())

    def test_sha_mismatch_fails_before_creating_output(self):
        with tempfile.TemporaryDirectory() as tmp_raw:
            output = pathlib.Path(tmp_raw) / "id-map"
            command = self.command(output)
            raw_sha_index = command.index("--expected-raw-sha256") + 1
            command[raw_sha_index] = "0" * 64
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("raw edge list SHA-256 mismatch", result.stderr)
            self.assertFalse(output.exists())

    def test_semantic_mismatch_with_valid_input_hash_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = pathlib.Path(tmp_raw)
            bad_dense = tmp / "bad-dense.txt"
            bad_dense.write_text("3\n0 7 1\n0 8 2\n1 -7 0\n", encoding="ascii")
            output = tmp / "id-map"
            result = subprocess.run(
                self.command(output, RAW, bad_dense), capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("type mismatch", result.stderr)
            self.assertFalse(output.exists())
            self.assertEqual(list(tmp.glob(".id-map.tmp-*")), [])

    def test_first_seen_mapping_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = pathlib.Path(tmp_raw)
            bad_dense = tmp / "bad-order.txt"
            bad_dense.write_text("3\n0 7 2\n0 7 1\n2 -7 0\n", encoding="ascii")
            output = tmp / "id-map"
            result = subprocess.run(
                self.command(output, RAW, bad_dense), capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("must receive next dense ID 1, found 2", result.stderr)
            self.assertFalse(output.exists())

    def test_header_vertex_count_must_match_complete_mapping(self):
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = pathlib.Path(tmp_raw)
            bad_dense = tmp / "bad-header.txt"
            bad_dense.write_text("4\n0 7 1\n0 7 2\n1 -7 0\n", encoding="ascii")
            output = tmp / "id-map"
            result = subprocess.run(
                self.command(output, RAW, bad_dense), capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("recovered vertex-count mismatch", result.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
