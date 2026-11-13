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
from langchain_core.runnables import RunnableConfig
from utils import time_advance, _format_attire
from time_events import apply_time_events

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
    mem = info.get("memory") or {"l1": []}
    chats = list(mem.get("l1") or [])
    mem_text = "\n".join(chats) if chats else "（你们之间还没有过接触）"
    return (
        f"姓名：{name} | 身份：{info.get('role', '路人')}\n"
        f"性格：{info.get('personality', '普通')} | 外貌：{info.get('appearance', '普通')} | 衣着：{_format_attire(info.get('attire', {}))}\n"
        f"真实目标：{info.get('goal', '')}\n"
        f"[认知状态]\n{cognition_text}\n"
        f"[你的个人记忆]\n{mem_text}\n"
    )


async def p2p_node(state: AgentState, config: RunnableConfig) -> dict:
    thread_id = config.get("configurable", {}).get("thread_id", "") if isinstance(config, dict) else getattr(config, "configurable", {}).get("thread_id", "")
    msg = state.get("user_msg", "")
    npcs = state.get("npcs") or {}
    scene = state.get("current_scene", "未知")

    bg_chat = state.get("bg_chat_targets")
    is_bg = bool(bg_chat)
    ui_events = []

    if is_bg:
        a, b = bg_chat[0], bg_chat[1]
        info_a = npcs.get(a) or {}
        info_b = npcs.get(b) or {}
        # 后台触发时，用前一次的历史动作/事件当话题，或者让模型自己找话
        recent_events = "；".join((state.get("world_events") or [])[-2:])
        topic = f"（你们在同一个房间内，没什么事发生。{recent_events}。请自然地和对方搭话，或者继续你们未说完的私密话题）"
    else:
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
        topic = msg

    name_a = info_a.get("name", a)
    name_b = info_b.get("name", b)
    if is_bg:
        ui_events.append({"type": "npc_chat_start", "npc_ids": [a, b], "names": [name_a, name_b]})

    # 调用新建的 P2P 博弈执行器
    from multi_party_chat import multi_party_chat
    _chat_result = await multi_party_chat(
        participants=[a, b],
        topic=topic,
        max_rounds=2, # 仅让 A 和 B 各说一句，避免刷屏
        state=state
    )
    
    transcript = _chat_result.get("transcript", "")
    memory_patches = _chat_result.get("memory_patches", {})

    # 回写记忆
    npcs_patch = {}
    for nid, mem_entries in memory_patches.items():
        if not mem_entries:
            continue
        info = npcs.get(nid) or {}
        mem = info.get("memory") or {"l1": []}
        l1 = list(mem.get("l1") or [])
        
        # 将当前轮次的对话作为一条整合记忆追加
        entry = "\n".join(mem_entries)
        l1.append(entry)
        if len(l1) > 10:
            l1 = l1[-10:]
        npcs_patch[nid] = {"memory": {"l1": l1}}

    # 如果是后台触发，彻底清理双方的所有 chat 意图，防止同一次对话被反复触发
    if is_bg:
        for _nid in [a, b]:
            _info = npcs.get(_nid) or {}
            _cleaned = [pi for pi in (_info.get("pending_intent") or []) if pi.get("kind") != "chat"]
            npcs_patch.setdefault(_nid, {})["pending_intent"] = _cleaned

    # 解析 transcript 为历史记录格式
    history_msgs = []
    for line in transcript.split("\n"):
        if not line.strip():
            continue
        if line.startswith(f"{name_a}："):
            _c = line.replace(f"{name_a}：", "", 1)
            history_msgs.append({"speaker": a, "content": _c})
            if is_bg: ui_events.append({"type": "npc_chat_line", "npc_id": a, "npc_name": name_a, "content": _c})
        elif line.startswith(f"{name_b}："):
            _c = line.replace(f"{name_b}：", "", 1)
            history_msgs.append({"speaker": b, "content": _c})
            if is_bg: ui_events.append({"type": "npc_chat_line", "npc_id": b, "npc_name": name_b, "content": _c})
        else:
            # 兜底：如果格式异常，直接丢给 A
            history_msgs.append({"speaker": a, "content": line})
            if is_bg: ui_events.append({"type": "npc_chat_line", "npc_id": a, "npc_name": name_a, "content": line})
    
    if is_bg:
        ui_events.append({"type": "npc_chat_end", "npc_ids": [a, b]})

    out = {
        "p2p_transcript": transcript,
        "world_events": [f"{name_a}和{name_b}在{scene}交谈"],
        "history": history_msgs,
        "npcs": npcs_patch,
        "analysis_counter": int(state.get("analysis_counter") or 0) + 1,
        "run_story": True,
    }
    if ui_events:
        out["ui_events"] = ui_events
    if is_bg:
        out["bg_chat_targets"] = []
    # 只有前台显式触发才推进时间，后台触发由 time_node 已经推进过了
    if not is_bg:
        _tp = time_advance(state)
        if _tp:
            _tp_hist = _tp.pop("history", None)
            old_p = float(state.get("time_progress") or 0.0)
            new_p = float(_tp.get("time_progress") or old_p)
            _te = apply_time_events(state, old_p, new_p)
            _te_hist = _te.pop("history", None) if _te else None
            out.update(_tp)
            if _te:
                out.update(_te)
            merged_hist = list(out.get("history") or [])
            if _tp_hist:
                merged_hist.extend(_tp_hist)
            if _te_hist:
                merged_hist.extend(_te_hist)
            if merged_hist:
                out["history"] = merged_hist

            new_tick = int(state.get("world_tick") or 0) + 1
            out["world_tick"] = new_tick
            
            from npc_life_tick import update_npc_life
            _life = await update_npc_life(state, new_tick)
            if _life:
                _life_npcs = _life.pop("npcs", None)
                out.update(_life)
                if _life_npcs:
                    out.setdefault("npcs", {}).update(_life_npcs)

            # 消费未完成意图缓存
            from utils import tick_pending
            _pending = tick_pending({**state, "world_tick": new_tick})
            if _pending:
                _p_npcs = _pending.pop("npcs", None)
                if _p_npcs:
                    existing = out.setdefault("npcs", {})
                    for _nid, _patch in _p_npcs.items():
                        if _nid in existing and isinstance(existing[_nid], dict):
                            existing[_nid] = {**existing[_nid], **_patch}
                        else:
                            existing[_nid] = _patch
                if "ui_events" in _pending:
                    out.setdefault("ui_events", []).extend(_pending["ui_events"])
                if "world_events" in _pending:
                    out.setdefault("world_events", []).extend(_pending["world_events"])
    # 写入 RAG
    try:
        from rag_manager import add_npc_dialog
        # 此时 transcript 里包含了双方的完整对话，直接作为整体记忆写入双方的 RAG
        add_npc_dialog(thread_id, a, f"我与{name_b}交流的记录：\n{transcript}", {"scene": scene})
        add_npc_dialog(thread_id, b, f"我与{name_a}交流的记录：\n{transcript}", {"scene": scene})
    except Exception as e:
        print(f"[RAG写入失败] {e}")
    return out
