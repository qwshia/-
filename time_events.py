"""时间事件执行器：apply_time_events —— 按 at_phase 触发剧本预设的世界变化。

设计：
- event 级前置条件（condition），任一条件不成立则整个事件跳过（包括场景描述）；
- 条件成立时执行 effects（npc_move / item_move），全量替换目标场景描述，写 world_events 客观记录 + history 模糊感知；
- 已触发的事件记入 fired_time_events，永不重复；
- 导演未设 time_limit 时，timed_events 必为空（world.py 已清空），本函数直接返回 {}。
"""
import copy

from state import AgentState
from utils import TIME_PHASES


def _phase_forward_dist(start: str, target: str) -> int:
    if start not in TIME_PHASES or target not in TIME_PHASES:
        return -1
    n = len(TIME_PHASES)
    si = TIME_PHASES.index(start)
    ti = TIME_PHASES.index(target)
    return (ti - si) % n


def _check_condition(cond: dict, state: AgentState) -> tuple[bool, str]:
    """返回 (是否满足, 不满足的原因简述)"""
    if not isinstance(cond, dict):
        return True, ""
    npcs = state.get("npcs") or {}
    props = state.get("props") or []
    npc_id = str(cond.get("npc_id", "") or "").strip()
    at_scene = str(cond.get("at_scene", "") or "").strip()
    has_items = cond.get("has_items") if isinstance(cond.get("has_items"), list) else []
    has_items = [str(x).strip() for x in has_items if str(x).strip()]

    if npc_id:
        info = npcs.get(npc_id)
        if not info:
            return False, f"NPC {npc_id} 不存在"
        if at_scene:
            if str(info.get("location", "")).strip() != at_scene:
                return False, f"{info.get('name', npc_id)} 不在 {at_scene}"
        if has_items:
            npc_name = str(info.get("name", "")).strip()
            held = {p.get("name") for p in props if isinstance(p, dict) and p.get("holder") == npc_name}
            missing = [it for it in has_items if it not in held]
            if missing:
                return False, f"{npc_name} 未携带 {'、'.join(missing)}"
    elif has_items:
        return False, "无 npc_id 时 has_items 无意义"

    return True, ""


def apply_time_events(state: AgentState, old_progress: float, new_progress: float) -> dict:
    timed_events = state.get("timed_events") or []
    if not timed_events:
        return {}

    fired = list(state.get("fired_time_events") or [])
    time_start = state.get("time_start", "清晨")
    npcs = state.get("npcs") or {}
    props = state.get("props") or []
    scenes = state.get("scenes") or {}

    new_fired = []
    sys_msgs = []
    world_evt_msgs = []
    npcs_patch = {}
    new_scenes = {}
    new_props = copy.deepcopy(props)
    props_changed = False

    sorted_evts = sorted(
        timed_events,
        key=lambda e: _phase_forward_dist(time_start, str((e or {}).get("at_phase", "") or "")),
    )

    for ev in sorted_evts:
        if not isinstance(ev, dict):
            continue
        evt_id = str(ev.get("id", "") or "").strip()
        if not evt_id or evt_id in fired:
            continue
        at_phase = str(ev.get("at_phase", "") or "").strip()
        target = _phase_forward_dist(time_start, at_phase)
        if target < 0:
            continue
        # 时间门槛：本轮首次跨过 at_phase（用 1e-6 epsilon 容浮点累积误差）
        if not (old_progress + 1e-6 < target and new_progress + 1e-6 >= target):
            continue

        ok, reason = _check_condition(ev.get("condition") or {}, state)
        if not ok:
            _fail_scene = str(ev.get("scene", "") or "").strip()
            _fail_desc = str(ev.get("new_desc_failed", "") or "").strip()
            if _fail_scene in scenes and _fail_desc:
                new_scenes[_fail_scene] = _fail_desc
            world_evt_msgs.append(f"[{at_phase}] {reason}，计划未发生")
            new_fired.append(evt_id)
            continue

        ev_effects = ev.get("effects", []) if isinstance(ev.get("effects"), list) else []
        ev_applied_effects = []
        for ef in ev_effects:
            if not isinstance(ef, dict):
                continue
            etype = str(ef.get("type", "") or "").strip()
            if etype == "npc_move":
                _nid = str(ef.get("npc_id", "") or "").strip()
                _to = str(ef.get("to", "") or "").strip()
                if _nid in npcs and _to in scenes:
                    npcs_patch.setdefault(_nid, {})["location"] = _to
                    _nname = (npcs.get(_nid) or {}).get("name", _nid)
                    ev_applied_effects.append(f"{_nname}去了{_to}")
            elif etype == "item_move":
                _item = str(ef.get("item", "") or "").strip()
                _to = str(ef.get("to", "") or "").strip()
                _found = None
                for p in new_props:
                    if isinstance(p, dict) and p.get("name") == _item:
                        _found = p
                        break
                if _found is None:
                    continue
                if _to.startswith("npc:"):
                    _owner = _to[4:].strip()
                    _found["holder"] = _owner
                    _found["carried"] = True
                    _loc = ""
                    for _nid2, _ninfo2 in npcs.items():
                        if _ninfo2.get("name") == _owner:
                            _loc = npcs_patch.get(_nid2, {}).get("location") or _ninfo2.get("location", "")
                            break
                    if _loc:
                        _found["location"] = _loc
                    ev_applied_effects.append(f"「{_item}」到了{_owner}手中")
                    props_changed = True
                elif _to.startswith("scene:"):
                    _scene_name = _to[6:].strip()
                    _found["holder"] = ""
                    _found["carried"] = False
                    _found["location"] = _scene_name
                    ev_applied_effects.append(f"「{_item}」出现在{_scene_name}")
                    props_changed = True

        _scene_key = str(ev.get("scene", "") or "").strip()
        _new_desc = str(ev.get("new_desc", "") or "").strip()
        if _scene_key in scenes and _new_desc:
            new_scenes[_scene_key] = _new_desc

        ev_hint = str(ev.get("hint", "") or "").strip()
        _effect_summary = "；".join(ev_applied_effects) if ev_applied_effects else "环境悄然变化"
        world_evt_msgs.append(f"[{at_phase}] {_scene_key}：{_effect_summary}")
        if ev_hint:
            sys_msgs.append(ev_hint)
        new_fired.append(evt_id)

    out = {}
    if new_fired:
        out["fired_time_events"] = new_fired
    if npcs_patch:
        out["npcs"] = npcs_patch
    if new_scenes:
        merged_scenes = dict(scenes)
        for sk, nd in new_scenes.items():
            merged_scenes[sk] = dict(merged_scenes.get(sk) or {})
            merged_scenes[sk]["desc"] = nd
        out["scenes"] = merged_scenes
    if props_changed:
        out["props"] = new_props
    if world_evt_msgs:
        out["world_events"] = world_evt_msgs
    if sys_msgs:
        out["history"] = [{"speaker": "system", "content": m} for m in sys_msgs]
    return out