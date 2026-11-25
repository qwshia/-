"""道具状态机节点：item_node —— 确定性处理物品给予/拾取/偷窃。

物品平铺：desc 存全部信息；锁用 locked + lock_mode/key_name/bio_holder。
无 content 嵌套、无 __content__/__full_desc__/__agreed__ 占位。
"""
import copy
import re

from config import actor_llm
from langchain_core.messages import HumanMessage
from state import AgentState
from item_utils import unlock_missing, unlock_hint, bio_holder_of, open_bio_part, lock_mode_of, key_name_of
from utils import _get_item_names, _upsert_item
from parsing import _parse_steal_cmd

_STEAL_RE = re.compile(r"(?:偷|窃取|顺走|拿走)(?:\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12}))?")


def is_steal_cmd(msg: str) -> bool:
    return msg.lstrip().startswith(("!", "！")) and bool(_STEAL_RE.search(msg.lstrip().lstrip("!！")))


async def item_node(state: AgentState) -> dict:
    transfers = state.get("item_transfers") or []
    scene = state.get("current_scene", "")
    npcs = state.get("npcs") or {}
    player_items = copy.deepcopy(state.get("player_items") or [])
    props = [dict(p) for p in (state.get("props") or [])]
    desc_map = dict(state.get("item_desc_map") or {})
    done = list(state.get("processed_transfers") or [])
    events = []
    sys_msgs = []
    ui_events = []

    def _land(name, tp, mode, opener_name=""):
        desc = str(tp.get("desc", "") or "").strip()
        locked = bool(tp.get("locked"))

        # NPC 帮忙开锁：NPC 用自己的生物凭证（本人验证）打开物品的生物锁部分
        if mode == "auto_open" and locked and opener_name:
            _auto = open_bio_part(tp, opener_name)
            if _auto is not None:
                tp.update(_auto)
                locked = bool(_auto.get("locked"))
                if not locked:
                    sys_msgs.append(f"【系统】{opener_name}用自己的生物凭证打开了「{name}」。")
                else:
                    # both 锁：生物部分已开，还差物理钥匙，绝不附带开物理锁
                    sys_msgs.append(f"【系统】{opener_name}打开了「{name}」的生物锁部分，还需{unlock_hint(tp)}。")

        if locked:
            # 玩家自己只能开物理部分；bio 部分必须 bio_holder 帮忙（在场不算同意），故这里 bio 恒判缺
            _missing = unlock_missing(tp, player_items, props, None, "")
            if _missing:
                # 缺凭证：进背包但保持锁定，desc 显示锁提示，锁字段一并保留（供 NPC 帮忙开锁判定）
                _found_it = next((x for x in player_items if isinstance(x, dict) and x.get("name") == name), None)
                if _found_it is not None:
                    _found_it["count"] = int(_found_it.get("count", 1) or 1) + 1
                else:
                    player_items.append({
                        "name": name, "count": 1,
                        "desc": unlock_hint(tp),
                        "locked": True,
                        "lock_mode": lock_mode_of(tp),
                        "key_name": key_name_of(tp),
                        "bio_holder": bio_holder_of(tp),
                    })
                desc_map[name] = desc
                props[:] = [p for p in props if p.get("name") != name]
                ui_events.append({"type": "item_acquired", "item": name})
                if mode in ("transfer", "auto_open"):
                    ui_events.append({"type": "item_transfer", "item": name, "count": int((tp or {}).get("count", 1) or 1), "from_npc": opener_name or str((tp or {}).get("holder", "") or ""), "to_player": True})
                if mode == "transfer":
                    events.append(f"{name}到了玩家手中")
                return

        # 无锁或已具备解锁凭证：直接拾取（desc 全文可读）
        _upsert_item(player_items, name, count=1, desc=desc)
        desc_map[name] = desc
        props[:] = [p for p in props if p.get("name") != name]
        ui_events.append({"type": "item_acquired", "item": name})
        if mode in ("transfer", "auto_open"):
            ui_events.append({"type": "item_transfer", "item": name, "count": int((tp or {}).get("count", 1) or 1), "from_npc": opener_name or str((tp or {}).get("holder", "") or ""), "to_player": True})
        if mode == "transfer":
            events.append(f"{name}到了玩家手中")

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
        _opener = str((npcs.get(tr.get("from")) or {}).get("name", "") or "")
        _land(name, tp, _mode, _opener)

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

        # NPC 帮忙开锁：物品锁定且 bio_holder 是该 NPC → 用生物凭证开生物部分，物品留在玩家背包
        _it_locked = bool(_found.get("locked")) if isinstance(_found, dict) else False
        if _it_locked and str((_found.get("bio_holder") if isinstance(_found, dict) else "") or "").strip() == _to_name:
            _auto = open_bio_part(_found, _to_name)
            if _auto is not None:
                _open_ok = not bool(_auto.get("locked"))
                for _k in ("locked", "lock_mode", "key_name", "bio_holder"):
                    _found[_k] = _auto.get(_k, "")
                _full_desc = str(desc_map.get(_gname, "") or "").strip()
                if _open_ok:
                    _found["desc"] = _full_desc if _full_desc else str(_found.get("desc", ""))
                    sys_msgs.append(f"【系统】{_to_name}用自己的生物凭证帮你打开了「{_gname}」。")
                else:
                    # both 锁：生物部分已开，还差物理钥匙，绝不附带开物理锁
                    _found["desc"] = unlock_hint(_found)
                    sys_msgs.append(f"【系统】{_to_name}帮你打开了「{_gname}」的生物锁部分，还需{_found['desc']}。")
                ui_events.append({"type": "item_unlocked", "item": _gname, "by_npc": _to_name})
                continue

        _cnt = int(_found.get("count", 1)) if isinstance(_found, dict) else 1
        _cnt -= 1
        if _cnt <= 0:
            player_items.remove(_found)
        elif isinstance(_found, dict):
            _found["count"] = _cnt

        _orig_prop = next((p for p in props if p.get("name") == _gname), None)
        _gdesc = str(desc_map.get(_gname, "") or (str(_found.get("desc", "")) if isinstance(_found, dict) else ""))
        props[:] = [p for p in props if p.get("name") != _gname]
        _new_prop = {
            "name": _gname,
            "holder": _to_name,
            "carried": True,
            "location": _to_loc or scene,
            "desc": _gdesc,
        }
        if _orig_prop:
            for _k in ("locked", "lock_mode", "key_name", "bio_holder"):
                if _k in _orig_prop:
                    _new_prop[_k] = _orig_prop[_k]
        props.append(_new_prop)

        sys_msgs.append(f"「{_gname}」已交给{_to_name}。")
        ui_events.append({"type": "item_given", "item": _gname, "to_npc": _to_name})

    msg = state.get("user_msg", "")

    steal_attempts = dict(state.get("steal_attempts") or {})
    processed_steals = list(state.get("processed_steals") or [])

    if is_steal_cmd(msg):
        _steal_sig = f"{scene}::{msg.strip()}::{int(state.get('world_tick') or 0)}"
        if _steal_sig in processed_steals:
            return {
                "player_items": player_items,
                "props": props,
                "item_desc_map": desc_map,
                "processed_transfers": done,
                "processed_player_gives": done_gives,
                "processed_steals": processed_steals,
                "steal_attempts": steal_attempts,
            }
        processed_steals.append(_steal_sig)
        if len(processed_steals) > 20:
            processed_steals = processed_steals[-20:]

        parsed_steal = _parse_steal_cmd(msg)
        item_name = parsed_steal["item_name"] if parsed_steal else ""
        npc_name = parsed_steal["npc_name"] if parsed_steal else ""

        _scene_npc_names = {
            info.get("name") for info in (state.get("npcs") or {}).values()
            if info.get("location") == scene
        }

        # 生物凭证=身体特征（虹膜/指纹/声纹），不生成物品；这里兜底排除一切凭证/证件类，防止被偷
        _CRED_WORDS = ("工牌", "员工卡", "工作证", "门禁卡", "身份证", "证件", "指纹", "虹膜", "声纹", "瞳孔", "DNA", "血液", "唾液", "毛发", "眼角膜", "视网膜")

        def _is_stealable(p):
            if not p or not p.get("name") or p.get("holder") not in _scene_npc_names:
                return False
            _txt = f"{p.get('name','')} {p.get('desc','')}"
            return not any(w in _txt for w in _CRED_WORDS)

        steal_target = None
        _matched_holder = ""

        # 1) 指定 NPC + 指定物品：精确匹配
        if npc_name and item_name:
            for p in props:
                if p.get("name") == item_name and p.get("holder") == npc_name and _is_stealable(p):
                    steal_target = item_name
                    _matched_holder = npc_name
                    break

        # 2) 仅指定物品：场景内任一 NPC 身上找
        if not steal_target and item_name:
            for p in props:
                if _is_stealable(p) and (p.get("name") == item_name or item_name in p.get("name") or p.get("name") in item_name):
                    steal_target = p.get("name")
                    _matched_holder = p.get("holder", "")
                    break

        # 3) 仅指定 NPC：随机偷其一件可偷物品（多件时随机）
        if not steal_target and npc_name:
            if npc_name in _scene_npc_names:
                _cands = [p for p in props if _is_stealable(p) and p.get("holder") == npc_name]
                if _cands:
                    import random
                    _pick = random.choice(_cands)
                    steal_target = _pick.get("name")
                    _matched_holder = _pick.get("holder", "")
            else:
                # 兜底：单参数其实是物品名（测试用），按物品名找
                for p in props:
                    if _is_stealable(p) and (p.get("name") == npc_name or npc_name in p.get("name") or p.get("name") in npc_name):
                        steal_target = p.get("name")
                        _matched_holder = p.get("holder", "")
                        break

        # 4) 无目标：随机偷场景内任一 NPC 的一件可偷物品
        if not steal_target and not npc_name and not item_name:
            _cands = [p for p in props if _is_stealable(p)]
            if _cands:
                import random
                _pick = random.choice(_cands)
                steal_target = _pick.get("name")
                _matched_holder = _pick.get("holder", "")

        if not steal_target:
            return {
                "history": [{"speaker": "system", "content": "【系统】这里没什么可偷的东西，或者你说的物品不在这里。"}],
                "processed_transfers": done,
                "processed_player_gives": done_gives,
                "processed_steals": processed_steals,
                "steal_attempts": steal_attempts,
            }

        tp = next((p for p in props if p.get("name") == steal_target), None)
        holder = (tp or {}).get("holder", "") or _matched_holder
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

            if tp is not None and tp.get("locked"):
                # 锁定物品：偷到但保持锁定进背包，锁字段保留（生物凭证本身是身体特征，无从偷起）
                _found_it = next((x for x in player_items if isinstance(x, dict) and x.get("name") == steal_target), None)
                if _found_it is not None:
                    _found_it["count"] = int(_found_it.get("count", 1) or 1) + 1
                else:
                    player_items.append({
                        "name": steal_target, "count": 1,
                        "desc": unlock_hint(tp),
                        "locked": True,
                        "lock_mode": lock_mode_of(tp),
                        "key_name": key_name_of(tp),
                        "bio_holder": bio_holder_of(tp),
                    })
                desc_map[steal_target] = str(tp.get("desc", "") or "").strip()
                props[:] = [p for p in props if p.get("name") != steal_target]
                sys_msgs.append(f"【系统】你偷到了「{steal_target}」，但它是锁着的（{unlock_hint(tp)}）。")
                steal_attempts[steal_target] = 0
                ui_events.append({"type": "item_acquired", "item": steal_target})
                out = {
                    "player_items": player_items,
                    "props": props,
                    "item_desc_map": desc_map,
                    "processed_transfers": done,
                    "processed_player_gives": done_gives,
                    "processed_steals": processed_steals,
                    "steal_attempts": steal_attempts,
                    "last_player_action": {"action": "steal", "target": steal_target, "scene": scene},
                    "active_targets": [],
                }
                if ui_events:
                    out["ui_events"] = ui_events
                if sys_msgs:
                    out["history"] = [
                        {"speaker": "user", "content": msg},
                        {"speaker": "system", "content": "【系统】" + "\n".join(sys_msgs)},
                    ]
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
                "processed_player_gives": done_gives,
                "processed_steals": processed_steals,
                "steal_attempts": steal_attempts,
                "last_player_action": {"action": "steal", "target": steal_target, "scene": scene},
                "active_targets": [],
            }
            if ui_events:
                out["ui_events"] = ui_events
            if sys_msgs:
                out["history"] = [
                    {"speaker": "user", "content": msg},
                    {"speaker": "system", "content": "【系统】" + "\n".join(sys_msgs)},
                ]
            return out
        else:
            penalty = 15 if aff < 50 else 8 if aff < 80 else 3
            npcs_patch = {}
            if holder_npc:
                npcs_patch = {holder_npc: {"affection": max(-100, int(holder_info.get("affection", 50)) - penalty)}}
            sys_msgs.append(f"【系统】偷窃「{steal_target}」失败，{holder}似乎有所察觉。")
            ui_events.append({"type": "action_feedback", "target": holder, "action": "steal_failed", "shake_screen": True})
            events_out = [f"玩家偷窃{steal_target}失败，{holder}产生警觉"]
            try:
                _p = f"你是{holder}，性格{holder_info.get('personality','')}，外貌{holder_info.get('appearance','')}。"
                _p += f"对玩家好感度{aff}。玩家试图偷你的{steal_target}但失败了，用一句话反应（40字内），好感低时更愤怒。"
                r = await actor_llm.ainvoke([HumanMessage(content=_p)])
                npc_reply = (r.content or "").strip()
            except Exception:
                npc_reply = "（没有说话。）"
            return {
                "player_items": player_items,
                "props": props,
                "item_desc_map": desc_map,
                "processed_transfers": done,
                "processed_player_gives": done_gives,
                "processed_steals": processed_steals,
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
                "last_player_action": {"action": "steal", "target": steal_target, "scene": scene},
                "active_targets": [],
            }

    out = {
        "player_items": player_items,
        "props": props,
        "item_desc_map": desc_map,
        "processed_transfers": done,
        "processed_player_gives": done_gives,
        "processed_steals": processed_steals,
        "steal_attempts": steal_attempts,
    }
    if events:
        out["world_events"] = events
    if ui_events:
        out["ui_events"] = ui_events
    if sys_msgs:
        out["history"] = [{"speaker": "system", "content": m} for m in sys_msgs]
    return out
