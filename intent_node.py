"""意图识别节点：@N 单人对话 → player_intent。规则优先，模型兜底。"""
import json
import re

from langchain_core.messages import HumanMessage

from config import intent_llm
from state import AgentState
from utils import _get_item_names

from parsing import (
    _parse_attire_change,
    _parse_item_request,
    _parse_npc_move,
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

    # 规则层
    hits = []
    command = None
    if _is_npc_command(user_msg):
        attire = _parse_attire_change(clean_msg)
        item = _parse_item_request(clean_msg, props, target_name)
        move = _parse_npc_move(clean_msg, available_scenes)
        if attire:
            action, it = attire
            command = {"type": "wear", "item": [it], "scene": None, "action": action}
            hits.append(attire)
        elif item:
            command = {"type": "give_item", "item": [item], "scene": None, "action": None}
            hits.append(item)
        elif move:
            command = {"type": "move", "item": None, "scene": move, "action": None}
            hits.append(move)

    if command is not None:
        residual = _rule_residual(clean_msg, hits)
        intent_type = "mixed" if residual else "command"
        return {"player_intent": {
            "intent_type": intent_type, "command": command,
            "clean_msg": clean_msg, "source": "rule",
        }}

    # 模型兜底
    npc_items_txt = "、".join(
        p.get("name", "") for p in props if p.get("holder") == target_name and p.get("name")
    ) or "（无）"
    prompt = (
        "你是意图识别器。根据玩家对 NPC 说的话，判断意图类型。\n"
        "只输出严格 JSON，不要任何额外文字，格式：\n"
        '{"intent_type": "chat|command|mixed", "command": {"type": "wear|move|give_item", '
        '"item": ["物品名"] 或 null, "scene": "场景名或null", "action": "put_on|take_off|null"}}\n'
         "根据玩家意图判断，如果玩家试图让NPC行动、索要物品、脱衣，或者让NPC去某地，输出 command。\n"
        "item 必须是数组：give_item 的 item 为物品名列表；wear 若玩家说把衣服全脱了，item 必须是 [\"全部\"]。\n"
        "若玩家提到多个物品/衣物（用“和/与/跟”并列），必须拆成数组的多个元素，不能合并成一句。\n"
"询问“在哪/在哪里/位置”一律属于 chat，禁止输出 give_item。\n"
"针对 wear 指令：如果玩家要求脱衣服，请结合该 NPC 的 [当前衣服字典]，精准提取要脱掉的衣服槽位键名（如'外套'、'鞋履'），直接输出在 item 数组里。不要输出玩家原话里的别名。\n"        f"当前场景：{scene}\n"
        f"可去场景：{'、'.join(available_scenes) or '（无）'}\n"
        f"玩家背包：{'、'.join(player_items) or '（空）'}\n"
        f"该 NPC 身上物品：{npc_items_txt}\n"
        f"玩家说：{clean_msg}\n"
        "若无明确指令则 intent_type=chat 且 command=null。"
    )
    try:
        resp = await intent_llm.ainvoke([HumanMessage(content=prompt)])
        raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    except Exception:
        return {"player_intent": {
            "intent_type": "chat", "command": None,
            "clean_msg": clean_msg, "source": "fallback",
        }}

    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"player_intent": {
            "intent_type": "chat", "command": None,
            "clean_msg": clean_msg, "source": "fallback",
        }}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {"player_intent": {
            "intent_type": "chat", "command": None,
            "clean_msg": clean_msg, "source": "fallback",
        }}

    intent_type = data.get("intent_type", "chat")
    if intent_type not in ("chat", "command", "mixed"):
        intent_type = "chat"
    command = data.get("command")
    if not isinstance(command, dict):
        command = None
    source = "llm"
    # 规范化类型别名
    _TYPE_ALIAS = {
        "attire": "wear", "attire_change": "wear", "player_wear": None, "player_attire": None,
        "self_attire": None, "item": "give_item", "give": "give_item", "go": "move",
    }
    allowed_types = {"wear", "give_item", "move"}

    if command is not None:
        ctype = command.get("type")
        if ctype in _TYPE_ALIAS:
            mapped = _TYPE_ALIAS[ctype]
            if mapped is None:
                command = None
                intent_type = "chat"
            else:
                command["type"] = mapped
                ctype = mapped
        citem = command.get("item") if isinstance(command, dict) else None
        # 数据兜底：强制转为列表
        if isinstance(citem, str):
            citem = [citem]
        elif isinstance(citem, list):
            citem = [str(x) for x in citem if x is not None]
        elif citem is None:
            if ctype in ("wear", "give_item"):
                citem = []
            else:
                citem = None
        if isinstance(command, dict):
            command["item"] = citem
        scen = command.get("scene") if isinstance(command, dict) else None
        if command is not None and ctype not in allowed_types:
            command = None
            intent_type = "chat"
        # 硬性拦截自换装
        if command is not None and command.get("type") in ("self_attire", "player_wear", "player_attire"):
            command = None
            intent_type = "chat"
        all_items = set(player_items) | {p.get("name") for p in props if p.get("name")}
        npc_private = state.get("npc_private_items") or {}
        for v in npc_private.values():
            if isinstance(v, (list, tuple)):
                all_items.update(str(x) for x in v)
        if command is not None and command.get("type") == "give_item" and isinstance(citem, list):
            _before = list(citem)
            citem = [i for i in citem if i in all_items]
            if _before and not citem:
                command = None
                intent_type = "chat"
                source = "whitelist_block"
            elif isinstance(command, dict):
                command["item"] = citem
        if command is not None and command.get("type") == "move" and scen is not None and scen not in available_scenes:
            command = None
            intent_type = "chat"

    if intent_type == "chat":
        command = None

    return {"player_intent": {
        "intent_type": intent_type, "command": command,
        "clean_msg": clean_msg, "source": source,
    }}
