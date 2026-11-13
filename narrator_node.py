"""并发收敛节点：narrator_node
单并发：只负责旁观者群像。
多并发：主焦点台词已由流式播出，本节点只输出其它 NPC 的痕迹。
"""
import re
from langchain_core.messages import HumanMessage
from config import actor_llm
from state import AgentState


async def narrator_node(state: AgentState) -> dict:
    history = state.get("history", [])
    user_idx = -1
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("speaker") == "user":
            user_idx = i
            break
    if user_idx == -1:
        return {}

    active_targets = state.get("active_targets") or []
    npcs = state.get("npcs") or {}

    observer_msgs = []
    focus_msgs = []
    for e in history[user_idx + 1:]:
        sp = e.get("speaker")
        if sp in ("system", "user"):
            continue
        content = str(e.get("content", "")).strip()
        if not content:
            continue
        content = re.sub(r"[\[【]\s*(do|aff|options|intent|emotion).*?[\]】]", "", content, flags=re.IGNORECASE).strip()
        if not content or content == "(发呆)":
            continue
        if sp in active_targets:
            focus_msgs.append({"speaker": sp, "content": content})
        else:
            observer_msgs.append({"speaker": sp, "content": content})

    # 多并发（@全部）：代码选主焦点 + 打包数据，LLM 生成群像描写
    if len(focus_msgs) >= 2:
        scored = sorted(focus_msgs, key=lambda m: 0 if any(k in m["content"] for k in ("[do_items:", "[weakness:", "[surrender:")) else 1)
        main = scored[0]
        others = scored[1:]

        main_info = npcs.get(main["speaker"]) or {}
        main_name = main_info.get("name", main["speaker"])
        main_content = re.sub(r"[\[【(（]\s*(do|aff|options?|intent|emotion|weakness|surrender).*?[\]】)）]", "", main["content"]).strip()
        main_ripple = str(main_info.get("last_ripple", "") or "").strip()
        main_options = list(main_info.get("last_options") or [])

        # 代码打包原始数据交给 LLM
        others_data = []
        for m in others[:2]:
            other_info = npcs.get(m["speaker"]) or {}
            other_name = other_info.get("name", m["speaker"])
            other_content = re.sub(r"[\[【(（]\s*(do|aff|options?|intent|emotion|weakness|surrender).*?[\]】)）]", "", m["content"]).strip()
            others_data.append({
                "名字": other_name,
                "余波": str(other_info.get("last_ripple", "") or "").strip(),
                "台词或动作": other_content,
            })

        data_block = (
            f"【主焦点NPC（台词原样保留，可润色标点）】\n"
            f"名字：{main_name}\n"
            f"余波：{main_ripple or '（无）'}\n"
            f"台词：{main_content}\n"
            f"\n【其他NPC（台词删掉，只留动作/神态作余光痕迹，最多2条）】\n"
        )
        if others_data:
            for o in others_data:
                data_block += f"- {o['名字']}：余波「{o['余波'] or '无'}」台词或动作「{o['台词或动作']}」\n"
        else:
            data_block += "（无）\n"

        prompt = f"""你是互动叙事的小说旁白。刚才玩家对着一群人说话，好几位 NPC 同时做出了反应。

请根据下方数据，写一段画面感的群像描写。规则：
1. 主焦点 NPC 的台词**必须保留**，可以润色标点、重新组织语序，但不能删减核心信息，不能编造新台词。
2. 把余波（神态/动作）自然融入，不要重复堆叠括号。
3. 其他 NPC 的台词全部砍掉，只留一个最能体现立场的动作或神态作为痕迹。最多 2 条。
4. 不要写成"A说、B说"的格式。
5. 字数 80-150 字之间。
6. 严禁编造数据里没有的人物、物品、地点。

{data_block}

直接输出群像描写文本，不要加任何前缀或标签。"""

        try:
            resp = await actor_llm.ainvoke([HumanMessage(content=prompt)])
            narrator_text = (resp.content or "").strip()
        except Exception:
            return {}

        if not narrator_text:
            return {}

        out_hist = [{"speaker": "system", "content": f"【群像】{narrator_text}"}]
        if main_options:
            out_hist.append({"speaker": "system", "content": f"【可快捷回复】 {' | '.join(main_options)}"})

        return {
            "history": out_hist,
            "npc_converged": True,
        }

    # 单并发：走旁观者群像
    if not observer_msgs:
        return {}

    obs_txt = "\n".join([f"【{npcs.get(m['speaker'], {}).get('name', m['speaker'])}】{m['content']}" for m in observer_msgs])

    prompt = f"""你是互动叙事的小说旁白。
刚才玩家在和其他人互动，那个人的完整反应已经呈现给玩家了。
现在请根据以下【旁观者的反应】，写一段极简的群像描写，烘托房间里的气氛。
绝对不要复述玩家和主目标的话，只需描写旁观者的神态或动作。

【旁观者的反应】
{obs_txt}

【铁律】
1. 字数控制在 80 字以内。
2. 绝对不能出现"主目标"（被玩家对话的人）的名字或动作。
3. 把旁观者的台词精简掉，只保留 1~2 个最生动的动作/神态。
4. 直接输出画面感文本，不要写成"A说、B说"。
"""
    try:
        resp = await actor_llm.ainvoke([HumanMessage(content=prompt)])
        narrator_text = (resp.content or "").strip()
    except Exception:
        return {}

    if not narrator_text:
        return {}

    return {
        "history": [{"speaker": "system", "content": f"【旁白】{narrator_text}"}],
        "npc_converged": True,
    }