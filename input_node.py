"""输入记录与动态唤醒节点：record_input（在场判定 + 目标解析）+ fan_out（Send 动态并发）。

- record_input：判断 @全部/在场 NPC，解析 @数字/@npc_N（统一按编号，不支持名字）；目标不在场或无人时给出系统反馈。
- fan_out：本版本 LangGraph 中 Send 分支的 state = payload（不会自动带上父状态），
  因此把 npc_worker 需要的字段全部快照进 payload，返回时再经 reducer 合并回父状态。
"""
from langgraph.types import Send

from state import AgentState
from utils import is_broadcast, resolve_target


def record_input(state: AgentState) -> dict:
    msg = state.get("user_msg", "")
    npcs = state.get("npcs") or {}
    scene = state.get("current_scene", "未知")
    present = [nid for nid, info in npcs.items() if info.get("location") == scene]

    counter = int(state.get("analysis_counter") or 0)

    if is_broadcast(msg) or not present:
        targets = present
        target_tag = "全部"
    else:
        tid = resolve_target(msg, npcs)
        if tid and tid not in present:
            info = npcs.get(tid, {})
            return {
                "history": [{"speaker": "system", "content": f"【系统】{info.get('name', tid)} 不在这里（当前在 {scene}）。"}],
                "active_targets": [],
                "analysis_counter": counter,
            }
        targets = [tid] if tid else []
        target_tag = tid or "全部"

    if not targets:
        return {
            "history": [{"speaker": "user", "target": target_tag, "content": msg},
                        {"speaker": "system", "content": f"【系统】{scene} 里没有其他人。"}],
            "active_targets": [],
            "analysis_counter": counter,
        }
    return {
        "history": [{"speaker": "user", "target": target_tag, "content": msg}],
        "active_targets": targets,
        "analysis_counter": counter + 1,
        "run_story": True,
    }


def fan_out(state: AgentState):
    """动态并发：Send 唤醒所有在场 NPC；无人时直接收尾"""
    targets = state.get("active_targets") or []
    if not targets:
        return "sync_status"
    snapshot = {
        "npcs": state.get("npcs"),
        "current_scene": state.get("current_scene"),
        "history": state.get("history", []),
        "user_msg": state.get("user_msg", ""),
        "global_truth": state.get("global_truth", ""),
        "player_revealed_clues": state.get("player_revealed_clues", []),
        "world_events": state.get("world_events", []),
        "player_items": state.get("player_items", []),
        "available_scenes": state.get("available_scenes", []),
        "time_phase": state.get("time_phase", ""),
        "time_start": state.get("time_start", ""),
        "time_limit": state.get("time_limit", ""),
        "time_progress": state.get("time_progress", 0.0),
        "time_turns": state.get("time_turns", 0),
        "props": state.get("props", []),
        "scenes": state.get("scenes", {}),
        "player_attire": state.get("player_attire", "普通衣着"),
        "player_name": state.get("player_name", ""),
        "item_desc_map": state.get("item_desc_map", {}),
        "p2p_transcript": state.get("p2p_transcript", ""),
        "opened_scenes": state.get("opened_scenes", []),
        "analysis_counter": state.get("analysis_counter", 0),
        "last_clue_idx": state.get("last_clue_idx", 0),
    }
    sends = [Send("npc_worker", {"npc_id": t, **snapshot}) for t in targets]
    return sends
