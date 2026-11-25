"""NPC 提示词构建器:模块化拼装 npc_genesis 的提示词.

按 style_profile 动态注入风格插槽,决定 NPC 血肉的「量」与「质」:
- tone(主风格/基调):决定 NPC 的性格倾向,秘密类型,恐惧来源,矛盾设计.
- setting(主类型/场景):决定 NPC 的身份,外貌,衣着,日常动作倾向.
- playability:basic / enhanced,控制 NPC 血肉丰富度.
- difficulty:easy / normal / hard,控制秘密推理步数,belief 可信度.
- scale:small / medium / large,控制 NPC 数量与血肉量.
- boundaries:玩家禁用项,NPC 的 appearance/attire/cognition/weakness 中不得违反.
"""
from typing import Any, Dict, List, Optional

from constants import VALID_MOODS, VALID_STABILITY, VALID_COGNITION_TYPES, VALID_SERVES
from world_prompt_builder import _get_profile


# ============================================================
# Tone(基调)→ NPC 血肉规则
# ============================================================
NPC_TONE_RULES: Dict[str, List[str]] = {
    "mystery": [
        "[悬疑] 至少 1 个 NPC 有 secrets(第一人称 fact,与 global_truth 片段对应).",
        "[悬疑] 至少 1 个 NPC 有 beliefs(基于有限信息的推断,用于推理干扰).",
        "[悬疑] 至少 1 个 NPC 有 weakness(trigger 可被玩家自然说出的短语,触发后透露 secret).",
        "[悬疑] 至少 1 个 NPC 有 fears(与真相/揭露相关).",
        "[悬疑] 至少 1 个 NPC 有 contradictions(如'想保护某人但又想自保'),制造不一致性.",
        "[悬疑] secrets 的 known_by 至少 1 个有值(同伙/证人),制造信息不对称.",
    ],
    "cozy": [
        "[温馨] NPC 可全无 core_drive/secrets/beliefs,村民类 NPC 不需要 plot 功能.",
        "[温馨] 至少 1 个 NPC 有 desires(日常渴望,如'想和某人和好').",
        "[温馨] NPC 的 fears 为日常恐惧(如'怕被误解'),非致命威胁.",
        "[温馨] contradictions 为轻量矛盾(如'想帮忙但又怕多管闲事').",
    ],
    "horror": [
        "[恐怖] 至少 1 个 NPC 的 mood.current 初始为「恐惧」或「压抑」.",
        "[恐怖] 至少 1 个 NPC 有 fears 为环境威胁(如'怕黑暗','怕声音').",
        "[恐怖] 至少 1 个 NPC 有 weakness.trigger 为恐怖相关短语.",
        "[恐怖] 至少 1 个 NPC 有 contradictions(如'想逃但不能丢下同伴').",
        "[恐怖] 至少 1 个 NPC 有 beliefs(对超自然现象的误解).",
        "[恐怖] 至少 1 个 NPC 的 emotional_baseline 为 volatile 或 fragile.",
    ],
    "romance": [
        "[浪漫] 至少 1 个 NPC 有 core_drive,且与玩家相关.",
        "[浪漫] 至少 1 个 NPC 有 weakness.trigger 为情感相关短语.",
        "[浪漫] 至少 1 个 NPC 有 secrets(第一人称),reveal_condition 为「玩家表现出关心」.",
        "[浪漫] 至少 1 个 NPC 有 desires(与玩家/某人相关).",
        "[浪漫] 至少 1 个 NPC 有 contradictions(如'想靠近但又怕受伤').",
        "[浪漫] 至少 1 个 NPC 的 affection 高于 100(亲近)或低于 50(冷淡).",
    ],
    "thriller": [
        "[惊悚] 至少 1 个 NPC 有 core_drive 为「隐藏身份」或「除掉某人」.",
        "[惊悚] 至少 1 个 NPC 有 weakness.trigger 为威胁相关短语.",
        "[惊悚] 至少 1 个 NPC 有 secrets(第一人称),与阴谋/背叛相关.",
        "[惊悚] 至少 1 个 NPC 有 beliefs(对他人动机的怀疑).",
        "[惊悚] 至少 1 个 NPC 有 contradictions(如'想信任但又不敢').",
        "[惊悚] 至少 1 个 NPC 的 impulsivity 高于 0.6(冲动决策).",
    ],
    "comedy": [
        "[喜剧] 至少 1 个 NPC 的 personality 含「滑稽/夸张」特质.",
        "[喜剧] 至少 1 个 NPC 有 beliefs(误解),用于制造笑点.",
        "[喜剧] 至少 1 个 NPC 有 contradictions(如'想帮忙但越帮越忙'),用于制造误会.",
        "[喜剧] NPC 的 fears 为轻量恐惧(如'怕出丑'),非致命威胁.",
        "[喜剧] 至少 1 个 NPC 有 desires(与笑点相关).",
    ],
    "tragedy": [
        "[悲剧] 至少 1 个 NPC 有 core_drive 为「牺牲自己」或「保护某人」.",
        "[悲剧] 至少 1 个 NPC 有 secrets(第一人称),reveal_condition 为「玩家逼问」.",
        "[悲剧] 至少 1 个 NPC 有 contradictions(如'想救人但又怕失去自己').",
        "[悲剧] 至少 1 个 NPC 的 mood.current 初始为「压抑」.",
        "[悲剧] 至少 1 个 NPC 有 fears(失去某人/某物).",
        "[悲剧] 至少 1 个 NPC 的 emotional_baseline 为 fragile.",
    ],
    "noir": [
        "[黑色] 至少 1 个 NPC 有 core_drive 为「利用玩家」或「隐藏罪行」.",
        "[黑色] 至少 1 个 NPC 有 weakness.trigger 为利益/骄傲相关短语.",
        "[黑色] 至少 1 个 NPC 有 secrets(第一人称),且为谎言/隐瞒.",
        "[黑色] 至少 1 个 NPC 有 beliefs(对他人动机的 cynical 推断).",
        "[黑色] 至少 1 个 NPC 有 contradictions(如'想做好人但利益驱使').",
        "[黑色] 至少 1 个 NPC 的 affection 低于 50(冷淡).",
    ],
    "psychological": [
        "[心理] 至少 1 个 NPC 有 beliefs(不可靠记忆/认知扭曲).",
        "[心理] 至少 1 个 NPC 有 weakness.trigger 为创伤相关短语.",
        "[心理] 至少 1 个 NPC 有 secrets(第一人称),与记忆/创伤相关.",
        "[心理] 至少 1 个 NPC 有 contradictions(如'记得某事但又怀疑自己记错了').",
        "[心理] 至少 1 个 NPC 的 emotional_baseline 为 volatile 或 fragile.",
        "[心理] 至少 1 个 NPC 有 fears(创伤相关).",
    ],
    "adventure": [
        "[冒险] 至少 1 个 NPC 有 core_drive 为「帮助玩家」或「共同目标」.",
        "[冒险] 至少 1 个 NPC 有 desires(探索/发现).",
        "[冒险] NPC 的 fears 为冒险相关(如'怕迷路','怕陷阱').",
    ],
    "supernatural": [
        "[超自然] 至少 1 个 NPC 有 beliefs(超自然感知/灵异体验).",
        "[超自然] 至少 1 个 NPC 有 weakness.trigger 为超自然相关短语.",
        "[超自然] 至少 1 个 NPC 有 secrets(第一人称),与超自然现象相关.",
        "[超自然] 至少 1 个 NPC 有 fears(超自然威胁).",
        "[超自然] 至少 1 个 NPC 有 contradictions(如'相信科学但又经历超自然').",
    ],
    "survival": [
        "[生存] 至少 1 个 NPC 有 core_drive 为「自保」或「逃离」.",
        "[生存] 至少 1 个 NPC 有 fears(资源稀缺/环境危害).",
        "[生存] 至少 1 个 NPC 有 desires(生存/资源).",
        "[生存] 至少 1 个 NPC 有 contradictions(如'想合作但又怕被背叛').",
        "[生存] 至少 1 个 NPC 有 weakness.trigger 为生存/利益相关短语.",
    ],
}


# ============================================================
# Setting(类型/场景)→ NPC 血肉规则
# ============================================================
NPC_SETTING_RULES: Dict[str, List[str]] = {
    "school": [
        "[校园] 至少 1 个 NPC 有 secrets(第一人称),与校园事件相关.",
        "[校园] 至少 1 个 NPC 有 weakness.trigger 为校园相关短语.",
        "[校园] NPC 的 fears 为校园相关(如'怕被孤立','怕成绩').",
    ],
    "workplace": [
        "[职场] 至少 1 个 NPC 有 core_drive 为「升职/保位/揭露」.",
        "[职场] 至少 1 个 NPC 有 secrets(第一人称),与公司秘密相关.",
        "[职场] NPC 的 fears 为职场相关(如'怕被裁员','怕背锅').",
    ],
    "medical": [
        "[医疗] 至少 1 个 NPC 有 secrets(第一人称),与病情/事故相关.",
        "[医疗] 至少 1 个 NPC 有 core_drive 为「隐瞒」或「救人」.",
        "[医疗] NPC 的 fears 为医疗相关(如'怕医疗事故','怕传染').",
    ],
    "legal": [
        "[法律] 至少 2 个 NPC 有 secrets(第一人称),与证词相关.",
        "[法律] 至少 1 个 NPC 有 forbidden(不愿作证).",
        "[法律] NPC 的 fears 为法律相关(如'怕定罪','怕作伪证').",
        "[法律] 至少 1 个 NPC 有 beliefs(对法律程序/证据的推断).",
    ],
    "historical": [
        "[历史] 至少 1 个 NPC 有 secrets(第一人称),与权力/家族相关.",
        "[历史] NPC 的 perspective 含时代偏见(如'重男轻女','门第观念').",
    ],
    "fantasy": [
        "[奇幻] 至少 1 个 NPC 有 secrets(第一人称),与魔法/诅咒相关.",
        "[奇幻] NPC 有 beliefs(对魔法/预言的推断).",
    ],
    "sci_fi": [
        "[科幻] 至少 1 个 NPC 有 beliefs(对科技/真相的误解).",
        "[科幻] NPC 的 fears 为科技相关(如'怕AI失控','怕数据泄露').",
    ],
    "cyberpunk": [
        "[赛博] 至少 1 个 NPC 有 core_drive 为「反抗公司」或「出卖信息」.",
        "[赛博] NPC 的 fears 为赛博相关(如'怕被追踪','怕义体排斥').",
    ],
    "family": [
        "[家庭] 至少 1 个 NPC 有 secrets(第一人称),与家族秘密相关.",
        "[家庭] 至少 1 个 NPC 有 weakness.trigger 为家庭相关短语.",
        "[家庭] NPC 的 fears 为家庭相关(如'怕家族分裂','怕失去亲人').",
        "[家庭] NPC 有 contradictions(如'想保护家族但又想逃离').",
    ],
    "western": [
        "[西部] 至少 1 个 NPC 有 core_drive 为「赏金」或「复仇」.",
        "[西部] 至少 1 个 NPC 有 weakness.type=greed.",
    ],
    "post_apocalyptic": [
        "[末日] 至少 1 个 NPC 有 core_drive 为「争夺资源」或「保护营地」.",
        "[末日] NPC 的 fears 为生存相关(如'怕饥饿','怕感染').",
    ],
    "wuxia": [
        "[武侠] 至少 1 个 NPC 有 secrets(第一人称),与门派恩怨相关.",
        "[武侠] 至少 1 个 NPC 有 weakness.trigger 为江湖相关短语.",
        "[武侠] NPC 的 perspective 含江湖观念(如'重义气','门派至上').",
    ],
    "xianxia": [
        "[仙侠] 至少 1 个 NPC 有 secrets(第一人称),与因果/天劫相关.",
        "[仙侠] NPC 有 beliefs(对天劫/因果的推断).",
    ],
    "military": [
        "[军事] NPC 的 fears 为军事相关(如'怕战败','怕牺牲').",
    ],
    "sports": [
        "[体育] NPC 有 desires(夺冠/突破).",
    ],
    "detective": [
        "[侦探] 至少 1 个 NPC 有 forbidden(不愿作证).",
        "[侦探] NPC 的 fears 为侦探相关(如'怕被怀疑','怕真相揭露').",
        "[侦探] 至少 1 个 NPC 有 beliefs(对案件的推断).",
    ],
    "espionage": [
        "[谍战] 至少 1 个 NPC 有 core_drive 为「隐藏身份」或「传递情报」.",
        "[谍战] NPC 的 fears 为谍战相关(如'怕身份暴露','怕被策反').",
        "[谍战] 至少 1 个 NPC 有 contradictions(如'想完成任务但又想背叛').",
    ],
}


# ============================================================
# 难度规则(NPC 相关)
# ============================================================
NPC_DIFFICULTY_RULES: Dict[str, List[str]] = {
    "easy": [
        "[难度-easy] secrets 直接可读(reveal_condition 容易触发),beliefs confidence 高(≥70).",
        "[难度-easy] red herring 不超过 1 个,且明确为氛围描写.",
        "[难度-easy] 至少 1 个 NPC 的 affection 高于 100(亲近).",
    ],
    "normal": [
        "[难度-normal] secrets 需 1-2 步推理(reveal_condition 需特定话题),beliefs 有矛盾.",
        "[难度-normal] red herring 1-2 个,部分与 truth 相关.",
    ],
    "hard": [
        "[难度-hard] secrets 需 2-3 步推理,beliefs 有矛盾且 ≥1 用于误导.",
        "[难度-hard] red herring 2-3 个,且与 truth 相关.",
        "[难度-hard] 至少 1 个 NPC 有 contradictions(增加推理难度).",
        "[难度-hard] 至少 1 个 NPC 的 affection 低于 50(冷淡).",
    ],
}


# ============================================================
# 可玩性档位规则(NPC 相关)
# ============================================================
NPC_PLAYABILITY_RULES: Dict[str, List[str]] = {
    "basic": [
        "[可玩性-basic] NPC 可全无血肉字段,仅保留 appearance/attire/personality/life_goal/mood.",
    ],
    "enhanced": [
        "[可玩性-enhanced] 建议至少 1 个 NPC 持有与 global_truth 相关的线索物(carried=true).",
        "[可玩性-enhanced] 建议至少 1 个 NPC 有 weakness(trigger 可被玩家自然说出).",
        "[可玩性-enhanced] 建议至少 1 个 NPC 有 beliefs(用于推理干扰).",
        "[可玩性-enhanced] 建议至少 1 个 NPC 有 contradictions(增加不一致性).",
        "[可玩性-enhanced] 建议至少 1 个 NPC 的 secrets 有 known_by(信息不对称).",
    ],
}


# ============================================================
# 通用 NPC 生成约束
# ============================================================
NPC_GENERAL_RULES = f"""[NPC 生成约束-通用]
1. 每个 NPC 独立生成,禁止互相参照. NPC 是活生生的人,有自己的记忆、动机、偏见,玩家只是众多变量之一.
2. life_goal 必须是 NPC 的个人人生目标, ≠ ending_goal. NPC 的目标服务自己,不是服务主线剧本.
3. cognition 中的 secret 必须第一人称(如"我藏了..."), belief 是基于有限信息的推断(可能错误), forbidden 是拒绝谈论的话题+原因. fact 是全部 NPC 都可能知道的公开事实(受限于 role/location),没有则置空. secrets 的 known_by 制造信息不对称: 不同 NPC 知道不同的秘密, 玩家需要从多处拼凑.
4. contradictions 是不一致性的来源: NPC 可以前后言行不完全统一, 只要符合其矛盾设定, 反而更真实.
5. perspective 是 NPC 的解读滤镜: 描述该 NPC 如何理解世界、他人与事件(多疑/友善/偏见/功利等). 它是 NPC 的主观认知框架, 用于生成对话与决策时的语气与倾向.
6. weakness.trigger_keywords 2-4 个短语, 禁用通用词. weakness 是初始值, 运行时可变(trigger/keywords 可随剧情变化). reaction 必须是具体行为, 其中提到的物品/信息必须已在剧本中出现. weakness 可为空对象, 交由后续节点动态补充.
7. mood.current 只能取: {"/".join(sorted(VALID_MOODS))}. mood.stability 只能取: {"/".join(sorted(VALID_STABILITY))}.
8. secrets/weakness 中提及的具名物品必须已登记于 props 或 scenes.items.
9. NPC 的 appearance/attire/cognition/weakness/beliefs 不得违反 boundaries. 两 NPC 的 personality 或 life_goal 高度相似时, 重写其中一个.
10. affection 为初始好感度, 根据 NPC 性格/role/与玩家的关系确定(友善/亲人→高, 冷漠/陌生人→中, 敌对/嫌疑人→低).
11. core_drive 为长期执念, 与 life_goal 并行(life_goal=想要什么, core_drive=怎么达成).
12. self_interest/moral_boundary/attachment 为 0.0-1.0 浮点数, 分别表示自利倾向/道德边界/依恋深情程度. impulsivity/social_initiative 为 0.0-1.0 浮点数, 分别表示冲动性/社交主动性.
13. 每个NPC生成字段的时候需要考虑到是否和别的字段有关联, 有的字段是互相影响的, 需要在生成的时候考虑到这些关联, 以保证NPC的合理性和一致性.
"""


# ============================================================
# 字段说明(输出 schema)
# ============================================================
NPC_FIELD_SPEC = f"""[NPC 字段说明]
background: [必填] 字符串, 一段话 NPC 背景.
appearance: [必填] 字符串, 一句话外貌(年龄/体型/五官/标志特征), 禁"普通/正常"空话.
attire: [必填] 对象, 分部位穿着(如{{"上身":"白衬衫","下身":"灰西裤"}}), 至少填上身+下身, 禁止空对象.
personality: [必填] 字符串, 一句话概括性格, 禁空话.
life_goal: [必填] 字符串, NPC 的个人人生目标(模糊人生愿望, 不是任务, ≠ ending_goal).
cognition: [必填] 数组,没有就为空, 内部字段互相不影响.  NPC 的认知库:
  - {{content, type:"fact"}}  // 全部 NPC 都可能知道的公开事实,没有则置空
  - {{content, type:"secret", reveal_condition:"...", known_by:["npc_id"]}}  // 个人秘密, content 中内嵌来源(如"我做了XX"/"我听到了XX")
  - {{content, type:"belief", confidence:0-100}}  // 基于有限信息的推断
  - {{content, type:"forbidden", reason:"..."}}  // 拒绝谈论的话题
mood: [必填] 对象{{current, reason, since_tick, stability}}. current 白名单: {"/".join(sorted(VALID_MOODS))}. stability 白名单: {"/".join(sorted(VALID_STABILITY))}.
core_drive: [必填] 对象{{goal, priority, serves}}. serves 建议值: {"/".join(sorted(VALID_SERVES))}, 允许自定义, 可多条.
fears: [可选] 数组[字符串], NPC 的恐惧列表.
desires: [可选] 数组[字符串], NPC 的渴望列表.
perspective: [可选] 字符串, NPC 的解读滤镜(多疑/友善/偏见描述).
contradictions: [可选] 数组[字符串], NPC 的内在矛盾(不一致性来源).
impulsivity: [可选] 浮点数 0.0-1.0, 数值越大,冲动性越高(默认 0.5).
weakness: [可选] 对象{{trigger, trigger_keywords, state, reaction}} 或 {{}}. trigger_keywords 2-4 个短语. state 初始 "closed".
social_initiative: [可选] 浮点数 0.0-1.0, 社交主动性表示NPC在社交互动中的主动程度,数值越大,主动性越强.
affection: [必填] 整数, 初始好感度(根据 NPC 性格/role/与玩家的关系确定, 运行时动态变化).
self_interest: [必填] 浮点数 0.0-1.0, 表示NPC的自利倾向,数值越大,自利性越强.
moral_boundary: [必填] 浮点数 0.0-1.0, 表示NPC的道德边界,数值越大,道德性越强.
attachment: [必填] 浮点数 0.0-1.0, 表示NPC的依恋/深情倾向,数值越大,依恋性越强.

[字段关联-通用]
personality 影响: impulsivity, social_initiative, perspective, affection
self_interest 影响: perspective, social_initiative, affection, fears, desires, contradictions
moral_boundary 影响: perspective, fears, desires, contradictions
attachment 影响: perspective, fears, desires, contradictions
life_goal 影响: core_drive, contradictions, mood
core_drive 影响: weakness, fears, desires, contradictions
perspective 影响: cognition(belief confidence), affection
tone 影响: mood.current 
global_truth + role 影响: cognition (secret/belief)"""


# ============================================================
# 输出格式
# ============================================================
NPC_OUTPUT_FORMAT = """[输出格式-严格JSON, 无其他文字]
{
  "npcs": [
    {
      "id": "npc_1",
      "background": "...",
      "appearance": "...",
      "attire": {"上身": "...", "下身": "..."},
      "personality": "...",
      "life_goal": "...",
      "cognition": [
        {"content": "...", "type": "fact"},
        {"content": "...", "type": "secret", "reveal_condition": "...", "known_by": []},
        {"content": "...", "type": "belief", "confidence": 70},
        {"content": "...", "type": "forbidden", "reason": "..."}
      ],
      "mood": {"current": "平静", "reason": "开局", "since_tick": 0, "stability": "stable"},
      "core_drive": {"goal": "...", "priority": 100, "serves": "self"},
      "affection": 100,
      "self_interest": 0.5,
      "moral_boundary": 0.5,
      "attachment": 0.5,
      "fears": ["..."],
      "desires": ["..."],
      "perspective": "...",
      "contradictions": ["..."],
      "impulsivity": 0.5,
      "weakness": {"trigger": "...", "trigger_keywords": ["..."], "state": "closed", "reaction": "..."},
      "social_initiative": 0.4
    }
  ]
}"""


# ============================================================
# 内部自检
# ============================================================
NPC_SELF_CHECK = """[输出前内部自检(不输出)]
1. 每个 NPC 的 life_goal 是否 ≠ ending_goal?
2. cognition 中的 secret 是否第一人称?
3. cognition 中的 belief 是否基于有限信息的合理推断(可能错误)?
4. weakness.trigger_keywords 是否 2-4 个短语且非通用词?
5. mood.current/stability 是否在有效取值内?
6. 是否至少 1 个 NPC 有 secrets/beliefs/fears/contradictions(按 tone 要求)?
7. 是否两 NPC 的 personality 或 life_goal 高度相似?若是,重写其中一个.
8. secrets 的 known_by 是否制造信息不对称?
9. 所有字段是否符合 boundaries 约束?
10. social_initiative 是否与 personality 一致?
11. affection 是否根据 NPC 性格/role/与玩家的关系确定?
12. self_interest/moral_boundary/attachment 是否与 personality 一致?
13. fears/desires 是否与 core_drive 和 self_interest/moral_boundary 一致?
14. contradictions 是否与 life_goal 和 core_drive 和 moral_boundary 一致?
15. perspective 是否与 self_interest 和 moral_boundary 一致?"""


def _normalize_genre_list(raw, valid_set) -> List[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out = []
    seen = set()
    for g in raw:
        g = str(g or "").strip().lower()
        if g and g in valid_set and g not in seen:
            out.append(g)
            seen.add(g)
    return out


def _tone_section(profile: Dict[str, Any]) -> str:
    tone = profile.get("tone", "")
    parts = []
    if tone and tone in NPC_TONE_RULES:
        # flavor from world_prompt_builder TONES
        try:
            from world_prompt_builder import TONES
            if tone in TONES:
                g = TONES[tone]
                parts.append(f"[主基调:{g['cn']}]")
                parts.append(f"flavor:{g['flavor']}")
                parts.append(f"atmosphere:{g['atmosphere']}")
        except Exception:
            parts.append(f"[主基调:{tone}]")
        parts.append("NPC 血肉规则:")
        for r in NPC_TONE_RULES.get(tone, []):
            parts.append(f"  - {r}")
    else:
        parts.append("[主基调:未指定,由角色铸造师根据玩家要求自行适配]")
    return "\n".join(parts)


def _setting_section(profile: Dict[str, Any]) -> str:
    setting = profile.get("setting", "")
    parts = []
    if setting and setting in NPC_SETTING_RULES:
        try:
            from world_prompt_builder import SETTINGS
            if setting in SETTINGS:
                g = SETTINGS[setting]
                parts.append(f"[主类型:{g['cn']}]")
                parts.append(f"flavor:{g['flavor']}")
                parts.append(f"atmosphere:{g['atmosphere']}")
        except Exception:
            parts.append(f"[主类型:{setting}]")
        parts.append("NPC 血肉规则:")
        for r in NPC_SETTING_RULES.get(setting, []):
            parts.append(f"  - {r}")
    else:
        parts.append("[主类型:未指定,由角色铸造师根据玩家要求自行适配]")
    return "\n".join(parts)


def _scale_npc_hint(scale: str) -> str:
    if scale == "small":
        return "规模:small(NPC 1-3 人,血肉可精简,secrets/beliefs/fears 可少量)"
    if scale == "large":
        return "规模:large(NPC 8+ 人,血肉丰富,secrets/beliefs/fears/contradictions 应充足)"
    return "规模:medium(NPC 3-8 人,血肉适中)"


def _pace_hint(pace: str) -> str:
    if pace == "slow":
        return "节奏:slow(慢节奏,重氛围与关系,NPC 性格细腻)"
    if pace == "fast":
        return "节奏:fast(快节奏,重冲突与推进,NPC 性格鲜明)"
    return "节奏:medium(平衡)"


def _difficulty_npc_section(difficulty: str) -> str:
    rules = NPC_DIFFICULTY_RULES.get(difficulty, NPC_DIFFICULTY_RULES["normal"])
    return "[难度:" + difficulty + "]\n" + "\n".join(rules)


def _playability_npc_section(playability: str) -> str:
    rules = NPC_PLAYABILITY_RULES.get(playability, NPC_PLAYABILITY_RULES["basic"])
    return "[可玩性:" + playability + "]\n" + "\n".join(rules)


def _boundaries_section(boundaries: List[str]) -> str:
    if not boundaries:
        return ""
    lines = ["[玩家边界/禁用项(必须遵守,NPC 的 appearance/attire/cognition/weakness/beliefs 不得违反)]"]
    for b in boundaries:
        lines.append(f"  - {b}")
    return "\n".join(lines)


def build_npc_prompt(
    backstory: str,
    ending_goal: str,
    global_truth: str,
    roster: List[Dict[str, Any]],
    style_profile: Optional[Dict[str, Any]] = None,
    scenes: Optional[List[str]] = None,
    props: Optional[List[str]] = None,
) -> str:
    profile = _get_profile(style_profile)
    tone = profile["tone"]
    setting = profile["setting"]
    scale = profile["scale"]
    pace = profile["pace"]
    playability = profile["playability"]
    difficulty = profile["difficulty"]
    boundaries = profile["boundaries"]

    if not playability:
        # fallback: tone default
        try:
            from world_prompt_builder import TONES
            if tone in TONES:
                playability = TONES[tone].get("playability_default", "basic")
        except Exception:
            playability = "basic"
    if not playability:
        playability = "basic"

    # time_pressure default (for deadline_phase hint)
    time_pressure = profile["time_pressure"]
    if time_pressure is None:
        try:
            from world_prompt_builder import TONES, SETTINGS
            if tone in TONES:
                time_pressure = TONES[tone].get("time_pressure_default", False)
            elif setting in SETTINGS:
                time_pressure = SETTINGS[setting].get("time_pressure_default", False)
        except Exception:
            time_pressure = False
    if time_pressure is None:
        time_pressure = False

    scenes_hint = ""
    if scenes:
        scenes_hint = "已登记场景:" + ",".join(scenes)
    props_hint = ""
    if props:
        props_hint = "已登记物品:" + ",".join(props)

    sections = []
    sections.append("你是角色铸造师. 给定NPC名单, 为每个NPC生成血肉.")
    sections.append("[铁律] 只输出严格JSON, 不要任何额外文字/说明/markdown标记. JSON必须合法: 双引号,无尾逗号,无注释.")
    sections.append(f"[剧本背景]\nbackstory: {backstory}\nending_goal: {ending_goal}\nglobal_truth: {global_truth}")
    sections.append(f"[NPC名单]\n{__import__('json').dumps(roster, ensure_ascii=False, indent=2)}")
    if scenes_hint:
        sections.append(f"[场景列表]\n{scenes_hint}")
    if props_hint:
        sections.append(f"[物品列表]\n{props_hint}")
    sections.append(_tone_section(profile))
    sections.append(_setting_section(profile))
    sections.append(_scale_npc_hint(scale))
    sections.append(_pace_hint(pace))
    sections.append(_difficulty_npc_section(difficulty))
    sections.append(_playability_npc_section(playability))
    bs = _boundaries_section(boundaries)
    if bs:
        sections.append(bs)
    sections.append(NPC_GENERAL_RULES)
    sections.append(NPC_FIELD_SPEC)
    sections.append(NPC_OUTPUT_FORMAT)
    sections.append(NPC_SELF_CHECK)

    return "\n\n".join(s for s in sections if s)


def get_tone_list() -> List[str]:
    try:
        from world_prompt_builder import TONES
        return sorted(TONES.keys())
    except Exception:
        return sorted(NPC_TONE_RULES.keys())


def get_setting_list() -> List[str]:
    try:
        from world_prompt_builder import SETTINGS
        return sorted(SETTINGS.keys())
    except Exception:
        return sorted(NPC_SETTING_RULES.keys())


def get_tone_info(tone: str) -> Optional[Dict[str, Any]]:
    try:
        from world_prompt_builder import TONES
        return TONES.get(str(tone or "").strip().lower())
    except Exception:
        return None


def get_setting_info(setting: str) -> Optional[Dict[str, Any]]:
    try:
        from world_prompt_builder import SETTINGS
        return SETTINGS.get(str(setting or "").strip().lower())
    except Exception:
        return None
