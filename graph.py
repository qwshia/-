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
from input_node import record_input, fan_out
from npc_node import npc_worker
from action_node import action_engine
from item_node import item_node, is_steal_cmd
from story_node import story_keeper
from p2p_node import p2p_node, is_p2p
from hint_node import hint_node
from ending_node import judge_ending
from utils import time_advance, phase_span, phase_display, time_step, is_scene_move_cmd

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


def time_node(state: AgentState) -> dict:
    """时间动态流逝：导演给定 time_turns（总交互轮数），把 time_start→time_limit 的档数
    按轮数比例分摊，每轮推进一小步；轮数用尽（progress 走满全程）即到关键时间点。
    导演未设时限（无 time_limit/time_turns）→ 时间固定不流逝。核心逻辑在 utils.time_advance，
    p2p_node 复用同一函数保证每轮恰好推进一次（并行改造后不重复、不遗漏）。"""
    return time_advance(state)


def sync_status(state: AgentState) -> dict:
    return {}


def build_graph(checkpointer=None):
    g = StateGraph(AgentState)
    g.add_node("world_genesis", world_genesis)
    g.add_node("record_input", record_input)
    g.add_node("npc_worker", npc_worker)
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

    g.add_edge("world_genesis", "sync_status")

    g.add_conditional_edges(
        "record_input", fan_out,
        {"sync_status": "sync_status"},
    )
    g.add_edge("npc_worker", "item_node")
    g.add_edge("action_engine", "item_node")
    g.add_edge("item_node", "time_node")
    g.add_edge("time_node", "sync_status")
    g.add_conditional_edges(
        "sync_status", lambda s: "story_keeper" if s.get("run_story") else "end",
        {"story_keeper": "story_keeper", "end": END},
    )
    g.add_edge("story_keeper", END)
    g.add_edge("p2p_node", "story_keeper")
    g.add_edge("judge_ending", END)
    g.add_edge("hint_node", END)

    return g.compile(checkpointer=checkpointer or MemorySaver())
