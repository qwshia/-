"""工具层：JSON 解析、NPC 目标解析、广播判定、时间档位等纯函数。"""
import re
import json
from typing import List

from state import AgentState

TIME_PHASES = ["清晨", "上午", "正午", "午后", "黄昏", "夜晚", "深夜", "凌晨"]


def time_index(phase: str) -> int:
    """时间档在序列中的下标；未知档位按序列中最接近者折算"""
    if phase in TIME_PHASES:
        return TIME_PHASES.index(phase)
    for i, p in enumerate(TIME_PHASES):
        if p in (phase or ""):
            return i
    return 0


def phase_span(start: str, limit: str) -> int:
    """从 start 走到 limit 需要经过的档数（循环距离；起点==终点视为一整圈）"""
    n = len(TIME_PHASES)
    si, li = time_index(start), time_index(limit)
    span = (li - si) % n
    return span if span else n


def phase_display(start: str, progress: float) -> str:
    """按已流逝档数 progress 折算当前显示档位（环形；加 epsilon 防浮点取整回退）"""
    n = len(TIME_PHASES)
    pos = (time_index(start) + int(progress + 1e-9)) % n
    return TIME_PHASES[pos]


def time_is_up(start: str, limit: str, progress: float) -> bool:
    """时间是否已走满 start→limit 全程（导演未设时限时为 False）"""
    if not limit:
        return False
    return progress >= phase_span(start, limit) - 1e-9


def time_step(state: AgentState) -> dict:
    """时间推进的公共计算（time_node 与 judge_ending 复用）：
    导演给定 time_turns 时，把 time_start→time_limit 的档数按轮数比例分摊，每轮推进一小步。
    导演未设时限（无 time_limit/time_turns）→ 返回空 dict（时间固定不流逝）。"""
    start = state.get("time_start", "清晨")
    limit = state.get("time_limit", "") or ""
    turns = int(state.get("time_turns") or 0)
    if not limit or turns <= 0:
        return {}
    span = phase_span(start, limit)
    progress = float(state.get("time_progress") or 0.0)
    new_progress = progress + span / turns
    return {"time_progress": new_progress, "time_phase": phase_display(start, new_progress)}


def time_advance(state: AgentState) -> dict:
    """时间推进的完整节点计算（time_node / p2p_node 复用，保证每轮恰好推进一次）：
    在 time_step 基础上，首次越过关键时间点时附加一条系统提示。
    导演未设时限（无 time_limit/time_turns）→ 返回空 dict（时间固定不流逝）。"""
    out = time_step(state)
    if not out:
        return {}
    limit = state.get("time_limit", "") or ""
    span = phase_span(state.get("time_start", "清晨"), limit)
    progress = float(state.get("time_progress") or 0.0)
    new_progress = out["time_progress"]
    if progress < span <= new_progress:
        goal = (state.get("ending_goal", "") or "").strip()[:60]
        out["history"] = [{"speaker": "system",
                           "content": f"【系统】时间已到 {limit}，关键节点来临（{goal}）。请抓紧行动，或给出你的结论。"}]
    return out


def _parse_json(text: str) -> dict:
    """从 LLM 输出中稳健提取 JSON：先去 ```json 围栏，再兜底用 raw_decode 从第一个 { 开始试解析"""
    try:
        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
        return json.loads(text)
    except Exception:
        decoder = json.JSONDecoder()
        idx = text.find("{")
        while idx != -1:
            try:
                obj, _ = decoder.raw_decode(text[idx:])
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass
            idx = text.find("{", idx + 1)
        return {}


def _npc_display(state: AgentState, npc_id: str) -> str:
    info = (state.get("npcs") or {}).get(npc_id, {})
    return info.get("name") or npc_id


def resolve_target(msg: str, npcs: dict) -> str | None:
    """解析 @数字 / @npc_N / @子N → npc_id；不支持按名字引用（统一用编号）"""
    m = re.search(r"@(?:子|npc_)?(\d+)", msg)
    if m:
        return f"npc_{m.group(1)}"
    return None


def is_broadcast(msg: str) -> bool:
    if re.search(r"@全部|@all\b|广播|(?:^|[\s:：，,])全部(?=$|[\s:：，,])", msg, re.I):
        return True
    return False


def is_scene_move_cmd(msg: str, state: AgentState) -> bool:
    """移动指令识别：!去X / ！去X（半角/全角）/ 去X / 进入X / 打开X / 走进X / 回到X
    （X 命中场景白名单才算移动指令，'打开相册'这类非场景词不算）。graph 路由与 story_keeper 共用。"""
    stripped = msg.lstrip()
    if not stripped.startswith(("!", "！")):
        return False
    stripped = stripped.lstrip("!！").strip()
    if stripped.startswith(("!去", "！去")):
        return True
    m = re.match(r"^(?:请)?(?:去|进入|走进|打开|回到|推开)\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})", stripped)
    if m:
        cand = m.group(1).strip(" 的")
        avail = state.get("available_scenes") or []
        if any(cand == s or cand in s or s in cand for s in avail):
            return True
        if "门" in cand:
            c2 = cand.replace("房门", "").replace("门", "").strip()
            if c2 and any(c2 == s or c2 in s or s in c2 for s in avail):
                return True
    return False


def _parse_content(content: list) -> list[dict]:
    """把容器 content 元素归一为 {"name": 道具名, "text": 文本全文}：
    - 字符串：普通道具（钥匙/物件），text 为空，开箱只登记名字；
    - 对象 {"name","text"}：文本道具（密信/字条/日记），开箱时系统当场展示 text。
    """
    out = []
    for c in content or []:
        if isinstance(c, str):
            out.append({"name": c, "text": ""})
        elif isinstance(c, dict) and c.get("name"):
            out.append({"name": c["name"], "text": c.get("text", "") or ""})
    return out
