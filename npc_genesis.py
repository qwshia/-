"""NPC 血肉生成节点：npc_genesis

读 world_genesis 输出的 NPC 名单（骨架），为每个 NPC 生成血肉：
- 必填：background / appearance / attire / personality / life_goal / cognition / mood / core_drive / affection / self_interest / moral_boundary / attachment
- 可选：fears / desires / perspective / contradictions / impulsivity / social_initiative / weakness

一次 LLM 调用生成全部 NPC。独立生成，不互相参照。必填字段缺则报错，不静默兜底。
可选字段缺省时按空值处理。

cognition 为认知字典，条目 type in {fact, secret, belief, forbidden}，运行时可动态增删。
fact: 全部 NPC 都可能知道的公开事实（npc_genesis 可生成，也可置空由运行时补充）。
secret: NPC 的个人秘密（npc_genesis 生成）。
belief: 基于有限信息的推断（npc_genesis 可生成，运行时可追加/修改）。
forbidden: NPC 拒绝谈论的话题（npc_genesis 生成）。
"""
import json
import sys
from typing import Dict, List

from langchain_core.messages import HumanMessage

from config import world_llm
from state import AgentState
from constants import VALID_MOODS, VALID_STABILITY, VALID_COGNITION_TYPES, VALID_SERVES
from npc_genesis_prompt_builder import build_npc_prompt


_VALID_MOODS = VALID_MOODS
_VALID_STABILITY = VALID_STABILITY
_VALID_COGNITION_TYPES = VALID_COGNITION_TYPES
_VALID_SERVES = VALID_SERVES


async def npc_genesis(state: AgentState) -> dict:
    npcs_raw = state.get("npcs") or {}
    if not npcs_raw:
        raise RuntimeError("[npc_genesis] 收到空 NPC 名单，world_genesis 未生成 NPC")

    style_profile = state.get("style_profile") if isinstance(state.get("style_profile"), dict) else {}

    backstory = state.get("backstory", "")
    ending_goal = state.get("ending_goal", "")
    global_truth = state.get("global_truth", "")

    # 场景名列表（供 props 引用校验）
    scenes_raw = state.get("scenes") or {}
    scene_names = list(scenes_raw.keys()) if isinstance(scenes_raw, dict) else []
    if not scene_names:
        scene_names = state.get("available_scenes") or []

    # 物品名列表（供 secrets/weakness 引用校验）
    item_names = []
    for sdef in (scenes_raw.values() if isinstance(scenes_raw, dict) else []):
        if isinstance(sdef, dict):
            for it in (sdef.get("items") or []):
                if isinstance(it, dict) and it.get("name"):
                    item_names.append(str(it["name"]).strip())
    for p in (state.get("props") or []):
        if isinstance(p, dict) and p.get("name"):
            item_names.append(str(p["name"]).strip())
    item_names = list(dict.fromkeys(item_names))

    roster = []
    for nid, info in npcs_raw.items():
        roster.append({
            "id": nid,
            "name": info.get("name", ""),
            "role": info.get("role", ""),
            "location": info.get("location", ""),
        })

    prompt = build_npc_prompt(
        backstory=backstory,
        ending_goal=ending_goal,
        global_truth=global_truth,
        roster=roster,
        style_profile=style_profile,
        scenes=scene_names,
        props=item_names,
    )

    try:
        response = await world_llm.ainvoke([HumanMessage(content=prompt)])
        parsed = _parse_json(response.content)
    except Exception as e:
        import traceback
        print(f"[npc_genesis 生成失败] {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        traceback.print_exc()
        raise RuntimeError(f"[npc_genesis] 血肉生成失败：{e}")

    npcs_parsed = None
    if isinstance(parsed, dict):
        if "npcs" in parsed and isinstance(parsed["npcs"], list):
            npcs_parsed = parsed["npcs"]
        elif "id" in parsed:
            npcs_parsed = [parsed]
    elif isinstance(parsed, list):
        npcs_parsed = parsed

    if not isinstance(npcs_parsed, list) or not npcs_parsed:
        print(f"[npc_genesis 解析失败] 原始输出: {str(parsed)[:500]}", file=sys.stderr)
        raise RuntimeError("[npc_genesis] 血肉生成返回为空或格式错误")

    flesh_by_id: Dict[str, dict] = {}
    for item in npcs_parsed:
        if isinstance(item, dict) and item.get("id"):
            flesh_by_id[str(item["id"])] = item

    npc_patches: Dict[str, dict] = {}
    missing = []

    for nid, info in npcs_raw.items():
        flesh = flesh_by_id.get(nid)
        if flesh is None:
            missing.append(f"{nid}(无返回)")
            continue

        # ---- 必填：background ----
        _background = str(flesh.get("background", "") or "").strip()
        if not _background:
            missing.append(f"{nid}.background")
            continue

        # ---- 必填：appearance ----
        _appearance = str(flesh.get("appearance", "") or "").strip()
        if not _appearance:
            missing.append(f"{nid}.appearance")
            continue

        # ---- 必填：attire ----
        _attire_raw = flesh.get("attire")
        _attire = {}
        if isinstance(_attire_raw, dict):
            for _ak, _av in _attire_raw.items():
                _av = str(_av or "").strip()
                if _av and _av not in ("无", "无无"):
                    _attire[str(_ak).strip()] = _av
        elif isinstance(_attire_raw, str) and _attire_raw.strip() and _attire_raw.strip() != "无":
            _attire = {"全装": _attire_raw.strip()}

        if not _attire:
            missing.append(f"{nid}.attire(空)")
            continue

        # ---- 必填：personality ----
        _personality = str(flesh.get("personality", "") or "").strip()
        if not _personality:
            missing.append(f"{nid}.personality")
            continue

        # ---- 必填：life_goal ----
        _life_goal = str(flesh.get("life_goal", "") or "").strip()
        if not _life_goal:
            missing.append(f"{nid}.life_goal")
            continue

        # ---- 必填：cognition ----
        _cognition_raw = flesh.get("cognition")
        _cognition = []
        if isinstance(_cognition_raw, list):
            for entry in _cognition_raw:
                if not isinstance(entry, dict):
                    continue
                content = str(entry.get("content", "") or "").strip()
                ctype = str(entry.get("type", "") or "").strip().lower()
                if not content or ctype not in _VALID_COGNITION_TYPES:
                    continue
                cleaned = {"content": content, "type": ctype}
                if ctype == "secret":
                    rc = str(entry.get("reveal_condition", "") or "").strip()
                    if rc:
                        cleaned["reveal_condition"] = rc
                    kb = entry.get("known_by")
                    if isinstance(kb, list):
                        cleaned["known_by"] = [str(k).strip() for k in kb if str(k).strip()]
                    else:
                        cleaned["known_by"] = []
                elif ctype == "belief":
                    try:
                        conf = int(entry.get("confidence", 50))
                    except (TypeError, ValueError):
                        conf = 50
                    cleaned["confidence"] = max(0, min(100, conf))
                elif ctype == "forbidden":
                    reason = str(entry.get("reason", "") or "").strip()
                    if reason:
                        cleaned["reason"] = reason
                _cognition.append(cleaned)

        # ---- 必填：mood ----
        _mood_raw = flesh.get("mood") if isinstance(flesh.get("mood"), dict) else {}
        _mood_current = str(_mood_raw.get("current", "") or "").strip()
        if _mood_current not in _VALID_MOODS:
            _mood_current = "平静"
        _mood_stability = str(_mood_raw.get("stability", "stable") or "stable").strip().lower()
        if _mood_stability not in _VALID_STABILITY:
            _mood_stability = "stable"
        _mood = {
            "current": _mood_current,
            "reason": str(_mood_raw.get("reason", "开局") or "开局").strip(),
            "since_tick": 0,
            "stability": _mood_stability,
        }

        # ---- 必填：core_drive ----
        _cg = flesh.get("core_drive")
        if not isinstance(_cg, dict):
            _cg = flesh.get("core_goal")  # 兼容旧字段名
        if not (isinstance(_cg, dict) and _cg.get("goal")):
            missing.append(f"{nid}.core_drive")
            continue
        try:
            _priority = int(_cg.get("priority", 100))
        except (TypeError, ValueError):
            _priority = 100
        _serves = str(_cg.get("serves", "self") or "self").strip().lower()
        if _serves not in _VALID_SERVES:
            _serves = "self"
        _core_drive = {
            "goal": str(_cg["goal"]).strip(),
            "priority": _priority,
            "serves": _serves,
        }

        # ---- 必填：affection ----
        try:
            _affection = int(flesh.get("affection", 100))
        except (TypeError, ValueError):
            missing.append(f"{nid}.affection")
            continue

        # ---- 必填：self_interest ----
        try:
            _self_interest = float(flesh.get("self_interest", 0.5))
        except (TypeError, ValueError):
            missing.append(f"{nid}.self_interest")
            continue
        _self_interest = max(0.0, min(1.0, _self_interest))

        # ---- 必填：moral_boundary ----
        try:
            _moral_boundary = float(flesh.get("moral_boundary", 0.5))
        except (TypeError, ValueError):
            missing.append(f"{nid}.moral_boundary")
            continue
        _moral_boundary = max(0.0, min(1.0, _moral_boundary))

        # ---- 必填：attachment ----
        try:
            _attachment = float(flesh.get("attachment", 0.5))
        except (TypeError, ValueError):
            missing.append(f"{nid}.attachment")
            continue
        _attachment = max(0.0, min(1.0, _attachment))

        # ---- 可选：fears ----
        _fears = []
        if isinstance(flesh.get("fears"), list):
            _fears = [str(f).strip() for f in flesh["fears"] if str(f).strip()]

        # ---- 可选：desires ----
        _desires = []
        if isinstance(flesh.get("desires"), list):
            _desires = [str(d).strip() for d in flesh["desires"] if str(d).strip()]

        # ---- 可选：perspective ----
        _perspective = str(flesh.get("perspective", "") or "").strip()

        # ---- 可选：contradictions ----
        _contradictions = []
        if isinstance(flesh.get("contradictions"), list):
            _contradictions = [str(c).strip() for c in flesh["contradictions"] if str(c).strip()]

        # ---- 可选：impulsivity ----
        try:
            _impulsivity = float(flesh.get("impulsivity", 0.5))
        except (TypeError, ValueError):
            _impulsivity = 0.5
        _impulsivity = max(0.0, min(1.0, _impulsivity))

        # ---- 可选：weakness ----
        _w_raw = flesh.get("weakness") if isinstance(flesh.get("weakness"), dict) else {}
        _weakness = {}
        if _w_raw.get("trigger") and _w_raw.get("reaction"):
            _kws = _w_raw.get("trigger_keywords")
            _clean_kws = []
            if isinstance(_kws, list):
                _clean_kws = [str(k).strip() for k in _kws if str(k).strip()][:4]
            if _clean_kws:
                _weakness = {
                    "trigger": str(_w_raw["trigger"]).strip(),
                    "trigger_keywords": _clean_kws,
                    "state": "closed",
                    "reaction": str(_w_raw["reaction"]).strip(),
                }

        # ---- 可选：social_initiative ----
        try:
            _social_initiative = float(flesh.get("social_initiative", 0.4))
        except (TypeError, ValueError):
            _social_initiative = 0.4
        _social_initiative = max(0.0, min(1.0, _social_initiative))

        # ---- 组装 patch ----
        npc_patch = {
            "background": _background,
            "appearance": _appearance,
            "attire": _attire,
            "personality": _personality,
            "life_goal": _life_goal,
            "cognition": _cognition,
            "mood": _mood,
            "core_drive": _core_drive,
            "affection": _affection,
            "self_interest": _self_interest,
            "moral_boundary": _moral_boundary,
            "attachment": _attachment,
        }

        # 可选字段：有值才写入
        if _fears:
            npc_patch["fears"] = _fears
        if _desires:
            npc_patch["desires"] = _desires
        if _perspective:
            npc_patch["perspective"] = _perspective
        if _contradictions:
            npc_patch["contradictions"] = _contradictions
        npc_patch["impulsivity"] = _impulsivity
        npc_patch["social_initiative"] = _social_initiative
        if _weakness:
            npc_patch["weakness"] = _weakness
        else:
            npc_patch["weakness"] = {}

        npc_patches[nid] = npc_patch

    if missing:
        raise RuntimeError(f"[npc_genesis] 以下 NPC 必填字段缺失：{missing}")

    return {"npcs": npc_patches}
