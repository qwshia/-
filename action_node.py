"""动作引擎节点：action_engine —— 指令集 !去 / 调查 / 打开。

物品模型：平铺物品，信息全部写在 desc；锁用 locked + lock_mode(physical/bio/both/either)
+ key_name + bio_holder 描述。无 content 嵌套。
"""
import copy
import re

from config import actor_llm
from langchain_core.messages import HumanMessage
from state import AgentState
from item_utils import can_acquire, unlock_missing, unlock_hint, lock_mode_of, key_name_of
from parsing import _parse_use_cmd
from utils import _get_item_names, _upsert_item


def _match_avail(target, avail):
    target = (target or "").strip(" ，,、")
    if not target:
        return None
    if target in avail:
        return target
    for s in sorted(avail, key=len, reverse=True):
        if target in s or s in target:
            return s
    if "门" in target or "房门" in target:
        t2 = target.replace("房门", "").replace("门", "").strip()
        if t2:
            if t2 in avail:
                return t2
            for s in sorted(avail, key=len, reverse=True):
                if t2 in s or s in t2:
                    return s
    return None


def _what_item_opens(item_name, props, scenes):
    """返回 item_name（钥匙/工具）能开启的锁列表（物品锁+门禁），用于拿到钥匙后的提示。"""
    opens = []
    for _x in (props or []):
        if isinstance(_x, dict) and _x.get("locked") and key_name_of(_x) == item_name:
            opens.append(_x.get("name"))
    for _sd in (scenes or {}).values():
        if not isinstance(_sd, dict):
            continue
        if _sd.get("locked") and key_name_of(_sd) == item_name:
            opens.append(f"{_sd.get('name', '')}（门）" if _sd.get("name") else "（门）")
        for _it in (_sd.get("items") or []):
            if isinstance(_it, dict) and _it.get("locked") and key_name_of(_it) == item_name:
                opens.append(_it.get("name"))
    return list(dict.fromkeys(opens))


async def action_engine(state: AgentState) -> dict:
    msg = state.get("user_msg", "")
    scene = state.get("current_scene", "未知")
    scenes = state.get("scenes") or {}
    new_items = copy.deepcopy(state.get("player_items") or [])
    sys_replies = []
    ui_events = []
    new_scene = scene
    avail = state.get("available_scenes", [])
    opened = list(state.get("opened_scenes") or [])
    props = [dict(p) for p in (state.get("props") or [])]
    desc_map = dict(state.get("item_desc_map") or {})
    npcs = state.get("npcs") or {}

    stripped_msg = msg.lstrip()
    _act_type = "unknown"
    _target = ""

    is_move = (
        stripped_msg.startswith(("!去", "！去"))
        or stripped_msg.startswith(("去", "进入", "走进", "回到", "推开"))
    )
    is_investigate = (
        stripped_msg.startswith(("!", "！"))
        and stripped_msg.lstrip("!！").startswith(("调查", "打开"))
    )

    _action_done = False

    # ==== !使用 道具 对 目标：确定性道具效果 ====
    is_use = stripped_msg.startswith(("!使用", "！使用"))
    npcs_patch = {}
    if is_use:
        _parsed_use = _parse_use_cmd(msg)
        if not _parsed_use:
            sys_replies.append("用法：!使用 道具 对 目标（例如：!使用 撬锁器 对 密码箱，或 !使用 香槟 对 张三）")
        else:
            _uitem = _parsed_use["item"]
            _utarget = _parsed_use["target"]
            _found_u = next((x for x in new_items if isinstance(x, dict) and str(x.get("name", "") or "").strip()
                             and (_uitem == str(x.get("name", "")).strip() or _uitem in str(x.get("name", "")) or str(x.get("name", "")) in _uitem)), None)
            if _found_u is None:
                sys_replies.append(f"你身上没有「{_uitem}」。")
            else:
                _uname = str(_found_u.get("name", "") or "").strip()
                _udesc = str(_found_u.get("desc", "") or "").strip() or str(desc_map.get(_uname, "") or "").strip()
                _u_text = f"{_uname} {_udesc}"

                def _consume():
                    _cnt = int(_found_u.get("count", 1) or 1) - 1
                    if _cnt <= 0:
                        if _found_u in new_items:
                            new_items.remove(_found_u)
                    else:
                        _found_u["count"] = _cnt

                def _find_npc(target_name):
                    for nid, info in npcs.items():
                        _n = str(info.get("name", "") or "").strip()
                        if _n and (target_name == _n or target_name in _n or _n in target_name):
                            return nid, info
                    return None, None

                # 1. 撬锁类：撬开 physical 锁（背包/场景/场景物品栏）
                if any(w in _u_text for w in ("撬锁", "万能钥匙", "开锁器", "撬棍", "发卡", "锁匠")):
                    if not _utarget:
                        sys_replies.append(f"用法：!使用 {_uname} 对 目标（撬开物理锁物品）")
                    else:
                        _tgt = None
                        _tgt_owner = None
                        for _it in new_items:
                            if (isinstance(_it, dict) and _it.get("locked") and lock_mode_of(_it) == "physical"
                                    and (_utarget in str(_it.get("name", "") or "") or str(_it.get("name", "") or "") in _utarget)):
                                _tgt, _tgt_owner = _it, "bag"
                                break
                        if _tgt is None:
                            _tgt = next((p for p in props if p.get("locked") and lock_mode_of(p) == "physical"
                                         and str(p.get("location", "") or "") == scene
                                         and (_utarget in str(p.get("name", "") or "") or str(p.get("name", "") or "") in _utarget)), None)
                            _tgt_owner = "props" if _tgt is not None else None
                        if _tgt is None:
                            _si = (scenes.get(scene) or {}).get("items") or []
                            _tgt = next((it for it in _si if isinstance(it, dict) and it.get("locked") and lock_mode_of(it) == "physical"
                                         and (_utarget in str(it.get("name", "") or "") or str(it.get("name", "") or "") in _utarget)), None)
                            _tgt_owner = "scene_items" if _tgt is not None else None
                        if _tgt is None:
                            sys_replies.append(f"附近没有能用{_uname}撬开的物理锁「{_utarget}」。")
                        else:
                            _tgt["locked"] = False
                            _tgt["lock_mode"] = ""
                            _tgt["key_name"] = ""
                            _tgt["bio_holder"] = ""
                            if _tgt_owner == "bag":
                                _full = str(desc_map.get(str(_tgt.get("name", "") or ""), "") or "").strip()
                                if _full:
                                    _tgt["desc"] = _full
                            _consume()
                            sys_replies.append(f"你用{_uname}撬开了「{_tgt.get('name')}」。")
                            ui_events.append({"type": "item_unlocked", "item": str(_tgt.get("name") or ""), "by_player": True})
                            _action_done = True
                # 2. 镇静类：让目标 NPC 情绪平息
                elif any(w in _u_text for w in ("安眠药", "镇静", "乙醚", "蒙汗药", "镇定剂")):
                    if not _utarget:
                        sys_replies.append(f"用法：!使用 {_uname} 对 目标NPC（安抚情绪）")
                    else:
                        _tnid, _tinfo = _find_npc(_utarget)
                        if _tnid is None:
                            sys_replies.append(f"这里没有叫「{_utarget}」的人。")
                        elif str(_tinfo.get("location", "") or "") != scene:
                            sys_replies.append(f"{_tinfo.get('name')} 不在当前场景。")
                        else:
                            _consume()
                            npcs_patch[_tnid] = {
                                "mood": {"current": "平静", "reason": f"被{_uname}安抚", "since_tick": int(state.get("world_tick") or 0)},
                                "emotion_tags": [],
                            }
                            sys_replies.append(f"你对{_tinfo.get('name')}使用了{_uname}，TA 的情绪平息了下来。")
                            ui_events.append({"type": "sedative_used", "npc_name": _tinfo.get("name"), "item": _uname})
                            _action_done = True
                # 3. 交给 NPC：普通物品拖到 NPC 身上 = 交给/送给（收礼按喜好动态好感）
                else:
                    if not _utarget:
                        sys_replies.append(f"「{_uname}」拖到 NPC 身上可交给 TA；撬锁类/安抚类道具才用 !使用 指令。")
                    else:
                        _tnid, _tinfo = _find_npc(_utarget)
                        if _tnid is None:
                            sys_replies.append(f"这里没有叫「{_utarget}」的人。")
                        elif str(_tinfo.get("location", "") or "") != scene:
                            sys_replies.append(f"{_tinfo.get('name')} 不在当前场景。")
                        else:
                            _consume()
                            _tn_name = str(_tinfo.get("name", "") or "").strip()
                            props.append({
                                "name": _uname, "holder": _tn_name, "carried": True,
                                "location": scene, "desc": _udesc,
                            })
                            # LLM 收礼反应 + 动态好感（喜欢+10~20 / 无感0 / 厌恶-5~-10）
                            _reply = ""
                            _aff_delta = 0
                            try:
                                _p = (
                                    f"你是{_tn_name}，性格{_tinfo.get('personality','')}，外貌{_tinfo.get('appearance','')}。"
                                    f"玩家把「{_uname}」（{_udesc}）交给了你。根据你对该物品的真实喜好给一句反应（40字内），"
                                    f"并在末尾用 [aff:数字] 表示好感变化：喜欢+10~20、无感0、厌恶-5~-10（收下≠必然涨好感）。"
                                )
                                _r = await actor_llm.ainvoke([HumanMessage(content=_p)])
                                _reply = (_r.content or "").strip()
                            except Exception:
                                _reply = ""
                            if _reply:
                                sys_replies.append(f"{_tn_name}：{_reply}")
                                _m = re.search(r"[\[【(（]\s*aff\s*[:：]\s*([+-]?\d+)\s*[\]】)）]", _reply)
                                if _m:
                                    _aff_delta = max(-40, min(40, int(_m.group(1))))
                            _cur_aff = int(_tinfo.get("affection", 100) or 100)
                            npcs_patch[_tnid] = {"affection": max(-100, min(500, _cur_aff + _aff_delta))}
                            sys_replies.append(f"「{_uname}」已交给{_tn_name}。")
                            ui_events.append({"type": "item_given", "item": _uname, "to_npc": _tn_name})
                            _act_type = "use"
                            _target = _tn_name
                            _action_done = True

    if is_move:
        target = None
        for prefix in ("!去", "！去"):
            if stripped_msg.lstrip("!！").startswith(prefix):
                target = stripped_msg.lstrip("!！")[len(prefix):].strip()
                break
        if target is None:
            m = re.match(r"^(?:请)?(?:去|进入|走进|回到|推开)\s*(.+)", stripped_msg.lstrip("!！"))
            target = m.group(1).strip() if m else stripped_msg.lstrip("!！")
        _act_type = "move"
        _target = target
        hit = _match_avail(target, avail)
        if hit:
            sdef = scenes.get(hit, {})
            _enter = False
            _opened_now = False
            if hit in opened:
                _enter = True
            elif sdef.get("locked"):
                # 门禁锁：bio=持有者 NPC 在场即可验证
                _missing = unlock_missing(sdef, new_items, props, npcs, scene)
                if _missing:
                    sys_replies.append(f"门锁着，还缺{'、'.join(_missing)}，无法进入 {hit}。")
                else:
                    opened.append(hit)
                    _enter = True
                    _opened_now = True
            else:
                _enter = True
            if _enter:
                new_scene = hit
                ui_events.append({"type": "scene_transition", "from_scene": scene, "to_scene": hit, "motion": "walk"})
                sys_replies.append(f"{'门已打开，你来到了' if _opened_now else '你来到了'} {hit}。")
                _scene_desc = str(sdef.get("desc", "") or "").strip()
                if _scene_desc:
                    sys_replies.append(_scene_desc)
                _action_done = True
        else:
            sys_replies.append(f"这里没有叫 {target} 的地方。可去：{'、'.join(avail) or '（无）'}")

    elif is_investigate:
        verb = "调查" if stripped_msg.lstrip("!！").startswith("调查") else "打开"
        loc = stripped_msg.lstrip("!！")[len(verb):].strip().strip(" ，,、")
        _act_type = "investigate"
        _target = loc

        if not loc:
            sys_replies.append("你想调查什么？输入「调查 物品名」或「调查 场景名」。")
        elif loc in avail and loc != scene:
            sys_replies.append(f"你不在 {loc}，没法调查那里。去其他场景请用「去 {loc}」。")
        elif loc in avail and loc == scene:
            loc = ""  # 当前场景全量调查

        if not sys_replies:
            # 1. 调查背包已有物品
            if loc and loc in _get_item_names(new_items):
                _target_item = next(
                    (it for it in new_items if (it.get("name") if isinstance(it, dict) else it) == loc), None
                )
                _locked = isinstance(_target_item, dict) and bool(_target_item.get("locked"))
                _d = str((_target_item.get("desc", "") if isinstance(_target_item, dict) else "") or "").strip()
                if not _d:
                    _d = str(desc_map.get(loc, "") or "").strip()
                if _locked:
                    # 玩家自己开背包物品：bio 部分必须由 bio_holder NPC 帮忙（玩家永远缺"本人验证"）
                    _missing = unlock_missing(_target_item, new_items, props, None, "")
                    if _missing:
                        sys_replies.append(f"「{loc}」还缺{'、'.join(_missing)}才能打开。")
                    else:
                        for _it in new_items:
                            if isinstance(_it, dict) and str(_it.get("name", "")).strip() == loc:
                                _it["locked"] = False
                                _full = str(desc_map.get(loc, "") or _d).strip()
                                _it["desc"] = _full
                                break
                        desc_map[loc] = _d
                        sys_replies.append(f"你打开了「{loc}」：{_d or '（里面是空的）'}")
                        _opens = _what_item_opens(loc, props, scenes)
                        if _opens:
                            sys_replies.append(f"「{loc}」可开启：{'、'.join(_opens)}")
                else:
                    sys_replies.append(f"「{loc}」：{_d or '用途不明'}")
                _action_done = True
            else:
                # 2. 调查场景物品（scenes.items + props 中未随身且在本场景的）
                _s_items = (scenes.get(scene) or {}).get("items", []) if isinstance((scenes.get(scene) or {}).get("items"), list) else []
                cand = []
                for it in _s_items:
                    if isinstance(it, dict) and it.get("name"):
                        _d = dict(it)
                        _d.setdefault("location", scene)
                        cand.append(_d)
                for p in props:
                    if isinstance(p, dict) and p.get("name") and not p.get("carried") and p.get("location") == scene:
                        cand.append(dict(p))
                # 去重
                _seen = set()
                cand2 = []
                for it in cand:
                    if it.get("name") in _seen:
                        continue
                    _seen.add(it.get("name"))
                    cand2.append(it)
                cand = cand2

                sel = [it for it in cand if it["name"] == loc] if loc else cand
                if loc and not sel:
                    sys_replies.append(f"这里没有 {loc}。")
                elif not sel:
                    sys_replies.append(f"你把{scene}翻了个遍，没有更多发现。")
                else:
                    found_any = False
                    for it in sel:
                        it_name = it["name"]
                        desc = str(it.get("desc", "") or "").strip()
                        if it_name in _get_item_names(new_items):
                            continue
                        status, detail = can_acquire(it, npcs, new_items, props, scene)
                        if status == "free":
                            _upsert_item(new_items, it_name, count=1, desc=desc)
                            _si = (scenes.get(scene) or {}).get("items", [])
                            if isinstance(_si, list):
                                scenes[scene]["items"] = [x for x in _si if not (isinstance(x, dict) and x.get("name") == it_name)]
                            for _p in list(props):
                                if _p.get("name") == it_name:
                                    props.remove(_p)
                            desc_map[it_name] = desc
                            sys_replies.append(f"你拿走了「{it_name}」。")
                            _opens = _what_item_opens(it_name, props, scenes)
                            if _opens:
                                sys_replies.append(f"「{it_name}」可开启：{'、'.join(_opens)}")
                            found_any = True
                        elif status == "locked":
                            _upsert_item(new_items, it_name, count=1, desc=unlock_hint(it))
                            _found_it = next((x for x in new_items if isinstance(x, dict) and x.get("name") == it_name), None)
                            if _found_it is not None:
                                _found_it["locked"] = True
                                _found_it["lock_mode"] = str(it.get("lock_mode") or "").strip()
                                _found_it["key_name"] = str(it.get("key_name") or "").strip()
                                _found_it["bio_holder"] = str(it.get("bio_holder") or "").strip()
                            _si = (scenes.get(scene) or {}).get("items", [])
                            if isinstance(_si, list):
                                scenes[scene]["items"] = [x for x in _si if not (isinstance(x, dict) and x.get("name") == it_name)]
                            for _p in list(props):
                                if _p.get("name") == it_name:
                                    props.remove(_p)
                            desc_map[it_name] = desc
                            sys_replies.append(f"你拿走了「{it_name}」，{unlock_hint(it)}。")
                            found_any = True
                        elif status == "npc_present":
                            sys_replies.append(f"「{it_name}」被{detail}盯着，需要先把{detail}支走再拿。")
                            found_any = True
                        elif status == "unknown":
                            sys_replies.append(detail if detail else f"这里没有 {it_name}。")
                            found_any = True
                    if not found_any and loc:
                        sys_replies.append(f"你把{scene}翻了个遍，没有更多发现。")
                    if found_any:
                        _action_done = True

    if not sys_replies:
        sys_replies.append(f"你把{scene}翻了个遍，没有更多发现。")

    return {
        "current_scene": new_scene,
        "player_items": new_items,
        "props": props,
        "item_desc_map": desc_map,
        "opened_scenes": opened,
        "scenes": scenes,
        "ui_events": ui_events,
        "npcs": npcs_patch if npcs_patch else {},
        "last_player_action": {"action": _act_type, "target": _target, "scene": scene} if _action_done else None,
        "active_targets": [],
        "history": [
            {"speaker": "user", "content": msg},
            {"speaker": "system", "content": f"【系统】{' '.join(sys_replies)}"},
        ],
    }
