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

from social_network import merge_social_graph


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
    style_profile: dict                            # 前端传入的风格配置：{tone, setting, scale, pace, time_pressure, playability, difficulty, boundaries, dynamic_events}
    backstory: str
    global_truth: str
    truth_leads: List[dict]                         # 真相拼图可达表：[{fact, via}]，后台校验用，不向玩家展示
    ending_goal: str
    endings: List[dict]                             # 结局列表（单结局或多结局）：[{id,name,desc,condition}]
    allowed_entities: List[str]
    available_scenes: List[str]
    scenes: Dict[str, dict]                         # {场景名: {desc, items:[{name,desc,locked?,lock_mode?,key_name?,bio_holder?}], locked?,lock_mode?,key_name?,bio_holder?}}
    npcs: Annotated[Dict[str, dict], merge_npc]     # 实体库：固定字段 + 动态字段（含 emotion_tags/pending_intent），Reducer 合并
    social_graph: Annotated[Dict[str, dict], merge_social_graph]  # 动态社交网络（独立于 npcs），Reducer 深合并
    social_updates: Annotated[List[dict], operator.add]            # 并发节点追加的社交更新指令，social_node 消费
    processed_social_updates: Annotated[List[str], merge_list_union]  # 已消费的更新指令 id
    props: List[dict]                               # 重要物品归属表（全剧统一认知，NPC 不得编造位置）
    time_start: str                                 # 开局时间档（如"清晨"）
    time_limit: str                                 # 关键时间点（如"黄昏"），可空=不限时
    time_turns: int                                 # 废弃字段（5轮一档后不再使用），保留以兼容老 checkpoint
    total_rounds: int                               # 导演评估的总轮数（剧本体量），用于动态计算步长
    time_step_delta: float                          # 动态时间步长（8.0 / total_rounds）
    time_progress: float                            # 已推进的档位数（每轮加 time_step_delta）
    world_tick: int                                 # 世界时钟：玩家每步（对话/指令/移动/调查/偷窃/p2p）+1
    timed_events: List[dict]                        # 导演生成的时间事件（按 at_phase 触发，每场景最多一个）
    fired_time_events: Annotated[List[str], merge_list_union]  # 已触发的事件 id（防重复）

    current_scene: str
    time_phase: str                                 # 当前时间档（按轮数比例动态推进）；无时间压力时为空
    player_items: List[dict]                         # 普通字段：item_node / action_engine 整体写回（可增可减）
    player_name: str                                # 玩家名字（前端玩家输入提供，world_genesis 透传）
    player_intent: dict                             # 本轮玩家意图（intent 节点产出：intent_type/command/confidence/clean_msg/source）
    item_desc_map: Dict[str, str]                   # 道具用途表（导演初始从场景汇总，动态获得由 item_node 补充）
    item_transfers: Annotated[List[dict], operator.add]  # NPC 同意给予的物品（reducer 收集，item_node 消费）
    processed_transfers: List[str]                  # 已消费的给予指纹（防 reducer 累积重复处理）
    player_gives: Annotated[List[dict], operator.add]  # 玩家同意交给 NPC 的物品（npc_worker 收集，item_node 消费）
    processed_player_gives: List[str]               # 已消费的玩家给予指纹（防重复处理）
    processed_steals: List[str]                     # 已消费的偷窃指纹（防 item_node 二次执行误偷，指纹=scene::msg::world_tick）
    analysis_counter: int                           # 判定官节奏计数（仅对话轮递增，指令不计数）
    last_clue_idx: int                              # 上次线索分析时 history 的长度（窗口起点）
    player_revealed_clues: Annotated[List[str], operator.add]
    secrets_revealed: Annotated[List[dict], operator.add]   # 已透露的秘密：[{npc, fact}]（套话机制，防重复透露）
    active_crisis: dict                                    # 进行中的危机：{id, at_phase, deadline_phase, condition, fail_desc, fail_effects, scene}
    world_events: Annotated[List[str], operator.add]
    game_over: bool
    steal_attempts: Dict[str, int]                  # 偷窃次数记录（保底用）
    ui_events: Annotated[List[dict], operator.add]  # UI 事件总线（给物品/移动/换装/获得物品等物理交互事件，reducer 追加）
    npc_private_items: Dict[str, list]              # NPC 私有物品表（固定，NPC 验证用）

    user_msg: str
    active_targets: List[str]                       # 本轮要唤醒的 npc id（record_input 计算）
    npc_id: str                                     # Send 分支参数（当前唤醒的 npc）
    last_output: str
    history: Annotated[List[dict], operator.add]

    p2p_transcript: str                             # 本轮 NPC 间交谈全文
    run_story: bool                                 # 本轮是否触发线索提取
    bg_chat_targets: List[str]                      # 本轮后台触发的 NPC 自主对话参与者 [npc_a, npc_b]
    last_player_action: dict                        # 玩家上一轮的物理动作（如：{"action": "investigate", "target": "急救箱", "scene": "书房"}）
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
