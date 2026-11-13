"""意图识别节点：@N 单人对话 → player_intent。规则优先，模型兜底。"""
import re

from state import AgentState
from utils import _get_item_names

from parsing import (
    _parse_attire_change,
    _parse_item_request,
    _parse_npc_move,
    _parse_player_give,
    _is_npc_command,
)


def _strip_target_prefix(msg: str) -> str:
    return re.sub(r"^\s*[!！]?\s*@(?:子|npc_)?\d+\s*", "", msg or "").strip()


_COMMAND_VERBS = ["把", "将", "穿上", "穿起来", "穿戴", "套上", "披上", "戴上",
                  "脱掉", "脱下", "拿下", "卸下", "除下", "脱光", "去", "前往",
                  "走到", "来到", "移动到", "给", "交", "拿"]


def _rule_residual(clean_msg: str, hits: list) -> str:
    residual = clean_msg
    for h in hits:
        if isinstance(h, tuple):
            for part in h:
                if isinstance(part, str) and part:
                    residual = residual.replace(part, "", 1)
        elif isinstance(h, str) and h:
            residual = residual.replace(h, "", 1)
    for w in _COMMAND_VERBS:
        residual = residual.replace(w, "")
    residual = residual.strip(" ，,。！？、()（）")
    return residual


async def intent_engine(state: AgentState) -> dict:
    user_msg = state.get("user_msg", "")
    npcs = state.get("npcs") or {}
    scene = state.get("current_scene", "")
    player_items = _get_item_names(state.get("player_items") or [])
    available_scenes = state.get("available_scenes") or []
    props = state.get("props") or []
    active_targets = state.get("active_targets") or []

    clean_msg = _strip_target_prefix(user_msg)

    if len(active_targets) != 1:
        return {"player_intent": {
            "intent_type": "chat", "command": None,
            "clean_msg": clean_msg, "source": "fallback",
        }}

    target_id = active_targets[0]
    target_name = (npcs.get(target_id) or {}).get("name", target_id)

    if "在哪" in (clean_msg or "") or "在哪" in (user_msg or "") or "哪里" in (clean_msg or "") or "哪里" in (user_msg or ""):
        return {"player_intent": {
            "intent_type": "chat", "command": None,
            "clean_msg": clean_msg, "source": "question_block",
        }}

    # 规则层优先（无论是否带 !@N）
    give_to_npc = _parse_player_give(clean_msg, player_items)
    attire = _parse_attire_change(clean_msg)
    item = _parse_item_request(clean_msg, props, target_name)
    if any(w in clean_msg for w in ("去", "前往", "走到", "移动到")):
        move = _parse_npc_move(clean_msg, available_scenes)
    else:
        move = None

    if give_to_npc:
        return {"player_intent": {
            "intent_type": "command",
            "command": {"type": "give_to_npc", "item": [give_to_npc], "scene": None, "action": None},
            "clean_msg": clean_msg, "source": "rule",
        }}
    if attire:
        action, it = attire
        return {"player_intent": {
            "intent_type": "command",
            "command": {"type": "wear", "item": [it], "scene": None, "action": action},
            "clean_msg": clean_msg, "source": "rule",
        }}
    if item:
        if item not in (set(player_items) | {p.get("name") for p in props if p.get("name")}):
            return {"player_intent": {
                "intent_type": "chat", "command": None,
                "clean_msg": clean_msg, "source": "whitelist_block",
            }}
        return {"player_intent": {
            "intent_type": "command",
            "command": {"type": "give_item", "item": [item], "scene": None, "action": None},
            "clean_msg": clean_msg, "source": "rule",
        }}
    if move:
        return {"player_intent": {
            "intent_type": "command",
            "command": {"type": "move", "item": None, "scene": move, "action": None},
            "clean_msg": clean_msg, "source": "rule",
        }}

    # 如果是显式指令 (!@N) 但规则层未命中 → 直接归为 chat，不走 LLM
    if _is_npc_command(user_msg):
        return {"player_intent": {
            "intent_type": "chat", "command": None,
            "clean_msg": clean_msg, "source": "rule_miss",
        }}

    # ================= 缩水版 LLM 兜底 =================
    from langchain_core.messages import HumanMessage
    from config import intent_llm
    import json

    npc_items_txt = "、".join(
        p.get("name", "") for p in props if p.get("holder") == target_name and p.get("name")
    ) or "（无）"
    _all_valid_items = list(set(player_items) | {p.get("name") for p in props if p.get("name")})
    items_txt = "、".join(_all_valid_items) or "（无）"

    prompt = (
        "你是意图提取器。判断玩家对 NPC 说的话里，是否包含【极度明确】的指令。\n"
        "只输出严格 JSON，不要任何额外文字，格式：\n"
        '{"intent_type": "chat|command", "command": {"type": "give_item|give_to_npc|wear|move", '
        '"item": ["物品名"] 或 null, "scene": "场景名或null", "action": "put_on|take_off|null"}}\n'
        "【严格限制】\n"
        "1. 玩家必须明确提到了【具体物品名】或【具体场景名】。\n"
        "2. 如果玩家表达模糊（如“给我吧”、“配和工作”），或者你无法确定具体物品名/场景名，必须输出 chat，command 为 null。\n"
        "3. give_item（玩家索要 NPC 的物品）：玩家明确说“给我 X / 把 X 拿来”。\n"
        "4. give_to_npc（玩家给 NPC 物品）：玩家明确说“给你 X / 把 X 交给你”。\n"
        "5. wear（穿/脱）：玩家明确说“穿上 X / 脱下 X”。\n"
        "6. move（移动）：玩家明确说“去 X”。\n"
        f"当前场景:{scene}\n"
        f"可去场景:{'、'.join(available_scenes) or '(无)'}\n"
        f"该 NPC 身上物品:{npc_items_txt}\n"
        f"场景及背包所有物品:{items_txt}\n"
        f"玩家说:{clean_msg}\n"
    )
    try:
        resp = await intent_llm.ainvoke([HumanMessage(content=prompt)])
        raw = resp.content if isinstance(resp.content, str) else str(resp.content)
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        data = {}

    intent_type = data.get("intent_type", "chat")
    if intent_type != "command":
        return {"player_intent": {
            "intent_type": "chat", "command": None,
            "clean_msg": clean_msg, "source": "llm_fallback",
        }}

    command = data.get("command")
    if not isinstance(command, dict):
        command = None
    source = "llm"

    _TYPE_ALIAS = {
        "attire": "wear", "attire_change": "wear",
        "item": "give_item", "give": "give_item", "go": "move",
        "give_to_npc": "give_to_npc", "player_give": "give_to_npc",
    }
    allowed_types = {"wear", "give_item", "move", "give_to_npc"}

    if command is not None:
        ctype = command.get("type")
        if ctype in _TYPE_ALIAS:
            command["type"] = _TYPE_ALIAS[ctype]
            ctype = command["type"]

        citem = command.get("item")
        if isinstance(citem, str):
            citem = [citem]
        elif not isinstance(citem, list):
            citem = []
        citem = [str(x).strip() for x in citem if x is not None]

        scen = command.get("scene")
        
        if ctype not in allowed_types:
            command = None
            intent_type = "chat"
        elif ctype in ("wear", "give_item", "give_to_npc") and not citem:
            command = None
            intent_type = "chat"
        elif ctype == "move" and scen not in available_scenes:
            command = None
            intent_type = "chat"

        # 物品白名单拦截（极其重要：防止“配和工作”当物品）
        _valid_items = set(player_items) | {p.get("name") for p in props if p.get("name")}
        npc_private = state.get("npc_private_items") or {}
        for v in npc_private.values():
            if isinstance(v, (list, tuple)):
                _valid_items.update(str(x) for x in v)

        if command is not None and ctype in ("give_item", "give_to_npc"):
            _before = list(citem)
            citem = [i for i in citem if i in _valid_items]
            if _before and not citem:
                command = None
                intent_type = "chat"
                source = "whitelist_block"
            else:
                command["item"] = citem

    if intent_type == "chat":
        command = None

    return {"player_intent": {
        "intent_type": intent_type, "command": command,
        "clean_msg": clean_msg, "source": source,
    }}
