import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import patch

from modules.KernelActivity import service as service_module


class KernelActivityServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(self.temp_dir.name, "kernel_activity.db")
        self.path_patch = patch.object(
            service_module, "DATABASE_PATH", self.database_path
        )
        self.path_patch.start()
        self.service = service_module.KernelActivityService()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_create_task_converts_integer_days_to_absolute_deadline(self):
        before = int(time.time())
        task = self.service.create_task(
            "10001", "阅读调度器", "阅读 kernel/sched/core.c", 7, "20001"
        )
        after = int(time.time())

        self.assertRegex(task["task_code"], r"^K-\d{8}-001$")
        self.assertEqual(task["status"], "active")
        self.assertGreaterEqual(task["deadline_at"], before + 7 * 86400)
        self.assertLessEqual(task["deadline_at"], after + 7 * 86400)

    def test_zero_day_task_has_no_deadline(self):
        task = self.service.create_task(
            "10001", "长期学习", "持续整理学习笔记", 0, "20001"
        )

        self.assertIsNone(task["deadline_at"])
        self.assertEqual(self.service.format_deadline(task["deadline_at"]), "长期任务")

    def test_deadline_display_decreases_with_current_time(self):
        deadline = 2_000_000_000

        first = self.service.format_deadline(deadline, now=deadline - 7 * 86400)
        next_day = self.service.format_deadline(deadline, now=deadline - 6 * 86400)
        final_hours = self.service.format_deadline(deadline, now=deadline - 18 * 3600)
        overdue = self.service.format_deadline(deadline, now=deadline + 2 * 86400)

        self.assertEqual(first, "还剩7天")
        self.assertEqual(next_day, "还剩6天")
        self.assertEqual(final_hours, "还剩18小时")
        self.assertEqual(overdue, "已逾期2天")

    def test_task_codes_increment_within_same_day(self):
        first = self.service.create_task("10001", "任务一", "内容一", 1, "20001")
        second = self.service.create_task("10001", "任务二", "内容二", 1, "20001")

        self.assertTrue(first["task_code"].endswith("-001"))
        self.assertTrue(second["task_code"].endswith("-002"))

    def test_update_task_changes_only_supplied_fields_and_records_revision(self):
        task = self.service.create_task("10001", "旧标题", "原内容", 7, "20001")
        old_deadline = task["deadline_at"]

        updated, changes = self.service.update_task(
            task["task_code"], "20002", title="新标题", deadline_days=10
        )

        self.assertEqual(updated["title"], "新标题")
        self.assertEqual(updated["content"], "原内容")
        self.assertEqual(updated["version"], 2)
        self.assertGreater(updated["deadline_at"], old_deadline)
        self.assertEqual(len(changes), 2)
        revisions = self.service.revisions(task["id"])
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["editor_id"], "20002")
        self.assertIn("标题", revisions[0]["changes"])
        self.assertIn("截止时间", revisions[0]["changes"])

    def test_update_deadline_to_zero_makes_task_long_running(self):
        task = self.service.create_task("10001", "任务", "内容", 7, "20001")

        updated, _ = self.service.update_task(
            task["task_code"], "20001", deadline_days=0
        )

        self.assertIsNone(updated["deadline_at"])

    def test_completed_task_is_trusted_without_review(self):
        task = self.service.create_task("10001", "任务", "内容", 7, "20001")

        _, result = self.service.set_participation(
            task["task_code"], "30001", "complete", "已经完成"
        )
        members = self.service.task_members(task["id"])

        self.assertEqual(result, "completed")
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["status"], "completed")

    def test_repeated_completion_does_not_duplicate_member(self):
        task = self.service.create_task("10001", "任务", "内容", 7, "20001")
        self.service.set_participation(task["task_code"], "30001", "complete")

        _, result = self.service.set_participation(
            task["task_code"], "30001", "complete"
        )

        self.assertEqual(result, "already_completed")
        self.assertEqual(len(self.service.task_members(task["id"])), 1)

    def test_cancel_completion_returns_member_to_accepted(self):
        task = self.service.create_task("10001", "任务", "内容", 7, "20001")
        self.service.set_participation(task["task_code"], "30001", "complete")

        _, result = self.service.set_participation(
            task["task_code"], "30001", "cancel"
        )

        self.assertEqual(result, "accepted")
        self.assertEqual(self.service.task_members(task["id"])[0]["status"], "accepted")

    def test_views_and_completion_are_counted(self):
        task = self.service.create_task("10001", "任务", "内容", 7, "20001")
        self.service.record_view(task, "30001")
        self.service.record_view(task, "30001")
        self.service.record_view(task, "30002")
        self.service.set_participation(task["task_code"], "30001", "complete")
        self.service.set_participation(task["task_code"], "30002", "accept")

        stats = self.service.task_stats(task["id"])

        self.assertEqual(stats["views"], 2)
        self.assertEqual(stats["accepted"], 2)
        self.assertEqual(stats["in_progress"], 1)
        self.assertEqual(stats["completed"], 1)

    def test_accepted_count_does_not_decrease_after_completion(self):
        task = self.service.create_task("10001", "任务", "内容", 7, "20001")
        self.service.set_participation(task["task_code"], "30001", "accept")
        before = self.service.task_stats(task["id"])

        self.service.set_participation(task["task_code"], "30001", "complete")
        after = self.service.task_stats(task["id"])

        self.assertEqual(before["accepted"], 1)
        self.assertEqual(before["in_progress"], 1)
        self.assertEqual(before["completed"], 0)
        self.assertEqual(after["accepted"], 1)
        self.assertEqual(after["in_progress"], 0)
        self.assertEqual(after["completed"], 1)

    def test_subscription_is_scoped_by_group(self):
        self.service.set_subscription("10001", "30001", True)
        self.service.set_subscription("10002", "30001", True)
        self.service.set_subscription("10001", "30002", True)
        self.service.set_subscription("10001", "30001", False)

        self.assertEqual(self.service.subscribers("10001"), ["30002"])
        self.assertEqual(self.service.subscriptions("30001"), ["10002"])

    def test_subscribing_counts_as_viewing_current_tasks(self):
        current = self.service.create_task("10001", "当前任务", "内容", 7, "20001")
        other_group = self.service.create_task("10002", "其他群任务", "内容", 7, "20001")

        self.service.set_subscription("10001", "30001", True)

        self.assertEqual(self.service.task_stats(current["id"])["views"], 1)
        self.assertEqual(self.service.task_stats(other_group["id"])["views"], 0)

    def test_new_task_counts_existing_subscribers_as_viewers(self):
        self.service.set_subscription("10001", "30001", True)
        self.service.set_subscription("10001", "30002", True)

        task = self.service.create_task("10001", "新任务", "内容", 7, "20001")

        self.assertEqual(self.service.task_stats(task["id"])["views"], 2)

    def test_unsubscribing_does_not_count_as_a_view(self):
        task = self.service.create_task("10001", "任务", "内容", 7, "20001")

        self.service.set_subscription("10001", "30001", False)

        self.assertEqual(self.service.task_stats(task["id"])["views"], 0)

    def test_initialize_backfills_existing_subscribers_as_viewers(self):
        task = self.service.create_task("10001", "已有任务", "内容", 7, "20001")
        with closing(sqlite3.connect(self.database_path)) as conn:
            with conn:
                conn.execute(
                    """INSERT INTO subscriptions(
                           group_id,user_id,enabled,created_at,updated_at
                       ) VALUES(?,?,?,?,?)""",
                    ("10001", "30001", 1, 1, 1),
                )

        service_module.KernelActivityService()

        self.assertEqual(self.service.task_stats(task["id"])["views"], 1)

    def test_first_subscription_becomes_default_group(self):
        self.service.set_subscription("10001", "30001", True)
        self.service.set_subscription("10002", "30001", True)

        self.assertEqual(self.service.get_default_group("30001"), "10001")

    def test_default_group_can_be_changed_manually(self):
        self.service.set_subscription("10001", "30001", True)
        self.service.set_subscription("10002", "30001", True)

        self.service.set_default_group("30001", "10002")

        self.assertEqual(self.service.get_default_group("30001"), "10002")

    def test_unsubscribing_default_selects_oldest_remaining_subscription(self):
        self.service.set_subscription("10001", "30001", True)
        self.service.set_subscription("10002", "30001", True)
        self.service.set_subscription("10003", "30001", True)

        self.service.set_subscription("10001", "30001", False)

        self.assertEqual(self.service.get_default_group("30001"), "10002")

    def test_unsubscribing_only_group_clears_default(self):
        self.service.set_subscription("10001", "30001", True)

        self.service.set_subscription("10001", "30001", False)

        self.assertIsNone(self.service.get_default_group("30001"))

    def test_effective_activity_uses_newer_source(self):
        self.service.mark_active("10001", "30001", "task_detail_view", now=200)

        self.assertEqual(
            self.service.get_effective_activity("10001", "30001", 100), 200
        )
        self.assertEqual(
            self.service.get_effective_activity("10001", "30001", 300), 300
        )

    def test_closed_task_moves_from_current_to_history(self):
        task = self.service.create_task("10001", "任务", "内容", 7, "20001")
        current, _ = self.service.list_tasks("10001", history=False)
        self.assertEqual([item["task_code"] for item in current], [task["task_code"]])

        self.service.set_task_status(task["task_code"], "closed", "20001")
        current, _ = self.service.list_tasks("10001", history=False)
        history, _ = self.service.list_tasks("10001", history=True)

        self.assertEqual(current, [])
        self.assertEqual([item["task_code"] for item in history], [task["task_code"]])

    def test_history_includes_active_tasks_as_publish_history(self):
        task = self.service.create_task("10001", "刚发布的任务", "内容", 7, "20001")

        history, total = self.service.list_tasks("10001", history=True)

        self.assertEqual(total, 1)
        self.assertEqual([item["task_code"] for item in history], [task["task_code"]])

    def test_expired_active_task_is_history_but_can_still_be_completed(self):
        task = self.service.create_task("10001", "任务", "内容", 1, "20001")
        with closing(sqlite3.connect(self.database_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE tasks SET deadline_at=? WHERE task_code=?",
                    (int(time.time()) - 60, task["task_code"]),
                )

        current, _ = self.service.list_tasks("10001", history=False)
        history, _ = self.service.list_tasks("10001", history=True)
        _, result = self.service.set_participation(
            task["task_code"], "30001", "complete"
        )

        self.assertEqual(current, [])
        self.assertEqual(len(history), 1)
        self.assertEqual(result, "completed")


if __name__ == "__main__":
    unittest.main()
