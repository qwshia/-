"""意图仲裁节点：arbiter_node
读取 NPC 的自主意图，决定执行、进 pending，或被玩家/其他 NPC 抢占。
"""
import random
from state import AgentState


async def arbiter_node(state: AgentState) -> dict:
    npcs = state.get("npcs") or {}
    player_intent = state.get("player_intent") or {}
    p_cmd = player_intent.get("command") or {}

    patches = {}
    ui_events = []
    sys_msgs = []
    props_dirty = False
    scenes_dirty = False

    p_targets = []
    if isinstance(p_cmd.get("item"), list):
        p_targets.extend(p_cmd.get("item"))
    if p_cmd.get("scene"):
        p_targets.append(p_cmd.get("scene"))

    for nid, info in npcs.items():
        intent = info.get("current_intent")
        if not intent or intent.get("kind") == "idle":
            continue

        kind = intent.get("kind")
        target = intent.get("target", "")
        npc_name = info.get("name", nid)
        npc_scene = info.get("location", "")

        # move 意图
        if kind == "move":
            if target and target in (state.get("available_scenes") or []):
                patches[nid] = {"location": target, "current_intent": None}
                ui_events.append({"type": "npc_move", "npc": npc_name, "target_scene": target})
            else:
                patches[nid] = {"current_intent": None}
            continue

        # ---------- take（NPC 自主拿场景物品）----------
        if kind == "take":
            # 争抢判定
            player_wants = bool(target and target in p_targets)
            npc_wants_list = []
            for other_nid, other_info in npcs.items():
                if other_nid != nid and other_info.get("location") == npc_scene:
                    o_int = other_info.get("current_intent") or {}
                    if o_int.get("kind") == "take" and o_int.get("target") == target:
                        npc_wants_list.append(other_nid)

            if player_wants or npc_wants_list:
                # 50% 概率 NPC 胜出
                if random.random() < 0.5:
                    # NPC 输了
                    _tags = list(info.get("emotion_tags") or [])
                    _tags.append({"target": "玩家", "tag": "被抢先", "tick": int(state.get("world_tick") or 0)})
                    if len(_tags) > 5:
                        _tags = _tags[-5:]
                    patches[nid] = {"current_intent": None, "emotion_tags": _tags}
                    continue
                else:
                    # NPC 赢了，往下走执行拿取
                    pass

            scenes = state.get("scenes") or {}
            scene_items = (scenes.get(npc_scene) or {}).get("items") or []
            scene_item = next(
                (it for it in scene_items if isinstance(it, dict) and it.get("name") == target),
                None,
            )
            if scene_item is None:
                # 场景里没有这件物品，清理意图并记录"发现东西没了"的失望情绪
                _tags = list(info.get("emotion_tags") or [])
                _tags.append({"target": "玩家", "tag": "发现东西没了", "tick": int(state.get("world_tick") or 0)})
                if len(_tags) > 5:
                    _tags = _tags[-5:]
                patches[nid] = {"current_intent": None, "emotion_tags": _tags}
                continue

            # 从场景移除并加入全局 props（全字段深拷贝，防 content/locked/key_name 丢失）
            import copy as _copy
            props = state.get("props")
            if props is None:
                props = []
                state["props"] = props
            _new_prop = {
                "name": target,
                "holder": npc_name,
                "carried": True,
                "location": npc_scene,
                "desc": scene_item.get("desc", ""),
            }
            for _k in ("content", "locked", "key_name"):
                if _k in scene_item:
                    _new_prop[_k] = _copy.deepcopy(scene_item[_k])
            props.append(_new_prop)
            scenes[npc_scene]["items"] = [x for x in scene_items if not (isinstance(x, dict) and x.get("name") == target)]
            
            props_dirty = True
            scenes_dirty = True
            ui_events.append({"type": "item_transfer", "item": target, "count": 1, "from_scene": True, "to_npc": npc_name})
            patches[nid] = {"current_intent": None}
            continue

        # ---------- give（NPC 主动给玩家物品，从 NPC 自身 props 中给出）----------
        if kind == "give":
            props = state.get("props")
            if props is None:
                props = []
                state["props"] = props
            target_prop = next(
                (p for p in props if isinstance(p, dict) and p.get("name") == target and p.get("holder") == npc_name),
                None,
            )
            if target_prop is None:
                # NPC 身上没有，清理
                patches[nid] = {"current_intent": None}
                continue

            player_scene = state.get("current_scene", "")
            if player_scene != npc_scene:
                # 玩家不在场，本次作废，不排队
                patches[nid] = {"current_intent": None}
                continue

            # 玩家在场，从 props 移出并放入 player_items
            import copy as _copy
            props.remove(target_prop)
            _new_item = {
                "name": target_prop.get("name"),
                "count": target_prop.get("count", 1),
                "desc": target_prop.get("desc", "")
            }
            for _k in ("content", "locked", "key_name"):
                if _k in target_prop:
                    _new_item[_k] = _copy.deepcopy(target_prop[_k])
            
            player_items = state.get("player_items")
            if player_items is None:
                player_items = []
                state["player_items"] = player_items
            player_items.append(_new_item)
            
            props_dirty = True
            _tags = list(info.get("emotion_tags") or [])
            _tags.append({"target": "玩家", "tag": "示好", "tick": int(state.get("world_tick") or 0)})
            if len(_tags) > 5:
                _tags = _tags[-5:]
            patches[nid] = {"current_intent": None, "emotion_tags": _tags}
            ui_events.append({"type": "item_transfer", "item": target, "count": 1, "from_npc": npc_name, "to_player": True})
            continue

        # ---------- put_on / take_off（NPC 自主更换衣着）----------
        if kind in ("put_on", "take_off"):
            action = "put_on" if kind == "put_on" else "take_off"
            from parsing import _apply_attire
            from utils import _format_attire
            
            curr_attire = info.get("attire", {})
            new_attire = _apply_attire(curr_attire, action, target)
            patches[nid] = {"attire": new_attire, "current_intent": None}
            ui_events.append({"type": "attire_change", "npc": npc_name, "action": action, "current_attire": new_attire})
            sys_msgs.append(f"【系统】{npc_name}的穿着更新为：{_format_attire(new_attire)}。")
            continue

        # ---------- 其他：直接清理 ----------
        patches[nid] = {"current_intent": None}

    # 清理
    for nid, info in npcs.items():
        if nid not in patches and info.get("current_intent"):
            patches[nid] = {"current_intent": None}

    if not patches and not ui_events and not sys_msgs:
        return {}

    out = {}
    if patches:
        out["npcs"] = patches
    if ui_events:
        out["ui_events"] = ui_events
    if sys_msgs:
        out["history"] = [{"speaker": "system", "content": m} for m in sys_msgs]
    if props_dirty:
        out["props"] = state.get("props") if state.get("props") is not None else []
        out["player_items"] = state.get("player_items") or []
    if scenes_dirty:
        out["scenes"] = state.get("scenes") or {}
    return out