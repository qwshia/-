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
  timeline_genesis.py 时间线生成（仅有限时剧本，与 NPC 血肉并行）
  graph.py       构图（本文件）

图结构：
  START → route_next 五分支
  record_input → fan_out（Send 并发在场 NPC | story_keeper | sync_status）——story_keeper 与 NPC 回话并行
  npc_worker → item_node → time_node → sync_status；action_engine → item_node → time_node → sync_status
  p2p_node → story_keeper → sync_status（p2p 节点内部推进时间）
  world_genesis → npc_genesis (+ 有时限时并行 timeline_genesis) → sync_status
  story_keeper / hint_node → sync_status → END
  judge_ending / end_node → END
"""
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
import re

from state import AgentState, load_checkpointer
from world import world_genesis
from npc_genesis import npc_genesis
from social_genesis import social_genesis
from social_node import social_node
from timeline_genesis import timeline_genesis
from input_node import record_input, fan_out, fan_out_action
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
        if _m.startswith("使用"):
            return "action_engine"
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

    # 3. 生活层更新（仅当跨越了完整的时间档位时触发）
    old_p = float(state.get("time_progress") or 0.0)
    new_p = float(out.get("time_progress") or old_p)
    if int(old_p) != int(new_p):
        from npc_life_tick import update_npc_life
        _life = await update_npc_life(state, new_tick)
        if _life:
            _life_npcs = _life.pop("npcs", None)
            out.update(_life)
            if _life_npcs:
                out.setdefault("npcs", {}).update(_life_npcs)

    # 4. 消费未完成意图缓存
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


def bg_chat_node(state: AgentState) -> dict:
    """后台 NPC 自主对话巡检（串行执行，避免在 Send 并发分支内重复触发同一场对话）。

    - 同场景：返回 bg_chat_targets=[a,b]，交给 p2p_node 生成对话。
    - 跨场景找人：先移动过去（remain 重置为 2，下一 tick 同场景触发对话）；
      若目标正是玩家当前场景，补一条"XX 来了"的系统提示，避免 NPC 突然出现。
    """
    out = {}
    bg_chat = []
    npcs = state.get("npcs") or {}
    for nid, info in npcs.items():
        for pi in (info.get("pending_intent") or []):
            if pi.get("kind") != "bg_chat" or pi.get("remain", 0) > 1:
                continue
            target_name = pi.get("payload", "")
            target_nid, target_info = None, None
            for nid2, info2 in npcs.items():
                if info2.get("name") == target_name and nid != nid2:
                    target_nid, target_info = nid2, info2
                    break
            if target_nid is None:
                continue
            if target_info.get("location") == info.get("location"):
                # 同场景：直接对话
                bg_chat.append([nid, target_nid])
            else:
                # 跨场景找人：移动过去，重置该意图 remain 供下一 tick 同场景触发对话
                _new_pending = []
                for pi2 in (info.get("pending_intent") or []):
                    if pi2.get("kind") == "bg_chat" and pi2.get("payload") == target_name:
                        _new_pending.append({**pi2, "remain": 2})
                    else:
                        _new_pending.append(pi2)
                _moved_to = target_info.get("location")
                _npc_patch = out.setdefault("npcs", {}).setdefault(nid, {})
                _npc_patch["location"] = _moved_to
                _npc_patch["pending_intent"] = _new_pending
                out.setdefault("world_events", []).append(
                    f"{info.get('name', nid)}离开{info.get('location')}，前往{_moved_to}找{target_name}")
                out.setdefault("ui_events", []).append(
                    {"type": "npc_move", "npc": info.get("name", nid), "target_scene": _moved_to})
    out["bg_chat_targets"] = bg_chat[0] if bg_chat else []
    return out


def sync_status(state: AgentState) -> dict:
    return {}


def _route_after_bg_chat(state: AgentState):
    """后台对话巡检后的路由：有 bg_chat 则进 p2p，否则按 run_story 决定是否提取线索。"""
    if state.get("bg_chat_targets"):
        return "p2p_node"
    if state.get("run_story"):
        return "story_keeper"
    return "end"


def _route_after_world(state: AgentState):
    if state.get("time_limit"):
        return ["npc_genesis", "timeline_genesis"]
    return "npc_genesis_no_timeline"


def _route_after_item(state: AgentState):
    """item_node 后的路由：有物理动作则并发广播旁观，否则推进时间。"""
    if state.get("last_player_action"):
        return fan_out_action(state)
    return "time_node"


def _route_after_p2p(state: AgentState):
    """p2p_node 后的路由：前台 p2p 有物理动作则并发广播旁观，后台 bg_chat 直达 story_keeper。"""
    if state.get("last_player_action"):
        return fan_out_action(state)
    return "story_keeper"


def build_graph(checkpointer=None):
    g = StateGraph(AgentState)
    g.add_node("world_genesis", world_genesis)
    g.add_node("npc_genesis", npc_genesis)
    g.add_node("social_genesis", social_genesis)
    g.add_node("npc_genesis_no_timeline", npc_genesis)
    g.add_node("social_node", social_node)
    g.add_node("timeline_genesis", timeline_genesis)
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
    g.add_node("bg_chat_node", bg_chat_node)

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

    g.add_conditional_edges(
        "world_genesis",
        _route_after_world,
        {
            "npc_genesis": "npc_genesis",
            "npc_genesis_no_timeline": "npc_genesis_no_timeline",
            "timeline_genesis": "timeline_genesis",
        },
    )
    g.add_edge("npc_genesis", "social_genesis")
    g.add_edge("npc_genesis_no_timeline", "social_genesis")
    g.add_edge("social_genesis", "sync_status")
    g.add_edge("timeline_genesis", "sync_status")
    # Keep the no-time path out of the parallel barrier, which waits for timeline_genesis.

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
    
    g.add_conditional_edges("item_node", _route_after_item)
    g.add_edge("time_node", "sync_status")
    g.add_edge("sync_status", "social_node")
    g.add_edge("social_node", "bg_chat_node")
    g.add_conditional_edges(
        "bg_chat_node", _route_after_bg_chat,
        {"p2p_node": "p2p_node", "story_keeper": "story_keeper", "end": END},
    )
    g.add_edge("story_keeper", END)
    g.add_conditional_edges("p2p_node", _route_after_p2p)
    g.add_edge("judge_ending", END)
    g.add_edge("hint_node", END)

    return g.compile(checkpointer=checkpointer or MemorySaver())
