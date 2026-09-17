from logger import logger
from . import MODULE_NAME
from .handlers import handle_group_message, handle_private_message, handle_response


async def handle_events(websocket, msg):
    try:
        echo = msg.get("echo", "")
        if isinstance(echo, str) and "-KernelActivity-activity-" in echo:
            await handle_response(websocket, msg)
            return
        if msg.get("status") == "ok":
            return
        if msg.get("post_type") != "message":
            return
        if msg.get("message_type") == "group":
            await handle_group_message(websocket, msg)
        elif msg.get("message_type") == "private":
            await handle_private_message(websocket, msg)
    except Exception as exc:
        logger.error(f"[{MODULE_NAME}]处理事件失败: {exc}")
