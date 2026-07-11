import tempfile
import unittest
from pathlib import Path

from render_priority_eval_supplement_20260709 import (
    parse_duration_seconds,
    parse_time_v,
)


class DurationParserTests(unittest.TestCase):
    def test_parses_minutes_seconds(self) -> None:
        self.assertAlmostEqual(parse_duration_seconds("3:19.49"), 199.49)

    def test_parses_hours_minutes_seconds(self) -> None:
        self.assertAlmostEqual(parse_duration_seconds("1:02:03.50"), 3723.50)

    def test_parses_plain_seconds(self) -> None:
        self.assertAlmostEqual(parse_duration_seconds("19.49"), 19.49)

    def test_parse_time_v_emits_seconds_column(self) -> None:
        content = "\n".join(
            [
                "Command being timed: test",
                "Elapsed (wall clock) time (h:mm:ss or m:ss): 3:19.49",
                "Maximum resident set size (kbytes): 1234",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "time-v.txt"
            path.write_text(content, encoding="utf-8")
            parsed = parse_time_v(path)
        self.assertEqual(parsed["import_wall_s"], "199.49")
        self.assertEqual(parsed["max_rss_kb"], "1234")


if __name__ == "__main__":
    unittest.main()
