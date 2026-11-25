# 动态社交网络更新说明

## 1. 改动概要

- 新增 `social_network.py`：动态社交网络核心模块，提供初始化、更新、查询、Prompt 注入函数。
- 新增 `social_genesis.py`：社交网络生成节点，读取剧本与 NPC 名单，生成初始社交图（LLM 辅助）。
- 新增 `social_node.py`：串行节点，消费更新指令并应用到社交图，同时从高层状态自动提取更新。
- 修改 `state.py`：追加 `social_graph`、`social_updates`、`processed_social_updates` 字段。
- 修改 `graph.py`：注册 `social_genesis` 与 `social_node`，接入图编排。
- 修改 `server.py`：`build_ui_state` 输出 `social_graph` 字段。
- 修改 `constants.py`：追加社交网络相关白名单。
- 新增 `SOCIAL_NETWORK_UPDATE.md`：本文件。

## 2. 社交图 Schema

```json
{
  "schema_version": 1,
  "npcs": { "npc_id": { "name": "..." } },
  "relationships": {
    "npc_a||npc_b": {
      "a": "npc_a", "b": "npc_b",
      "familiarity": 0.0-1.0, "trust": 0.0-1.0, "tension": 0.0-1.0,
      "affinity": 0.0-1.0, "respect": 0.0-1.0, "fear": 0.0-1.0,
      "obligation": 0.0-1.0, "resentment": 0.0-1.0, "curiosity": 0.0-1.0,
      "dependency": 0.0-1.0,
      "interactions": 0, "last_tick": 0, "last_interaction_type": "",
      "connection_types": [], "history": [], "dormant": false
    }
  },
  "secrets": {
    "owner::hash": {
      "owner": "npc_id", "fact": "...",
      "known_by": ["npc_id", ...],
      "player_knows": false, "revealed_tick": null,
      "reveal_condition": "..."
    }
  },
  "consensus_regions": {
    "consensus::hash": {
      "id": "...", "theme": "...", "members": ["npc_id"],
      "level": 0.0-1.0, "visibility": "secret/public/faction_only",
      "formed_tick": 0, "history": []
    }
  },
  "factions": {
    "faction_id": {
      "id": "...", "name": "...", "goal": "...",
      "members": ["npc_id"], "allies": [], "hostiles": [],
      "formed_tick": 0, "history": []
    }
  },
  "rumors": {
    "rumor::id": {
      "id": "...", "content": "...", "source": "npc_id",
      "credibility": 0.0-1.0, "type": "fact/distorted/fabricated/archived",
      "heat": 0.0-1.0, "created_tick": 0,
      "known_by": ["npc_id"], "history": []
    }
  },
  "reputation": { "global": 0.5, "factions": { "faction_id": 0.5 } },
  "grudges": { "npc_id": [{ "target", "reason", "severity", "tick" }] },
  "debts": { "npc_id": [{ "target", "reason", "severity", "tick" }] }
}
```

## 3. 初始化

- `social_genesis` 节点在 `npc_genesis` 之后运行，读取 `backstory`、`global_truth`、`ending_goal`、NPC 名单，LLM 生成初始关系/阵营/恩怨/债务/共识/舆论。
- 秘密知情边由 `init_secrets_from_npcs` 确定性生成，不依赖 LLM。
- 图存储在 `state.social_graph`，通过 `merge_social_graph` Reducer 深合并。

## 4. 更新机制

### 4.1 更新指令协议

并发节点（如 `npc_node`）不直接修改图，而是追加 `social_updates` 指令：

```python
from social_network import create_update

out["social_updates"] = [
    create_update("reveal", owner=npc_id, fact=_fact, knower="player"),
    create_update("grudge_add", owner=npc_id, target=other_id, reason="被冒犯", severity=0.6),
]
```

### 4.2 指令类型（`VALID_SOCIAL_UPDATE_OPS`）

- `interact`：记录交互，微调关系权重
- `reveal`：扩展秘密知情者（单向）
- `set`：直接设置关系属性
- `connect` / `disconnect`：建立/断开连接
- `consensus_add` / `consensus_remove`：共识区域增删成员
- `faction_join` / `faction_leave`：阵营加入/退出
- `rumor_create` / `rumor_spread`：舆论创建/传播
- `reputation_change`：声誉变化
- `debt_add` / `grudge_add`：债务/恩怨新增
- `player_catalyst`：玩家催化（介绍/撮合/破坏）

### 4.3 串行节点 `social_node`

在 `sync_status → social_node → bg_chat_node` 链路中运行，负责：
1. 消费 `social_updates` 指令（去重）。
2. 从 `secrets_revealed` 自动扩展秘密知情者。
3. 从 `p2p_transcript` + `bg_chat_targets` 自动记录 NPC 间交互。
4. 舆论衰减（`decay_rumors`）。

### 4.4 串行节点直接更新

串行节点（如 `p2p_node`）可直接调用函数并返回更新后的图：

```python
from social_network import record_interaction, expand_known_by

graph = record_interaction(graph, a, b, tick=tick, df=0.02, dt=0.01, note="交流", interaction_type="p2p")
out["social_graph"] = graph
```

## 5. 查询与 Prompt 注入

```python
from social_network import (
    social_context_for_npc, get_relationship, who_knows,
    emotion_contagion, compute_status,
)

# 注入 NPC prompt
ctx = social_context_for_npc(graph, npc_id, npcs, tick=world_tick)

# 查询关系
rel = get_relationship(graph, npc_a, npc_b)

# 查询秘密知情者
knowers = who_knows(graph, owner_id, fact)

# 情绪传染（返回 mood patch）
patches = emotion_contagion(graph, npcs, tick=world_tick)

# 地位分数
scores = compute_status(npcs, graph)
```

## 6. 白名单（`constants.py`）

- `VALID_CONNECTION_TYPES`：连接类型
- `VALID_FACTION_RELATION_TYPES`：阵营关系类型
- `VALID_RUMOR_TYPES`：舆论类型
- `VALID_CONSENSUS_VISIBILITY`：共识可见性
- `VALID_SOCIAL_UPDATE_OPS`：更新指令操作类型
- `VALID_EDGE_ATTRS`：关系边数值属性名
- `VALID_INTERACTION_TYPES`：交互类型

## 7. 约束与边界

- `known_by` 单向扩展（秘密一旦透露不可收回）。
- 关系权重单次变化 ≤ ±0.02（阻尼），最终 ∈ [0,1]。
- 社交图独立于 `npcs`，不修改 NPC 血肉结构。
- 阵营可嵌套/交集，NPC 可同时属于多个阵营。
- 舆论有失真、定向传播、衰减机制。
- 不兼容旧档（新档测试）。

## 8. 下游接入示例

### npc_node（Prompt 注入）

```python
from social_network import social_context_for_npc
ctx = social_context_for_npc(state.get("social_graph") or {}, npc_id, npcs, tick=state.get("world_tick"))
# 将 ctx 追加到 prompt 中
```

### arbiter_node（决策权重）

```python
from social_network import get_relationship
rel = get_relationship(graph, nid, other_nid)
trust = rel.get("trust", 0.3)
```

### server.py（UI 字段）

`build_ui_state` 已输出 `social_graph` 字段，前端可直接消费。

## 9. 验证清单

- `social_genesis` 后 `social_graph` 包含 secrets（从 NPC cognition 生成）。
- `social_node` 消费 `social_updates` 不丢失、不重复。
- p2p 交流后 `relationships` 交互计数递增。
- 秘密透露给玩家后 `player_knows=True`。
- 舆论衰减正常（`heat` 随轮次下降）。
- `/game/state` 返回 `social_graph` 字段。