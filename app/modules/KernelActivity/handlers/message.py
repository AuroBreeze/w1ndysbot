import re
import time

from api.group import get_group_member_list
from api.message import send_group_msg, send_private_msg
from core.get_group_list import get_group_name_by_id
from core.get_group_member_list import get_group_member_user_ids
from core.menu_manager import MENU_COMMAND, MenuManager
from core.switchs import (
    handle_module_group_switch,
    handle_module_private_switch,
    is_group_switch_on,
    is_private_switch_on,
)
from logger import logger
from utils.auth import is_group_admin, is_system_admin
from utils.generate import (
    generate_at_message,
    generate_reply_message,
    generate_text_message,
)

from .. import MODULE_NAME, SWITCH_NAME
from ..service import service


def _text(value):
    return generate_text_message(value)


async def _group_reply(websocket, group_id, message_id, text, expire=60):
    await send_group_msg(
        websocket,
        group_id,
        [generate_reply_message(message_id), _text(text)],
        note=f"del_msg={expire}" if expire else "",
    )


async def _private_reply(websocket, user_id, message_id, text):
    await send_private_msg(
        websocket, user_id, [generate_reply_message(message_id), _text(text)]
    )


async def _send_published_task(websocket, group_id, task):
    """发送永久保留的任务公告，并提醒全体成员。"""
    await send_group_msg(
        websocket,
        group_id,
        [
            generate_at_message("all"),
            _text("\n✅ 任务发布成功\n\n" + _task_text(task)),
        ],
    )


def _parse_fields(raw_message, command_line):
    body = raw_message[len(command_line) :].strip()
    fields = {}
    current_field = None
    for line in body.splitlines():
        match = re.match(r"^\s*(标题|截止|内容|详情|任务详情)\s*[：:]\s*(.*)$", line, re.S)
        if match:
            current_field = "内容" if match.group(1) in {"详情", "任务详情"} else match.group(1)
            value = match.group(2).rstrip()
            if current_field == "内容" and fields.get("内容"):
                fields["内容"] += "\n" + value
            else:
                fields[current_field] = value
        elif current_field == "内容":
            fields["内容"] = fields.get("内容", "") + "\n" + line
    if "内容" in fields:
        fields["内容"] = fields["内容"].strip()
    return fields


def _parse_deadline(value):
    if value is None:
        return None
    if not re.fullmatch(r"\d+", value.strip()):
        raise ValueError("截止时间必须是非负整数天数，例如：截止：7；0 表示长期任务")
    days = int(value)
    if days > 3650:
        raise ValueError("截止天数不能超过3650天")
    return days


def _task_status(task):
    if task["status"] == "closed":
        return "已关闭"
    if task["deadline_at"] and service.format_deadline(task["deadline_at"]).startswith("已逾期"):
        return "已截止（允许补交）"
    return "进行中"


def _task_text(task, include_content=True):
    stats = service.task_stats(task["id"])
    lines = [
        f"编号：{task['task_code']}",
        f"标题：{task['title']}",
        f"状态：{_task_status(task)}",
        f"截止：{service.format_deadline(task['deadline_at'])}",
        f"实际截止：{service.format_time(task['deadline_at'])}",
        f"版本：{task['version']}",
        f"查看：{stats['views']}人｜接受：{stats['accepted']}人｜完成：{stats['completed']}人",
    ]
    if include_content:
        lines.extend(["", "内容：", task["content"]])
    return "\n".join(lines)


def _can_access_group(group_id, user_id):
    return str(user_id) in get_group_member_user_ids(str(group_id))


def _format_activity_age(timestamp, now=None):
    if not timestamp:
        return "无记录"
    now = int(now or time.time())
    seconds = max(0, now - int(timestamp))
    days, remainder = divmod(seconds, 86400)
    hours = remainder // 3600
    if days:
        return f"{days}天前"
    if hours:
        return f"{hours}小时前"
    return "1小时内"


def _find_member(members, user_id):
    user_id = str(user_id)
    for member in members:
        if str(member.get("user_id", "")) == user_id:
            return member
    return None


def _activity_detail_text(group_id, member, now=None):
    now = int(now or time.time())
    user_id = str(member.get("user_id", ""))
    last_sent = int(member.get("last_sent_time", 0) or 0)
    task_record = service.get_activity(group_id, user_id)
    task_active = int(task_record["last_active_at"]) if task_record else 0
    effective = max(last_sent, task_active)
    if effective == task_active and task_active >= last_sent and task_record:
        source = f"任务系统（{task_record['source']}）"
    elif effective == last_sent and last_sent:
        source = "群内发言"
    else:
        source = "无"
    display_name = member.get("card") or member.get("nickname") or user_id
    return (
        f"👤 {display_name}（{user_id}）\n"
        f"群内最后发言：{service.format_time(last_sent)}（{_format_activity_age(last_sent, now)}）\n"
        f"任务系统活跃：{service.format_time(task_active)}（{_format_activity_age(task_active, now)}）\n"
        f"综合活跃时间：{service.format_time(effective)}（{_format_activity_age(effective, now)}）\n"
        f"最近活跃来源：{source}"
    )


def _group_activity_report(group_id, members, days, now=None, bot_user_id=None):
    now = int(now or time.time())
    threshold = now - int(days) * 86400
    active, inactive, unknown = 0, [], []
    considered = 0
    for member in members:
        if (
            member.get("role") in {"owner", "admin"}
            or member.get("is_robot")
            or (bot_user_id and str(member.get("user_id", "")) == str(bot_user_id))
        ):
            continue
        considered += 1
        user_id = str(member.get("user_id", ""))
        last_sent = int(member.get("last_sent_time", 0) or 0)
        effective = service.get_effective_activity(group_id, user_id, last_sent)
        name = member.get("card") or member.get("nickname") or user_id
        if not effective:
            unknown.append(f"{name}（{user_id}）：无活跃记录")
        elif effective < threshold:
            inactive.append(f"{name}（{user_id}）：{_format_activity_age(effective, now)}")
        else:
            active += 1
    lines = [
        f"📊 本群综合活跃度检查（{days}天）",
        "",
        f"普通成员：{considered}人",
        f"近期活跃：{active}人",
        f"超过阈值：{len(inactive)}人",
        f"无有效记录：{len(unknown)}人",
    ]
    candidates = inactive + unknown
    if candidates:
        lines.extend(["", "⚠️ 不活跃候选：", *candidates[:50]])
        if len(candidates) > 50:
            lines.append(f"另有{len(candidates) - 50}人未显示，请缩短群范围或分批处理。")
    else:
        lines.extend(["", "没有发现不活跃候选成员。"])
    lines.extend(
        [
            "",
            "判断依据：群内最后发言与任务系统最后活跃取较新值。",
            "本命令只生成检查报告，不会自动踢人。",
        ]
    )
    return "\n".join(lines)


async def _notify_subscribers(websocket, task, heading, changes=None):
    group_name = get_group_name_by_id(task["group_id"]) or f"群{task['group_id']}"
    extra = f"\n\n变更：\n" + "\n".join(changes) if changes else ""
    content = (
        f"{heading}\n\n群：{group_name}\n{_task_text(task)}{extra}\n\n"
        f"发送“任务详情 {task['task_code']}”可再次查看。"
    )
    for subscriber in service.subscribers(task["group_id"]):
        try:
            await send_private_msg(websocket, subscriber, [_text(content)])
        except Exception as exc:
            logger.error(f"[{MODULE_NAME}]向{subscriber}推送任务失败: {exc}")


async def _show_task_list(websocket, target, message_id, group_id, user_id, history, page=1, private=False):
    tasks, total = service.list_tasks(group_id, history=history, page=page)
    service.mark_active(group_id, user_id, "task_history_view" if history else "task_list_view")
    title = "📚 历史任务" if history else "📋 当前任务"
    if not tasks:
        text = f"{title}\n\n暂无任务。\n本次查看已刷新活跃度。"
    else:
        blocks = []
        for index, task in enumerate(tasks, 1):
            stats = service.task_stats(task["id"])
            blocks.append(
                f"{index}. {task['task_code']}｜{task['title']}\n"
                f"   {_task_status(task)}｜{service.format_deadline(task['deadline_at'])}｜完成{stats['completed']}人"
            )
        pages = max(1, (total + 9) // 10)
        text = f"{title}\n\n" + "\n\n".join(blocks) + f"\n\n第{page}/{pages}页\n本次查看已刷新活跃度。"
    if private:
        await _private_reply(websocket, target, message_id, text)
    else:
        await _group_reply(websocket, target, message_id, text)


async def _handle_common(websocket, raw, user_id, message_id, group_id, reply, is_admin):
    match = re.fullmatch(r"查询活跃度(?:\s+(?:\[CQ:at,qq=(\d+)(?:,[^]]*)?\]|(\d+)))?", raw)
    if match:
        target_user_id = match.group(1) or match.group(2) or user_id
        if target_user_id != user_id and not is_admin:
            await reply("只有群管理员可以查询其他成员的活跃度。")
            return True
        service.mark_active(group_id, user_id, "activity_check")
        await get_group_member_list(
            websocket,
            group_id,
            True,
            note=(
                f"KernelActivity-activity-detail-requester={user_id}"
                f"-target={target_user_id}-message={message_id}"
            ),
        )
        return True
    match = re.fullmatch(r"(?:检查活跃度|活跃度检查)(?:\s+(\d+))?", raw)
    if match:
        if not is_admin:
            await reply("只有群管理员可以执行全群活跃度检查。")
            return True
        days = int(match.group(1) or 30)
        if not 1 <= days <= 3650:
            await reply("检查天数必须是 1 到 3650 之间的整数。")
            return True
        service.mark_active(group_id, user_id, "activity_check")
        await get_group_member_list(
            websocket,
            group_id,
            True,
            note=(
                f"KernelActivity-activity-report-days={days}"
                f"-requester={user_id}-message={message_id}"
            ),
        )
        return True
    if raw == "当前任务":
        await _show_task_list(websocket, group_id, message_id, group_id, user_id, False)
        return True
    match = re.fullmatch(r"历史任务(?:\s+(\d+))?", raw)
    if match:
        await _show_task_list(websocket, group_id, message_id, group_id, user_id, True, int(match.group(1) or 1))
        return True
    match = re.match(r"任务详情\s+(K-\d{8}-\d{3})$", raw, re.I)
    if match:
        task = service.get_task(match.group(1).upper())
        if not task or task["group_id"] != group_id:
            await reply("未找到本群对应的任务。")
        else:
            service.record_view(task, user_id)
            await reply("📌 任务详情\n\n" + _task_text(task) + "\n\n本次查看已刷新活跃度。")
        return True
    match = re.match(r"任务进度\s+(K-\d{8}-\d{3})$", raw, re.I)
    if match:
        task = service.get_task(match.group(1).upper())
        if not task or task["group_id"] != group_id:
            await reply("未找到本群对应的任务。")
        else:
            stats = service.task_stats(task["id"])
            service.mark_active(group_id, user_id, "task_progress_view")
            rate = stats["completed"] / stats["accepted"] * 100 if stats["accepted"] else 0
            await reply(
                f"📊 任务进度\n\n{task['task_code']}｜{task['title']}\n"
                f"查看：{stats['views']}人\n接受总数：{stats['accepted']}人\n"
                f"进行中：{stats['in_progress']}人\n"
                f"已完成：{stats['completed']}人\n完成率：{rate:.1f}%\n\n本次查看已刷新活跃度。"
            )
        return True
    match = re.match(r"任务成员\s+(K-\d{8}-\d{3})$", raw, re.I)
    if match:
        task = service.get_task(match.group(1).upper())
        if not task or task["group_id"] != group_id:
            await reply("未找到本群对应的任务。")
        else:
            members = service.task_members(task["id"])
            completed = [row["user_id"] for row in members if row["status"] == "completed"]
            accepted = [row["user_id"] for row in members if row["status"] == "accepted"]
            service.mark_active(group_id, user_id, "task_members_view")
            await reply(
                f"👥 任务成员｜{task['task_code']}\n\n"
                f"已完成（{len(completed)}）：\n{', '.join(completed) or '无'}\n\n"
                f"进行中（{len(accepted)}）：\n{', '.join(accepted) or '无'}\n\n"
                "本次查看已刷新活跃度。"
            )
        return True
    for command, action in (("接受任务", "accept"), ("完成任务", "complete"), ("取消完成", "cancel")):
        match = re.match(rf"{command}\s+(K-\d{{8}}-\d{{3}})(?:\s+([\s\S]+))?$", raw, re.I)
        if match:
            task = service.get_task(match.group(1).upper())
            if not task or task["group_id"] != group_id:
                await reply("未找到本群对应的任务。")
                return True
            task, result = service.set_participation(task["task_code"], user_id, action, match.group(2) or "")
            messages = {
                "accepted": "✅ 已接受任务，活跃度已刷新。",
                "completed": "✅ 已按信任机制记录为完成，无需审核，活跃度已刷新。",
                "already_completed": "你已经完成过该任务，本次查看已刷新活跃度。",
                "not_completed": "你还没有该任务的完成记录。",
            }
            await reply(messages.get(result, result))
            return True
    if raw == "我的任务":
        rows = service.my_tasks(group_id, user_id)
        service.mark_active(group_id, user_id, "my_tasks_view")
        text = "📖 我的任务\n\n" + ("\n".join(f"{r['task_code']}｜{r['title']}｜{'已完成' if r['status']=='completed' else '进行中'}" for r in rows) or "暂无参与记录")
        await reply(text + "\n\n本次查看已刷新活跃度。")
        return True
    if raw in {"订阅任务", "取消订阅"}:
        enabled = raw == "订阅任务"
        service.set_subscription(group_id, user_id, enabled)
        await reply("✅ 已订阅本群任务通知。" if enabled else "✅ 已取消本群任务通知。")
        return True
    if raw == "刷新活跃":
        service.mark_active(group_id, user_id, "manual_refresh")
        await reply("✅ 已刷新你在本群的活跃度。")
        return True
    if raw == "我的活跃度":
        record = service.get_activity(group_id, user_id)
        service.mark_active(group_id, user_id, "activity_view")
        previous = service.format_time(record["last_active_at"]) if record else "无记录"
        await reply(f"刷新前的最近任务活跃：{previous}\n当前查看已刷新活跃度。")
        return True
    if raw.startswith("发布任务"):
        if not is_admin:
            return True
        fields = _parse_fields(raw, "发布任务")
        if not fields and raw.strip() != "发布任务":
            match = re.match(r"发布任务\s+(\d+)\s+(.+)$", raw, re.S)
            if match:
                fields = {"截止": match.group(1), "标题": match.group(2).strip(), "内容": match.group(2).strip()}
        title = fields.get("标题", "").strip()
        if not title:
            await reply(
                "格式错误。请使用：\n发布任务\n标题：任务标题\n截止：7\n"
                "详情：可包含多行任务说明、列表、代码路径和链接"
            )
            return True
        try:
            deadline = _parse_deadline(fields.get("截止", "0"))
        except ValueError as exc:
            await reply(str(exc)); return True
        task = service.create_task(group_id, title, fields.get("内容") or title, deadline, user_id)
        await _send_published_task(websocket, group_id, task)
        await _notify_subscribers(websocket, task, "📌 你订阅的群发布了新任务")
        return True
    match = re.match(r"修改任务\s+(K-\d{8}-\d{3})", raw, re.I)
    if match:
        if not is_admin:
            return True
        code = match.group(1).upper()
        task = service.get_task(code)
        if not task or task["group_id"] != group_id:
            await reply("未找到本群对应的任务。")
            return True
        fields = _parse_fields(raw, match.group(0))
        try:
            deadline = _parse_deadline(fields["截止"]) if "截止" in fields else None
        except ValueError as exc:
            await reply(str(exc)); return True
        updated, changes = service.update_task(code, user_id, fields.get("标题"), fields.get("内容"), deadline)
        if not changes:
            await reply("没有检测到需要修改的字段。")
        else:
            await reply("✅ 任务修改成功\n\n" + "\n".join(changes) + "\n\n" + _task_text(updated))
            await _notify_subscribers(websocket, updated, "📝 任务已更新", changes)
        return True
    for command, status in (("关闭任务", "closed"), ("重新开启任务", "active")):
        match = re.match(rf"{command}\s+(K-\d{{8}}-\d{{3}})$", raw, re.I)
        if match:
            if not is_admin:
                return True
            task = service.get_task(match.group(1).upper())
            if not task or task["group_id"] != group_id:
                await reply("未找到本群对应的任务。")
            else:
                task = service.set_task_status(task["task_code"], status, user_id)
                await reply(f"✅ 任务已{'关闭' if status=='closed' else '重新开启'}。\n\n" + _task_text(task))
            return True
    match = re.match(r"任务修改记录\s+(K-\d{8}-\d{3})$", raw, re.I)
    if match:
        task = service.get_task(match.group(1).upper())
        if not task or task["group_id"] != group_id:
            await reply("未找到本群对应的任务。")
        else:
            rows = service.revisions(task["id"])
            service.mark_active(group_id, user_id, "task_revision_view")
            text = "\n\n".join(f"版本{r['version']}｜{service.format_time(r['created_at'])}\n{r['changes']}" for r in rows) or "暂无修改记录"
            await reply(f"📝 {task['task_code']} 修改记录\n\n{text}")
        return True
    return False


async def handle_group_message(websocket, msg):
    raw = str(msg.get("raw_message", "")).strip()
    group_id, user_id = str(msg.get("group_id", "")), str(msg.get("user_id", ""))
    message_id = str(msg.get("message_id", ""))
    role = msg.get("sender", {}).get("role", "")
    if raw.lower() == SWITCH_NAME.lower():
        if is_group_admin(role) or is_system_admin(user_id):
            await handle_module_group_switch(MODULE_NAME, websocket, group_id, message_id)
        return
    if raw.lower() == f"{SWITCH_NAME}{MENU_COMMAND}".lower():
        await _group_reply(websocket, group_id, message_id, MenuManager.get_module_commands_text(MODULE_NAME))
        return
    if not is_group_switch_on(group_id, MODULE_NAME):
        return
    async def reply(text):
        await _group_reply(websocket, group_id, message_id, text)
    await _handle_common(websocket, raw, user_id, message_id, group_id, reply, is_group_admin(role) or is_system_admin(user_id))


async def handle_private_message(websocket, msg):
    raw = str(msg.get("raw_message", "")).strip()
    user_id, message_id = str(msg.get("user_id", "")), str(msg.get("message_id", ""))
    if raw.lower() == SWITCH_NAME.lower():
        if is_system_admin(user_id):
            await handle_module_private_switch(MODULE_NAME, websocket, user_id, message_id)
        return
    if raw.lower() == f"{SWITCH_NAME}{MENU_COMMAND}".lower():
        await _private_reply(websocket, user_id, message_id, MenuManager.get_module_commands_text(MODULE_NAME))
        return
    if not is_private_switch_on(MODULE_NAME):
        return
    if raw == "我的订阅":
        groups = service.subscriptions(user_id)
        default_group = service.get_default_group(user_id)
        display = [f"{group_id}{'（默认）' if group_id == default_group else ''}" for group_id in groups]
        await _private_reply(websocket, user_id, message_id, "我的任务订阅：\n" + ("\n".join(display) or "暂无订阅"))
        return
    if raw == "我的默认群":
        group_id = service.get_default_group(user_id)
        if group_id:
            group_name = get_group_name_by_id(group_id) or f"群{group_id}"
            await _private_reply(websocket, user_id, message_id, f"当前默认群：{group_name}（{group_id}）")
        else:
            await _private_reply(websocket, user_id, message_id, "尚未设置默认群。首次订阅群聊时会自动设置。")
        return
    match = re.fullmatch(r"设置默认群\s+(\d+)", raw)
    if match:
        group_id = match.group(1)
        if not is_group_switch_on(group_id, MODULE_NAME) or not _can_access_group(group_id, user_id):
            await _private_reply(websocket, user_id, message_id, "该群未开启任务功能，或你不在该群中。")
            return
        service.set_default_group(user_id, group_id)
        service.mark_active(group_id, user_id, "default_group_change")
        group_name = get_group_name_by_id(group_id) or f"群{group_id}"
        await _private_reply(websocket, user_id, message_id, f"✅ 已将默认群设置为：{group_name}（{group_id}）")
        return
    match = re.fullmatch(r"(订阅任务|取消订阅|刷新活跃|我的活跃度|当前任务|历史任务)(?:\s+(\d+))?", raw)
    if match:
        command, group_id = match.group(1), match.group(2)
        if not group_id:
            group_id = service.get_default_group(user_id)
            if not group_id:
                await _private_reply(
                    websocket,
                    user_id,
                    message_id,
                    f"尚未设置默认群。请使用“{command} 群号”，或先发送“设置默认群 群号”。",
                )
                return
        if not is_group_switch_on(group_id, MODULE_NAME) or not _can_access_group(group_id, user_id):
            await _private_reply(websocket, user_id, message_id, "该群未开启任务功能，或你不在该群中。")
            return
        if command == "订阅任务" or command == "取消订阅":
            service.set_subscription(group_id, user_id, command == "订阅任务")
            await _private_reply(websocket, user_id, message_id, "✅ 操作成功，活跃度已刷新。")
        elif command == "刷新活跃":
            service.mark_active(group_id, user_id, "manual_refresh")
            await _private_reply(websocket, user_id, message_id, "✅ 活跃度刷新成功。")
        elif command == "我的活跃度":
            record = service.get_activity(group_id, user_id)
            service.mark_active(group_id, user_id, "activity_view")
            await _private_reply(websocket, user_id, message_id, f"刷新前最近活跃：{service.format_time(record['last_active_at']) if record else '无记录'}\n当前查看已刷新活跃度。")
        else:
            await _show_task_list(websocket, user_id, message_id, group_id, user_id, command == "历史任务", private=True)
        return
    task_match = re.search(r"(K-\d{8}-\d{3})", raw, re.I)
    if task_match:
        task = service.get_task(task_match.group(1).upper())
        if not task or not is_group_switch_on(task["group_id"], MODULE_NAME) or not _can_access_group(task["group_id"], user_id):
            await _private_reply(websocket, user_id, message_id, "任务不存在，或你不在任务所属群。")
            return
        async def reply(text):
            await _private_reply(websocket, user_id, message_id, text)
        await _handle_common(websocket, raw, user_id, message_id, task["group_id"], reply, is_system_admin(user_id))


async def handle_response(websocket, msg):
    """处理本模块主动请求的最新群成员列表。"""
    echo = msg.get("echo", "")
    if not isinstance(echo, str) or "-KernelActivity-activity-" not in echo:
        return
    group_match = re.search(r"group_id=(\d+)", echo)
    message_match = re.search(r"-message=([^\-]+)$", echo)
    if not group_match or not message_match:
        logger.warning(f"[{MODULE_NAME}]无法解析活跃度检查响应: {echo}")
        return
    group_id = group_match.group(1)
    message_id = message_match.group(1)
    if msg.get("status") != "ok":
        await _group_reply(websocket, group_id, message_id, "获取群成员列表失败，请稍后重试。")
        return
    members = msg.get("data")
    if not isinstance(members, list):
        await _group_reply(websocket, group_id, message_id, "获取群成员列表失败，请稍后重试。")
        return

    detail_match = re.search(r"activity-detail-requester=(\d+)-target=(\d+)", echo)
    if detail_match:
        member = _find_member(members, detail_match.group(2))
        if member:
            text = "📈 成员活跃度\n\n" + _activity_detail_text(group_id, member)
        else:
            text = "没有在当前群成员列表中找到该成员。"
        await _group_reply(websocket, group_id, message_id, text, expire=120)
        return

    report_match = re.search(r"activity-report-days=(\d+)-requester=(\d+)", echo)
    if report_match:
        text = _group_activity_report(
            group_id,
            members,
            int(report_match.group(1)),
            bot_user_id=msg.get("self_id"),
        )
        await _group_reply(websocket, group_id, message_id, text, expire=300)
