import os


MODULE_NAME = "KernelActivity"
MODULE_ENABLED = True
SWITCH_NAME = "KA"
MODULE_DESCRIPTION = "内核学习任务、任务订阅与群活跃度管理"

# 固定到仓库根目录，避免从 repo/ 与 repo/app/ 启动时生成两份数据库。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
DATA_DIR = os.path.join(PROJECT_ROOT, "data", MODULE_NAME)
os.makedirs(DATA_DIR, exist_ok=True)

COMMANDS = {
    "发布任务": "管理员发布任务；支持标题、截止（整数天）、内容三个字段",
    "修改任务 任务编号": "管理员修改标题、截止或内容，未填写的字段保持不变",
    "关闭任务/重新开启任务 任务编号": "管理员关闭或重新开启任务",
    "当前任务": "查看本群进行中的任务并刷新活跃度",
    "历史任务 [页码]": "分页查看本群往期任务并刷新活跃度",
    "任务详情 任务编号": "查看任务详情和动态剩余时间",
    "任务进度/任务成员 任务编号": "查看任务完成统计或参与成员",
    "接受任务/完成任务/取消完成 任务编号": "更新自己的任务状态",
    "我的任务": "查看自己在本群的任务记录",
    "订阅任务/取消订阅": "订阅或取消本群任务私聊通知",
    "设置默认群 群号/我的默认群": "私聊设置或查看默认任务群；首次订阅会自动设置",
    "刷新活跃/我的活跃度": "刷新或查看本群的群外活跃记录",
}
