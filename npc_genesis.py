"""NPC 血肉生成节点：npc_genesis

读 world_genesis 输出的 NPC 名单（骨架），为每个 NPC 生成血肉：
- 骨架必有：personality_traits / goal / daily_routine / mood
- 血肉可选：core_goal / short_term_goal / secrets / forbidden / hallucinations / weakness

一次 LLM 调用生成全部 NPC。独立生成，不互相参照。骨架字段缺则报错，不静默兜底。
"""
import json
import sys
from typing import Dict

from langchain_core.messages import HumanMessage

from config import world_llm
from state import AgentState
from utils import _parse_json


_VALID_MOODS = {"平静", "焦虑", "烦躁", "愉悦", "恐惧", "压抑"}
_VALID_WEAKNESS_TYPES = {"fear", "guilt", "greed", "pride", "attachment"}
_VALID_PHASES = ["清晨", "上午", "正午", "午后", "黄昏", "夜晚", "深夜", "凌晨"]


async def npc_genesis(state: AgentState) -> dict:
    npcs_raw = state.get("npcs") or {}
    if not npcs_raw:
        raise RuntimeError("[npc_genesis] 收到空 NPC 名单，world_genesis 未生成 NPC")

    roster = []
    for nid, info in npcs_raw.items():
        roster.append({
            "id": nid,
            "name": info.get("name", ""),
            "role": info.get("role", ""),
            "personality": info.get("personality", ""),
            "appearance": info.get("appearance", ""),
            "attire": info.get("attire", {}),
            "location": info.get("location", ""),
        })

    backstory = state.get("backstory", "")
    ending_goal = state.get("ending_goal", "")
    global_truth = state.get("global_truth", "")

    prompt = f"""你是角色铸造师。给定一个剧本的 NPC 名单，为每个 NPC 生成血肉。只输出严格 JSON，不要任何额外文字、说明或 markdown 标记。

[剧本背景]
backstory: {backstory}
ending_goal: {ending_goal}
global_truth: {global_truth}

[NPC 名单]
{json.dumps(roster, ensure_ascii=False, indent=2)}

[任务]
为每个 NPC 生成以下字段：

[骨架字段 - 必填]
1. personality_traits: 数组，2~4 条特质，结构 [{{"trait": "多疑", "intensity": 80}}]
   - trait 必须具体，禁止使用"普通""温和""善良"这类空话
   - intensity 为 0-100 的整数
2. goal: 字符串，一句话写"这个人想过什么日子"（模糊的人生目标，不是任务）
3. daily_routine: 数组，3~5 条日常行为，结构 [{{"phase": "清晨", "actions": ["擦杯子", "看窗外"]}}]
   - phase 只能取：清晨 / 上午 / 正午 / 午后 / 黄昏 / 夜晚 / 深夜 / 凌晨
   - actions 每个时段 1~3 个具体日常动作，必须具体（写"擦杯子"不写"工作"）
4. mood: 对象，结构 {{"current": "平静", "reason": "开局", "since_tick": 0}}
   - current 只能取：平静 / 焦虑 / 烦躁 / 愉悦 / 恐惧 / 压抑

[血肉字段 - 可选，按剧本类型决定]
5. short_term_goal: 对象 {{"goal": "...", "source": "core_goal|daily|player"}} 或 null
6. core_goal: 对象 {{"goal": "...", "priority": 100}} 或 null
7. secrets: 数组 [{{"fact": "...", "reveal_condition": "..."}}] 或 []
8. forbidden: 数组 [{{"fact": "...", "conceal_condition": "..."}}] 或 []
9. hallucinations: 数组 [{{"belief": "...", "source": "..."}}] 或 []
10. weakness: 对象 {{"trigger": "...", "trigger_keywords": ["..."], "type": "fear", "state": "closed", "reaction": "..."}} 或 {{}}

[生成约束]
1. 每个 NPC 独立生成，禁止互相参照。
2. 剧本类型决定血肉量：悬疑/推理/密室类，至少 1 个 NPC 有 secrets；日常/治愈/田园类，可以全部 NPC 都没有 core_goal。
3. 不强制每个 NPC 都有 core_goal/secrets/forbidden/weakness。村民可以全无，凶手才有全套。
4. 若 global_truth 描述有人做了坏事，至少 1 个 NPC 知道（secrets 里包含这件事）。这个"知情者"不一定是敌人，也可以是帮玩家的人。
5. 两个 NPC 的 core_goal 或 personality_traits 高度相似时，重写其中一个，避免同质化。
6. weakness.trigger_keywords 2~4 个短语，禁止用"你""什么""知道"这类无区分度通用词。
7. weakness.type 只能取：fear / guilt / greed / pride / attachment。
8. weakness.reaction 必须是具体行为，其中提到的物品/信息必须已在剧本中出现。
9. short_term_goal.source 只能取：core_goal（服务长期目标）/ daily（日常小事）/ player（针对玩家）。

[输出格式]
严格 JSON，无其他文字：
{{
  "npcs": [
    {{
      "id": "npc_1",
      "personality_traits": [{{"trait": "多疑", "intensity": 80}}],
      "goal": "...",
      "daily_routine": [{{"phase": "清晨", "actions": ["..."]}}],
      "mood": {{"current": "平静", "reason": "开局", "since_tick": 0}},
      "short_term_goal": null,
      "core_goal": null,
      "secrets": [],
      "forbidden": [],
      "hallucinations": [],
      "weakness": {{}}
    }}
  ]
}}"""

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

        _traits = flesh.get("personality_traits")
        if not isinstance(_traits, list) or not _traits:
            missing.append(f"{nid}.personality_traits")
            continue
        _clean_traits = []
        for t in _traits:
            if isinstance(t, dict) and t.get("trait"):
                try:
                    _intensity = int(t.get("intensity", 50))
                except (TypeError, ValueError):
                    _intensity = 50
                _clean_traits.append({
                    "trait": str(t["trait"]).strip(),
                    "intensity": max(0, min(100, _intensity)),
                })
        if not _clean_traits:
            missing.append(f"{nid}.personality_traits(空)")
            continue

        _goal = str(flesh.get("goal", "") or "").strip()
        if not _goal:
            missing.append(f"{nid}.goal")
            continue

        _routine = flesh.get("daily_routine")
        if not isinstance(_routine, list) or not _routine:
            missing.append(f"{nid}.daily_routine")
            continue
        _clean_routine = []
        for r in _routine:
            if isinstance(r, dict) and r.get("phase") in _VALID_PHASES:
                _actions = r.get("actions")
                if isinstance(_actions, list):
                    _clean_actions = [str(a).strip() for a in _actions if str(a).strip()]
                    if _clean_actions:
                        _clean_routine.append({"phase": r["phase"], "actions": _clean_actions})
        if not _clean_routine:
            missing.append(f"{nid}.daily_routine(空)")
            continue

        _mood_raw = flesh.get("mood") if isinstance(flesh.get("mood"), dict) else {}
        _mood_current = str(_mood_raw.get("current", "") or "").strip()
        if _mood_current not in _VALID_MOODS:
            _mood_current = "平静"
        _mood = {
            "current": _mood_current,
            "reason": str(_mood_raw.get("reason", "开局") or "开局").strip(),
            "since_tick": 0,
        }

        _stg = flesh.get("short_term_goal")
        if isinstance(_stg, dict) and _stg.get("goal"):
            _short_term_goal = {
                "goal": str(_stg["goal"]).strip(),
                "source": str(_stg.get("source", "daily") or "daily").strip(),
            }
        else:
            _short_term_goal = None

        _cg = flesh.get("core_goal")
        if isinstance(_cg, dict) and _cg.get("goal"):
            try:
                _priority = int(_cg.get("priority", 100))
            except (TypeError, ValueError):
                _priority = 100
            _core_goal = {"goal": str(_cg["goal"]).strip(), "priority": _priority}
        else:
            _core_goal = None

        def _clean_list(raw, keys):
            if not isinstance(raw, list):
                return []
            out = []
            for x in raw:
                if not isinstance(x, dict):
                    continue
                cleaned = {}
                for k in keys:
                    v = str(x.get(k, "") or "").strip()
                    if v:
                        cleaned[k] = v
                if cleaned:
                    out.append(cleaned)
            return out

        _secrets = _clean_list(flesh.get("secrets"), ["fact", "reveal_condition"])
        _forbidden = _clean_list(flesh.get("forbidden"), ["fact", "conceal_condition"])
        _hallucinations = _clean_list(flesh.get("hallucinations"), ["belief", "source"])

        _w_raw = flesh.get("weakness") if isinstance(flesh.get("weakness"), dict) else {}
        if _w_raw.get("trigger") and _w_raw.get("reaction"):
            _kws = _w_raw.get("trigger_keywords")
            _clean_kws = []
            if isinstance(_kws, list):
                _clean_kws = [str(k).strip() for k in _kws if str(k).strip()][:4]
            _wtype = str(_w_raw.get("type", "") or "").strip()
            if _wtype not in _VALID_WEAKNESS_TYPES:
                _wtype = "fear"
            if _clean_kws:
                _weakness = {
                    "trigger": str(_w_raw["trigger"]).strip(),
                    "trigger_keywords": _clean_kws,
                    "type": _wtype,
                    "state": "closed",
                    "reaction": str(_w_raw["reaction"]).strip(),
                }
            else:
                _weakness = {}
        else:
            _weakness = {}

        npc_patches[nid] = {
            "personality_traits": _clean_traits,
            "goal": _goal,
            "daily_routine": _clean_routine,
            "mood": _mood,
            "short_term_goal": _short_term_goal,
            "core_goal": _core_goal,
            "secrets": _secrets,
            "forbidden": _forbidden,
            "hallucinations": _hallucinations,
            "weakness": _weakness,
        }

    if missing:
        raise RuntimeError(f"[npc_genesis] 以下 NPC 骨架字段缺失：{missing}")

    return {"npcs": npc_patches}