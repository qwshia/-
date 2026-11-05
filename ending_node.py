"""结局判定节点：judge_ending —— 玩家推理 vs 导演设计真相对比判定。

第一步：系统先判时间——导演设了时限且轮数已走满（time_progress 越过 time_limit），
        直接按"超时未达成目标"判 LOSE（不调 LLM，省 token）。
第二步（未超时/不限时）：把玩家推理与 global_truth / ending_goal 宽松对比，
        核心大致对上即 WIN（提及伪善/隐藏动机等加分）。
判定结果以 'WIN: '/'LOSE: ' 开头写进 history。
结局判定【不结束游戏】：揭晓后玩家仍可继续对话/调查，也可再次输入"结局是"改判。
"""
from langchain_core.messages import HumanMessage

from config import judge_llm
from state import AgentState
from utils import time_is_up, time_step


async def judge_ending(state: AgentState) -> dict:
    player_answer = state.get("user_msg", "").replace("结局是", "", 1).strip()
    if not player_answer:
        return {
            "history": [{"speaker": "system",
                         "content": "【系统】你似乎想下结论，但还没说出完整的推理。把你掌握的线索和你的怀疑说清楚。"}],
        }

    stepped = time_step(state)
    effective_progress = float(stepped.get("time_progress") or state.get("time_progress") or 0.0)

    if time_is_up(state.get("time_start", "清晨"), state.get("time_limit", ""), effective_progress):
        limit = state.get("time_limit", "")
        goal = (state.get("ending_goal", "") or "").strip()[:60]
        truth = (state.get("global_truth", "") or "")[:200]
        return {
            "history": [{"speaker": "system", "content": (
                f"LOSE: 时间已到 {limit}，关键节点已经来临（{goal}），你来不及完成布局，功败垂成。"
                f"复盘：{truth}")}],
        }

    clues = state.get("player_revealed_clues") or []
    truth_leads = state.get("truth_leads") or []
    leads_txt = "\n".join(f"- {l.get('fact')}" for l in truth_leads
                          if isinstance(l, dict) and l.get("fact")) or "（导演未拆分，以真相全文为准）"
    prompt = (
        "你是最终判定官.玩家给出推理,与导演真相对比.\n\n"
        f"[真相]\n{state.get('global_truth', '')}\n"
        f"[关键事实点]\n{leads_txt}\n"
        f"[主线目标]\n{state.get('ending_goal', '')}\n"
        f"[时间状态]\n"
        + (f"当前:{state.get('time_phase', '清晨')};关键点:{state.get('time_limit')}\n" if state.get('time_limit') else "(不限时)\n")
        + f"\n[玩家线索]\n{(';'.join(clues)) or '(无)'}\n\n"
        f"[玩家推理]\n{player_answer}\n\n"
        "判定:\n"
        "1. 获胜前提:玩家推理与真相[核心语义]对上(幕后黑手/关键机制/道具用途/事件链);名词记错不算错,方向对即对上;只有核心方向完全错误才失败.\n"
        "2. 对照[关键事实点]逐条判断覆盖与正确性,覆盖核心事实点即 WIN.\n"
        "3. 加分:核心已对上时,额外提到伪善/隐藏动机/隐藏信息且方向对,仍 WIN;核心错误时加分不能单独 WIN.\n"
        "4. 超时:有关键时间点且轮数已超,即使核心正确也失败,复盘注明'超时失败'.\n"
        "5. 输出:WIN: [线路名] 或 LOSE: [线路名] 开头;正确先点明对在哪里再80-120字复盘;错误指明偏差再80-120字复盘.\n"
        "直接输出结果文本,不要 JSON."
    )
    
    try:
        response = await judge_llm.ainvoke([HumanMessage(content=prompt)])
        content = response.content
    except Exception:
        content = "LOSE: 判定时发生网络错误。"
    return {
        "history": [{"speaker": "system", "content": content}],
    }
