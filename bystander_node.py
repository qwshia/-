"""旁观者 NPC 执行器：bystander_worker

旁观者 NPC（非玩家当前交流对象）的行为：
- 做自己的事优先级 > 回话：只有玩家动作与自己相关（涉及自己的物品/名字/秘密）才插嘴。
- 不感兴趣时返回空，由生活层气泡展示其日常，不每条必回。
- 感兴趣时输出一句极短插嘴，作为气泡悬停头顶。
"""
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import HumanMessage

from config import actor_llm
from state import AgentState


def _is_interested(state: AgentState, info: dict, scene: str) -> tuple:
    """判断旁观者是否对玩家当前动作感兴趣。返回 (是否感兴趣, 兴趣原因)。"""
    name = str(info.get("name", "") or "").strip()
    user_msg = str(state.get("user_msg", "") or "")
    props = state.get("props") or []
    last_action = state.get("last_player_action") or {}
    target = str(last_action.get("target", "") or "").strip()
    action_type = str(last_action.get("action", "") or "").strip()

    # 1. 玩家话语提到自己名字
    if name and name in user_msg:
        return True, f"玩家提到了我"

    # 2. 玩家动作目标是自己持有的物品
    if target:
        for p in props:
            if isinstance(p, dict) and p.get("name") == target and p.get("holder") == name:
                return True, f"玩家动了我的东西「{target}」"

    # 3. 玩家动作直接针对自己
    if target and name and (target == name or name in target):
        return True, "玩家的动作直接冲着我"

    # 4. 玩家话语触碰到自己的秘密关键词
    secrets = [str(c.get("content", "")).strip() for c in (info.get("cognition") or []) if isinstance(c, dict) and c.get("type") == "secret" and c.get("content")]
    for s in secrets:
        if not s:
            continue
        for kw in s.split("。"):
            kw = str(kw).strip()
            if kw and len(kw) >= 2 and kw in user_msg:
                return True, "玩家戳到了我的秘密"

    # 5. 玩家调查/偷窃自己所在场景的私有物品
    if action_type in ("investigate", "steal") and target:
        my_items = [p.get("name") for p in props if isinstance(p, dict) and p.get("holder") == name]
        if target in my_items:
            return True, f"玩家盯上了我的「{target}」"

    return False, ""


async def bystander_worker(state: AgentState, config: RunnableConfig) -> dict:
    thread_id = ""
    if isinstance(config, dict):
        thread_id = config.get("configurable", {}).get("thread_id", "") if config.get("configurable") else ""
    else:
        thread_id = getattr(getattr(config, "configurable", {}), "thread_id", "") if hasattr(config, "configurable") else ""

    npc_id = state.get("npc_id", "")
    npcs = state.get("npcs") or {}
    info = npcs.get(npc_id) or {}
    scene = state.get("current_scene", "未知")
    if info.get("location") != scene:
        return {}

    interested, reason = _is_interested(state, info, scene)
    if not interested:
        # 做自己的事优先：不插嘴，返回空，生活层气泡负责展示日常
        return {}

    name = info.get("name", npc_id)
    role = info.get("role", "路人")
    personality = info.get("personality", "普通")
    user_msg = state.get("user_msg", "")

    prompt = f"""你是互动叙事中的旁观者NPC.
[你的身份] {name} / {role} / 性格{personality}
[你为什么想插嘴] {reason}
[玩家刚才的话] {user_msg}

[任务] 以旁观者身份插一句嘴.
[规则]
1. 只能输出一句极短台词, 总字数≤30字, 可带一个动作/神态.
2. 严禁输出英文/代码/乱码/标签(如[aff:]/[options:]/[intent:]).
3. 动作主体锁定: 玩家的行为只能作为你观察的对象, 绝不能把玩家行为说成NPC的主动行为.
[输出格式] `(动作/神态) 台词` 或 `(动作/神态)` 或 纯台词."""
    try:
        resp = await actor_llm.ainvoke([HumanMessage(content=prompt)])
        content = (resp.content or "").strip()
    except Exception:
        return {}

    if not content or "发呆" in content or "无反应" in content:
        return {}

    return {
        "history": [{"speaker": npc_id, "content": content}],
        "ui_events": [{"type": "npc_bubble", "npc_id": npc_id, "npc_name": name, "text": content}],
    }
