"""多方会话执行器：multi_party_chat
功能：让 NPC 之间进行多回合博弈交流。支持阵营维护、试探、隐瞒与结盟。
设计：串行执行，不直接修改全局 state，只返回 transcript 和记忆增量，由调用方（如 p2p_node）合并。
"""
import random
from langchain_core.messages import HumanMessage

from config import actor_llm
from utils import _format_attire


def _build_npc_profile(info: dict, npc_id: str) -> str:
    """构建 NPC 的深度档案，包含特质、目标、认知与记忆"""
    name = info.get("name", npc_id)
    attire = _format_attire(info.get("attire", {}))
    
    traits = info.get("personality_traits") or []
    traits_txt = "、".join([f"{t.get('trait')}(强度{t.get('intensity')})" for t in traits if isinstance(t, dict)]) or "无"
    
    core_goal = info.get("core_goal") or {}
    goal_txt = core_goal.get("goal", "无长期执念")
    
    secrets = "\n".join([f"- {s}" for s in (info.get("secrets") or [])]) or "（无）"
    forbidden = "\n".join([f"- {f}" for f in (info.get("forbidden") or [])]) or "（无）"
    
    mem = info.get("memory") or {"l1": []}
    l1 = list(mem.get("l1") or [])
    recent_mem = "\n".join(l1[-3:]) if l1 else "（暂无近期记忆）"
    
    return f"""姓名：{name} | 身份：{info.get('role', '路人')} | 当前衣着：{attire}
性格：{info.get('personality', '普通')} | 特质：{traits_txt}
真实目标：{goal_txt}
[认知状态]
确知事实：
{secrets}
极力隐瞒：
{forbidden}
[近期记忆]
{recent_mem}
"""


def _build_chat_prompt(my_info: dict, my_id: str, other_info: dict, other_id: str, history: str, topic: str) -> str:
    """构建极致的博弈对话 Prompt"""
    my_name = my_info.get("name", my_id)
    other_name = other_info.get("name", other_id)
    
    my_profile = _build_npc_profile(my_info, my_id)
    other_profile = _build_npc_profile(other_info, other_id)
    
    # 动态推演阵营与博弈倾向
    stance_instruction = (
        "【关系推演与博弈铁律】\n"
        "1. 不要做无情的复读机。你需要根据双方的[真实目标]、[确知事实]与[极力隐瞒]，自行判断你与对方此刻的【关系立场】。\n"
        "2. 如果你们利益一致或同属一阵营，你要试探对方的口风，互相打掩护；如果你们利益冲突，你要用语言试探、挖坑、夹枪带棒，甚至逼问。\n"
        "3. 绝不允许主动交代自己的核心秘密。如果对方戳中你的软肋，你要通过转移话题、反问、或者用假话去掩盖。\n"
        "4. 玩家正在一旁看着。你可以刻意说给玩家听，也可以故意避讳玩家，这取决于你的性格和当前目的。\n"
    )

    # 如果历史为空，说明是开场
    if not history.strip():
        action_instruction = f"此刻你主动开口和【{other_name}】搭话。你可以借机寒暄、试探对方近况，或者直接切入正题。"
    else:
        action_instruction = f"这是【{other_name}】刚才对你说的话：\n{history}\n\n请立刻给出你的回应，用你的台词去接住话头并达成你的目的。"

    prompt = f"""你正在参与一场极为真实的互动叙事角色扮演。
你现在的身份是【{my_name}】，正在和【{other_name}】交谈。
当前场景话题：{topic}

[你的角色档案]
{my_profile}

[对方的角色档案（这是你通过观察和接触能了解到的对方信息）]
{other_profile}

{stance_instruction}

[对话要求]
{action_instruction}
1. 你的回复必须完全符合你的人物特质、情绪和真实目的。
2. 语句必须精简有力，格式严格为：`(动作/神态) 说话内容`，字数控制在 30~80 字。
3. 绝对不允许代写对方的台词，绝对不允许跳出角色。
4. 提到的地点、人物必须已经在上述档案或记忆中真实存在，禁止瞎编。

请直接输出你的台词："""

    return prompt


async def multi_party_chat(participants: list[str], topic: str, max_rounds: int, state: dict) -> dict:
    """
    执行多方会话的主控函数。
    participants: 参与者的 npc_id 列表（如 ["npc_1", "npc_2"]）
    topic: 玩家给的交流主题或指令内容
    max_rounds: 最大对话轮数（通常设为 len(participants) * 2）
    """
    npcs = state.get("npcs") or {}
    
    # 确保所有参与者都在场（不强制要求玩家也在场，测试阶段放开视野限制）
    valid_participants = [p for p in participants if p in npcs]
    if len(valid_participants) < 2:
        return {"transcript": "（对话无法进行：参与者不存在。）", "memory_patches": {}}
    
    # 确保他们处于同一个场景（不再依赖玩家的 current_scene）
    loc_a = npcs[valid_participants[0]].get("location")
    loc_b = npcs[valid_participants[1]].get("location")
    if loc_a != loc_b:
        return {"transcript": "（对话无法进行：参与者不在同一场景。）", "memory_patches": {}}
        
    scene = loc_a
        
    chat_log = []
    memory_patches = {pid: [] for pid in valid_participants}
    
    # 确定谁先开口。如果玩家指定了顺序（比如 @1和@2交流），就让第一个被指定的先开口。
    current_speaker = valid_participants[0]
    other_speaker = valid_participants[1] # 如果有多人，暂定针对另一人，后续可扩展为全体广播

    # 最近一句的历史缓存
    recent_statement = ""

    for round_idx in range(max_rounds):
        sp_info = npcs.get(current_speaker, {})
        ot_info = npcs.get(other_speaker, {})
        
        prompt = _build_chat_prompt(
            my_info=sp_info, my_id=current_speaker,
            other_info=ot_info, other_id=other_speaker,
            history=recent_statement, topic=topic
        )
        
        try:
            resp = await actor_llm.ainvoke([HumanMessage(content=prompt)])
            reply = (resp.content or "").strip()
            # 清洗可能漏出的标签
            import re
            reply = re.sub(r"\[aff:.*?\]", "", reply)
            reply = re.sub(r"\[intent:.*?\]", "", reply).strip()
        except Exception as e:
            reply = "（沉默不语。）"
            
        sp_name = sp_info.get("name", current_speaker)
        chat_log.append(f"{sp_name}：{reply}")
        
        # 记录为两人的记忆
        memory_patches[current_speaker].append(f"我对{ot_info.get('name', other_speaker)}说：{reply}")
        memory_patches[other_speaker].append(f"{sp_name}对我说：{reply}")
        
        recent_statement = reply
        
        # 交换发言权（如果是多人在场，这里可以加逻辑轮换，目前 2 人纯粹对切）
        current_speaker, other_speaker = other_speaker, current_speaker

    # 组装最终的 transcript 文本
    transcript = "\n".join(chat_log)
    
    return {
        "transcript": transcript,
        "memory_patches": memory_patches
    }