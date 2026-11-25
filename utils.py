"""工具层：JSON 解析、NPC 目标解析、广播判定、时间档位等纯函数。"""

import re

import json

from typing import List


from state import AgentState


from constants import VALID_TIME_PHASES as TIME_PHASES


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

    """时间是否已超过 limit 一整个档位。
    progress 从 start 算起，走到 limit 时为 span，走到 limit 的下一档时为 span+1。
    在 limit 这一档内（progress ∈ [span, span+1)）仍算未超时；进入下一档才超时。
    导演未设时限时返回 False。"""

    if not limit:

        return False

    return progress >= phase_span(start, limit) + 1 - 1e-9


def time_step(state: AgentState) -> dict:

    """时间推进：不再依赖 time_limit。步长由 time_step_delta 动态决定。"""
    start = state.get("time_start", "清晨")

    progress = float(state.get("time_progress") or 0.0)
    delta = float(state.get("time_step_delta") or 0.2)
    new_progress = progress + delta
    return {"time_progress": new_progress, "time_phase": phase_display(start, new_progress)}


def time_advance(state: AgentState) -> dict:

    """时间推进的完整节点计算（time_node / p2p_node 复用，保证每轮恰好推进一次）:
    在 time_step 基础上，首次越过关键时间点时附加一条系统提示。
    导演未设时限（无 time_limit/time_turns）→ 返回空 dict（时间固定不流逝）。"""

    out = time_step(state)

    if not out:
        return {}

    limit = state.get("time_limit", "") or ""
    
    # 只有设置了时限才计算跨度并提示
    if limit:
        span = phase_span(state.get("time_start", "清晨"), limit)
        progress = float(state.get("time_progress") or 0.0)
        new_progress = out["time_progress"]
        if progress + 1e-6 < span and new_progress + 1e-6 >= span:
            goal = (state.get("ending_goal", "") or "").strip()[:60]
            out["history"] = [{"speaker": "system",
                               "content": f"【系统】时间已到 {limit}，关键节点来临（{goal}）。请抓紧行动，或给出你的结论。"}]

    return out


def _parse_json(text) -> dict:
    def _unwrap_blocks(obj):
        if isinstance(obj, list):
            parts = []
            for part in obj:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("text", ""))
                elif isinstance(part, str):
                    parts.append(part)
            if parts:
                return "".join(parts)
        return None

    # 1) 字符串形式的 block 数组：先 json.loads 再解包
    if isinstance(text, str):
        s = text.strip()
        try:
            maybe = json.loads(s)
            unwrapped = _unwrap_blocks(maybe)
            if unwrapped is not None:
                text = unwrapped
        except Exception:
            pass
    # 2) 真 list 形式的 block 数组
    elif isinstance(text, list):
        unwrapped = _unwrap_blocks(text)
        if unwrapped is not None:
            text = unwrapped
    elif not isinstance(text, str):
        text = str(text)

    if not isinstance(text, str):
        text = str(text)

    # 3) 剥离 think 标签与代码围栏
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    try:
        t = text.strip()
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t).strip()
        result = json.loads(t)
        if isinstance(result, dict):
            return result
        # 4) 解析结果仍是 block 列表 → 递归解包
        if isinstance(result, list):
            unwrapped = _unwrap_blocks(result)
            if unwrapped is not None:
                inner = _parse_json(unwrapped)
                if isinstance(inner, dict):
                    return inner
    except Exception:
        pass

    # 5) 兜底：从文本中搜索第一个完整 JSON 对象
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



def _format_attire(attire) -> str:
    if isinstance(attire, dict):
        parts = []
        for k, v in (attire or {}).items():
            if v not in (None, ''):
                parts.append(f'{k}：{v}')
        return '，'.join(parts) if parts else '无'
    if isinstance(attire, str):
        return attire.strip() or '无'
    if attire is None:
        return '无'
    return str(attire)


def _get_item_names(items: list) -> list:
    """把对象化背包（或任意物品列表）提取为纯净名称字符串列表。"""
    out = []
    for it in (items or []):
        if isinstance(it, dict):
            name = it.get("name")
        else:
            name = it
        if isinstance(name, str):
            name = name.strip()
        if name and name not in out:
            out.append(name)
    return out


def _upsert_item(items: list, name: str, count: int = 1, desc: str = "") -> list:
    """向对象化物品列表中添加/合并物品；已存在则 count 累加，不存在则新建条目。"""
    items = items if isinstance(items, list) else []
    name = str(name or "").strip()
    if not name:
        return items
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 1
    if count <= 0:
        count = 1
    for it in items:
        if isinstance(it, dict) and str(it.get("name", "")).strip() == name:
            it["count"] = int(it.get("count", 1) or 1) + count
            if desc and not str(it.get("desc", "") or "").strip():
                it["desc"] = desc
            return items
        if not isinstance(it, dict) and str(it).strip() == name:
            items[items.index(it)] = {"name": name, "count": count + 1, "desc": desc or ""}
            return items
    items.append({"name": name, "count": count, "desc": desc or ""})
    return items





def tick_pending(state: dict) -> dict:
    """倒计时消费 pending_intent：remain -= 1；归零尝试执行；失败重试满 2 次放弃并记 emotion_tags。"""
    npcs = state.get("npcs") or {}
    available_scenes = state.get("available_scenes") or []
    world_tick = int(state.get("world_tick") or 0)

    patches = {}
    ui_events = []
    world_events = []

    for nid, info in npcs.items():
        pending_list = list(info.get("pending_intent") or [])
        if not pending_list:
            continue

        new_pending = []
        for item in pending_list:
            remain = int(item.get("remain", 0)) - 1
            if remain > 0:
                item["remain"] = remain
                new_pending.append(item)
                continue

            kind = item.get("kind", "")
            target = item.get("payload", "")
            retry = int(item.get("retry_count", 0))

            # 只有 move 需要延迟重试；chat 在 p2p_node 一次性处理，其余当场执行
            if kind != "move":
                continue

            success = False
            npc_name = info.get("name", nid)
            npc_scene = info.get("location", "")

            # ---------- move ----------
            if kind == "move" and target in available_scenes:
                patches.setdefault(nid, {})["location"] = target
                ui_events.append({"type": "npc_move", "npc": npc_name, "target_scene": target})
                world_events.append(f"{npc_name}去了{target}")
                success = True

            if success:
                continue

            retry += 1
            if retry >= 2:
                _tags = list(info.get("emotion_tags") or [])
                _tags.append({"target": "玩家", "tag": "被阻拦", "tick": world_tick})
                if len(_tags) > 5:
                    _tags = _tags[-5:]
                patches.setdefault(nid, {})["emotion_tags"] = _tags
                # 将未能完成的动作向全局广播，供 story_keeper 提取世界动态
                world_events.append(f"{info.get('name', nid)}想做点什么（{kind}），但最终没能做成")
                ui_events.append({"type": "pending_expired", "npc_id": nid, "npc_name": info.get("name", nid), "kind": kind, "target": target})
                continue

            item["remain"] = 1
            item["retry_count"] = retry
            new_pending.append(item)

        patches.setdefault(nid, {})["pending_intent"] = new_pending

    out = {}
    if patches:
        out["npcs"] = patches
    if ui_events:
        out["ui_events"] = ui_events
    if world_events:
        out["world_events"] = world_events
    return out
