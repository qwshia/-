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


def handle_container_open(container_name, desc_map, props, player_items, scenes):
    """开箱全流程：解析 __content__，把子物品加入背包，清空父容器，还原子物品描述。返回子物品名列表。"""
    container_name = str(container_name or "").strip()
    if not container_name:
        return []
    raw = (desc_map or {}).get(f"__content__:{container_name}", "[]")
    try:
        raw_subs = json.loads(str(raw)) if raw else []
    except Exception:
        raw_subs = [x.strip() for x in str(raw).split(",") if x.strip()]
    subs = []
    sub_texts = {}
    for x in (raw_subs if isinstance(raw_subs, list) else []):
        if isinstance(x, dict):
            nm = str(x.get("name", "") or "").strip()
            if nm:
                subs.append(nm)
                t = str(x.get("text", "") or x.get("desc", "") or "").strip()
                if t:
                    sub_texts[nm] = t
        else:
            nm = str(x).strip()
            if nm:
                subs.append(nm)
    for s in subs:
        if s not in _get_item_names(player_items):
            _upsert_item(player_items, s, count=1)
        real = sub_texts.get(s, "") or _resolve_sub_desc(s, container_name, props, scenes, desc_map)
        # Brute-force: also try any container content if still empty
        if not real or real == "一件普通的物品。":
            for p in (props or []):
                if isinstance(p, dict) and isinstance(p.get("content"), list):
                    for c in p["content"]:
                        if isinstance(c, dict) and str(c.get("name", "")).strip() == s:
                            if c.get("text"):
                                real = str(c["text"]).strip()
                            elif c.get("desc"):
                                real = str(c["desc"]).strip()
                            break
                if real and real != "一件普通的物品。":
                    break
        if not real or real == "一件普通的物品。":
            for sdef in (scenes or {}).values():
                if not isinstance(sdef, dict):
                    continue
                for it in (sdef.get("items") or []):
                    if isinstance(it, dict) and isinstance(it.get("content"), list):
                        for c in it["content"]:
                            if isinstance(c, dict) and str(c.get("name", "")).strip() == s:
                                if c.get("text"):
                                    real = str(c["text"]).strip()
                                elif c.get("desc"):
                                    real = str(c["desc"]).strip()
                                break
                    if real and real != "一件普通的物品。":
                        break
                if real and real != "一件普通的物品。":
                    break
        # Assign or remove placeholder completely
        if not real or real == "一件普通的物品。":
            real = "一件普通的物品。"
        if real:
            desc_map[s] = real
        else:
            desc_map[s] = "一件普通的物品。"
        for it in (player_items or []):
            if isinstance(it, dict) and str(it.get("name", "")).strip() == s:
                if real:
                    it["desc"] = real
                else:
                    it["desc"] = "一件普通的物品。"
        for pp in (props or []):
            if isinstance(pp, dict) and pp.get("name") == s:
                pp.pop("container", None)
    d = str((desc_map or {}).get(container_name, "") or "")
    base = d.split("（锁着")[0].strip() if "（锁着" in d else d.strip()
    if isinstance(desc_map, dict):
        desc_map[container_name] = f"{base}（已开启）"
        desc_map.pop(f"__content__:{container_name}", None)
        desc_map.pop(f"__agreed__:{container_name}", None)
    return subs


def _resolve_sub_desc(sub_name, loc, props, scenes, desc_map):
    sub_name = str(sub_name).strip()
    # 1. Look into raw __content__ JSON first
    raw = (desc_map or {}).get(f"__content__:{loc}", "")
    if raw:
        try:
            parsed = json.loads(raw)
            for c in parsed:
                if isinstance(c, dict) and str(c.get("name")).strip() == sub_name:
                    if c.get("text"):
                        return str(c["text"]).strip()
                    if c.get("desc"):
                        return str(c["desc"]).strip()
        except Exception:
            pass

    # 2. Brute-force search in props
    for p in (props or []):
        if isinstance(p.get("content"), list):
            for c in p["content"]:
                if isinstance(c, dict) and str(c.get("name")).strip() == sub_name:
                    if c.get("text"):
                        return str(c["text"]).strip()
                    if c.get("desc"):
                        return str(c["desc"]).strip()

    # 3. Brute-force search in scenes
    for sdef in (scenes or {}).values():
        if not isinstance(sdef, dict):
            continue
        for it in (sdef.get("items") or []):
            if isinstance(it, dict) and isinstance(it.get("content"), list):
                for c in it["content"]:
                    if isinstance(c, dict) and str(c.get("name")).strip() == sub_name:
                        if c.get("text"):
                            return str(c["text"]).strip()
                        if c.get("desc"):
                            return str(c["desc"]).strip()

    return "一件普通的物品。"
