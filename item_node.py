"""道具状态机节点：item_node —— 确定性处理物品给予/开箱/偷窃。"""
import copy
import json
import re

from config import actor_llm
from langchain_core.messages import HumanMessage
from state import AgentState
from utils import _parse_content, _resolve_sub_desc
from item_utils import can_acquire, split_keys, player_has_key, key_name_of, key_owner_npc
from utils import _get_item_names, _upsert_item

_STEAL_RE = re.compile(r"(?:偷|窃取|顺走|拿走)(?:\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12}))?")

def is_steal_cmd(msg: str) -> bool:
    return msg.lstrip().startswith(("!", "！")) and bool(_STEAL_RE.search(msg.lstrip().lstrip("!！")))

async def item_node(state: AgentState) -> dict:
    transfers = state.get("item_transfers") or []
    scene = state.get("current_scene", "")
    player_items = copy.deepcopy(state.get("player_items") or [])
    props = [dict(p) for p in (state.get("props") or [])]
    desc_map = dict(state.get("item_desc_map") or {})
    done = list(state.get("processed_transfers") or [])
    events = []
    sys_msgs = []
    ui_events = []

    def _land(name, tp, mode):
        parsed = _parse_content(tp.get("content"))
        desc = str(tp.get("desc", "") or "").strip()
        locked = bool(tp.get("locked"))
        keys = split_keys(key_name_of(tp)) if locked else []
        got_names = [c["name"] for c in parsed]
        npc_owners = [key_owner_npc(k, props) for k in keys] if keys else []
        is_npc_cred = locked and bool(keys) and any(o is not None for o in npc_owners)
        content_present = bool(tp.get("content"))

        if content_present:
            if locked and is_npc_cred:
                if mode == "transfer":
                    sys_msgs.append(f"「{name}」该物品需 NPC 本人打开")
                    return
                if mode == "steal":
                    sys_msgs.append("这东西被生物锁保护，你拿不走。")
                    return
            if locked and mode != "auto_open":
                _upsert_item(player_items, name, count=1)
                desc_map[name] = f"{desc}（锁着，需要{key_name_of(tp)}）"
                desc_map[f"__content__:{name}"] = json.dumps(parsed, ensure_ascii=False)
                for _c in parsed:
                    _cn = _c.get("name") if isinstance(_c, dict) else str(_c)
                    _cn = str(_cn or "").strip()
                    if not _cn:
                        continue
                    _rd = _resolve_sub_desc(_cn, name, props, scenes, desc_map)
                    if _rd:
                        desc_map[_cn] = _rd
                    else:
                        # remove placeholder completely
                        _ph = desc_map.get(_cn, "")
                        if isinstance(_ph, str) and "内部" in _ph:
                            desc_map.pop(_cn, None)
                        for _it in player_items:
                            if isinstance(_it, dict) and str(_it.get("name", "")).strip() == _cn:
                                _d0 = str(_it.get("desc", "") or "")
                                if "内部" in _d0:
                                    _it["desc"] = ""
                props[:] = [p for p in props if p.get("name") != name]
                for _c in parsed:
                    _cn = _c.get("name") if isinstance(_c, dict) else _c
                    for _pp in props:
                        if _pp.get("name") == _cn:
                            _pp.pop("container", None)
                if mode == "transfer":
                    events.append(f"{name}到了玩家手中")
                ui_events.append({"type": "item_acquired", "item": name})
                if mode in ("transfer", "auto_open"):
                    ui_events.append({"type": "item_transfer", "item": name, "count": int((tp or {}).get("count", 1) or 1), "from_npc": str((tp or {}).get("holder", "") or ""), "to_player": True})  # 2D frontend
                return

            # 如果是 auto_open，或者是无锁，都会走到这里，直接展开
            _upsert_item(player_items, name, count=1)
            desc_map[name] = f"{desc}（已开启）"
            for c in parsed:
                cn = c.get("name") if isinstance(c, dict) else str(c)
                cn = str(cn or "").strip()
                if not cn:
                    continue
                if cn not in _get_item_names(player_items):
                    _upsert_item(player_items, cn, count=1)
                _cur_desc = str(desc_map.get(cn, "") or "")
                if (not _cur_desc) or (_cur_desc.startswith("在") and "内部" in _cur_desc):
                    _real = ""
                    if isinstance(c, dict):
                        _t = str(c.get("text", "") or "").strip()
                        if _t and "内部" not in _t:
                            _real = _t
                    if not _real:
                        _real = _resolve_sub_desc(cn, name, props, scenes, desc_map)
                    if not _real:
                        _subp = next((pp for pp in props if pp.get("name") == cn), None)
                        if _subp and _subp.get("desc"):
                            _d = str(_subp.get("desc")).strip()
                            if "内部" not in _d:
                                _real = _d
                    if not _real and isinstance(c, dict) and c.get("desc"):
                        _d = str(c.get("desc")).strip()
                        if "内部" not in _d:
                            _real = _d
                    if _real:
                        desc_map[cn] = _real
                        for _it in player_items:
                            if isinstance(_it, dict) and str(_it.get("name", "")).strip() == cn:
                                _it["desc"] = _real
                    else:
                        if _cur_desc.startswith("在") and "内部" in _cur_desc:
                            desc_map.pop(cn, None)
                if cn not in desc_map:
                    desc_map[cn] = ""
                if isinstance(c, dict) and c.get("text"):
                    _txt = str(c.get("text")).strip()
                    if _txt and "内部" not in _txt:
                        desc_map[cn] = (str(desc_map.get(cn, "") or "") + "；" + _txt).strip("；")
                for _pp in props:
                    if _pp.get("name") == cn:
                        _pp.pop("container", None)
            props[:] = [p for p in props if p.get("name") != name]
            if mode in ("transfer", "auto_open"):
                events.append(f"{name}到了玩家手中")
            ui_events.append({"type": "item_acquired", "item": name})
            ui_events.append({"type": "container_opened", "container": name, "revealed_items": [c.get("name") if isinstance(c, dict) else str(c) for c in parsed]})  # 2D frontend
            if mode in ("transfer", "auto_open"):
                ui_events.append({"type": "item_transfer", "item": name, "count": int((tp or {}).get("count", 1) or 1), "from_npc": str((tp or {}).get("holder", "") or ""), "to_player": True})  # 2D frontend
            return
        # 无 content
        _upsert_item(player_items, name, count=1)
        desc_map[name] = desc
        props[:] = [p for p in props if p.get("name") != name]
        if mode == "transfer":
            events.append(f"{name}到了玩家手中")
        ui_events.append({"type": "item_acquired", "item": name})
        if mode in ("transfer", "auto_open"):
            ui_events.append({"type": "item_transfer", "item": name, "count": int((tp or {}).get("count", 1) or 1), "from_npc": str((tp or {}).get("holder", "") or ""), "to_player": True})  # 2D frontend
        return

    avail = set(state.get("available_scenes") or [])
    scenes = state.get("scenes") or {}
    for tr in transfers:
        name = tr.get("item")
        sig = f"{tr.get('from')}::{name}"
        if not name or sig in done:
            continue
        done.append(sig)
        tp = next((p for p in props if p.get("name") == name), None)
        if tp is None:
            continue
        loc = (tp or {}).get("location", "") or ""
        if loc in avail and loc != scene:
            sys_msgs.append(f"「{name}」在{loc}，NPC没法在这里给你——需要先到{loc}，再让她转交。")
            continue
        _mode = "auto_open" if tr.get("auto_open") else "transfer"
        _land(name, tp, _mode)

    npcs = state.get("npcs") or {}
    player_gives = state.get("player_gives") or []
    done_gives = list(state.get("processed_player_gives") or [])
    for pg in player_gives:
        _gname = pg.get("item")
        _to_id = pg.get("to")
        _sig = f"{pg.get('from')}::{_to_id}::{_gname}"
        if not _gname or _sig in done_gives:
            continue
        done_gives.append(_sig)
        if _gname not in _get_item_names(player_items):
            continue
        _to_info = npcs.get(_to_id) or {}
        _to_name = _to_info.get("name", _to_id)
        _to_loc = _to_info.get("location", "")
        if _to_loc and _to_loc != scene:
            sys_msgs.append(f"你不在 {_to_name} 附近，没法把「{_gname}」交给 TA。")
            continue

        # 缺口1：count-1，为 0 才移除
        _found = None
        for _it in player_items:
            if isinstance(_it, dict) and str(_it.get("name", "")).strip() == _gname:
                _found = _it
                break
            if not isinstance(_it, dict) and str(_it).strip() == _gname:
                _found = _it
                break
        if _found is None:
            sys_msgs.append(f"你身上没有「{_gname}」，无法交给{_to_name}。")
            continue
        _cnt = int(_found.get("count", 1)) if isinstance(_found, dict) else 1
        _cnt -= 1
        if _cnt <= 0:
            player_items.remove(_found)
        elif isinstance(_found, dict):
            _found["count"] = _cnt

        # 缺口2：复制 content / locked / key_name（若原 props 有，或 desc_map 里有 __content__）
        _orig_prop = next((p for p in props if p.get("name") == _gname), None)
        _gdesc = str(desc_map.get(_gname, "") or "")
        props[:] = [p for p in props if p.get("name") != _gname]
        _new_prop = {
            "name": _gname,
            "holder": _to_name,
            "carried": True,
            "location": _to_loc or scene,
            "desc": _gdesc,
        }
        if _orig_prop:
            for _k in ("content", "locked", "key_name"):
                if _k in _orig_prop:
                    _new_prop[_k] = _orig_prop[_k]
        _c_key = f"__content__:{_gname}"
        if _c_key in desc_map and "content" not in _new_prop:
            try:
                _raw = json.loads(desc_map[_c_key])
                if isinstance(_raw, list) and _raw:
                    _new_prop["content"] = _raw
            except Exception:
                pass
        props.append(_new_prop)

        # 缺口3：清 __agreed__ 前缀
        for _k in list(desc_map.keys()):
            if _k == f"__agreed__:{_gname}":
                desc_map.pop(_k, None)

        sys_msgs.append(f"「{_gname}」已交给{_to_name}。")
        ui_events.append({"type": "item_given", "item": _gname, "to_npc": _to_name})  # 2D frontend

    msg = state.get("user_msg", "")

    steal_attempts = dict(state.get("steal_attempts") or {})
    if is_steal_cmd(msg):
        steal_target = None
        for p in props:
            if p.get("name") and p.get("name") in msg:
                steal_target = p.get("name")
                break
        if not steal_target:
            for p in props:
                if p.get("holder") not in ("玩家", "玩家身上", "") and p.get("name"):
                    steal_target = p.get("name")
                    break
        if not steal_target:
            return {
                "history": [{"speaker": "system", "content": "【系统】这里没什么可偷的东西。"}],
                "processed_transfers": done,
                "steal_attempts": steal_attempts,
            }
        tp = next((p for p in props if p.get("name") == steal_target), None)
        holder = (tp or {}).get("holder", "")
        holder_npc = next((nid for nid, info in (state.get("npcs") or {}).items() if info.get("name") == holder), None)
        holder_info = (state.get("npcs") or {}).get(holder_npc, {}) if holder_npc else {}
        aff = int(holder_info.get("affection", 50))
        attempts = steal_attempts.get(steal_target, 0) + 1
        steal_attempts[steal_target] = attempts
        PITY = 5
        if attempts >= PITY:
            success = True
        else:
            import random
            base = 0.2 + aff / 500.0
            base = min(0.7, max(0.05, base))
            success = random.random() < base

        if success:
            tp = next((p for p in props if p.get("name") == steal_target), None)
            _is_bio = False
            if tp and tp.get("locked"):
                _keys = split_keys(tp.get("key_name", ""))
                if any(key_owner_npc(k, props) is not None for k in _keys):
                    _is_bio = True

            if _is_bio:
                sys_msgs.append(f"【系统】你试图偷取「{steal_target}」，但它是生物凭证锁，你拿不走。")
                steal_attempts[steal_target] = 0
                out = {
                    "player_items": player_items,
                    "props": props,
                    "item_desc_map": desc_map,
                    "processed_transfers": done,
                    "steal_attempts": steal_attempts,
                }
                if ui_events:
                    out["ui_events"] = ui_events
                if sys_msgs:
                    out["history"] = [{"speaker": "system", "content": "【系统】" + "\n".join(sys_msgs)}]
                return out

            sys_msgs.append(f"【系统】你成功偷到了「{steal_target}」，它已进入背包。")
            if tp is not None:
                _land(steal_target, tp, "steal")
            steal_attempts[steal_target] = 0
            out = {
                "player_items": player_items,
                "props": props,
                "item_desc_map": desc_map,
                "processed_transfers": done,
                "steal_attempts": steal_attempts,
            }
            if ui_events:
                out["ui_events"] = ui_events
            if sys_msgs:
                out["history"] = [{"speaker": "system", "content": "【系统】" + "\n".join(sys_msgs)}]
            return out
        else:
            penalty = 15 if aff < 50 else 8 if aff < 80 else 3
            npcs_patch = {}
            if holder_npc:
                npcs_patch = {holder_npc: {"affection": max(-100, int(holder_info.get("affection", 50)) - penalty)}}
            sys_msgs.append(f"【系统】偷窃「{steal_target}」失败，{holder}似乎有所察觉。")
            ui_events.append({"type": "action_feedback", "target": holder, "action": "steal_failed", "shake_screen": True})  # 2D frontend
            events_out = [f"玩家偷窃{steal_target}失败，{holder}产生警觉"]
            try:
                r = await actor_llm.ainvoke([HumanMessage(content=f"你是{holder}，性格{holder_info.get('personality','')}，对玩家好感度{aff}。玩家试图偷你的{steal_target}但失败了，用一句话反应（40字内），好感低时更愤怒。")])
                npc_reply = (r.content or "").strip()
            except Exception:
                npc_reply = "（没有说话。）"
            return {
                "player_items": player_items,
                "props": props,
                "item_desc_map": desc_map,
                "processed_transfers": done,
                "steal_attempts": steal_attempts,
                "npcs": npcs_patch,
                "ui_events": ui_events,
                "history": [
                    {"speaker": "user", "content": msg},
                    {"speaker": holder_npc or "system", "content": npc_reply},
                    {"speaker": "system", "content": "【系统】" + "\n".join(sys_msgs)},
                ],
                "world_events": events_out,
                "analysis_counter": int(state.get("analysis_counter") or 0) + 1,
                "run_story": True,
            }

    out = {
        "player_items": player_items,
        "props": props,
        "item_desc_map": desc_map,
        "processed_transfers": done,
        "processed_player_gives": done_gives,
        "steal_attempts": steal_attempts,
    }
    if events:
        out["world_events"] = events
    if ui_events:
        out["ui_events"] = ui_events
    if sys_msgs:
        out["history"] = [{"speaker": "system", "content": m} for m in sys_msgs]
    return out