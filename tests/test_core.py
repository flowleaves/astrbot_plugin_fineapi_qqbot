import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from fineapi_core.quota import (
    format_quota,
    quota_to_usd,
    quota_to_yadian,
    usd_to_quota,
)
from fineapi_core.store import Store


class QuotaTest(unittest.TestCase):
    def test_default_conversion(self):
        self.assertEqual(quota_to_usd(500000), 1.0)
        self.assertEqual(usd_to_quota(1.0), 500000)
        self.assertEqual(quota_to_yadian(500000), 100.0)
        self.assertEqual(usd_to_quota(1.0), 500000)

    def test_half_conversion(self):
        self.assertAlmostEqual(quota_to_usd(250000), 0.5)
        self.assertAlmostEqual(quota_to_yadian(250000), 50.0)

    def test_zero_and_none(self):
        self.assertEqual(quota_to_usd(None), 0.0)
        self.assertEqual(quota_to_yadian(0), 0.0)

    def test_format(self):
        text = format_quota(500000, 500000, show_yadian=True, show_cny=False)
        self.assertIn("$1.0000", text)
        self.assertIn("100.00 芽点", text)
        plain = format_quota(500000, 500000, show_yadian=False, show_cny=False)
        self.assertEqual(plain, "$1.0000")


class StoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "test.db")
        await self.store.connect()

    async def asyncTearDown(self):
        await self.store.close()
        self._tmp.cleanup()

    async def test_create_and_query(self):
        ok, reason = await self.store.create_binding("10001", 123, "alice")
        self.assertTrue(ok, reason)

        binding = await self.store.get_binding("10001")
        self.assertIsNotNone(binding)
        self.assertEqual(binding["newapi_user_id"], 123)
        self.assertEqual(binding["username"], "alice")
        self.assertEqual(binding["gift_state"], "none")

        by_uid = await self.store.get_binding_by_user_id(123)
        self.assertEqual(by_uid["qq"], "10001")

    async def test_duplicate_qq_and_user(self):
        await self.store.create_binding("10001", 123, "alice")

        ok, reason = await self.store.create_binding("10001", 456, "bob")
        self.assertFalse(ok)
        self.assertEqual(reason, "qq_bound")

        ok, reason = await self.store.create_binding("10002", 123, "alice")
        self.assertFalse(ok)
        self.assertEqual(reason, "user_bound")

    async def test_gift_state(self):
        await self.store.create_binding("10001", 123, "alice")
        await self.store.set_gift_state("10001", "pending", quota=500000, reason="first_bind")

        binding = await self.store.get_binding("10001")
        self.assertEqual(binding["gift_state"], "pending")
        self.assertEqual(binding["gift_quota"], 500000)

        await self.store.set_gift_state("10001", "granted", quota=500000, reason="first_bind")
        binding = await self.store.get_binding("10001")
        self.assertEqual(binding["gift_state"], "granted")
        self.assertIsNotNone(binding["gift_at"])

        await self.store.set_gift_state("10001", "skipped", quota=0, reason="old_user")
        binding = await self.store.get_binding("10001")
        self.assertEqual(binding["gift_state"], "skipped")
        self.assertEqual(binding["gift_quota"], 0)

    async def test_stale_pending_becomes_unknown(self):
        await self.store.create_binding("10001", 123, "alice")
        await self.store.set_gift_state("10001", "pending", quota=500000)

        changed = await self.store.mark_stale_pending_unknown(older_than_seconds=0)
        self.assertEqual(changed, 1)

        binding = await self.store.get_binding("10001")
        self.assertEqual(binding["gift_state"], "unknown")

    async def test_delete(self):
        await self.store.create_binding("10001", 123, "alice")
        self.assertTrue(await self.store.delete_binding("10001"))
        self.assertIsNone(await self.store.get_binding("10001"))
        self.assertFalse(await self.store.delete_binding("10001"))

    async def test_gift_ledger_survives_unbind(self):
        await self.store.create_binding("10001", 123, "alice")
        reserved, state = await self.store.reserve_gift("10001", 123, 500000, "first_bind")
        self.assertTrue(reserved, state)
        self.assertTrue(await self.store.complete_gift("10001", 123, 500000, "first_bind"))

        self.assertTrue(await self.store.delete_binding("10001"))
        grant = await self.store.get_gift_grant("10001", 123)
        self.assertIsNotNone(grant)
        self.assertEqual(grant["state"], "granted")

        # 重新绑定同一账号：台账仍然存在
        await self.store.create_binding("10001", 123, "alice")
        grant = await self.store.get_gift_grant("10001", 123)
        self.assertEqual(grant["state"], "granted")

    async def test_gift_ledger_blocks_duplicate(self):
        self.assertTrue((await self.store.reserve_gift("10001", 123, 500000, "x"))[0])
        self.assertFalse((await self.store.reserve_gift("10001", 456, 500000, "x"))[0])
        self.assertFalse((await self.store.reserve_gift("10002", 123, 500000, "x"))[0])

    async def test_gift_ledger_release_allows_retry(self):
        self.assertTrue((await self.store.reserve_gift("10001", 123, 500000, "x"))[0])
        await self.store.release_gift("10001")
        self.assertIsNone(await self.store.get_gift_grant("10001", 123))
        self.assertTrue((await self.store.reserve_gift("10001", 123, 500000, "x"))[0])

    async def test_gift_unknown_stale(self):
        self.assertTrue((await self.store.reserve_gift("10001", 123, 500000, "x"))[0])
        await self.store.mark_gift_unknown("10001", 123, 500000, "timeout")
        grant = await self.store.get_gift_grant("10001", 123)
        self.assertEqual(grant["state"], "unknown")

    async def test_lottery_flow(self):
        now = 1_700_000_000
        lottery_id, error = await self.store.create_lottery(
            "umo:test", "测试抽奖", 10, 1, now + 60, "admin", group_id="20002"
        )
        self.assertTrue(lottery_id, error)
        lottery = await self.store.get_lottery(lottery_id)
        self.assertEqual(lottery["group_id"], "20002")

        duplicate, error = await self.store.create_lottery(
            "umo:test", "重复", 10, 1, now + 60, "admin"
        )
        self.assertEqual(duplicate, 0)
        self.assertEqual(error, "open_exists")

        ok, reason = await self.store.join_lottery(
            lottery_id, "u1", 123, "alice", now=now
        )
        self.assertTrue(ok, reason)
        self.assertEqual(await self.store.count_lottery_entries(lottery_id), 1)

        ok, reason = await self.store.join_lottery(
            lottery_id, "u1", 123, "alice", now=now
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "joined")

        # 开奖时的资格查询：仍然绑定同一星芽账号才算符合条件
        await self.store.create_binding("u1", 123, "alice")
        eligible = await self.store.list_eligible_lottery_entries(lottery_id)
        self.assertEqual([row["platform_id"] for row in eligible], ["u1"])
        await self.store.delete_binding("u1")
        self.assertEqual(
            await self.store.list_eligible_lottery_entries(lottery_id), []
        )

        ok, reason = await self.store.join_lottery(
            lottery_id, "u2", 456, "bob", now=now + 120
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "closed")

        due = await self.store.list_due_lotteries(now + 61)
        self.assertEqual([row["id"] for row in due], [lottery_id])

        finalized = await self.store.finalize_lottery(
            lottery_id,
            [
                {
                    "platform_id": "u1",
                    "newapi_user_id": 123,
                    "username": "alice",
                    "quota": 50000,
                }
            ],
        )
        self.assertTrue(finalized)
        self.assertFalse(
            await self.store.finalize_lottery(
                lottery_id,
                [
                    {
                        "platform_id": "u1",
                        "newapi_user_id": 123,
                        "username": "alice",
                        "quota": 50000,
                    }
                ],
            )
        )

        winners = await self.store.list_lottery_winners(lottery_id)
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["state"], "pending")

        await self.store.update_lottery_winner(lottery_id, "u1", "granted")
        winners = await self.store.list_lottery_winners(lottery_id)
        self.assertEqual(winners[0]["state"], "granted")
        self.assertIsNotNone(winners[0]["granted_at"])

        lottery = await self.store.get_lottery(lottery_id)
        self.assertEqual(lottery["status"], "drawn")

    async def test_lottery_cancel(self):
        lottery_id, _ = await self.store.create_lottery(
            "umo:cancel", "取消测试", 10, 1, 1_700_000_060, "admin"
        )
        self.assertTrue(await self.store.cancel_lottery(lottery_id))
        self.assertFalse(await self.store.cancel_lottery(lottery_id))
        lottery = await self.store.get_lottery(lottery_id)
        self.assertEqual(lottery["status"], "cancelled")

    async def test_stats(self):
        await self.store.create_binding("10001", 123, "alice")
        await self.store.create_binding("10002", 456, "bob")
        await self.store.set_gift_state("10001", "granted", quota=500000)
        stats = await self.store.gift_stats()
        self.assertEqual(stats.get("granted"), 1)
        self.assertEqual(stats.get("none"), 1)


if __name__ == "__main__":
    unittest.main()
