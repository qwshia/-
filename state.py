"""状态层：AgentState 定义、npcs 实体库的 Reducer 合并器、checkpoint 加载。

- npcs 用 Annotated[Dict, merge_npc]：多分支（Send 并发 / 多节点）写同一实体库时按
  npc_id 深合并，各 NPC 只改自己的 key，互不覆盖。
- history / clues / world_events 用 operator.add 追加合并。
"""
import copy
import operator
from contextlib import asynccontextmanager
from typing import TypedDict, Dict, List, Annotated

from langgraph.checkpoint.memory import MemorySaver

from config import _SQLITE_OK


def merge_npc(left: dict, right: dict) -> dict:
    """并发写 npcs 时的合并器：按 npc_id 深合并，各 NPC 只改自己的 key，互不覆盖"""
    out = copy.deepcopy(left) if left else {}
    for npc_id, patch in (right or {}).items():
        if not isinstance(patch, dict):
            continue
        if npc_id in out and isinstance(out[npc_id], dict):
            merged = copy.deepcopy(out[npc_id])
            for k, v in patch.items():
                merged[k] = v
            out[npc_id] = merged
        else:
            out[npc_id] = copy.deepcopy(patch)
    return out


def merge_list_union(left, right) -> list:
    out = list(left or [])
    for x in (right or []):
        if x not in out:
            out.append(x)
    return out


class AgentState(TypedDict):
    task: str
    backstory: str
    global_truth: str
    truth_leads: List[dict]                         # 真相拼图可达表：[{fact, via}]，后台校验用，不向玩家展示
    ending_goal: str
    allowed_entities: List[str]
    available_scenes: List[str]
    scenes: Dict[str, dict]                         # {场景名: {desc, items:[{name,desc,locked?}], requires_key?}} 调查用
    npcs: Annotated[Dict[str, dict], merge_npc]     # 实体库：固定字段 + 动态字段，Reducer 合并
    props: List[dict]                               # 重要物品归属表（全剧统一认知，NPC 不得编造位置）
    time_start: str                                 # 开局时间档（如"清晨"）
    time_limit: str                                 # 关键时间点（如"黄昏"），可空=不限时
    time_turns: int                                 # 从 time_start 到 time_limit 的预计交互轮数（导演给定）

    current_scene: str
    time_phase: str                                 # 当前时间档（按轮数比例动态推进）；无时间压力时为空
    player_items: List[str]                         # 普通字段：item_node / action_engine 整体写回（可增可减）
    player_name: str                                # 玩家名字（导演开局创建，NPC 直接知道）
    player_attire: str                              # 玩家当前穿着（同场景 NPC 可见）
    item_desc_map: Dict[str, str]                   # 道具用途表（导演初始从场景汇总，动态获得由 item_node 补充）
    item_transfers: Annotated[List[dict], operator.add]  # NPC 同意给予的物品（reducer 收集，item_node 消费）
    processed_transfers: List[str]                  # 已消费的给予指纹（防 reducer 累积重复处理）
    analysis_counter: int                           # 判定官节奏计数（仅对话轮递增，指令不计数）
    last_clue_idx: int                              # 上次线索分析时 history 的长度（窗口起点）
    player_revealed_clues: Annotated[List[str], operator.add]
    world_events: Annotated[List[str], operator.add]
    game_over: bool
    steal_attempts: Dict[str, int]                  # 偷窃次数记录（保底用）
    npc_private_items: Dict[str, list]              # NPC 私有物品表（固定，NPC 验证用）

    user_msg: str
    active_targets: List[str]                       # 本轮要唤醒的 npc id（record_input 计算）
    npc_id: str                                     # Send 分支参数（当前唤醒的 npc）
    last_output: str
    history: Annotated[List[dict], operator.add]

    p2p_transcript: str                             # 本轮 NPC 间交谈全文
    run_story: bool                                 # 本轮是否触发线索提取
    opened_scenes: Annotated[List[str], merge_list_union]  # 已打开的门禁场景



@asynccontextmanager
async def load_checkpointer(db_path: str):
    """SQLite 官方 checkpoint（可用时）；不可用回退内存。yield (saver, 类型)"""
    if _SQLITE_OK:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            yield saver, "sqlite"
    else:
        yield MemorySaver(), "memory"
