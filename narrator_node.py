"""并发收敛节点：narrator_node
不再生成"群像"描写——各 NPC 台词直接作为独立条目透传，
由前端以 NPC 头顶气泡 / 聊天页透视的方式逐条展示。
"""
import re

from state import AgentState


def _clean(s: str) -> str:
    s = re.sub(r"[\[【(（]\s*(do|aff|options?|intent|emotion|weakness|surrender).*?[\]】)）]", "", s or "", flags=re.IGNORECASE)
    return s.strip()


async def narrator_node(state: AgentState) -> dict:
    # 各 NPC 台词已由 npc_worker 清洗后直接写入 history，
    # 此处不再回写 history（否则 operator.add 会重复追加同一句台词）。
    return {"last_player_action": None}
