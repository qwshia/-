"""NPC 间交流节点：p2p_node —— 玩家下指令让两个 NPC 互相对话。

指令格式：@1和@2交流 / @npc_1与@npc_2聊聊（必须都是当前场景内的 NPC；统一按编号，不支持名字）。
执行：A 先开口（基于 A 的设定/记忆），B 回应（基于 B 的设定/记忆）。
交流内容【不写入双方个人记忆、不进全局历史】（省 token、不污染记忆），
只通过 p2p_transcript 交给 story_keeper 提取其中有用的线索；并广播一条世界事件让其他 NPC 感知。
"""
import re

from langchain_core.messages import HumanMessage

from config import actor_llm
from state import AgentState
from memory import _compress, _compress_two
from utils import time_advance

_P2P_RE = re.compile(
    r"(?:让|叫|请|麻烦|安排)?\s*"
    r"@(?:子|npc_)?(\d+)"
    r"\s*(?:和|与|跟|同)\s*"
    r"@(?:子|npc_)?(\d+)"
    r"\s*(?:交流|聊聊|谈话|交谈|对话|聊一聊|说说话|谈一谈|聊聊天)"
)


def is_p2p(msg: str) -> bool:
    """识别"@X和@Y交流"指令"""
    return bool(_P2P_RE.search(msg))


def parse_p2p(msg: str, npcs: dict) -> tuple[str | None, str | None]:
    """返回 (npc_id_a, npc_id_b)；无法解析或编号不存在返回 (None, None)"""
    m = _P2P_RE.search(msg)
    if not m:
        return None, None
    a, b = f"npc_{m.group(1)}", f"npc_{m.group(2)}"
    if a not in npcs or b not in npcs or a == b:
        return None, None
    return a, b


async def _npc_talk(prompt: str) -> tuple[str, bool]:
    for _ in range(2):
        try:
            resp = await actor_llm.ainvoke([HumanMessage(content=prompt)])
            c = (resp.content or "").strip()
            if c:
                return c, True
        except Exception:
            pass
    return "（沉默不语。）", False


def _role_block(info: dict, npc_id: str) -> str:
    name = info.get("name", npc_id)
    secrets = "\n".join(f"- {s}" for s in info.get("secrets", []))
    forbidden = "\n".join(f"- {f}" for f in info.get("forbidden", []))
    cognition_parts = []
    if secrets:
        cognition_parts.append(f"- 你确知的事实：\n{secrets}")
    if forbidden:
        cognition_parts.append(f"- 你极力隐瞒的事：\n{forbidden}")
    cognition_text = "\n".join(cognition_parts) if cognition_parts else "- 你是一个坦诚的人。"
    mem = info.get("memory") or {}
    if isinstance(mem, list):
        long_mem = next((m.get("content", "") for m in mem if isinstance(m, dict) and m.get("type") == "summary_long"), None)
        recent_mem = next((m.get("content", "") for m in mem if isinstance(m, dict) and m.get("type") == "summary_recent"), None)
        chats = [m.get("content", "") for m in mem if isinstance(m, dict) and m.get("type") == "chat"]
    else:
        long_mem = mem.get("l3")
        recent_mem = mem.get("l2")
        chats = list(mem.get("l1") or [])
    mem_lines = []
    if long_mem:
        mem_lines.append(f"[长期记忆] {long_mem}")
    if recent_mem:
        mem_lines.append(f"[近期记忆] {recent_mem}")
    mem_lines.extend(chats)
    mem_text = "\n".join(mem_lines) if mem_lines else "（你们之间还没有过接触）"
    return (
        f"姓名：{name} | 身份：{info.get('role', '路人')}\n"
        f"性格：{info.get('personality', '普通')} | 外貌：{info.get('appearance', '普通')} | 衣着：{info.get('attire', '常服')}\n"
        f"真实目标：{info.get('goal', '')}\n"
        f"【认知状态】\n{cognition_text}\n"
        f"【你的个人记忆】\n{mem_text}\n"
    )


async def p2p_node(state: AgentState) -> dict:
    msg = state.get("user_msg", "")
    npcs = state.get("npcs") or {}
    scene = state.get("current_scene", "未知")

    a, b = parse_p2p(msg, npcs)
    if not a or not b:
        return {
            "history": [{"speaker": "system",
                         "content": "【系统】请用「@1和@2交流」这样的格式指定两个当前场景内的NPC交谈。"}],
        }

    info_a = npcs.get(a) or {}
    info_b = npcs.get(b) or {}
    if info_a.get("location") != scene or info_b.get("location") != scene:
        absent = [info_b.get("name", b)] if info_a.get("location") == scene else \
                 [info_a.get("name", a)] if info_b.get("location") == scene else \
                 [info_a.get("name", a), info_b.get("name", b)]
        return {
            "history": [{"speaker": "system", "content": f"【系统】{'、'.join(absent)} 不在 {scene}，没法让他们在这里交谈。"}],
        }

    name_a = info_a.get("name", a)
    name_b = info_b.get("name", b)

    attire_words = ("衣服", "服装", "服饰", "衣着", "穿着", "穿上", "脱掉", "脱下", "换衣", "换装",
                    "打扮", "装束", "裙子", "衣衫", "光着", "赤裸", "外套", "外袍", "衬衫", "鞋袜")
    if any(w in msg for w in attire_words):
        locs_txt = "、".join(f"{i.get('name', '?')}(在{i.get('location', '?')}，穿{i.get('attire', '常服')})"
                             for i in npcs.values() if i.get("location") == scene) or "（无）"
        locs_line = f"本场景在场NPC的位置与穿着：{locs_txt}\n"
    else:
        locs_line = ""

    time_line = f"；当前时间：{state.get('time_phase')}" if state.get("time_phase") else ""

    prompt_a = (
        "你正在参与一场互动叙事扮演.此刻[玩家在旁观看],你与另一位NPC交谈.\n"
        f"你所在场景:{scene}{time_line}.\n"
        f"{_role_block(info_a, a)}\n"
        f"[世界动态]\n"
        f"玩家当前在:{scene}.\n"
        f"{locs_line}"
        f"最近世界事件:{(';'.join((state.get('world_events') or [])[-3:])) or '(无)'}\n\n"
        f"你要主动和[{name_b}]攀谈,说符合你身份与关系的话(老朋友叙旧,试探,寒暄,提醒都可以),"
        "开场说1至2句即可.若你与对方有秘密纠葛,可以旁敲侧击,甚至在对峙中说破对方的秘密,但不得说破你自己的核心秘密.\n"
        "你知道玩家在场旁听,说话时会自然顾及这一点(是否避讳,是否故意说给玩家听,由你的身份与性格决定).\n"
        "提到的地点或人物必须已在设定/世界动态中出现,不得编造.\n"
        "你的认知状态里没有提到的名词/人物/概念,你不得承认自己知道,也不得猜测,只能表示没听过/不清楚.\n"
        f"[你正在和{name_b}说话]\n"
        "请以角色口吻回答,只输出你自己要说的话,不要代写对方.格式:(动作/神态) 说话内容,40至100字"
    )   

    a_talk, ok_a = await _npc_talk(prompt_a)
    if not ok_a:
        return {
            "history": [{"speaker": "system", "content": f"【系统】{name_a}张了张嘴，终究没有说话。"}],
        }

    prompt_b = (
        "你正在参与一场互动叙事扮演.此刻[玩家在旁观看],另一位NPC在和你说话.\n"
        f"你所在场景:{scene}{time_line}.\n"
        f"{_role_block(info_b, b)}\n"
        f"[世界动态]\n"
        f"玩家当前在:{scene}.\n"
        f"{locs_line}"
        f"最近世界事件:{(';'.join((state.get('world_events') or [])[-3:])) or '(无)'}\n\n"
        f"[{name_a}对你说]\n{a_talk}\n\n"
        "如果对方的话不符合你确知的事实，你可以表现出疑惑、反驳或纠正，而不是盲目附和.\n"
        "请以你的身份回应对方:接住话头,回答问题或反问他,说话符合你的性格与立场.\n"
        "如果对方提到你隐瞒的事,可以避重就轻或转移话题,但别直接说破.\n"
        "你知道玩家在场旁听,回应时会自然顾及这一点(是否避讳,是否借机说给玩家听,由你的身份与性格决定).\n"
        "提到的地点或人物必须已在设定/世界动态中出现,不得编造;你的认知状态里没有提到的名词,不得承认知道或猜测.\n"
        "请以角色口吻回答.格式:`(动作/神态) 说话内容`,40至100字."
    )
    
    b_talk, ok_b = await _npc_talk(prompt_b)
    if not ok_b:
        b_talk = "（沉默不语。）"

    npcs_patch = {}
    for nid, self_talk, other_talk, other_name in (
        (a, a_talk, b_talk, name_b),
        (b, b_talk, a_talk, name_a),
    ):
        info = npcs.get(nid) or {}
        mem = info.get("memory") or {"l1": [], "l2": None, "l3": None}
        if not isinstance(mem, dict):
            mem = {"l1": [], "l2": None, "l3": None}
        l1 = list(mem.get("l1") or [])
        l2 = mem.get("l2")
        l3 = mem.get("l3")
        entry = f"与{other_name}交谈：\n我：{self_talk}\n对方：{other_talk}"
        l1.append(entry)
        old_l2 = l2
        if len(l1) >= 8:
            front = l1[:5]
            l1 = l1[5:]
            if l3 is None:
                if old_l2 is not None:
                    l3 = old_l2
            else:
                if old_l2:
                    l3 = await _compress_two(old_l2, l3)
            l2 = await _compress(front)
        npcs_patch[nid] = {"memory": {"l1": l1, "l2": l2, "l3": l3}}

    out = {
        "p2p_transcript": f"{name_a}说：{a_talk}；{name_b}说：{b_talk}",
        "world_events": [f"{name_a}和{name_b}在{scene}交谈"],
        "history": [
            {"speaker": a, "content": a_talk},
            {"speaker": b, "content": b_talk},
        ],
        "npcs": npcs_patch,
        "analysis_counter": int(state.get("analysis_counter") or 0) + 1,
        "run_story": True,
    }
    out.update(time_advance(state))
    return out
