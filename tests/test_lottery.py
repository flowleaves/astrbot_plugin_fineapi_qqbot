import sys
import time
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from fineapi_core.lottery import (
    LotteryTimeError,
    format_draw_time,
    parse_draw_time,
    pick_winners,
    split_prize,
)


def local_ts(year, month, day, hour, minute):
    return int(
        time.mktime(time.struct_time((year, month, day, hour, minute, 0, 0, 0, -1)))
    )


BASE = local_ts(2026, 11, 10, 12, 0)


class ParseTimeTest(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(parse_draw_time("30m", now=BASE), BASE + 1800)
        self.assertEqual(parse_draw_time("2h", now=BASE), BASE + 7200)
        self.assertEqual(parse_draw_time("1d", now=BASE), BASE + 86400)

    def test_hhmm(self):
        self.assertEqual(parse_draw_time("21:30", now=BASE), local_ts(2026, 11, 10, 21, 30))
        # 已过的时间自动顺延到明天
        self.assertEqual(parse_draw_time("11:30", now=BASE), local_ts(2026, 11, 11, 11, 30))

    def test_chinese_day(self):
        self.assertEqual(
            parse_draw_time("明天 21:30", now=BASE), local_ts(2026, 11, 11, 21, 30)
        )
        self.assertEqual(
            parse_draw_time("后天 09:00", now=BASE), local_ts(2026, 11, 12, 9, 0)
        )

    def test_month_day_rolls_to_next_year(self):
        result = parse_draw_time("11-09T21:30", now=BASE, max_days=400)
        self.assertEqual(time.localtime(result).tm_year, 2027)

    def test_full_date(self):
        result = parse_draw_time("2027-01-01T00:00", now=BASE, max_days=400)
        self.assertEqual(result, local_ts(2027, 1, 1, 0, 0))

    def test_invalid(self):
        with self.assertRaises(LotteryTimeError):
            parse_draw_time("明天晚上", now=BASE)
        with self.assertRaises(LotteryTimeError):
            parse_draw_time("30s", now=BASE)  # 少于 1 分钟

    def test_format(self):
        self.assertRegex(format_draw_time(BASE), r"\d{2}-\d{2} \d{2}:\d{2}")


class PrizeTest(unittest.TestCase):
    def test_split(self):
        shares = split_prize(500000, 3)
        self.assertEqual(sum(shares), 500000)
        self.assertEqual(max(shares) - min(shares), 1)
        self.assertEqual(split_prize(10, 3), [4, 3, 3])

    def test_split_too_small(self):
        with self.assertRaises(ValueError):
            split_prize(2, 3)

    def test_pick_winners(self):
        entries = [{"platform_id": str(i)} for i in range(5)]
        picked = pick_winners(entries, 3)
        self.assertEqual(len(picked), 3)
        self.assertTrue({item["platform_id"] for item in picked}.issubset({"0", "1", "2", "3", "4"}))
        self.assertEqual(len(pick_winners(entries, 10)), 5)


if __name__ == "__main__":
    unittest.main()
