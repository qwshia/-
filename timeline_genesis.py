"""Timeline generation node: create and validate timed story events."""
import json
import sys
from typing import List

from langchain_core.messages import HumanMessage

from config import world_llm
from state import AgentState
from utils import TIME_PHASES, _parse_json


def _extract_phase(value: object) -> str:
    phase = str(value or "").strip()
    if phase in TIME_PHASES:
        return phase
    for candidate in TIME_PHASES:
        if phase.endswith(candidate):
            return candidate
    return phase


def _phase_forward_dist(start: str, target: str) -> int:
    if start not in TIME_PHASES or target not in TIME_PHASES:
        return -1
    return (TIME_PHASES.index(target) - TIME_PHASES.index(start)) % len(TIME_PHASES)


def _content_item_names(items: list) -> set:
    names = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for child in content:
            if isinstance(child, dict) and "text" in child:
                continue
            name = child.get("name") if isinstance(child, dict) else child
            name = str(name or "").strip()
            if name:
                names.add(name)
    return names


def _collect_known_items(scenes: dict, props: list, initial_items: list) -> set:
    known = set()
    for prop in props or []:
        if isinstance(prop, dict) and prop.get("name"):
            known.add(str(prop["name"]).strip())
    for item in initial_items or []:
        name = item.get("name") if isinstance(item, dict) else item
        name = str(name or "").strip()
        if name:
            known.add(name)
    for scene in (scenes or {}).values():
        if not isinstance(scene, dict):
            continue
        items = scene.get("items") or []
        for item in items:
            if isinstance(item, dict) and item.get("name"):
                known.add(str(item["name"]).strip())
        known.update(_content_item_names(items))
    known.update(_content_item_names(props or []))
    return known


def _clean_timed_events(
    raw_events: list,
    scenes: dict,
    npcs: dict,
    props: list,
    initial_items: list,
    time_start: str,
    time_limit: str,
) -> List[dict]:
    npc_id_by_name = {
        str(info.get("name", "")).strip(): npc_id
        for npc_id, info in (npcs or {}).items()
        if isinstance(info, dict) and info.get("name")
    }
    prop_by_name = {
        str(prop["name"]).strip(): prop
        for prop in (props or [])
        if isinstance(prop, dict) and prop.get("name")
    }
    known_items = _collect_known_items(scenes, props, initial_items)
    total_span = _phase_forward_dist(time_start, time_limit)
    seen_scenes = set()
    seen_event_ids = set()
    timed_events = []

    for event in raw_events:
        if len(timed_events) >= 3:
            print("[时间线校验] 定时事件超过3个，额外事件已丢弃", file=sys.stderr)
            break
        if not isinstance(event, dict):
            continue
        at_phase = _extract_phase(event.get("at_phase", ""))
        scene = str(event.get("scene", "") or "").strip()
        new_desc = str(event.get("new_desc", "") or "").strip()
        new_desc_failed = str(event.get("new_desc_failed", "") or "").strip()
        hint = str(event.get("hint", "") or "").strip()
        condition = event.get("condition") if isinstance(event.get("condition"), dict) else {}
        effects = event.get("effects") if isinstance(event.get("effects"), list) else []

        if at_phase not in TIME_PHASES:
            print(f"[时间线校验] at_phase='{at_phase}' 非法，事件已丢弃", file=sys.stderr)
            continue
        distance = _phase_forward_dist(time_start, at_phase)
        if distance <= 0 or (total_span > 0 and distance > total_span):
            print(
                f"[时间线校验] at_phase='{at_phase}' 不在 {time_start}~{time_limit} 区间，事件已丢弃",
                file=sys.stderr,
            )
            continue
        if scene not in scenes:
            print(f"[时间线校验] scene='{scene}' 不在 scenes 中，事件已丢弃", file=sys.stderr)
            continue
        if scene in seen_scenes:
            print(f"[时间线校验] 场景 '{scene}' 重复，仅保留首个事件", file=sys.stderr)
            continue
        if not new_desc or not hint:
            print("[时间线校验] 事件缺少 new_desc 或 hint，事件已丢弃", file=sys.stderr)
            continue

        condition_npc_id = str(condition.get("npc_id", "") or "").strip()
        condition_scene = str(condition.get("at_scene", "") or "").strip()
        raw_items = condition.get("has_items") if isinstance(condition.get("has_items"), list) else []
        condition_items = [str(item).strip() for item in raw_items if str(item).strip()]
        raw_player_items = condition.get("player_has_items") if isinstance(condition.get("player_has_items"), list) else []
        condition_player_items = [str(item).strip() for item in raw_player_items if str(item).strip()]
        condition_goal_kw = str(condition.get("npc_goal_contains", "") or "").strip()

        condition_valid = True
        if condition_npc_id:
            if condition_npc_id not in npcs:
                print(
                    f"[时间线校验] condition.npc_id='{condition_npc_id}' 不存在，事件已丢弃",
                    file=sys.stderr,
                )
                condition_valid = False
            elif condition_scene and condition_scene not in scenes:
                print(
                    f"[时间线校验] condition.at_scene='{condition_scene}' 不在 scenes 中，事件已丢弃",
                    file=sys.stderr,
                )
                condition_valid = False
            elif any(item not in known_items for item in condition_items):
                missing = [item for item in condition_items if item not in known_items]
                print(
                    f"[时间线校验] condition.has_items 中 {missing} 不在剧本物品中，事件已丢弃",
                    file=sys.stderr,
                )
                condition_valid = False
            elif condition_items:
                npc_name = str(npcs[condition_npc_id].get("name", "") or "").strip()
                held_items = {
                    str(prop.get("name", "")).strip()
                    for prop in (props or [])
                    if isinstance(prop, dict) and prop.get("holder") == npc_name
                }
                unheld = [item for item in condition_items if item not in held_items]
                if unheld:
                    print(
                        f"[时间线校验] NPC '{npc_name}' 未持有 condition.has_items 中的 {unheld}，事件已丢弃",
                        file=sys.stderr,
                    )
                    condition_valid = False
        elif condition_items:
            print(
                "[时间线校验] condition.has_items 非空但缺 npc_id，事件已丢弃",
                file=sys.stderr,
            )
            condition_valid = False

        for _pitem in condition_player_items:
            if _pitem not in known_items:
                print(f"[时间线校验] condition.player_has_items 中 {_pitem} 不在剧本物品中，事件已丢弃", file=sys.stderr)
                condition_valid = False
        if condition_goal_kw and not condition_npc_id:
            print("[时间线校验] condition.npc_goal_contains 非空但缺 npc_id，事件已丢弃", file=sys.stderr)
            condition_valid = False

        if not condition_valid:
            continue

        # ==== 危机字段清洗 ====
        cleaned_crisis = {}
        raw_crisis = event.get("crisis") if isinstance(event.get("crisis"), dict) else {}
        crisis_name = str(event.get("crisis_name", "") or "").strip()
        if raw_crisis.get("deadline_phase"):
            _dl = _extract_phase(raw_crisis.get("deadline_phase", ""))
            _dl_dist = _phase_forward_dist(time_start, _dl)
            _at_dist = _phase_forward_dist(time_start, at_phase)
            if _dl not in TIME_PHASES or _dl_dist <= _at_dist:
                print(f"[时间线校验] 危机 deadline_phase='{raw_crisis.get('deadline_phase')}' 非法（必须晚于 at_phase），危机字段已丢弃", file=sys.stderr)
            elif total_span > 0 and _dl_dist > total_span:
                print(f"[时间线校验] 危机 deadline_phase='{_dl}' 超出 {time_limit} 时限，危机字段已丢弃", file=sys.stderr)
            else:
                _fail_effects = raw_crisis.get("fail_effects") if isinstance(raw_crisis.get("fail_effects"), list) else []
                _clean_fail_effects = []
                for fe in _fail_effects:
                    if not isinstance(fe, dict):
                        continue
                    _ft = str(fe.get("type", "") or "").strip()
                    if _ft == "npc_move":
                        _fnid = str(fe.get("npc_id", "") or "").strip()
                        _fto = str(fe.get("to", "") or "").strip()
                        if _fnid in npcs and _fto in scenes:
                            _clean_fail_effects.append({"type": "npc_move", "npc_id": _fnid, "to": _fto})
                        else:
                            print(f"[时间线校验] 危机 fail_effects npc_move 目标无效，已跳过", file=sys.stderr)
                    elif _ft == "item_move":
                        _fitem = str(fe.get("item", "") or "").strip()
                        _fto = str(fe.get("to", "") or "").strip()
                        if _fitem in prop_by_name and (_fto.startswith("npc:") and _fto[4:].strip() in npc_id_by_name or _fto.startswith("scene:") and _fto[6:].strip() in scenes):
                            _clean_fail_effects.append({"type": "item_move", "item": _fitem, "to": _fto})
                        else:
                            print(f"[时间线校验] 危机 fail_effects item_move 目标无效，已跳过", file=sys.stderr)
                cleaned_crisis = {
                    "deadline_phase": _dl,
                    "fail_desc": str(raw_crisis.get("fail_desc", "") or "").strip(),
                    "fail_effects": _clean_fail_effects,
                }

        valid_effects = []
        for effect in effects:
            if not isinstance(effect, dict):
                continue
            effect_type = str(effect.get("type", "") or "").strip()
            if effect_type == "npc_move":
                npc_id = str(effect.get("npc_id", "") or "").strip()
                target = str(effect.get("to", "") or "").strip()
                if npc_id not in npcs or target not in scenes:
                    print(
                        f"[时间线校验] npc_move 目标无效 (npc_id='{npc_id}', to='{target}')，已跳过",
                        file=sys.stderr,
                    )
                    continue
                valid_effects.append({"type": "npc_move", "npc_id": npc_id, "to": target})
            elif effect_type == "item_move":
                item_name = str(effect.get("item", "") or "").strip()
                target = str(effect.get("to", "") or "").strip()
                if item_name not in prop_by_name:
                    print(f"[时间线校验] item_move 物品 '{item_name}' 不在 props 中，已跳过", file=sys.stderr)
                    continue
                if target.startswith("npc:"):
                    owner = target[4:].strip()
                    if owner not in npc_id_by_name:
                        print(f"[时间线校验] item_move 目标 NPC '{owner}' 不存在，已跳过", file=sys.stderr)
                        continue
                elif target.startswith("scene:"):
                    target_scene = target[6:].strip()
                    if target_scene not in scenes:
                        print(f"[时间线校验] item_move 目标场景 '{target_scene}' 不存在，已跳过", file=sys.stderr)
                        continue
                else:
                    print(f"[时间线校验] item_move to='{target}' 格式非法，已跳过", file=sys.stderr)
                    continue
                valid_effects.append({"type": "item_move", "item": item_name, "to": target})
            else:
                print(f"[时间线校验] effect type='{effect_type}' 不支持，已跳过", file=sys.stderr)

        valid_npc_effects = []
        raw_npc_effects = event.get("npc_effects")
        if isinstance(raw_npc_effects, list):
            for npc_effect in raw_npc_effects:
                if not isinstance(npc_effect, dict):
                    continue
                npc_id = str(npc_effect.get("npc_id", "") or "").strip()
                if npc_id not in npcs:
                    print(f"[时间线校验] npc_effects npc_id='{npc_id}' 不存在，已跳过", file=sys.stderr)
                    continue
                cleaned = {"npc_id": npc_id}
                for key in ("short_term_goal", "intent", "target"):
                    if npc_effect.get(key):
                        cleaned[key] = str(npc_effect[key]).strip()
                valid_npc_effects.append(cleaned)

        event_id = str(event.get("id", "") or "").strip() or f"evt_{len(timed_events) + 1}"
        if event_id in seen_event_ids:
            print(f"[时间线校验] 事件 id '{event_id}' 重复，已重新编号", file=sys.stderr)
            suffix = len(timed_events) + 1
            event_id = f"evt_{suffix}"
            while event_id in seen_event_ids:
                suffix += 1
                event_id = f"evt_{suffix}"
        timed_events.append({
            "id": event_id,
            "at_phase": at_phase,
            "scene": scene,
            "new_desc": new_desc,
            "new_desc_failed": new_desc_failed,
            "condition": {
                "npc_id": condition_npc_id,
                "at_scene": condition_scene,
                "has_items": condition_items,
                "player_has_items": condition_player_items,
                "npc_goal_contains": condition_goal_kw,
            },
            "effects": valid_effects,
            "npc_effects": valid_npc_effects,
            "hint": hint,
            "crisis_name": crisis_name,
            "crisis": cleaned_crisis,
        })
        seen_scenes.add(scene)
        seen_event_ids.add(event_id)

    timed_events.sort(key=lambda event: _phase_forward_dist(time_start, event["at_phase"]))
    return timed_events


async def timeline_genesis(state: AgentState) -> dict:
    scenes = state.get("scenes") or {}
    npcs = state.get("npcs") or {}
    props = state.get("props") or []
    truth_leads = state.get("truth_leads") or []
    time_start = _extract_phase(state.get("time_start", ""))
    time_limit = _extract_phase(state.get("time_limit", ""))

    if not time_limit:
        return {"timed_events": []}
    if time_start not in TIME_PHASES or time_limit not in TIME_PHASES:
        print(
            f"[时间线校验] 时间范围非法 (time_start='{time_start}', time_limit='{time_limit}')，返回空事件列表",
            file=sys.stderr,
        )
        return {"timed_events": []}

    initial_items = state.get("player_items") or []
    prompt_context = {
        "scenes": scenes,
        "npcs": npcs,
        "props": props,
        "truth_leads": truth_leads,
        "initial_items": initial_items,
        "time_start": time_start,
        "time_limit": time_limit,
    }
    prompt = f"""你是互动叙事导演的时间线设计师。根据给定剧本资料生成定时事件。
[铁律] 只输出纯净JSON，不要思考过程、开场白、解释或markdown代码块。

[剧本资料]
{json.dumps(prompt_context, ensure_ascii=False, indent=2)}

[定时事件生成规则]
1. 生成2-4个事件；每个场景最多一个事件。至少1个事件必须与truth_leads中的事实或获取路径相关（主线固定），其余可生成随机插曲事件（环境变化/NPC小动作/物品易位），增加剧本随机性.
2. at_phase必须在time_start之后且不晚于time_limit，只能严格取清晨/上午/正午/午后/黄昏/夜晚/深夜/凌晨之一.
3. scene必须是已登记场景。new_desc是条件成立时的全量新描述；new_desc_failed是条件不成立时的全量新描述.
4. condition结构为{{"npc_id":"","at_scene":"","has_items":[],"player_has_items":[],"npc_goal_contains":""}}.
   - npc_id+at_scene+has_items: NPC在某场景且携带某物品才触发.
   - player_has_items: 玩家持有某物品才触发(独立条件).
   - npc_goal_contains: 该NPC当前短期/长期目标含此关键词才触发(用于"NPC意图影响剧本走向"时动态变化).
   - 不依赖的字段填空字符串/空数组.
5. condition.has_items中填写的物品名，必须是condition.npc_id对应NPC实际持有的实体物品（登记在props中）；绝不能填写场景物品、玩家物品，否则时间事件无法触发.
6. effects数组可为空，仅支持{{"type":"npc_move","npc_id":"npc_1","to":"场景"}}和{{"type":"item_move","item":"物品名","to":"npc:NPC名或scene:场景名"}}.
7. npc_effects数组可为空，支持{{"npc_id":"npc_1","short_term_goal":"","intent":"move","target":"场景"}}。short_term_goal可动态改变该NPC的近期目标，进而影响其后续行为与剧本走向.
8. hint为一句玩家视角的模糊感知。事件条件、效果和线索必须自洽，不得引入剧本资料中不存在的NPC、场景或物品.
9. [危机事件-可选] 2-4个事件中可设计0-1个危机事件（有明确时间压力的剧本必设1个，紧张感直接拉满）:
   - 事件带 crisis 字段: {{"deadline_phase":"黄昏","fail_desc":"一句话描述最坏结果","fail_effects":[{{"type":"npc_move","npc_id":"npc_2","to":"场景"}}]}}，同时事件自带 crisis_name 字段（危机短名，如"证据销毁危机"）.
   - 语义: at_phase时刻若condition不满足，危机激活并挂起；玩家在deadline_phase结束前让condition成立即可解除；否则fail_effects执行(关键NPC逃走/关键物品易位/证据被毁).
   - deadline_phase必须严格晚于at_phase且不晚于time_limit; fail_desc必须与fail_effects一致(如fail_effects是凶手逃跑, fail_desc就写"凶手携证据逃之夭夭").
   - fail_effects只支持npc_move/item_move两种类型，规则同第6条.

[输出格式]
{{"timed_events":[{{"id":"evt_1","at_phase":"黄昏","scene":"","new_desc":"","new_desc_failed":"","condition":{{"npc_id":"npc_1","at_scene":"","has_items":[],"player_has_items":[],"npc_goal_contains":""}},"effects":[{{"type":"npc_move","npc_id":"npc_1","to":""}},{{"type":"item_move","item":"","to":"scene:"}}],"npc_effects":[{{"npc_id":"npc_1","short_term_goal":"","intent":"move","target":"场景"}}],"hint":"","crisis_name":"","crisis":{{"deadline_phase":"","fail_desc":"","fail_effects":[]}}}}]}}"""
    try:
        response = await world_llm.ainvoke([HumanMessage(content=prompt)])
        parsed = _parse_json(response.content)
    except Exception as error:
        print(f"[时间线生成失败] {type(error).__name__}: {str(error)[:300]}", file=sys.stderr)
        raise RuntimeError(f"[timeline_genesis] 定时事件生成失败：{error}") from error

    if not isinstance(parsed, dict) or not isinstance(parsed.get("timed_events"), list):
        print(f"[时间线解析失败] 原始输出: {str(parsed)[:500]}", file=sys.stderr)
        raise RuntimeError("[timeline_genesis] 返回格式错误：timed_events 必须是数组")

    timed_events = _clean_timed_events(
        parsed["timed_events"],
        scenes,
        npcs,
        props,
        initial_items,
        time_start,
        time_limit,
    )
    return {"timed_events": timed_events}
