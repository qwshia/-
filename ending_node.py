"""结局判定节点：judge_ending —— 玩家推理 vs 导演设计的结局列表对比判定。

第一步：系统先判时间——导演设了时限且轮数已走满，直接按超时 LOSE（不调 LLM）。
第二步（未超时/不限时）：把玩家推理与 global_truth / endings 对比，由判定官从结局列表
    中选最匹配的一个结局（单结局或多结局均可），输出"结局名 + 复盘"。
判定结果以 '结局：' 开头写进 history。结局判定不结束游戏：揭晓后仍可继续，也可再次输入改判。
"""
from langchain_core.messages import HumanMessage

from config import judge_llm
from state import AgentState
from utils import time_is_up


async def judge_ending(state: AgentState) -> dict:
    player_answer = state.get("user_msg", "").replace("结局是", "", 1).strip()
    if not player_answer:
        return {
            "history": [{"speaker": "system",
                         "content": "【系统】你似乎想下结论，但还没说出完整的推理。把你掌握的线索和你的怀疑说清楚。"}],
        }

    effective_progress = float(state.get("time_progress") or 0.0)

    if time_is_up(state.get("time_start", "清晨"), state.get("time_limit", ""), effective_progress):
        limit = state.get("time_limit", "")
        goal = (state.get("ending_goal", "") or "").strip()[:60]
        truth = (state.get("global_truth", "") or "")[:200]
        return {
            "history": [{"speaker": "system", "content": (
                f"结局：功败垂成 - 时间已到 {limit}，关键节点已经来临（{goal}），你来不及完成布局。"
                f"复盘：{truth}")}],
        }

    clues = state.get("player_revealed_clues") or []
    truth_leads = state.get("truth_leads") or []
    leads_txt = "\n".join(f"- {l.get('fact')}" for l in truth_leads
                          if isinstance(l, dict) and l.get("fact")) or "（导演未拆分，以真相全文为准）"
    endings = state.get("endings") or []
    endings_txt = ""
    if endings:
        endings_txt = "\n".join(
            f"{e.get('id', '')}: {e.get('name', '')} —— 达成条件：{e.get('condition', '')}；结局画面：{e.get('desc', '')}"
            for e in endings if isinstance(e, dict) and e.get("name")
        )
    if not endings_txt:
        endings_txt = "（未配置结局列表，按真相达成与否判定）"

    prompt = (
        "你是最终判定官. 对比玩家推理与导演结局列表.\n"
        f"[真相]\n{state.get('global_truth', '')}\n"
        f"[关键事实点]\n{leads_txt}\n"
        f"[主线目标]\n{state.get('ending_goal', '')}\n"
        f"[时间状态]\n"
        + (f"当前:{state.get('time_phase', '清晨')};关键点:{state.get('time_limit')}\n" if state.get('time_limit') else "(不限时)\n")
        + f"[玩家线索]\n{(';'.join(clues)) or '(无)'}\n"
        f"[结局列表]\n{endings_txt}\n"
        f"[玩家推理]\n{player_answer}\n\n"
        "[判定规则]\n"
        "1. 从[结局列表]中选一个最符合玩家推理结果的结局：对照[真相]与[关键事实点]，核心方向对上选达成类结局，"
        "部分对上选部分达成类结局，完全错误选失败类结局.\n"
        "2. 若结局列表只有1个结局，则按其达成条件判断：满足则输出该结局，不满足则输出'功败垂成'作为失败结局.\n"
        "3. 名词记错不算错, 方向对即对上; 只有核心方向完全错误才判失败.\n"
        "[输出格式-严格]\n"
        "  结局：<结局名> - <80-120字复盘>\n"
        "直接输出文本, 不要JSON."
    )

    try:
        response = await judge_llm.ainvoke([HumanMessage(content=prompt)])
        content = response.content
    except Exception:
        content = "结局：判定失败 - 判定时发生网络错误。"
    return {
        "history": [{"speaker": "system", "content": content}],
    }
