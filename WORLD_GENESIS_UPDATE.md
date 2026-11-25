# world_genesis 节点升级说明

## 1. 改动概要

- 新增 `world_prompt_builder.py`：把原来 `world.py` 中的巨型 `_PROMPT_TEMPLATE` 字符串拆成模块化函数，按 `style_profile` 动态拼装提示词。
- `world.py` 改为调用 `build_world_prompt(task, rag_reference, style_profile)`，仅保留解析/校验/规范化/落库逻辑。
- `state.py` 新增 `style_profile: dict` 字段。
- `server.py` 的 `NewGameReq` 新增 `style_profile: dict = {}`，并在 `/game/new` 中传入 graph。
- `main.py` 的 CLI 开局调用同步传入 `style_profile: {}`（默认空，向后兼容）。

## 2. style_profile 结构

```json
{
  "tone": "horror",
  "setting": "school",
  "scale": "medium",
  "pace": "medium",
  "time_pressure": true,
  "playability": "enhanced",
  "difficulty": "normal",
  "boundaries": [],
  "dynamic_events": false
}
```

| 字段 | 类型 | 取值 | 含义 |
|---|---|---|---|
| `tone` | str | 见 TONES 列表 (12 种) | 主风格，决定基调感受 |
| `setting` | str | 见 SETTINGS 列表 (17 种) | 主类型，决定场景/人物/道具特色 |
| `scale` | str | small / medium / large | 剧本规模 (NPC 1-3 / 3-8 / 8+, 场景 2-4 / 5-7 / 10+) |
| `pace` | str | slow / medium / fast | 节奏 |
| `time_pressure` | bool | true/false | 是否启用时间系统 |
| `playability` | str | basic / enhanced | 玩家交互/线索收集丰富度 |
| `difficulty` | str | easy / normal / hard | 推理链长度 (2 / 3-6 / 7+ 步) |
| `boundaries` | list[str] | 任意字符串 | 玩家禁用项 |
| `dynamic_events` | bool | true/false | 预留开关 (未实现) |

## 3. Tone 列表 (12 种)

mystery, cozy, horror, romance, thriller, comedy, tragedy, noir, psychological, adventure, supernatural, survival

每个 tone 包含：cn（中文名）、flavor（软性描述）、atmosphere（引导性疑问句）、time_pressure_default、playability_default。

## 4. Setting 列表 (17 种)

school, workplace, medical, legal, historical, fantasy, sci_fi, cyberpunk, family, western, post_apocalyptic, wuxia, xianxia, military, sports, detective, espionage

每个 setting 包含：cn（中文名）、flavor（软性描述）、atmosphere（引导性疑问句）、time_pressure_default、playability_default。

## 5. atmosphere 设计

- tone atmosphere：引导性疑问句，如 "这个剧本的场景、背景、真相应该营造什么感觉？"
- setting atmosphere：引导性疑问句，如 "这个剧本的场景、人物、道具应该体现什么样的校园特色？"
- 无具体描述词，让模型自行思考，不被限制

## 6. difficulty 规则

- easy: 推理链 2 步，好人偏多，坏人/嫌疑人偏少
- normal: 推理链 3-6 步，好人与坏人/嫌疑人各半
- hard: 推理链 7+ 步，坏人/嫌疑人偏多，好人偏少
- 好坏人数量由模型根据难度+规模动态划分，不固定具体数字
- 好感度/真假话/NPC 性格 → NPC 重构窗口负责

## 7. playability 规则

- basic: 基础交互 (调查/对话/移动), truth_leads 每条 fact 必须有 via, endings 至少 1 个
- enhanced: 多路径 truth_leads, bio/both/either 锁, NPC 持有线索物, 交互道具, 时间门控, 多结局

## 8. 前端-后端数据对齐

- 前端在 `/game/new` 请求体中传入 `style_profile`（可选，默认 `{}`）
- 后端 `world_genesis` 从 `state.style_profile` 读取，缺失字段回退默认值
- `world_genesis` 产出的 `style_profile` 会写回 state，保持一致
- 输出 schema（scenes/npcs/props/items 字段名）保持不变，下游节点强依赖
- 向后兼容：旧的 `primary_genre`/`secondary_genres` 字段仍可被 `_get_profile` 识别并迁移

## 9. 动态时间事件（预留）

- `dynamic_events=true` 时，运行时允许根据世界变化动态生成新的定时事件
- 当前 `timeline_genesis` 负责固定锚点事件的生成与校验
- 动态生成逻辑尚未实现，后续在 `time_node` 或 `story_keeper` 中接入

## 10. 文件清单

- `world_prompt_builder.py`：提示词构建器（TONES 12 种 + SETTINGS 17 种、难度/可玩性规则、通用规则、字段说明、输出格式、自检）
- `world.py`：世界引擎节点，调用构建器，保留解析/校验/规范化
- `state.py`：新增 `style_profile` 字段
- `server.py`：`NewGameReq` 新增 `style_profile`
- `main.py`：CLI 开局传入 `style_profile: {}`
- `WORLD_GENESIS_UPDATE.md`：本说明文档

## 11. 未涉及的节点（留给其他窗口）

- `npc_genesis.py` / `npc_genesis_prompt_builder.py` — NPC 血肉生成
- `scene_layout_genesis.py` — 场景布局风格注入
- `timeline_genesis.py` — 时间事件风格注入

## 12. 真实世界设计规则（新增）

以下规则已整合到提示词中，通过 prompt 规则实现，不新增字段：

- global_truth 可以包含永远不可知的事实（证据销毁/死人带秘密/时间流逝），truth_leads 不必覆盖全部
- scenes.desc 应包含"冗余细节"（墙上涂鸦、食物、工具等不服务解谜的物品），填充生活感
- 环境应与 NPC 性格/秘密对应，风格统一，不强行放违和物品埋线索
- 允许"无线索"的日常场景，不是每个场景都要埋伏笔
- 单场景不要增加太多的无关物品，冗余细节写在 desc 中，不登记为 items
- endings 应包含至少 1 个坏结局/无赢家结局/没答案的结局（如果剧本需要多结局）
- backstory 应允许玩家什么主线都不做，世界照常运转
- 场景 desc 描述的是初始状态，过去事件的痕迹由 time_events 后续更新