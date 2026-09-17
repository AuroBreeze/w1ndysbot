import json
import unittest
from unittest.mock import patch

from modules.KernelActivity.handlers.message import (
    _activity_detail_text,
    _group_activity_report,
    _parse_deadline,
    _parse_fields,
    _send_published_task,
)


class MessageParsingTestCase(unittest.TestCase):
    def test_parse_multiline_publish_fields(self):
        raw = (
            "发布任务\n"
            "标题：阅读 Linux 调度器\n"
            "截止：7\n"
            "内容：阅读 core.c\n"
            "并整理主要调用链"
        )

        fields = _parse_fields(raw, "发布任务")

        self.assertEqual(fields["标题"], "阅读 Linux 调度器")
        self.assertEqual(fields["截止"], "7")
        self.assertEqual(fields["内容"], "阅读 core.c\n并整理主要调用链")

    def test_parse_modify_fields_keeps_only_provided_values(self):
        raw = "修改任务 K-20260916-001\n截止：10"

        fields = _parse_fields(raw, "修改任务 K-20260916-001")

        self.assertEqual(fields, {"截止": "10"})

    def test_parse_long_detail_alias_preserves_paragraphs_lists_and_links(self):
        raw = (
            "发布任务\n"
            "标题：实现早期串口输出\n"
            "截止：14\n"
            "详情：目标：在内核启动早期输出日志。\n"
            "\n"
            "要求：\n"
            "1. 阅读 arch/loongarch/boot/boot.c\n"
            "2. 实现 putchar 与 puts\n"
            "3. 提交代码和设计说明\n"
            "\n"
            "参考：https://example.com/kernel-uart"
        )

        fields = _parse_fields(raw, "发布任务")

        self.assertEqual(fields["标题"], "实现早期串口输出")
        self.assertEqual(fields["截止"], "14")
        self.assertIn("目标：在内核启动早期输出日志。", fields["内容"])
        self.assertIn("1. 阅读 arch/loongarch/boot/boot.c", fields["内容"])
        self.assertIn("3. 提交代码和设计说明", fields["内容"])
        self.assertIn("https://example.com/kernel-uart", fields["内容"])

    def test_content_and_detail_aliases_are_combined(self):
        raw = "发布任务\n标题：任务\n内容：第一段\n详情：第二段"

        fields = _parse_fields(raw, "发布任务")

        self.assertEqual(fields["内容"], "第一段\n第二段")

    def test_parse_deadline_accepts_non_negative_integer(self):
        self.assertEqual(_parse_deadline("0"), 0)
        self.assertEqual(_parse_deadline("7"), 7)

    def test_parse_deadline_rejects_date_and_negative_value(self):
        for value in ("2026-09-23", "-1", "七"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _parse_deadline(value)

    def test_parse_deadline_rejects_unreasonable_duration(self):
        with self.assertRaises(ValueError):
            _parse_deadline("3651")

    def test_activity_detail_uses_newer_task_activity(self):
        member = {
            "user_id": "10001",
            "card": "测试成员",
            "last_sent_time": 1_000,
        }
        with patch(
            "modules.KernelActivity.handlers.message.service.get_activity",
            return_value={"last_active_at": 2_000, "source": "task_history_view"},
        ):
            text = _activity_detail_text("20001", member, now=3_000)

        self.assertIn("测试成员（10001）", text)
        self.assertIn("任务系统（task_history_view）", text)
        self.assertIn("1970-01-01 08:33", text)

    def test_group_activity_report_combines_sources_and_skips_admins(self):
        members = [
            {"user_id": "1", "nickname": "群主", "role": "owner", "last_sent_time": 0},
            {"user_id": "2", "nickname": "活跃", "role": "member", "last_sent_time": 900_000},
            {"user_id": "3", "nickname": "过期", "role": "member", "last_sent_time": 100},
            {"user_id": "4", "nickname": "未知", "role": "member", "last_sent_time": 0},
            {"user_id": "5", "nickname": "机器人", "role": "member", "last_sent_time": 0},
        ]
        with patch(
            "modules.KernelActivity.handlers.message.service.get_effective_activity",
            side_effect=[900_000, 100, 0],
        ):
            text = _group_activity_report(
                "20001", members, 5, now=1_000_000, bot_user_id="5"
            )

        self.assertIn("普通成员：3人", text)
        self.assertIn("近期活跃：1人", text)
        self.assertIn("超过阈值：1人", text)
        self.assertIn("无有效记录：1人", text)
        self.assertIn("过期（3）", text)
        self.assertIn("未知（4）", text)
        self.assertNotIn("群主（1）", text)
        self.assertNotIn("机器人（5）", text)
        self.assertIn("不会自动踢人", text)


class FakeWebSocket:
    def __init__(self):
        self.payloads = []

    async def send(self, payload):
        self.payloads.append(json.loads(payload))


class PublishMessageTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_publish_message_mentions_everyone_and_has_no_recall_note(self):
        websocket = FakeWebSocket()
        task = {
            "id": 1,
            "task_code": "K-20260916-001",
            "title": "阅读调度器",
            "content": "阅读 core.c",
            "status": "active",
            "deadline_at": None,
            "version": 1,
        }
        stats = {"views": 0, "accepted": 0, "in_progress": 0, "completed": 0}

        with patch(
            "modules.KernelActivity.handlers.message.service.task_stats",
            return_value=stats,
        ):
            await _send_published_task(websocket, "10001", task)

        self.assertEqual(len(websocket.payloads), 1)
        payload = websocket.payloads[0]
        self.assertEqual(payload["action"], "send_group_msg")
        self.assertEqual(payload["params"]["group_id"], "10001")
        self.assertEqual(
            payload["params"]["message"][0],
            {"type": "at", "data": {"qq": "all"}},
        )
        self.assertNotIn("del_msg=", payload["echo"])
        self.assertIn("任务发布成功", payload["params"]["message"][1]["data"]["text"])


if __name__ == "__main__":
    unittest.main()
