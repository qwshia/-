"""生活层更新：npc_life_tick —— 纯代码 + 批量提炼 life_events。"""
import random
from langchain_core.messages import HumanMessage
from config import memory_llm
from state import AgentState
from utils import _parse_json
from constants import VALID_MOODS, VALID_STABILITY, VALID_TIME_PHASES


async def update_npc_life(state: AgentState, current_tick: int) -> dict:
    # 触发条件已由 graph.py 在外层控制（跨越时间档），此处直接执行

    npcs = state.get("npcs") or {}
    time_phase = state.get("time_phase", "")
    patches = {}
    life_ui_events = []

    # ==========================================
    # 1. 准备批量提炼的 Prompt
    # ==========================================
    if npcs:
        recent_world_events = "；".join((state.get("world_events") or [])[-5:]) or "（无）"

        npc_context_lines = []
        for nid, info in npcs.items():
            name = info.get("name", nid)
            goal = info.get("core_drive") or info.get("core_goal") or {}
            goal_txt = goal.get("goal", "无明确执念") if isinstance(goal, dict) else "无明确执念"
            recent_l1 = (info.get("memory") or {}).get("l1") or []
            dialogue_txt = " | ".join(recent_l1[-3:]) if recent_l1 else "（最近无对话）"
            npc_context_lines.append(f"【{nid} - {name}】\n执念：{goal_txt}\n近期对话：{dialogue_txt}")

        npc_context = "\n\n".join(npc_context_lines)

        prompt = f"""你是游戏系统的记忆提炼器.
[最近世界事件]
{recent_world_events}
[NPC近期状态]
{npc_context}

[任务] 判断每个NPC是否经历了值得写入长期记忆的[主观事件].
[规则]
1. 只有让该NPC产生情绪波动/利益受损/认知改变/安全感丧失的事, 才算[值得记住]. 日常寒暄/无意义对话/与NPC无关的事件, 绝不生成.
2. 无值得记住的事, 该NPC数组必须为空.
3. event必须写NPC主观视角(如'我发现仓库门没锁'), 禁止上帝视角(如'玩家去了仓库').
4. emotion只能从[平静/焦虑/烦躁/愉悦/恐惧/压抑]选一个.

[输出格式-只输出JSON]
{{"npc_1": [{{"event": "...", "emotion": "..."}}], "npc_2": []}}"""

        try:
            resp = await memory_llm.ainvoke([HumanMessage(content=prompt)])
            parsed = _parse_json(resp.content)
        except Exception as e:
            print(f"[npc_life_tick LLM提炼失败] {e}")
            parsed = {}
    else:
        parsed = {}

    # ==========================================
    # 2. 更新动作与情绪
    # ==========================================
    for nid, info in npcs.items():
        p = {}

        # a. 提取 life_events
        new_events = parsed.get(nid)
        if isinstance(new_events, list) and new_events:
            old_events = list(info.get("life_events") or [])
            for ev in new_events:
                if isinstance(ev, dict) and ev.get("event"):
                    old_events.append({
                        "tick": current_tick,
                        "event": str(ev.get("event")).strip(),
                        "emotion": str(ev.get("emotion", "平静")).strip(),
                        "source": "world"
                    })
            # 保留最近 10 条
            if len(old_events) > 10:
                old_events = old_events[-10:]
            p["life_events"] = old_events

            # 如果有新经历，按最新经历的情绪覆盖当前 mood
            last_ev_emo = old_events[-1].get("emotion", "平静")
            _stability = mood_dict.get("stability", "stable") if (mood_dict := (info.get("mood") or {})) else "stable"
            p["mood"] = {
                "current": last_ev_emo,
                "reason": f"经历：{old_events[-1].get('event', '')}",
                "since_tick": current_tick,
                "stability": _stability,
            }

        # b. 更新 current_action
        routine = info.get("daily_routine") or []
        matched_actions = []
        for r in routine:
            if isinstance(r, dict) and r.get("phase") == time_phase:
                matched_actions.extend(r.get("actions", []))

        if matched_actions:
            act = random.choice(matched_actions)
        else:
            if time_phase in ("深夜", "凌晨"):
                act = "睡觉"
            elif time_phase in ("正午", "午后"):
                act = "打盹"
            else:
                act = "发呆"

        old_act = (info.get("current_action") or {}).get("action", "")
        if old_act != act:
            p["current_action"] = {"action": act, "since_tick": current_tick}
            # 日常动作变化 → 发气泡事件（前端 NPC 头顶气泡展示生活切片）
            life_ui_events.append({
                "type": "npc_bubble",
                "npc_id": nid,
                "npc_name": str(info.get("name", nid)),
                "text": f"正在{act}…",
            })

        # c. 基础情绪偏移（如果本轮没生成新事件，用时间兜底）
        if "mood" not in p:
            mood_dict = info.get("mood") or {}
            current_mood = mood_dict.get("current", "平静")
            stability = mood_dict.get("stability", "stable")
            base_mood = current_mood
            # 只在当前是平静时，深夜才转压抑。不再强行把非平静转平静。
            if time_phase in ("深夜", "凌晨") and current_mood == "平静":
                base_mood = "压抑"
            # fragile 的 NPC 在压力下更容易 mood swing
            if stability == "fragile" and current_mood in ("平静", "愉悦"):
                if random.random() < 0.3:
                    base_mood = "焦虑"
            if base_mood != current_mood:
                p["mood"] = {"current": base_mood, "reason": f"时间到了{time_phase}", "since_tick": current_tick, "stability": stability}

        # d. 社交欲望生成：空闲 NPC 主动找人交流（提高频率 + 支持跨场景 + 双向续聊）
        # 条件：当前无明确意图、且 social_initiative 概率发起
        _si = float(info.get("social_initiative", 0.4) or 0.4)
        if not info.get("current_intent") and random.random() < _si:
            # 优先找近期对话过的 NPC 续聊（自然产生 B→A 的回应方开口），其次同场景，最后跨场景随机
            recent_names = []
            for _m in (info.get("memory") or {}).get("l1") or []:
                for _n2, _i2 in npcs.items():
                    if _n2 != nid and _i2.get("name") and _i2["name"] in str(_m):
                        recent_names.append(_i2["name"])
                        break
            candidates = []
            if recent_names:
                candidates = [nm for nm in recent_names if any(i2.get("name") == nm for i2 in npcs.values())]
            if not candidates:
                candidates = [i2.get("name") for n2, i2 in npcs.items()
                              if i2.get("location") == info.get("location") and n2 != nid and i2.get("name")]
            if candidates:
                target_name = random.choice(candidates)
                p["pending_intent"] = [{
                    "kind": "bg_chat",
                    "payload": target_name,
                    "remain": 1,
                    "source": "autonomy"
                }]

        if p:
            patches[nid] = p

    if patches or life_ui_events:
        out = {}
        if patches:
            out["npcs"] = patches
        if life_ui_events:
            out["ui_events"] = life_ui_events
        return out
    return {}
