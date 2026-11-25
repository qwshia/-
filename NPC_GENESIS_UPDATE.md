# npc_genesis 节点升级说明

## 1. 改动概要

- 新增 `npc_genesis_prompt_builder.py`：把原来 `npc_genesis.py` 中的巨型 prompt 字符串拆成模块化函数，按 `style_profile` 动态拼装提示词。
- `npc_genesis.py` 改为调用 `build_npc_prompt(backstory, ending_goal, global_truth, roster, style_profile, scenes, props)`，仅保留解析/校验/规范化/落库逻辑。
- NPC 血肉字段最终集见第 3 节。新增 attachment 核心价值观，affection 替代 initial_affection，background 为 NPC 一句话背景（不动态变化）。
- 不修改 `state.py` 的 npc 结构（字段名下游强依赖）。

## 2. style_profile 消费

`npc_genesis` 从 `state.style_profile` 读取，缺失字段回退默认。`_get_profile` 复用 `world_prompt_builder._get_profile`（向后兼容 `primary_genre → tone`）。

## 3. 最终字段集

### 必填（14）
background, appearance, attire, personality, life_goal, cognition, mood, core_drive, affection, self_interest, moral_boundary, attachment

### 可选（7）
fears, desires, perspective, contradictions, impulsivity, social_initiative, weakness

### cognition 类型
- fact: 全部 NPC 都可能知道的公开事实，没有则置空（无 source 字段）
- secret: 个人秘密（reveal_condition, known_by），content 中内嵌来源（如"我做了XX"/"我听到了XX"）
- belief: 基于有限信息的推断（confidence 0-100）
- forbidden: 拒绝谈论的话题（reason）

### 不包含（专门节点/运行时）
relationships, home_location, current_intent, affection (运行时更新), memory.l1, weakness.state (运行时)

## 4. 动态字段

动态字段：cognition, mood, fears, desires, contradictions, perspective, impulsivity, affection, attire, personality, self_interest, moral_boundary, attachment, social_initiative, weakness（种子，可后续补充但非每轮变化）。

## 5. 消费方更新

- `npc_node.py`：secrets/forbidden/belief → cognition (type=secret/forbidden/belief)；移除 mannerism/voice/traits 消费；新增 perspective/contradictions/fears/desires/attachment 注入；affection 初始值回退。
- `bystander_node.py`：secrets → cognition (type=secret)。
- `p2p_node.py`：secrets/forbidden → cognition。
- `npc_life_tick.py`：core_goal → core_drive；social_initiative 替代硬编码 0.40；mood.stability 消费。
- `time_events.py`：npc_effects 支持更新 fears/desires/contradictions/perspective/weakness/self_interest/moral_boundary/attachment。
- `server.py`：build_ui_state 同步新增字段映射（background, affection, attachment, self_interest, moral_boundary, social_initiative, weakness_state 等）。
- `arbiter_node.py`：impulsivity 影响 NPC 做决策时的随机因子；self_interest/moral_boundary/attachment 影响接受/拒绝与道德判断。

## 6. 白名单与枚举

constants.py 中 NPC 血肉相关白名单：
- VALID_MOODS：平静/焦虑/烦躁/愉悦/恐惧/压抑（6 核心，运行时可动态追加）
- VALID_STABILITY：stable/volatile/fragile
- VALID_COGNITION_TYPES：fact/secret/belief/forbidden
- VALID_SERVES：self/other/faction/group（允许自定义，动态登记）
- FLOAT_FIELD_MIN/MAX：0.0-1.0（self_interest/moral_boundary/attachment/impulsivity/social_initiative）
- INITIAL_AFFECTION_MIN/MAX：-100~200（affection 种子值）
- NPCBloodField 枚举：字段名集合，供下游节点查询/修改

## 7. 硬编码与默认值

- weakness 可为空对象 `{}`，交由后续节点动态补充。
- mood.current/stability 非法回退：平静/stable。
- 浮点字段默认值：self_interest 0.5、moral_boundary 0.5、attachment 0.5、impulsivity 0.5、social_initiative 0.4。
- affection 默认入参：100。
- belief.confidence 默认：50，钳制 0-100。
- secret.known_by 默认：[]。

## 8. 字段关联

- personality → impulsivity, social_initiative, perspective, affection
- self_interest → perspective, social_initiative, affection, fears, desires, contradictions
- moral_boundary → perspective, fears, desires, contradictions
- attachment → perspective, fears, desires, contradictions
- life_goal → core_drive, contradictions, mood
- core_drive → weakness, fears, desires, contradictions
- perspective → cognition(belief confidence), affection
- tone → mood.current
- global_truth + role → cognition (secret/belief)

## 9. 下游节点参考字段

NPC 血肉节点输出的核心字段供下游节点参考：
- 核心价值观：self_interest / moral_boundary / attachment（0.0-1.0），影响 perspective / fears / desires / contradictions 的生成与运行时对话。
- mood.current / mood.stability：运行时 npc_life_tick / npc_node 消费，情绪变化需同步更新 mood.reason。
- core_drive.serves：后续 NPC 自主层参考，决定 NPC 为谁行动。
- weakness（可为空）：运行时可动态补充/更换 trigger，state 单向 closed→broken。
- cognition：secret 的 known_by 作为动态社交网络的初始种子；belief 的 confidence 用于 NPC 是否动摇。
- impulsivity / social_initiative：0.0-1.0，分别影响决策随机性与主动社交概率。
- affection：初始好感度种子，运行时更新同一字段。

下游节点做事/说话时应参考上述字段，保持人物一致性。

## 10. 通用提示词优化

- NPC_GENERAL_RULES 精简，移除重复和冗余。
- NPC_FIELD_SPEC 每个字段标注 [必填]/[可选]，白名单字段统一列出可选值（从 constants 动态读取）。
- NPC_FIELD_SPEC 新增 [字段关联-通用] 章节，统一列出所有字段的关联关系。
- NPC_OUTPUT_FORMAT 保持纯 JSON 示例，无必填/可选备注。
- NPC_SELF_CHECK 合并重复项，新增自检条目。
- 所有 hardcoded 默认值应让模型自由生成，代码仅校验/钳制。
- 数值字段需在 prompt 中明确区间与类型倾向。