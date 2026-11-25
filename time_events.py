"""时间事件执行器：apply_time_events —— 按 at_phase 触发剧本预设的世界变化。

设计：
- event 级前置条件（condition），任一条件不成立则整个事件跳过（包括场景描述）；
- 条件成立时执行 effects（npc_move / item_move），全量替换目标场景描述，写 world_events 客观记录 + history 模糊感知；
- 已触发的事件记入 fired_time_events，永不重复；
- 危机事件：带 crisis 字段的事件在 condition 不满足时挂起为 active_crisis，
  玩家在 deadline_phase 结束前满足 condition 即可解除，否则危机爆发（执行 fail_effects）；
- 导演未设 time_limit 时，timed_events 必为空（world.py 已清空），本函数直接返回 {}。
"""
import copy

from state import AgentState
from utils import phase_display
from constants import VALID_TIME_PHASES as TIME_PHASES


def _phase_forward_dist(start: str, target: str) -> int:
    if start not in TIME_PHASES or target not in TIME_PHASES:
        return -1
    n = len(TIME_PHASES)
    si = TIME_PHASES.index(start)
    ti = TIME_PHASES.index(target)
    return (ti - si) % n


def _check_condition(cond: dict, state: AgentState) -> tuple[bool, str]:
    """返回 (是否满足, 不满足的原因简述)。支持 npc_id/at_scene/has_items/player_has_items/npc_goal_contains。"""
    if not isinstance(cond, dict):
        return True, ""
    npcs = state.get("npcs") or {}
    props = state.get("props") or []
    player_items = state.get("player_items") or []
    npc_id = str(cond.get("npc_id", "") or "").strip()
    at_scene = str(cond.get("at_scene", "") or "").strip()
    has_items = cond.get("has_items") if isinstance(cond.get("has_items"), list) else []
    has_items = [str(x).strip() for x in has_items if str(x).strip()]
    player_has_items = cond.get("player_has_items") if isinstance(cond.get("player_has_items"), list) else []
    player_has_items = [str(x).strip() for x in player_has_items if str(x).strip()]
    npc_goal_contains = str(cond.get("npc_goal_contains", "") or "").strip()

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
        if npc_goal_contains:
            _stg = info.get("short_term_plan") if isinstance(info.get("short_term_plan"), dict) else (info.get("short_term_goal") if isinstance(info.get("short_term_goal"), dict) else {})
            _cg = info.get("core_drive") if isinstance(info.get("core_drive"), dict) else (info.get("core_goal") if isinstance(info.get("core_goal"), dict) else {})
            _goal_txt = str(_stg.get("goal", "") or "") + " " + str(_cg.get("goal", "") or "")
            if npc_goal_contains not in _goal_txt:
                return False, f"{info.get('name', npc_id)} 当前目标不含「{npc_goal_contains}」"
    else:
        if has_items:
            return False, "无 npc_id 时 has_items 无意义"
        if npc_goal_contains:
            return False, "无 npc_id 时 npc_goal_contains 无意义"

    if player_has_items:
        held_names = {str(it.get("name", "") or "").strip() for it in player_items if isinstance(it, dict)}
        held_names |= {str(it).strip() for it in player_items if not isinstance(it, dict)}
        missing = [it for it in player_has_items if it not in held_names]
        if missing:
            return False, f"玩家未持有 {'、'.join(missing)}"

    return True, ""


def _apply_effects(ev_effects: list, npcs: dict, npcs_patch: dict, new_props: list, scenes: dict) -> tuple[list, bool]:
    """执行 npc_move / item_move 效果。返回 (已执行效果描述列表, props 是否变化)。"""
    applied = []
    changed = False
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
                applied.append(f"{_nname}去了{_to}")
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
                applied.append(f"「{_item}」到了{_owner}手中")
                changed = True
            elif _to.startswith("scene:"):
                _scene_name = _to[6:].strip()
                _found["holder"] = ""
                _found["carried"] = False
                _found["location"] = _scene_name
                applied.append(f"「{_item}」出现在{_scene_name}")
                changed = True
    return applied, changed


def apply_time_events(state: AgentState, old_progress: float, new_progress: float) -> dict:
    timed_events = state.get("timed_events") or []
    active_crisis = state.get("active_crisis")

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
    pending_crisis = None
    crisis_cleared = False

    # ==== 进行中危机的检查（每次时间推进都查）====
    if active_crisis and isinstance(active_crisis, dict) and active_crisis.get("id"):
        _c_cond = active_crisis.get("condition") or {}
        _ok_now, _why_now = _check_condition(_c_cond, state)
        _deadline = str(active_crisis.get("deadline_phase", "") or "").strip()
        if _ok_now:
            new_fired.append(str(active_crisis.get("id", "")))
            world_evt_msgs.append(f"【危机解除】{active_crisis.get('name', '危机')}：威胁已消除，局面暂时安全。")
            crisis_cleared = True
        else:
            _dd = _phase_forward_dist(time_start, _deadline)
            if _deadline and _dd >= 0 and (new_progress + 1e-6) >= _dd + 1:
                # 危机爆发：执行 fail_effects
                _fail_applied, _fail_changed = _apply_effects(
                    active_crisis.get("fail_effects") if isinstance(active_crisis.get("fail_effects"), list) else [],
                    npcs, npcs_patch, new_props, scenes,
                )
                if _fail_changed:
                    props_changed = True
                _fail_desc = str(active_crisis.get("fail_desc", "") or "").strip() or "最坏的情况发生了。"
                world_evt_msgs.append(f"【危机爆发】{_fail_desc}")
                sys_msgs.append(f"【系统】危机爆发：{_fail_desc}")
                if _fail_applied:
                    sys_msgs.append("【系统】" + "；".join(_fail_applied) + "。")
                new_fired.append(str(active_crisis.get("id", "")))
                crisis_cleared = True

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

        _crisis = ev.get("crisis") if isinstance(ev.get("crisis"), dict) else {}
        ok, reason = _check_condition(ev.get("condition") or {}, state)
        if not ok:
            if _crisis.get("deadline_phase"):
                # 危机事件条件不满足 → 挂起为 active_crisis，不 fired
                pending_crisis = {
                    "id": evt_id,
                    "name": str(ev.get("crisis_name", "") or "").strip() or "危机",
                    "at_phase": at_phase,
                    "deadline_phase": str(_crisis.get("deadline_phase", "") or "").strip(),
                    "condition": ev.get("condition") or {},
                    "fail_desc": str(_crisis.get("fail_desc", "") or "").strip(),
                    "fail_effects": _crisis.get("fail_effects") if isinstance(_crisis.get("fail_effects"), list) else [],
                }
                world_evt_msgs.append(f"【危机·{at_phase}】{pending_crisis['name']}逼近：{reason}")
                continue
            _fail_scene = str(ev.get("scene", "") or "").strip()
            _fail_desc = str(ev.get("new_desc_failed", "") or "").strip()
            if _fail_scene in scenes and _fail_desc:
                new_scenes[_fail_scene] = _fail_desc
            world_evt_msgs.append(f"【世界动态·{at_phase}】{reason}，计划未发生")
            new_fired.append(evt_id)
            continue

        ev_effects = ev.get("effects", []) if isinstance(ev.get("effects"), list) else []
        ev_applied_effects, _chg = _apply_effects(ev_effects, npcs, npcs_patch, new_props, scenes)
        if _chg:
            props_changed = True

        # 注入 NPC 认知与目标
        ev_npc_effects = ev.get("npc_effects", []) if isinstance(ev.get("npc_effects"), list) else []
        for ne in ev_npc_effects:
            if not isinstance(ne, dict):
                continue
            _ne_nid = ne.get("npc_id")
            if _ne_nid not in npcs:
                continue
            patch = npcs_patch.setdefault(_ne_nid, {})
            if ne.get("short_term_goal"):
                patch["short_term_plan"] = {"goal": ne["short_term_goal"], "source": "time_event"}
            if ne.get("fears"):
                patch["fears"] = ne["fears"] if isinstance(ne["fears"], list) else [str(ne["fears"])]
            if ne.get("desires"):
                patch["desires"] = ne["desires"] if isinstance(ne["desires"], list) else [str(ne["desires"])]
            if ne.get("contradictions"):
                patch["contradictions"] = ne["contradictions"] if isinstance(ne["contradictions"], list) else [str(ne["contradictions"])]
            if ne.get("perspective"):
                patch["perspective"] = str(ne["perspective"]).strip()
            if ne.get("status"):
                patch["status"] = str(ne["status"]).strip().lower()
            if ne.get("physical_state"):
                patch["physical_state"] = str(ne["physical_state"]).strip().lower()
            if ne.get("intent"):
                _intent_kind = str(ne["intent"]).strip()
                _pending = list(patch.get("pending_intent", npcs[_ne_nid].get("pending_intent") or []))
                _pending.append({
                    "kind": _intent_kind,
                    "payload": str(ne.get("target", "")).strip(),
                    "remain": 1,
                    "source": "time_event"
                })
                patch["pending_intent"] = _pending
                _npc_name = npcs[_ne_nid].get("name", _ne_nid)
                ev_applied_effects.append(f"{_npc_name}的心里有了盘算")

        _scene_key = str(ev.get("scene", "") or "").strip()
        _new_desc = str(ev.get("new_desc", "") or "").strip()
        if _scene_key in scenes and _new_desc:
            new_scenes[_scene_key] = _new_desc

        ev_hint = str(ev.get("hint", "") or "").strip()
        _effect_summary = "；".join(ev_applied_effects) if ev_applied_effects else "环境悄然变化"
        world_evt_msgs.append(f"【大事·{at_phase}】{_scene_key}：{_effect_summary}")
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
    if pending_crisis:
        out["active_crisis"] = pending_crisis
        sys_msgs.append(f"【系统】危机降临：{pending_crisis['name']}。若不及时应对，{pending_crisis['deadline_phase']}后将无法挽回。")
    elif crisis_cleared:
        out["active_crisis"] = {}
    if sys_msgs:
        out["history"] = [{"speaker": "system", "content": m} for m in sys_msgs]
    return out