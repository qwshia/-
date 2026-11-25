"""世界引擎节点：world_genesis —— 导演按玩家要求一次生成整个剧本世界。

输出：backstory / global_truth / ending_goal / scenes / npcs 实体库
（固定字段），并兜底"开局场景至少一个 NPC 在场"。

结局机制：不设剧情节点。导演设计真相（global_truth）与结局判定标准（ending_goal），
玩家通过调查与对话形成推理，输入"结局是：推理"由 judge_ending 对比判定。
"""
import sys
from typing import Dict

from langchain_core.messages import HumanMessage

from config import world_llm
from state import AgentState
from utils import _parse_json
from rag_manager import search_script_summary, add_script_summary
from langchain_core.runnables import RunnableConfig
from world_prompt_builder import build_world_prompt


def _match_scene(loc, scene_names, fallback):
    if not loc:
        return fallback
    if loc in scene_names:
        return loc
    for s in scene_names:
        if loc in s or s in loc:
            return s
    return fallback


from constants import VALID_LOCK_MODES as _LOCK_MODES, NO_KEY_WORDS as _NO_KEY_WORDS


def _normalize_lock(item: dict) -> dict:
    """规范化锁字段：未锁则清空解锁字段；锁定则校验解锁方式完整性，不完整自动解锁。"""
    if not isinstance(item, dict):
        return item
    if not bool(item.get("locked", False)):
        item.pop("lock_mode", None)
        item.pop("key_name", None)
        item.pop("bio_holder", None)
        return item
    mode = str(item.get("lock_mode", "") or "").strip()
    key_name = str(item.get("key_name", "") or "").strip()
    bio_holder = str(item.get("bio_holder", "") or "").strip()
    if mode not in _LOCK_MODES:
        mode = ""
    need_key = mode in ("physical", "both", "either")
    need_bio = mode in ("bio", "both", "either")
    if not mode or (need_key and not key_name) or (need_bio and not bio_holder):
        item["locked"] = False
        item.pop("lock_mode", None)
        item.pop("key_name", None)
        item.pop("bio_holder", None)
        return item
    if any(w in key_name for w in _NO_KEY_WORDS):
        key_name = ""
    item["lock_mode"] = mode
    if key_name:
        item["key_name"] = key_name
    else:
        item.pop("key_name", None)
    if bio_holder:
        item["bio_holder"] = bio_holder
    else:
        item.pop("bio_holder", None)
    return item


def _validate_truth_leads(truth_leads, scene_names, npcs, props, item_names) -> list:
    warnings = []
    npc_names = {info.get("name") for info in (npcs or {}).values()}
    prop_names = {p.get("name") for p in (props or [])}
    entities = set(scene_names) | npc_names | prop_names | set(item_names)
    for lead in truth_leads or []:
        if not isinstance(lead, dict):
            continue
        fact, via = str(lead.get("fact", "")), str(lead.get("via", ""))
        if not via:
            warnings.append(f"真相拼图「{fact}」缺少获取途径")
        elif not any(e and e in via for e in entities):
            warnings.append(f"真相拼图「{fact}」的途径可能不指向任何真实场景/NPC/道具：{via}")
    return warnings


async def world_genesis(state: AgentState, config: RunnableConfig = None) -> dict:
    task = state.get("task", "")
    rag_reference = ""
    try:
        similar_script = await search_script_summary(task, top_k=1)
        if similar_script:
            rag_reference = f"\n[过往剧本参考(仅供参考，不要照抄)]\n{similar_script}\n"
            print("[导演提示] 成功检索到相似剧本，已注入参考。")
    except Exception as e:
        print(f"[剧本检索失败] {e}")
    style_profile = state.get("style_profile") if isinstance(state.get("style_profile"), dict) else {}
    prompt = build_world_prompt(task, rag_reference, style_profile)
    _raw_output_for_debug = ""
    parsed = {}
    npcs = {}
    
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            response = await world_llm.ainvoke([HumanMessage(content=prompt)])
            _raw_output_for_debug = response.content if isinstance(response.content, str) else str(response.content)
            parsed = _parse_json(response.content)
            
            # 1. 先解析场景（为解析 NPC 提供 fallback）
            init_scene = parsed.get("initial_scene", "大厅")
            scene_names = [s for s in parsed.get("available_scenes", []) if isinstance(s, str)]
            if not scene_names:
                for s in (parsed.get("scenes", []) if isinstance(parsed.get("scenes"), list) else []):
                    if isinstance(s, dict) and s.get("name"):
                        scene_names.append(s["name"])
            if init_scene not in scene_names:
                scene_names.append(init_scene)
            if not scene_names:
                scene_names = [init_scene]

            # 2. 解析 npcs
            npcs = {}
            for item in parsed.get("npcs", []) if isinstance(parsed.get("npcs"), list) else []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                npc_id = str(item["id"])
                npcs[npc_id] = {
                    "name": item.get("name", npc_id),
                    "role": item.get("role", "路人"),
                    "appearance": "",
                    "attire": {},
                    "location": _match_scene(item.get("location"), scene_names, init_scene),
                    "affection": 100,
                    "memory": {"l1": []},
                    "emotion_tags": [],
                    "pending_intent": [],
                    "life_events": [],
                    "current_action": {"action": "", "since_tick": 0},
                    "round_count": 0,
                }
            
            if parsed and npcs:
                break  # 成功拿到完整数据，跳出重试
            else:
                print(f"[导演生成警告] 第 {attempt+1} 次生成失败（JSON为空或NPC列表为空），准备重试...", file=sys.stderr)
        except Exception as e:
            import traceback
            print(f"[导演生成失败] {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
            traceback.print_exc()
            parsed = {}
            npcs = {}

    if not parsed:
        import os as _os
        _debug_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "debug_world_output.txt")
        try:
            with open(_debug_path, "w", encoding="utf-8") as _f:
                _f.write(_raw_output_for_debug)
            print(f"[导演调试] 原始输出已写入 {_debug_path}", file=sys.stderr)
        except Exception as _e:
            print(f"[导演调试] 写调试文件失败: {_e}", file=sys.stderr)
        raise RuntimeError(
            "[world_genesis] 导演输出 JSON 解析失败。原始输出已写入 debug_world_output.txt，"
            "请检查末尾是否被截断（可能是 max_tokens 不足或输出超限）。"
        )

    if not npcs:
        raise RuntimeError(
            "[world_genesis] 导演输出中 npcs 为空（重试 2 次后仍失败）。剧本生成失败，请重新生成。"
        )

    _seen_names = set()
    for nid, info in npcs.items():
        nm = str(info.get("name", "") or "").strip()
        if nm and nm in _seen_names:
            new_nm = nm + "_2"
            print(f"[导演校验] NPC 重名，已重命名为 {new_nm}", file=sys.stderr)
            info["name"] = new_nm
            _seen_names.add(new_nm)
        elif nm:
            _seen_names.add(nm)

    # === id 重编号兜底：强制 npc_1 / npc_2 / ... 标准格式 ===
    _old_to_new = {}
    _new_npcs = {}
    for _idx, (_old_id, _info) in enumerate(list(npcs.items()), 1):
        _new_id = f"npc_{_idx}"
        _old_to_new[_old_id] = _new_id
        _new_npcs[_new_id] = _info
        if _old_id != _new_id:
            print(f"[导演校验] NPC id '{_old_id}' 非标准格式，已重编号为 '{_new_id}'", file=sys.stderr)
    npcs = _new_npcs

    scenes: Dict[str, dict] = {}
    for s in parsed.get("scenes", []) if isinstance(parsed.get("scenes"), list) else []:
        if isinstance(s, dict) and s.get("name"):
            scenes[s["name"]] = {
                "desc": s.get("desc", ""),
                "items": s.get("items", []),
                "locked": bool(s.get("locked", False)),
                "lock_mode": str(s.get("lock_mode", "") or "").strip(),
                "key_name": str(s.get("key_name", "") or "").strip(),
                "bio_holder": str(s.get("bio_holder", "") or "").strip(),
            }

    if init_scene not in scenes:
        if scenes:
            init_scene = next(iter(scenes))
        else:
            scenes[init_scene] = {"desc": "", "items": [], "locked": False, "lock_mode": "", "key_name": "", "bio_holder": ""}

    for sdef in scenes.values():
        if isinstance(sdef, dict):
            _normalize_lock(sdef)
            for it in (sdef.get("items") or []):
                if isinstance(it, dict):
                    _normalize_lock(it)

    from utils import TIME_PHASES as _TP
    _TIME_PHASES_SET = set(_TP)

    def _extract_phase(raw_str):
        raw_str = str(raw_str or "").strip()
        if not raw_str:
            return ""
        if raw_str in _TIME_PHASES_SET:
            return raw_str
        for phase in _TP:
            if raw_str.endswith(phase):
                return phase
        return raw_str

    time_start = _extract_phase(parsed.get("time_start", ""))
    if not time_start:
        time_start = "清晨"
    time_limit = _extract_phase(parsed.get("time_limit", ""))
    try:
        total_rounds = max(10, int(parsed.get("total_rounds", 30) or 30))
    except Exception:
        total_rounds = 30
    time_step_delta = 8.0 / total_rounds
    try:
        time_turns = max(0, int(parsed.get("time_turns", 0) or 0))
    except Exception:
        time_turns = 0
    _NO_TIME_WORDS = ("时间不限", "不限时", "不限时间", "没有时间限制", "无时间限制", "时间上没有限制", "不限定时限")
    if any(w in task for w in _NO_TIME_WORDS) and time_limit:
        print("[导演校验] 玩家要求'时间不限'，强制清空时限。", file=sys.stderr)
        time_limit, time_turns = "", 0
        
    # 兜底：如果没有开局时间，默认“清晨”，让世界时钟转起来
    if not time_start:
        time_start = "清晨"
    initial_raw = parsed.get("initial_items", []) if isinstance(parsed.get("initial_items"), list) else []
    _npc_names_set = {info.get("name") for info in npcs.values()}
    _p_name = str(state.get("player_name", "") or "").strip()
    props = []
    for p in parsed.get("props", []) if isinstance(parsed.get("props"), list) else []:
        if isinstance(p, dict) and p.get("name"):
            if p.get("holder") and p["holder"] not in _npc_names_set:
                if p["holder"] == _p_name:
                    print(f"[导演校验] props「{p.get('name')}」holder=玩家名，已转进背包", file=sys.stderr)
                    initial_raw.append({"name": p.get("name"), "desc": p.get("desc", "")})
                    continue
                else:
                    print(f"[导演校验] props「{p.get('name')}」holder「{p['holder']}」不存在，已转为无主场景物品", file=sys.stderr)
                    p["holder"] = ""
                    p["carried"] = False
            raw_loc = p.get("location") or ""
            _c = p.get("carried", False)
            carried = _c if isinstance(_c, bool) else str(_c).strip().lower() in ("true", "1", "yes", "是")
            prop = {
                "name": p.get("name"),
                "holder": p.get("holder") or "",
                "carried": carried,
                "location": _match_scene(raw_loc, scene_names, raw_loc),
                "desc": p.get("desc", ""),
                "locked": bool(p.get("locked", False)),
                "lock_mode": str(p.get("lock_mode", "") or "").strip(),
            }
            kn = str(p.get("key_name", "") or "").strip()
            if kn:
                prop["key_name"] = kn
            bh = str(p.get("bio_holder", "") or "").strip()
            if bh:
                prop["bio_holder"] = bh
            if carried and prop["holder"]:
                hloc = None
                for nid, info in npcs.items():
                    if info.get("name") == prop["holder"]:
                        hloc = info.get("location")
                        break
                if hloc:
                    prop["location"] = hloc
            # NPC 私有物品不随身时，放在 NPC 当前场景（废除藏匿到其他场景）
            if (not carried) and prop["holder"]:
                for nid, info in npcs.items():
                    if info.get("name") == prop["holder"]:
                        if info.get("location"):
                            prop["location"] = info.get("location")
                        break
            props.append(prop)

    for p in props:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        _normalize_lock(p)

    # 防死锁校验：门禁解锁凭证若被锁在门禁内，强制移出
    import re
    for sname, sdef in scenes.items():
        if not sdef.get("locked"):
            continue
        unlock_names = []
        _kn = str(sdef.get("key_name", "") or "").strip()
        if _kn:
            unlock_names.append(_kn)
        _bh = str(sdef.get("bio_holder", "") or "").strip()
        if _bh:
            for nid, info in npcs.items():
                if info.get("name") == _bh and info.get("location") == sname:
                    print(f"[导演校验] 生物凭证持有者「{_bh}」被困在门禁「{sname}」内，已将其移出到{init_scene}", file=sys.stderr)
                    info["location"] = init_scene
        for k in unlock_names:
            if not k:
                continue
            for p in props:
                if p.get("name") == k:
                    if p.get("holder") and p.get("carried"):
                        for nid, info in npcs.items():
                            if info.get("name") == p["holder"] and info.get("location") == sname:
                                print(f"[导演校验] 解锁凭证「{k}」的持有者「{p['holder']}」被困在门禁「{sname}」内，已将其移出到{init_scene}", file=sys.stderr)
                                info["location"] = init_scene
                                p["location"] = init_scene
                    if p.get("location") == sname:
                        print(f"[导演校验] 解锁凭证「{k}」被锁在门禁「{sname}」内，已将其移出到{init_scene}", file=sys.stderr)
                        p["location"] = init_scene
                        if p.get("holder") and not p.get("carried"):
                            for nid, info in npcs.items():
                                if info.get("name") == p["holder"]:
                                    info["location"] = init_scene

    initial_names = []
    initial_items = []
    for it in initial_raw:
        if isinstance(it, str) and it.strip():
            nm = it.strip()
            initial_names.append(nm)
            initial_items.append({"name": nm, "desc": "", "count": 1})
        elif isinstance(it, dict) and it.get("name"):
            nm = str(it["name"]).strip()
            desc = str(it.get("desc", "") or "").strip()
            initial_names.append(nm)
            initial_items.append({"name": nm, "desc": desc, "count": 1})

    truth_leads = parsed.get("truth_leads", []) if isinstance(parsed.get("truth_leads"), list) else []
    for _w in _validate_truth_leads(truth_leads, scene_names, npcs, props, initial_names):
        print(f"[真相可达性警告] {_w}", file=sys.stderr)

    item_desc_map = {}
    for sdef in scenes.values():
        for it in sdef.get("items", []) or []:
            if isinstance(it, dict) and it.get("name"):
                item_desc_map[it["name"]] = it.get("desc", "")
    for p in props:
        if p.get("name"):
            item_desc_map.setdefault(p["name"], p.get("desc", ""))
    for it in initial_raw:
        if isinstance(it, dict) and it.get("name"):
            item_desc_map[it["name"]] = it.get("desc", item_desc_map.get(it["name"], ""))

    _prop_names = {p.get("name") for p in props if isinstance(p, dict) and p.get("name")}

    # 清洗冲突：NPC随身携带的物品，绝不能出现在场景中
    for sdef in scenes.values():
        _new_items = []
        for it in (sdef.get("items") or []):
            if not isinstance(it, dict):
                _new_items.append(it)
                continue
            _hit_name = str(it.get("name", "") or "").strip()
            if any(_pn and (_pn == _hit_name or _pn in _hit_name or _hit_name in _pn) for _pn in _prop_names):
                continue
            _new_items.append(it)
        sdef["items"] = _new_items

    npc_private_items = {}
    for nid, info in npcs.items():
        npc_private_items[nid] = [p.get("name") for p in props if p.get("holder") == info.get("name") and p.get("name")]

    try:
        thread_id = "default"
        if config and isinstance(config, dict):
            thread_id = config.get("configurable", {}).get("thread_id", "default")
            
        summary_text = f"玩家要求：{task}\n背景：{parsed.get('backstory', '')}\n真相：{parsed.get('global_truth', '')}"
        add_script_summary(thread_id, task, summary_text)
    except Exception as e:
        print(f"[RAG剧本写入失败] {e}")
    # ================================================================

    # 结局解析：单结局/多结局统一为列表
    endings_raw = parsed.get("endings", []) if isinstance(parsed.get("endings"), list) else []
    endings = []
    for _e in endings_raw:
        if isinstance(_e, dict) and _e.get("name"):
            endings.append({
                "id": str(_e.get("id", "") or "").strip() or f"ending_{len(endings) + 1}",
                "name": str(_e.get("name", "") or "").strip(),
                "desc": str(_e.get("desc", "") or "").strip(),
                "condition": str(_e.get("condition", "") or "").strip(),
            })
    if not endings:
        endings = [{
            "id": "ending_1",
            "name": "真相大白",
            "desc": "你还原了真相，指认了真凶。",
            "condition": "玩家推理与真相核心一致",
        }]

    return {
        "backstory": parsed.get("backstory", "故事开始了。"),
        "player_name": str(state.get("player_name", "") or "").strip(),
        "ending_goal": parsed.get("ending_goal", "查明真相。"),
        "endings": endings,
        "global_truth": parsed.get("global_truth", "真相未知。"),
        "truth_leads": parsed.get("truth_leads", []) if isinstance(parsed.get("truth_leads"), list) else [],
        "allowed_entities": parsed.get("allowed_entities", []),
        "available_scenes": scene_names,
        "scenes": scenes,
        "npcs": npcs,
        "props": props,
        "total_rounds": total_rounds,
        "time_step_delta": time_step_delta,
        "time_start": time_start,
        "time_limit": time_limit,
        "time_turns": time_turns,
        "timed_events": [],
        "fired_time_events": [],
        "current_scene": init_scene,
        "time_phase": time_start,
        "time_progress": 0.0,
        "world_tick": 0,
        "player_items": initial_items,
        "player_intent": {},
        "item_desc_map": item_desc_map,
        "item_transfers": [],
        "processed_transfers": [],
        "analysis_counter": 0,
        "last_clue_idx": 0,
        "player_revealed_clues": [],
        "secrets_revealed": [],
        "active_crisis": {},
        "world_events": [],
        "game_over": False,
        "p2p_transcript": "",
        "opened_scenes": [],
        "steal_attempts": {},
        "npc_private_items": npc_private_items,
        "run_story": False,
        "style_profile": style_profile if isinstance(style_profile, dict) else {},
    }
