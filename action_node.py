"""动作引擎节点：action_engine —— 指令集 !去 / 调查 / 开锁。"""
import copy
import json
import re

from state import AgentState
from utils import _parse_content, _resolve_sub_desc
from item_utils import can_acquire, split_keys, player_has_key, key_name_of, key_owner_npc
from utils import _get_item_names, _upsert_item, handle_container_open


def _need_keys(scene_def: dict) -> list:
    rk = (scene_def or {}).get("requires_key", "") or ""
    if not rk:
        return []
    return [k.strip() for k in re.split(r"[+＋]", rk) if k.strip()]


def _key_available(key, new_items, props):
    return player_has_key(key, new_items, props)


def _match_avail(target: str, avail: list) -> str | None:
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


async def action_engine(state: AgentState) -> dict:
    msg = state.get("user_msg", "")
    scene = state.get("current_scene", "未知")
    scenes = state.get("scenes") or {}
    items = copy.deepcopy(state.get("player_items") or [])
    sys_replies = []
    ui_events = []  # 2D frontend UI events
    new_items = copy.deepcopy(items)
    new_scene = scene
    avail = state.get("available_scenes", [])
    opened = list(state.get("opened_scenes") or [])
    props = [dict(p) for p in (state.get("props") or [])]
    desc_map = dict(state.get("item_desc_map") or {})
    npcs = state.get("npcs") or {}

    if (msg.lstrip().startswith(("!去", "！去"))
            or msg.lstrip().startswith(("去", "进入", "走进", "回到", "推开"))):
        stripped = msg.lstrip().lstrip("!！").strip()
        target = None
        for prefix in ("!去", "！去"):
            if stripped.startswith(prefix):
                target = stripped[len(prefix):].strip()
                break
        if target is None:
            m = re.match(r"^(?:请)?(?:去|进入|走进|回到|推开)\s*(.+)", stripped)
            target = m.group(1).strip() if m else stripped
        hit = _match_avail(target, avail)
        if hit:
            need = _need_keys(scenes.get(hit, {}))
            if hit in opened:
                need = []
            missing = [k for k in need if not _key_available(k, new_items, props)]
            if missing:
                sys_replies.append(f"门锁着，还缺{'、'.join(missing)}，无法进入 {hit}。")
            else:
                new_scene = hit
                if hit not in opened and need:
                    opened.append(hit)
                ui_events.append({"type": "scene_transition", "from_scene": scene, "to_scene": hit, "motion": "walk"})  # 2D frontend
                prefix = "门已打开，" if hit in opened else "你推开门，"
                sys_replies.append(f"{prefix}来到了 {hit}。")
                _scene_desc = str((scenes.get(hit) or {}).get("desc", "") or "").strip()
                if _scene_desc:
                    sys_replies.append(_scene_desc)
        else:
            sys_replies.append(f"这里没有叫 {target} 的地方。可去：{'、'.join(avail) or '（无）'}")

    elif msg.lstrip().startswith(("!", "！")) and msg.lstrip().lstrip("!！").startswith(("调查", "打开")):
        stripped = msg.lstrip().lstrip("!！").strip()
        verb = "调查" if stripped.startswith("调查") else "打开"
        loc = stripped[len(verb):].strip().strip(" ，,、")
        
        if verb == "调查":
            if not loc:
                sys_replies.append("你想调查什么？输入「调查 场景名」或「调查 物品名」。")
                return {
                    "current_scene": new_scene,
                    "player_items": new_items,
                    "props": props,
                    "item_desc_map": desc_map,
                    "opened_scenes": opened,
                    "ui_events": ui_events,
                    "history": [{"speaker": "system", "content": f"【系统】{' '.join(sys_replies)}"}],
                }
            
# 背包物品直接调查（不依赖场景/ props 候选）
            if loc in _get_item_names(new_items):
                # 兼容从场景/父容器捡起的物品：如果 player_items 里有 content 字段，但 desc_map 里没占位符，则动态补充
                _target_item = next((it for it in new_items if (it.get("name") if isinstance(it, dict) else it) == loc), None)
                if isinstance(_target_item, dict) and _target_item.get("content"):
                    if f"__content__:{loc}" not in desc_map:
                        desc_map[f"__content__:{loc}"] = json.dumps(_parse_content(_target_item.get("content")), ensure_ascii=False)

                _d = str(desc_map.get(loc, "") or "")
                if "（锁着，需要" in _d:
                    _m = re.search(r"（锁着，需要(.+?)）", _d)
                    _lock = _m.group(1) if _m else ""
                    _missing = [k for k in split_keys(_lock) if not player_has_key(k, new_items, props)]
                    if _missing:
                        sys_replies.append(f"「{loc}」还缺{'、'.join(_missing)}才能打开。")
                    else:
                        _subs = handle_container_open(loc, desc_map, props, new_items, scenes)
                        ui_events.append({"type": "container_opened", "container": loc, "revealed_items": list(_subs)})
                        sys_replies.append(f"你打开了「{loc}」，子物品是：{'、'.join(_subs) or '（空）'}。")
                elif f"__agreed__:{loc}" in desc_map:
                    _m = re.search(r"还缺(.+?)(?:）|$)", _d)
                    _rem = _m.group(1) if _m else ""
                    _rem_keys = [k.strip() for k in re.split(r"[+＋、,，]", _rem) if k.strip()]
                    _missing = [k for k in _rem_keys if not player_has_key(k, new_items, props) and key_owner_npc(k, props) is None]
                    if _missing:
                        sys_replies.append(f"「{loc}」还缺{'、'.join(_missing)}才能打开。")
                    else:
                        _subs = handle_container_open(loc, desc_map, props, new_items, scenes)
                        ui_events.append({"type": "container_opened", "container": loc, "revealed_items": list(_subs)})
                        sys_replies.append(f"你打开了「{loc}」，子物品是：{'、'.join(_subs) or '（空）'}。")
                elif f"__content__:{loc}" in desc_map:
                    # 未锁容器：在背包内直接展开
                    _subs = handle_container_open(loc, desc_map, props, new_items, scenes)
                    ui_events.append({"type": "container_opened", "container": loc, "revealed_items": list(_subs)})
                    sys_replies.append(f"你打开了「{loc}」，子物品是：{'、'.join(_subs) or '（空）'}。")
                else:
                    sys_replies.append(f"「{loc}」：{_d or '用途不明'}")
                return {
                    "current_scene": new_scene,
                    "player_items": new_items,
                    "props": props,
                    "item_desc_map": desc_map,
                    "opened_scenes": opened,
                    "ui_events": ui_events,
                    "history": [{"speaker": "system", "content": f"【系统】{' '.join(sys_replies)}"}],
                }
                
            hit = loc if loc in avail else None
            if hit:
                if hit != scene:
                    sys_replies.append(f"你不在 {hit}，没法调查那里。去其他场景请用「去 {hit}」。")
                    return {
                        "current_scene": new_scene,
                        "player_items": new_items,
                        "props": props,
                        "item_desc_map": desc_map,
                        "opened_scenes": opened,
                        "ui_events": ui_events,
                        "history": [{"speaker": "system", "content": f"【系统】{' '.join(sys_replies)}"}],
                    }
                loc = ""  # 当前场景名 → 全量调查

        sdef = scenes.get(scene, {})
        s_items = sdef.get("items", []) if isinstance(sdef.get("items"), list) else []

        def _loc_match(loc, sc):
            return bool(loc) and loc == sc

        cand = [(it, False) for it in s_items if isinstance(it, dict) and it.get("name")]
        for p in props:
            if not (isinstance(p, dict) and p.get("name")):
                continue
            # 强制修复随身物品的 location 漂移
            if p.get("carried") and p.get("holder") and p.get("holder") not in ("玩家", "玩家身上"):
                _owner_scene = next((info.get("location") for info in npcs.values() if info.get("name") == p.get("holder")), "")
                if _owner_scene:
                    p["location"] = _owner_scene
            
            if p.get("content"):
                if _loc_match(p.get("location"), scene) or p.get("holder") == "玩家":
                    cand.append((p, True))
            else:
                if _loc_match(p.get("location"), scene) and p.get("holder") not in ("玩家", "玩家身上"):
                    cand.append((p, True))
        sel = [(it, is_p) for it, is_p in cand if it["name"] == loc] if loc else cand
        
        if loc and loc != scene and not sel:
            sys_replies.append(f"这里没有 {loc}。调查只能查看当前场景；去其他场景请用「去 {loc}」。" if not _match_avail(loc, avail)
                               else f"你不在 {loc}，没法调查那里。去其他场景请用「去 {loc}」。")
        elif not sel:
            sys_replies.append(f"你把{scene}翻了个遍，没有更多发现。")
        else:
            if not loc:
                # 全场景调查
                found_any = False
                for it, is_prop in sel:
                    it_name = it["name"]
                    if it_name in _get_item_names(new_items):
                        continue
                    desc = str(it.get("desc", "") or "").strip()
                    if not is_prop:
                        it = dict(it)
                        it["location"] = scene
                        it["holder"] = it.get("holder") or ""
                        it["carried"] = False
                    parsed_content = _parse_content(it.get("content"))
                    status, detail = can_acquire(it, npcs, new_items, props, scene)
                    
                    if status == "free":
                        _upsert_item(new_items, it_name, count=1)
                        if not is_prop:
                            _si = (scenes.get(scene) or {}).get("items", [])
                            if isinstance(_si, list):
                                scenes[scene]["items"] = [x for x in _si if not (isinstance(x, dict) and x.get("name") == it_name)]
                        # 新增：如果物品在 props 里，也要从 props 转移走，防止残留
                        for _p in list(props):
                            if _p.get("name") == it_name:
                                props.remove(_p)
                        _desc_out = desc
                        if it.get("locked"):
                            _desc_out = re.sub(r"[，,。]?\s*(上了锁|需要钥匙|锁着|需钥匙)[^，,。]*", "", desc).strip()
                        if parsed_content:
                            for c in parsed_content:
                                _cname = c.get("name", "")
                                _ctext = c.get("text", "")
                                if ("：" in _cname or ":" in _cname) and len(_cname) > 10 and not _ctext:
                                    _ctext = _cname
                                    _cname = f"{it_name}内容"
                                if _cname not in _get_item_names(new_items):
                                    _upsert_item(new_items, _cname, count=1)
                                _full_desc = _ctext if _ctext else desc_map.get(_cname, "")
                                if _full_desc:
                                    desc_map[_cname] = _full_desc
                                _cur_desc = str(desc_map.get(_cname, "") or "")
                                if (not _cur_desc) or (_cur_desc.startswith("在") and "内部" in _cur_desc):
                                    _sp = next((pp for pp in props if pp.get("name") == _cname), None)
                                    if _sp and _sp.get("desc"):
                                        desc_map[_cname] = str(_sp.get("desc")).strip()
                                for _pp in props:
                                    if _pp.get("name") == _cname:
                                        _pp.pop("container", None)
                            desc_map[it_name] = f"{_desc_out}（已开启）"
                            _sub_names = [c.get("name", "") for c in parsed_content]
                            _holder = it.get("holder", "")
                            _is_npc_item = _holder and _holder not in ("玩家", "玩家身上", "")
                            if _is_npc_item:
                                sys_replies.append(f"你拿走了「{it_name}」，这是{_holder}的东西，里面是：{'、'.join(_sub_names)}。")
                            else:
                                sys_replies.append(f"你拿走了「{it_name}」，里面是：{'、'.join(_sub_names)}。")
                        else:
                            desc_map[it_name] = _desc_out
                            _holder = it.get("holder", "")
                            _is_npc_item = _holder and _holder not in ("玩家", "玩家身上", "")
                            if _is_npc_item:
                                sys_replies.append(f"你拿走了「{it_name}」，这是{_holder}的东西。")
                            else:
                                sys_replies.append(f"你拿走了「{it_name}」。")
                        
                        _opens = []
                        for _x in list(props) + [i for _s in scenes.values() for i in (_s.get("items") or []) if isinstance(i, dict)]:
                            if _x.get("locked") and it_name in split_keys(key_name_of(_x)):
                                _opens.append(_x.get("name"))
                        for _sn, _sd in scenes.items():
                            if _sn not in opened and it_name in _need_keys(_sd):
                                _opens.append(f"{_sn}（门）")
                        if _opens:
                            sys_replies.append(f"「{it_name}」可开启：{'、'.join(_opens)}")
                        found_any = True
                        
                    elif status == "locked":
                        keys = split_keys(detail)
                        missing_normal = [k for k in keys if not player_has_key(k, new_items, props) and key_owner_npc(k, props) is None]
                        missing_npc = [(k, key_owner_npc(k, props)) for k in keys if key_owner_npc(k, props) is not None]
                        if missing_npc and missing_normal:
                            sys_replies.append(f"「{it_name}」锁着，还缺{'、'.join(missing_normal)}；且{'、'.join(o for _, o in missing_npc)}的凭证需本人同意（用 !@N 给我 {it_name}）")
                            found_any = True
                        elif missing_npc and not missing_normal:
                            owner = missing_npc[0][1]
                            sys_replies.append(f"「{it_name}」需要{owner}的凭证锁，需{owner}本人同意才能打开（用 !@N 给我 {it_name}）")
                            found_any = True
                        else:
                            _upsert_item(new_items, it_name, count=1)
                            if not is_prop:
                                _si = (scenes.get(scene) or {}).get("items", [])
                                if isinstance(_si, list):
                                    scenes[scene]["items"] = [x for x in _si if not (isinstance(x, dict) and x.get("name") == it_name)]
                            desc_map[it_name] = f"{desc}（锁着，需要{detail}）"
                            desc_map[f"__content__:{it_name}"] = json.dumps(parsed_content, ensure_ascii=False)
                            for _c in (parsed_content or []):
                                _cn = _c.get("name") if isinstance(_c, dict) else str(_c)
                                _cn = str(_cn or "").strip()
                                if not _cn:
                                    continue
                                _rd = _resolve_sub_desc(_cn, it_name, props, scenes, desc_map)
                                if _rd:
                                    desc_map[_cn] = _rd
                                else:
                                    _ph = desc_map.get(_cn, "")
                                    if isinstance(_ph, str) and "内部" in _ph:
                                        desc_map.pop(_cn, None)
                                    for _it in new_items:
                                        if isinstance(_it, dict) and str(_it.get("name", "")).strip() == _cn:
                                            _d0 = str(_it.get("desc", "") or "")
                                            if "内部" in _d0:
                                                _it["desc"] = ""
                            sys_replies.append(f"你发现了「{it_name}」，它锁着，还缺{detail}才能打开。")
                            found_any = True
                            
                    elif status == "npc_present":
                        # 如果已经在当前场景，且该物品是开启此场景的钥匙，则跳过提示
                        is_key_for_current = it_name in _need_keys(scenes.get(scene, {}))
                        if is_key_for_current:
                            continue
                        if it.get("carried"):
                            sys_replies.append(f"「{it_name}」在{detail}身上（随身携带），需要向 TA 索要或偷取。")
                        else:
                            sys_replies.append(f"「{it_name}」在场景中，但{detail}正在旁边盯着，你需要先支开 TA，或尝试偷取。")
                        found_any = True
                    elif status == "unknown":
                        sys_replies.append(detail if detail else f"这里没有 {it_name}。")
                        found_any = True
                if not sys_replies:
                    sys_replies.append("【系统】这里什么都没有。")
                if not found_any and "【系统】这里什么都没有。" not in sys_replies:
                    sys_replies.append(f"你把{scene}翻了个遍，没有更多发现。")
            else:
                # 单物品调查
                found_any = False
                for it, is_prop in sel:
                    if not is_prop:
                        it = dict(it)
                        it["location"] = scene
                        it["holder"] = it.get("holder") or ""
                        it["carried"] = False
                    it_name = it["name"]
                    desc = str(it.get("desc", "") or "").strip()
                    parsed_content = _parse_content(it.get("content"))
                    
                    if it_name in _get_item_names(new_items) and "（锁着，需要" not in str(desc_map.get(it_name, "")):
                        continue
                    if it_name in _get_item_names(new_items) and f"__agreed__:{it_name}" in desc_map:
                        rem_str = ""
                        m = re.search(r"还缺(.+?)(?:）|$)", desc_map.get(it_name, ""))
                        if m:
                            rem_str = m.group(1)
                        rem_keys = [k.strip() for k in re.split(r"[+＋、,，]", rem_str) if k.strip()]
                        missing = [k for k in rem_keys if not player_has_key(k, new_items, props) and key_owner_npc(k, props) is None]
                        if not missing:
                            subs = handle_container_open(it_name, desc_map, props, new_items, scenes)
                            ui_events.append({"type": "container_opened", "container": it_name, "revealed_items": list(subs)})  # 2D frontend
                            sys_replies.append(f"你打开了「{it_name}」，子物品是：{'、'.join(subs) or '（空）'}。")
                            found_any = True
                            continue
                        else:
                            sys_replies.append(f"「{it_name}」还缺{'、'.join(missing)}才能打开。")
                            found_any = True
                            continue
                            
                    if it_name in _get_item_names(new_items) and "（锁着，需要" in str(desc_map.get(it_name, "")):
                        m = re.search(r"（锁着，需要(.+?)）", desc_map.get(it_name, ""))
                        lock_name = m.group(1) if m else ""
                        keys = split_keys(lock_name)
                        missing = [k for k in keys if not player_has_key(k, new_items, props)]
                        if missing:
                            sys_replies.append(f"「{it_name}」还缺{'、'.join(missing)}才能打开。")
                            found_any = True
                            continue
                        subs = handle_container_open(it_name, desc_map, props, new_items, scenes)
                        sys_replies.append(f"你打开了「{it_name}」，子物品是：{'、'.join(subs) or '（空）'}。")
                        found_any = True
                        continue
                        
                    status, detail = can_acquire(it, npcs, new_items, props, scene)
                    if status == "free":
                        _upsert_item(new_items, it_name, count=1)
                        if not is_prop:
                            _si = (scenes.get(scene) or {}).get("items", [])
                            if isinstance(_si, list):
                                scenes[scene]["items"] = [x for x in _si if not (isinstance(x, dict) and x.get("name") == it_name)]
                        # 新增：如果物品在 props 里，也要从 props 转移走，防止残留
                        for _p in list(props):
                            if _p.get("name") == it_name:
                                props.remove(_p)
                        _desc_out = desc
                        if it.get("locked"):
                            _desc_out = re.sub(r"[，,。]?\s*(上了锁|需要钥匙|锁着|需钥匙)[^，,。]*", "", desc).strip()
                        if parsed_content:
                            for c in parsed_content:
                                _cname = c.get("name", "")
                                _ctext = c.get("text", "")
                                if ("：" in _cname or ":" in _cname) and len(_cname) > 10 and not _ctext:
                                    _ctext = _cname
                                    _cname = f"{it_name}内容"
                                if _cname not in _get_item_names(new_items):
                                    _upsert_item(new_items, _cname, count=1)
                                _full_desc = _ctext if _ctext else desc_map.get(_cname, "")
                                if _full_desc:
                                    desc_map[_cname] = _full_desc
                                _cur_desc = str(desc_map.get(_cname, "") or "")
                                if (not _cur_desc) or (_cur_desc.startswith("在") and "内部" in _cur_desc):
                                    _sp = next((pp for pp in props if pp.get("name") == _cname), None)
                                    if _sp and _sp.get("desc"):
                                        desc_map[_cname] = str(_sp.get("desc")).strip()
                                for _pp in props:
                                    if _pp.get("name") == _cname:
                                        _pp.pop("container", None)
                            desc_map[it_name] = f"{_desc_out}（已开启）"
                            _sub_names = [c.get("name", "") for c in parsed_content]
                            _holder = it.get("holder", "")
                            _is_npc_item = _holder and _holder not in ("玩家", "玩家身上", "")
                            if _is_npc_item:
                                sys_replies.append(f"你拿走了「{it_name}」，这是{_holder}的东西，里面是：{'、'.join(_sub_names)}。")
                            else:
                                sys_replies.append(f"你拿走了「{it_name}」，里面是：{'、'.join(_sub_names)}。")
                        else:
                            desc_map[it_name] = _desc_out
                            _holder = it.get("holder", "")
                            _is_npc_item = _holder and _holder not in ("玩家", "玩家身上", "")
                            if _is_npc_item:
                                sys_replies.append(f"你拿走了「{it_name}」，这是{_holder}的东西。")
                            else:
                                sys_replies.append(f"你拿走了「{it_name}」。")
                        
                        _opens = []
                        for _x in list(props) + [i for _s in scenes.values() for i in (_s.get("items") or []) if isinstance(i, dict)]:
                            if _x.get("locked") and it_name in split_keys(key_name_of(_x)):
                                _opens.append(_x.get("name"))
                        for _sn, _sd in scenes.items():
                            if _sn not in opened and it_name in _need_keys(_sd):
                                _opens.append(f"{_sn}（门）")
                        if _opens:
                            sys_replies.append(f"「{it_name}」可开启：{'、'.join(_opens)}")
                        found_any = True
                        
                    elif status == "locked":
                        keys = split_keys(detail)
                        missing_normal = [k for k in keys if not player_has_key(k, new_items, props) and key_owner_npc(k, props) is None]
                        missing_npc = [(k, key_owner_npc(k, props)) for k in keys if key_owner_npc(k, props) is not None]
                        if missing_npc and missing_normal:
                            sys_replies.append(f"「{it_name}」锁着，还缺{'、'.join(missing_normal)}；且{'、'.join(o for _, o in missing_npc)}的凭证需本人同意（用 !@N 给我 {it_name}）")
                            found_any = True
                        elif missing_npc and not missing_normal:
                            owner = missing_npc[0][1]
                            sys_replies.append(f"「{it_name}」需要{owner}的凭证锁，需{owner}本人同意才能打开（用 !@N 给我 {it_name}）")
                            found_any = True
                        else:
                            _upsert_item(new_items, it_name, count=1)
                            if not is_prop:
                                _si = (scenes.get(scene) or {}).get("items", [])
                                if isinstance(_si, list):
                                    scenes[scene]["items"] = [x for x in _si if not (isinstance(x, dict) and x.get("name") == it_name)]
                            desc_map[it_name] = f"{desc}（锁着，需要{detail}）"
                            desc_map[f"__content__:{it_name}"] = json.dumps(parsed_content, ensure_ascii=False)
                            for _c in (parsed_content or []):
                                _cn = _c.get("name") if isinstance(_c, dict) else str(_c)
                                _cn = str(_cn or "").strip()
                                if not _cn:
                                    continue
                                _rd = _resolve_sub_desc(_cn, it_name, props, scenes, desc_map)
                                if _rd:
                                    desc_map[_cn] = _rd
                                else:
                                    _ph = desc_map.get(_cn, "")
                                    if isinstance(_ph, str) and "内部" in _ph:
                                        desc_map.pop(_cn, None)
                                    for _it in new_items:
                                        if isinstance(_it, dict) and str(_it.get("name", "")).strip() == _cn:
                                            _d0 = str(_it.get("desc", "") or "")
                                            if "内部" in _d0:
                                                _it["desc"] = ""
                            sys_replies.append(f"你发现了「{it_name}」，它锁着，还缺{detail}才能打开。")
                            found_any = True
                            
                    elif status == "npc_present":
                        if it.get("carried"):
                            sys_replies.append(f"「{it_name}」在{detail}身上（随身携带），需要向 TA 索要或偷取。")
                        else:
                            sys_replies.append(f"「{it_name}」在场景中，但{detail}正在旁边盯着，你需要先支开 TA，或尝试偷取。")
                        found_any = True
                    elif status == "unknown":
                        sys_replies.append(detail if detail else f"这里没有 {it_name}。")
                        found_any = True
                if not found_any:
                    sys_replies.append(f"你把{scene}翻了个遍，没有更多发现。")

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
        "history": [{"speaker": "system", "content": f"【系统】{' '.join(sys_replies)}"}],
    }