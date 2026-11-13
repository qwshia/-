"""多智能体互动叙事游戏 · 构图入口

模块划分：
  config.py      配置 / .env / LLM 客户端（deepseek-v4-flash-0731，关闭推理）
  state.py       AgentState + npcs Reducer(merge_npc) + checkpoint 加载
  utils.py       通用工具（JSON 解析 / 目标解析 / 广播判定 / 历史渲染）
  world.py       世界引擎（开局生成剧本世界 + NPC 实体库）
  input_node.py  输入记录 + 在场判定 + Send 动态并发
  npc_node.py    通用 NPC 执行器（固定字段 + 动态字段 + 个人记忆）
  action_node.py 动作引擎（!去 / !给 / 调查 + 系统旁白）
  story_node.py  剧情判定官（LLM 辅助 + 五维确定性解锁终审）
  hint_node.py   导演提示（按需，不进历史）
  ending_node.py 结局判定（多结局保留，前置拦截）
  graph.py       构图（本文件）

图结构：
  START → route_next 五分支
  record_input → fan_out（Send 并发在场 NPC | story_keeper | sync_status）——story_keeper 与 NPC 回话并行
  npc_worker → item_node → time_node → sync_status；action_engine → item_node → time_node → sync_status
  p2p_node → story_keeper → sync_status（p2p 节点内部推进时间）
  world_genesis / story_keeper / hint_node → sync_status → END
  judge_ending / end_node → END
"""
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
import re

from state import AgentState, load_checkpointer
from world import world_genesis
from npc_genesis import npc_genesis
from input_node import record_input, fan_out
from npc_node import npc_worker
from arbiter_node import arbiter_node
from narrator_node import narrator_node
from intent_node import intent_engine
from action_node import action_engine
from item_node import item_node, is_steal_cmd
from story_node import story_keeper
from p2p_node import p2p_node, is_p2p
from hint_node import hint_node
from ending_node import judge_ending
from utils import time_advance, phase_span, phase_display, time_step, is_scene_move_cmd
from time_events import apply_time_events

__all__ = ["build_graph", "load_checkpointer", "AgentState"]


# 入口路由：根据输入类型分发到对应节点
def route_next(state: AgentState):
    msg = state.get("user_msg", "")
    if msg.startswith("结局是"):
        return "judge_ending"
    if not state.get("npcs"):
        return "world_genesis"
    # ! 指令
    if msg.lstrip().startswith(("!", "！")):
        _m = msg.lstrip().lstrip("!！").strip().lower()
        if _m in ("提示", "hint", "帮助", "help") or _m.startswith("提示"):
            return "hint_node"
        if _m.startswith(("调查", "打开")):
            return "action_engine"
    if is_p2p(msg):
        return "p2p_node"
    if "@" in msg and not any(w in msg for w in ("打开", "开启", "开锁")):
        return "record_input"
    if is_steal_cmd(msg):
        return "item_node"
    if is_scene_move_cmd(msg, state):
        return "action_engine"
    # !去
    if msg.lstrip().startswith(("!", "！")) and msg.lstrip().lstrip("!！").strip().startswith("去"):
        return "action_engine"
    return "record_input"


async def time_node(state: AgentState) -> dict:
    """时间推进 + world_tick 递增 + 时间事件执行。"""
    out = {}
    # 1. 无论是否有时间限制，世界步数必 +1
    new_tick = int(state.get("world_tick") or 0) + 1
    out["world_tick"] = new_tick

    # 2. 原有时间推进逻辑
    _tp = time_advance(state)
    if _tp:
        old_p = float(state.get("time_progress") or 0.0)
        new_p = float(_tp.get("time_progress") or old_p)
        _tp_hist = _tp.pop("history", None)
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

    # 3. 生活层更新
    from npc_life_tick import update_npc_life
    _life = await update_npc_life(state, new_tick)
    if _life:
        _life_npcs = _life.pop("npcs", None)
        out.update(_life)
        if _life_npcs:
            out.setdefault("npcs", {}).update(_life_npcs)

    # 4. 巡检：检查是否有 NPC 自主发起的 chat 意图
    bg_chat = []
    for nid, info in (state.get("npcs") or {}).items():
        for pi in (info.get("pending_intent") or []):
            if pi.get("kind") == "chat" and pi.get("remain", 0) <= 1:
                target_name = pi.get("payload", "")
                # 查找同场景的 target
                for nid2, info2 in (state.get("npcs") or {}).items():
                    if info2.get("name") == target_name and info2.get("location") == info.get("location") and nid != nid2:
                        bg_chat.append([nid, nid2])
                        break
    if bg_chat:
        out["bg_chat_targets"] = bg_chat[0] if bg_chat else []

    # 5. 消费未完成意图缓存
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

    return out


def sync_status(state: AgentState) -> dict:
    return {}


def build_graph(checkpointer=None):
    g = StateGraph(AgentState)
    g.add_node("world_genesis", world_genesis)
    g.add_node("npc_genesis", npc_genesis)
    g.add_node("record_input", record_input)
    g.add_node("intent_engine", intent_engine)
    g.add_node("npc_worker", npc_worker)
    g.add_node("narrator_node", narrator_node)
    g.add_node("arbiter_node", arbiter_node)
    g.add_node("action_engine", action_engine)
    g.add_node("item_node", item_node)
    g.add_node("story_keeper", story_keeper)
    g.add_node("p2p_node", p2p_node)
    g.add_node("time_node", time_node)
    g.add_node("hint_node", hint_node)
    g.add_node("judge_ending", judge_ending)
    g.add_node("sync_status", sync_status)

    g.add_conditional_edges(
        START, route_next,
        {
            "world_genesis": "world_genesis",
            "record_input": "record_input",
            "action_engine": "action_engine",
            "item_node": "item_node",
            "hint_node": "hint_node",
            "judge_ending": "judge_ending",
            "p2p_node": "p2p_node",
        },
    )

    g.add_edge("world_genesis", "npc_genesis")
    g.add_edge("npc_genesis", "sync_status")

    g.add_conditional_edges(
        "record_input",
        lambda s: "intent_engine" if (s.get("active_targets") or []) else "sync_status",
        {"intent_engine": "intent_engine", "sync_status": "sync_status"},
    )
    g.add_conditional_edges(
        "intent_engine", fan_out,
        {"sync_status": "sync_status"},
    )
    g.add_edge("npc_worker", "narrator_node")
    g.add_edge("narrator_node", "arbiter_node")
    g.add_edge("arbiter_node", "item_node")
    g.add_edge("action_engine", "item_node")
    g.add_edge("item_node", "time_node")
    g.add_conditional_edges(
        "time_node", 
        lambda s: "p2p_node" if s.get("bg_chat_targets") else "sync_status",
        {"p2p_node": "p2p_node", "sync_status": "sync_status"},
    )
    g.add_conditional_edges(
        "sync_status", lambda s: "story_keeper" if s.get("run_story") else "end",
        {"story_keeper": "story_keeper", "end": END},
    )
    g.add_edge("story_keeper", END)
    g.add_edge("p2p_node", "story_keeper")
    g.add_edge("judge_ending", END)
    g.add_edge("hint_node", END)

    return g.compile(checkpointer=checkpointer or MemorySaver())
