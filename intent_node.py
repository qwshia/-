"""意图识别节点：@N 单人对话 → player_intent。纯规则解析，无 LLM 兜底。"""
import re

from state import AgentState
from utils import _get_item_names

from parsing import (
    _parse_attire_change,
    _parse_item_request,
    _parse_npc_move,
    _parse_player_give,
)


def _strip_target_prefix(msg: str) -> str:
    return re.sub(r"^\s*[!！]?\s*@(?:子|npc_)?\d+\s*", "", msg or "").strip()


async def intent_engine(state: AgentState) -> dict:
    user_msg = state.get("user_msg", "")
    npcs = state.get("npcs") or {}
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

    if "在哪" in (clean_msg or "") or "哪里" in (clean_msg or ""):
        return {"player_intent": {
            "intent_type": "chat", "command": None,
            "clean_msg": clean_msg, "source": "question_block",
        }}

    # 规则层（覆盖全部指令类型，未命中一律归 chat，交给 NPC 对话自然回应）
    give_to_npc = _parse_player_give(clean_msg, player_items)
    attire = _parse_attire_change(clean_msg)
    item = _parse_item_request(clean_msg, props, target_name)
    move = _parse_npc_move(clean_msg, available_scenes) if any(w in clean_msg for w in ("去", "前往", "走到", "移动到")) else None

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

    # 规则层未命中 → 一律 chat（模糊表达交给 NPC 对话自然回应）
    return {"player_intent": {
        "intent_type": "chat", "command": None,
        "clean_msg": clean_msg, "source": "rule_miss",
    }}
