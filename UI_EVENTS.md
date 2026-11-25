本文档与 server.py / action_node.py / item_node.py / npc_node.py / arbiter_node.py / p2p_node.py / graph.py 当前版本逐字段对齐。前端按此契约直接消费即可，无需与后端二次协商。

0. 全局约定
坐标：后端不下发任何坐标。场景内 NPC、物品的布局位置由前端自行随机/固定生成。后端只保证"该场景中有哪些 NPC、哪些物品"。

容器子物品：scene_items 只下发容器本体（name / desc / locked），不含 content 子物品。玩家点击容器触发 !调查 容器名 后，由 container_opened 事件携带 revealed_items 二级下发。

ui_events / new_dialogs 均为增量：server.py 通过 thread_ui_events_len / thread_history_len 缓存追踪，每次只返回上次之后的增量。服务重启会重置缓存，可能重复下发最近一批，前端需做幂等（按 unique key 去重）。

事件顺序：ui_events 数组按产出顺序排列，前端按序消费；无顺序依赖的事件可并行播放动画。

affection_change 特殊：不是图节点产出的 ui_events，而是 server.py::build_ui_state 在返回前动态追加。它同样走增量逻辑，但追踪依据是 thread_affection_snap，与 ui_events 独立。

view 参数：/game/act 和 /game/state 支持 view=full|strict。strict 会过滤掉未携带、锁着、容器内的物品（用于战斗/偷窃 UI 严格模式），默认 full。

1. 状态快照结构（ui_state）
每次 /game/new、/game/act、/game/state/{tid} 都返回完整的 ui_state，前端做全量刷新。

1.1 顶层字段
字段	类型	说明
world_tick	int	世界时钟，每步玩家操作 +1。可用于判断"事件是否本轮产生"
current_scene	str	当前场景名
current_scene_desc	str	当前场景描述文本
scene_items	list	当前场景可交互物品清单，见 1.2
time_phase	str	当前时间档（如"黄昏"）
player_items	list	玩家背包，见 1.3
available_scenes	list	可去场景清单，见 1.4
present_npcs	list	当前场景的 NPC 清单，见 1.5
all_npcs_detail	list	全部 NPC 清单，结构同 1.5 但含 location，不含 affection_delta
new_dialogs	list	本轮新增对话，见 1.6
ui_events	list	本轮新增事件，见第 2 章
backstory	str	剧本背景
ending_goal	str	主线目标
clues	list[str]	玩家已知线索
world_events	list[str]	世界事件文本流
p2p_transcript	str	最近一次 p2p 对话全文（可为空）
1.2 scene_items 元素
json
{ "name": "红木密匣", "desc": "一个上了锁的旧木匣。", "locked": true }
字段	类型	说明
name	str	物品名
desc	str	描述（可空）
locked	bool	是否上锁（前端显示锁图标；玩家点击触发 !调查 名字 后端裁决）
注意：容器子物品不在此列。前端要在玩家打开容器后，用 container_opened.revealed_items 动态补充临时渲染。

1.3 player_items 元素
json
{ "name": "怀表", "count": 1, "desc": "银质外壳，指针停在三点十七分。" }
字段	类型	说明
name	str	物品名
count	int	数量（≥1）
desc	str	描述（已去重合并）
1.4 available_scenes 元素
json
{ "name": "走廊", "locked": false, "requires": "" }
字段	类型	说明
name	str	场景名
locked	bool	是否被门禁挡住
requires	str	若 locked，需要什么凭证（如 "储物间钥匙" 或 "A+B"，B 多凭证）
前端语义：点击"门"打开场景列表时，locked=false 可直接点，locked=true 显示 requires 提示，点击后由后端 action_engine 裁决实际能否进入。

1.5 present_npcs / all_npcs_detail 元素
json
{
  "id": "npc_1",
  "name": "沈清漪",
  "role": "新转来的同桌兼年级第一",
  "appearance": "…",
  "attire": { "内搭": "白衬衫", "外套": "藏青校服" },
  "personality": "…",
  "affection": 120,
  "affection_delta": 5,
  "weakness_state": "closed",
  "weakness_type": "fear",
  "emotion_tags": [{"target": "玩家", "tag": "戒备", "tick": 47}],
  "stealable_items": [{"name": "八音盒", "desc": "…"}],
  "visible_items": [{"name": "八音盒", "desc": "…"}],
  "has_pending": false,
  "pending_summary": [],
  "current_intent": null,
  "personality_traits": [{"trait": "多疑", "intensity": 80}],
  "goal": "…",
  "core_goal": {"goal": "…", "priority": 100},
  "short_term_goal": {"goal": "…", "source": "daily"},
  "mood": {"current": "平静", "reason": "开局", "since_tick": 0},
  "current_action": {"action": "擦杯子", "since_tick": 12},
  "daily_routine": [{"phase": "清晨", "actions": ["…"]}],
  "life_events": [{"tick": 5, "event": "…", "emotion": "…", "source": "world"}]
}
关键字段语义：

字段	语义
affection_delta	本轮好感变化量（仅 present_npcs 有），可用于飘字
weakness_state	closed（防线完整）/ broken（已破防）
stealable_items	小手按钮候选列表。view=strict 时已过滤掉锁着和容器内的，即"确定能偷"；view=full（默认）包含所有 carried 物品，是否成功由后端裁决
visible_items	悬停 NPC 时展示的"能看到的物品"清单（不含容器内子物品）
current_intent	未执行的自主意图（结构 {kind, target, detail}）；null 表示无
pending_summary	待执行意图的摘要（人类可读字符串，如 "bg_chat:沈清漪"）
has_pending	是否有 pending_intent 队列
present_npcs 与 all_npcs_detail 差异：

present_npcs	all_npcs_detail
范围	仅当前场景	全部 NPC
location	无（即当前场景）	有
affection_delta	有	无
1.6 new_dialogs 元素
json
{ "speaker": "npc_1", "content": "你动过那个抽屉？" }
{ "speaker": "user", "content": "你好", "target": "npc_1" }
{ "speaker": "system", "content": "【系统】沈清漪去了走廊。" }
字段	类型	说明
speaker	str	user / system / npc_id
content	str	台词或系统提示
target	str	仅 user 条目有：玩家这轮对话的对象（npc_id 或 "全部"）
前端渲染建议：

system → 灰色旁白样式

user → 玩家气泡

npc_id → 转 present_npcs[id].name 后渲染 NPC 气泡

2. UI 事件清单（ui_state.ui_events）
按类别分组。字段详解见第 3 章。

2.1 场景类
type	生产者	场景
scene_transition	action_engine	玩家成功切换场景
npc_move	npc_node / arbiter_node / time_node(tick_pending) / p2p_node	NPC 位移
2.2 物品类
type	生产者	场景
item_transfer	item_node / npc_node / arbiter_node	物品在 玩家↔NPC↔场景 之间移动
item_acquired	item_node	物品进入玩家背包（不含来源）
item_given	item_node	玩家把物品交给 NPC（已完成交割）
container_opened	action_engine / item_node	容器被打开，revealed_items 为子物品名列表
2.3 外观与情绪类
type	生产者	场景
attire_change	npc_node / arbiter_node	NPC 换装
affection_change	server.py（在 build_ui_state 中追加）	好感变化
emotion_shift	npc_node	NPC 新增情绪标签
mental_break	npc_node	NPC 心理防线崩溃（weakness 命中）
mental_guard	npc_node	NPC 防线被探测但未击穿（weakness miss）
2.4 对话与旁白类
type	生产者	场景
show_options	npc_node	快捷回复选项（每轮必发，空数组表示清屏）
ripple	npc_node	NPC 余波（第一人称本能反应）
observer_reaction	npc_node（is_observer=true）	旁观 NPC 反应
npc_chat_start	p2p_node（后台 bg_chat）	后台 NPC 私聊开始
npc_chat_line	p2p_node（后台）	后台 NPC 私聊台词
npc_chat_end	p2p_node（后台）	后台 NPC 私聊结束
2.5 反馈与暗流类
type	生产者	场景
action_feedback	npc_node / item_node	动作反馈（激怒/威胁/偷窃失败震屏）
pending_expired	utils.py::tick_pending（经 time_node / p2p_node 合并）	NPC 待执行意图最终放弃
待确认 / 历史遗留：

pending_set：main.py 有消费代码，当前 arbiter_node.py 已不产出。如果前端已有对接，需等 arbiter_node 恢复产出或删除前端消费。

3. 事件字段详解
3.1 scene_transition
玩家成功切换场景时触发。

json
{ "type": "scene_transition", "from_scene": "天台", "to_scene": "走廊", "motion": "walk" }
字段	类型	语义
from_scene	str	原场景
to_scene	str	目标场景
motion	str	固定 "walk"，预留 "run" / "teleport"
渲染：黑屏过渡或滑动切换；切换后立即用新 ui_state.current_scene 刷新场景。

3.2 npc_move
NPC 场景内/跨场景移动。

json
{ "type": "npc_move", "npc": "沈清漪", "target_scene": "走廊" }
字段	类型	语义
npc	str	NPC 显示名（不是 id）
target_scene	str	目标场景名
渲染：在场 NPC 从屏幕走到门/出口淡出；目标场景刷新后重新出现。

3.3 item_transfer
物品所有权转移。这是最重要的物品事件，前端所有"物品飞行动画"都基于它。

json
{ "type": "item_transfer", "item": "怀表", "count": 1, "from_npc": "沈清漪", "to_player": true }
{ "type": "item_transfer", "item": "钥匙", "count": 1, "from_player": true, "to_npc": "赵明轩" }
{ "type": "item_transfer", "item": "八音盒", "count": 1, "from_scene": true, "to_npc": "沈清漪" }
字段	类型	语义
item	str	物品名
count	int	数量
from_npc	str?	来源是 NPC（显示名）
from_player	bool?	来源是玩家
from_scene	bool?	来源是场景（无人持有）
to_npc	str?	目标是 NPC（显示名）
to_player	bool?	目标是玩家
互斥规则：来源三选一（from_npc / from_player / from_scene），目标二选一（to_npc / to_player）。

渲染：以物品图标飞行插值动画实现转移可视化。

3.4 item_acquired
物品进入玩家背包（无来源信息，用于"获得物品"弹窗）。

json
{ "type": "item_acquired", "item": "密信" }
字段	类型	语义
item	str	物品名
注意：若同时有 item_transfer(to_player=true)，优先播放 transfer 动画，acquired 可只做"背包 +1"提示。

3.5 item_given
玩家把物品交给 NPC，交割完成（区别于 item_transfer，这是"最终确认"事件）。

json
{ "type": "item_given", "item": "钥匙", "to_npc": "赵明轩" }
前端语义：可以放心从背包移除（不需要等后续确认）。

3.6 container_opened
容器被打开，子物品暴露。

json
{ "type": "container_opened", "container": "红木密匣", "revealed_items": ["密信", "怀表"] }
字段	类型	语义
container	str	容器名
revealed_items	list[str]	子物品名清单（不含描述，描述走 new_dialogs 系统消息或 player_items 更新）
渲染：容器本体的 UI 展开/缩放动画，依次展示 revealed_items 图标。

重要：前端必须用此事件来补充 scene_items 缺失的子物品信息。scene_items 不含子物品是设计如此，避免剧透。

3.7 attire_change
NPC 换装。

json
{ "type": "attire_change", "npc": "沈清漪", "action": "take_off", "current_attire": {"内搭": "白衬衫"} }
字段	类型	语义
npc	str	显示名
action	str	put_on（穿上）/ take_off（脱下）
current_attire	dict	变化后的完整着装（字典，键 = 部位名）
渲染：NPC 立绘/立绘叠加层按 current_attire 刷新。空 dict 表示无衣着。

3.8 affection_change
json
{ "type": "affection_change", "payload": { "npc_id": "npc_1", "delta": 5 } }
字段	类型	语义
payload.npc_id	str	NPC id（不是显示名）
payload.delta	int	好感变化量（正负皆有）
渲染：NPC 头顶飘 +5 / -5 文字（绿/红），2 秒淡出。

3.9 emotion_shift
json
{ "type": "emotion_shift", "npc_id": "npc_1", "npc_name": "沈清漪", "tag": "戒备", "category": "对玩家" }
字段	语义
tag	新增的情绪标签文本
category	情绪对象（当前固定 "对玩家"）
渲染：NPC 头顶新增一个小标签/图标；配合 present_npcs[id].emotion_tags 展示完整列表（后端 FIFO 截断到 5 条）。

3.10 mental_break / mental_guard
json
{ "type": "mental_break", "npc_id": "npc_1", "npc_name": "沈清漪", "weakness_type": "fear", "reaction": "（退缩，声音发颤）" }
{ "type": "mental_guard", "npc_id": "npc_1", "npc_name": "沈清漪" }
字段	语义
weakness_type	fear / guilt / greed / pride / attachment 之一
reaction	命中时的行为描述（可空）
渲染：

mental_break → 屏幕轻微震动 + NPC 立绘切换破防态 + 弱化描边

mental_guard → NPC 立绘短暂高亮 + "防御"图标闪现

3.11 show_options
每轮 NPC 对话结束都发一次，无选项时也发空数组（用于清屏）。

json
{ "type": "show_options", "options": ["解释原因", "拉她过去", "沉默"] }
{ "type": "show_options", "options": [] }
前端语义：

options 非空 → 渲染 3 个快捷按钮，玩家点击直接作为下一句输入发出

options 为空 → 清空快捷按钮区

注意：指令模式（!@N 穿脱/给物/移动）下不会产生 options 事件（npc_node.py 的 cmd_parts 分支已禁 options），此时前端应保持按钮区为上一次状态或清空——建议清空。

3.12 ripple
NPC 第一人称"余波"（听到玩家动作时的本能反应）。

json
{ "type": "ripple", "npc_id": "npc_1", "npc_name": "沈清漪", "text": "(指尖扣紧笔记封面)" }
渲染：短暂显示在 NPC 头顶或气泡边缘，2 秒后自动消失。

3.13 observer_reaction
旁观 NPC 的短反应（一次 !调查 / !偷 / !去 / @1和@2交流 后，同场景其他 NPC 可能触发）。

json
{ "type": "observer_reaction", "npc_id": "npc_3", "npc_name": "林宇泽", "text": "(扒着栏杆探头) 哟，怎么了？" }
渲染：作为普通 NPC 气泡推送；如果前端有"旁白层"，可与主对话气泡区分层级。

3.14 npc_chat_start / npc_chat_line / npc_chat_end
后台 NPC 私聊（玩家不可控，仅旁观）。由 bg_chat_targets 触发。

json
{ "type": "npc_chat_start", "npc_ids": ["npc_1", "npc_2"], "names": ["沈清漪", "赵明轩"] }
{ "type": "npc_chat_line", "npc_id": "npc_1", "npc_name": "沈清漪", "content": "你动过那个抽屉？" }
{ "type": "npc_chat_line", "npc_id": "npc_2", "npc_name": "赵明轩", "content": "没有。" }
{ "type": "npc_chat_end", "npc_ids": ["npc_1", "npc_2"] }
渲染：可以做一个"旁听窗"的展开动画——start 时展开，line 时按顺序打字，end 时收起。

3.15 action_feedback
json
{ "type": "action_feedback", "target": "沈清漪", "action": "angry", "shake_screen": true }
{ "type": "action_feedback", "target": "沈清漪", "action": "steal_failed", "shake_screen": true }
{ "type": "action_feedback", "target": "沈清漪", "action": "threatened", "shake_screen": true }
action 值	触发条件	语义
angry	好感下降 ≥ 15	NPC 激怒
steal_failed	偷窃失败	被偷者察觉
threatened	移动指令被门禁阻挡	NPC 无法执行
渲染：shake_screen=true → 屏幕震动 0.3 秒；target 对应的 NPC 立绘可能抖动/变红。

3.16 pending_expired
json
{ "type": "pending_expired", "npc_id": "npc_2", "npc_name": "赵明轩", "kind": "move", "target": "走廊" }
待确认：此事件由 utils.py::tick_pending 产出（本次未提供该文件最新版）。字段名以上述为基础，若 utils.py 已变更请核对。

语义：NPC 有个未完成的自主意图重试 2 次后放弃。前端可显示灰色"（放弃）XXX 最终没能做成（move）"提示。

3.17 pending_set（当前版本不产出）
历史契约，main.py 有消费但 arbiter_node 已不产出。待确认：如果下一版本 arbiter_node 恢复产出，字段参考：

json
{ "type": "pending_set", "npc_name": "沈清漪", "reason": "目标不在场" }
4. 增量语义与幂等
4.1 new_dialogs / ui_events 增量机制
server.py 用两个字典追踪：

thread_history_len[tid] → history 数组已下发的长度

thread_ui_events_len[tid] → state["ui_events"] 数组已下发的长度

每次 build_ui_state 从这两个位置之后截取新增部分下发。LangGraph 的 history 和 ui_events 都通过 operator.add reducer 追加，只增不减。

风险：服务重启后这两个字典丢失，回到 0，会重新下发全部历史。前端需要按 speaker + content + tick（若有）做去重，或直接接受最后一次全量快照。

4.2 affection_change 增量
不依赖 state["ui_events"]，由 thread_affection_snap 独立追踪。语义为"本轮 snapshot 与上次 snapshot 的差值"。首次访问 delta 恒为 0。

4.3 建议的幂等 key
前端如需去重，建议：

new_dialogs：(speaker, target||'', content)

ui_events：按 type + 关键字段组合，如 item_transfer 用 (item, from_*, to_*)，npc_move 用 (npc, target_scene, world_tick)（需要服务端在事件里补 world_tick，当前未下发，可暂时不做）

5. 前端场景渲染顺序建议
针对任务书中的 2D 场景流程，一次玩家操作（如点击物品）的推荐渲染顺序：

text
玩家点击物品 → 前端播放走到物品旁边的动画
→ POST /game/act { msg: "!调查 物品名" }
→ 收到 ui_state：
   1. 先更新 current_scene_desc / scene_items / present_npcs / player_items（全量刷新）
   2. 消费 new_dialogs：system 条目走旁白层
   3. 消费 ui_events：
      - scene_transition → 切换场景
      - container_opened → 容器展开动画
      - item_transfer → 物品飞行
      - item_acquired → 背包 +1 提示
      - observer_reaction → 旁观气泡
      - ripple → NPC 余波
      - show_options → 按钮更新
      - action_feedback → 震动/图标
      - affection_change → 飘字
      - npc_move → NPC 走出动画
   4. 最后消费 affinities（如果不需要飘字，直接按 present_npcs 刷新即可）
6. 已知未闭环 / 待确认项
#	事项	状态
1	pending_expired 字段名	需与最新 utils.py::tick_pending 核对
2	pending_set	当前 arbiter_node 不产出，main.py 消费代码为历史遗留
3	!提示 与 结局是： 路径不产出 ui_events	前端在这两种情况下不更新 scene_items / present_npcs 即可
4	p2p 前台的 【旁听】 前缀	仅终端打印，不下发 ui_events；前端看到的只有 npc_chat_start/line/end（后台场景）或直接走 new_dialogs（前台场景，无前缀）
契约版本：v1（阶段六·步机制收口后）
对齐代码：action_node.py / item_node.py / npc_node.py / arbiter_node.py / p2p_node.py / graph.py / server.py 当前运行版
维护规则：任何 ui_events.append(...) 的增删改或字段变更，必须同步更新本文档对应章节。