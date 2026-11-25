"""世界提示词构建器：模块化拼装 world_genesis 的提示词。

按 style_profile 动态注入风格(tone)与类型(setting)插槽:
- tone: 主风格，决定基调感受 (atmosphere 为引导性疑问句)
- setting: 主类型，决定场景/人物/道具特色 (atmosphere 为引导性疑问句)
- playability: basic / enhanced (玩家交互/线索收集丰富度)
- difficulty: easy / normal / hard (推理链长度: 2 / 3-6 / 7+)
- scale: small / medium / large (NPC 1-3 / 3-8 / 8+, 场景 2-4 / 5-7 / 10+)
- pace: slow / medium / fast 节奏
- time_pressure: 是否启用时间系统
- boundaries: 玩家禁用项列表
- dynamic_events: 预留开关 (未实现)
"""
from constants import VALID_DIFFICULTIES, VALID_PLAYABILITIES, VALID_SCALES, VALID_PACES, VALID_TIME_PHASES, VALID_LOCK_MODES
from typing import Any, Dict, List, Optional


# ============================================================
# Tone（风格/氛围）：决定基调感受，atmosphere 为引导性疑问句
# ============================================================
TONES: Dict[str, Dict[str, Any]] = {
    "mystery": {"cn": "悬疑", "flavor": "真相拼图、谎言与矛盾点、证据链", "atmosphere": "这个剧本的场景、背景、真相应该营造什么感觉？", "time_pressure_default": True, "playability_default": "enhanced"},
    "cozy": {"cn": "温馨", "flavor": "人物关系、日常互动、轻量秘密、治愈氛围", "atmosphere": "这个剧本的场景、背景、真相应该营造什么感觉？", "time_pressure_default": False, "playability_default": "basic"},
    "horror": {"cn": "恐怖", "flavor": "时间压力、资源稀缺、NPC 逃走/异变、环境威胁", "atmosphere": "这个剧本的场景、背景、真相应该营造什么感觉？", "time_pressure_default": True, "playability_default": "enhanced"},
    "romance": {"cn": "浪漫", "flavor": "affection 权重、give/receive 交互、情感分支、秘密与误解", "atmosphere": "这个剧本的场景、背景、真相应该营造什么感觉？", "time_pressure_default": False, "playability_default": "enhanced"},
    "thriller": {"cn": "惊悚", "flavor": "紧迫感、追逐、背叛、信息不对称", "atmosphere": "这个剧本的场景、背景、真相应该营造什么感觉？", "time_pressure_default": True, "playability_default": "enhanced"},
    "comedy": {"cn": "喜剧", "flavor": "误会、巧合、轻量秘密、欢乐互动", "atmosphere": "这个剧本的场景、背景、真相应该营造什么感觉？", "time_pressure_default": False, "playability_default": "basic"},
    "tragedy": {"cn": "悲剧", "flavor": "命运、牺牲、不可逆选择、情感重量", "atmosphere": "这个剧本的场景、背景、真相应该营造什么感觉？", "time_pressure_default": True, "playability_default": "enhanced"},
    "noir": {"cn": "黑色", "flavor": "morally gray、城市阴影、背叛、利用", "atmosphere": "这个剧本的场景、背景、真相应该营造什么感觉？", "time_pressure_default": False, "playability_default": "enhanced"},
    "psychological": {"cn": "心理", "flavor": "内心冲突、不可靠叙述、记忆、创伤", "atmosphere": "这个剧本的场景、背景、真相应该营造什么感觉？", "time_pressure_default": False, "playability_default": "enhanced"},
    "adventure": {"cn": "冒险", "flavor": "探索、解谜、NPC 合作、多路线推进", "atmosphere": "这个剧本的场景、背景、真相应该营造什么感觉？", "time_pressure_default": False, "playability_default": "enhanced"},
    "supernatural": {"cn": "超自然", "flavor": "灵异、诅咒、超自然现象、禁忌知识", "atmosphere": "这个剧本的场景、背景、真相应该营造什么感觉？", "time_pressure_default": False, "playability_default": "enhanced"},
    "survival": {"cn": "生存", "flavor": "资源稀缺、环境危害、合作/背叛、逃生", "atmosphere": "这个剧本的场景、背景、真相应该营造什么感觉？", "time_pressure_default": True, "playability_default": "enhanced"},
}


# ============================================================
# Setting（类型/场景）：决定场景/人物/道具特色，atmosphere 为引导性疑问句
# ============================================================
SETTINGS: Dict[str, Dict[str, Any]] = {
    "school": {"cn": "校园", "flavor": "同学关系、成长、秘密、青春期冲突", "atmosphere": "这个剧本的场景、人物、道具应该体现什么样的校园特色？", "time_pressure_default": False, "playability_default": "enhanced"},
    "workplace": {"cn": "职场", "flavor": "利益、竞争、隐瞒、职业伦理", "atmosphere": "这个剧本的场景、人物、道具应该体现什么样的职场特色？", "time_pressure_default": False, "playability_default": "enhanced"},
    "medical": {"cn": "医疗", "flavor": "病例、药物、伦理、隐瞒病情", "atmosphere": "这个剧本的场景、人物、道具应该体现什么样的医疗特色？", "time_pressure_default": True, "playability_default": "enhanced"},
    "legal": {"cn": "法律", "flavor": "证据、证词、程序、真相与法律的张力", "atmosphere": "这个剧本的场景、人物、道具应该体现什么样的法律特色？", "time_pressure_default": True, "playability_default": "enhanced"},
    "historical": {"cn": "历史", "flavor": "时代背景、礼教、权力斗争、信物", "atmosphere": "这个剧本的场景、人物、道具应该体现什么样的历史特色？", "time_pressure_default": False, "playability_default": "enhanced"},
    "fantasy": {"cn": "奇幻", "flavor": "魔法道具、诅咒/预言、NPC 阵营、传说物品", "atmosphere": "这个剧本的场景、人物、道具应该体现什么样的奇幻特色？", "time_pressure_default": False, "playability_default": "enhanced"},
    "sci_fi": {"cn": "科幻", "flavor": "科技道具、生物锁、未来场景、AI/NPC 秘密", "atmosphere": "这个剧本的场景、人物、道具应该体现什么样的科幻特色？", "time_pressure_default": False, "playability_default": "enhanced"},
    "cyberpunk": {"cn": "赛博", "flavor": "科技、公司阴谋、义体、数据泄露、地下交易", "atmosphere": "这个剧本的场景、人物、道具应该体现什么样的赛博特色？", "time_pressure_default": True, "playability_default": "enhanced"},
    "family": {"cn": "家庭", "flavor": "血缘、误解、牺牲、家族秘密", "atmosphere": "这个剧本的场景、人物、道具应该体现什么样的家庭特色？", "time_pressure_default": False, "playability_default": "enhanced"},
    "western": {"cn": "西部", "flavor": "荒野、枪支、赏金、背叛、正义", "atmosphere": "这个剧本的场景、人物、道具应该体现什么样的西部特色？", "time_pressure_default": False, "playability_default": "enhanced"},
    "post_apocalyptic": {"cn": "末日", "flavor": "资源稀缺、生存、部落、危险区域", "atmosphere": "这个剧本的场景、人物、道具应该体现什么样的末日特色？", "time_pressure_default": True, "playability_default": "enhanced"},
    "wuxia": {"cn": "武侠", "flavor": "江湖、门派、武功秘籍、恩怨情仇", "atmosphere": "这个剧本的场景、人物、道具应该体现什么样的武侠特色？", "time_pressure_default": False, "playability_default": "enhanced"},
    "xianxia": {"cn": "仙侠", "flavor": "修仙、法宝、天劫、因果、境界", "atmosphere": "这个剧本的场景、人物、道具应该体现什么样的仙侠特色？", "time_pressure_default": False, "playability_default": "enhanced"},
    "military": {"cn": "军事", "flavor": "战场、指挥、军衔、情报", "atmosphere": "这个剧本的场景、人物、道具应该体现什么样的军事特色？", "time_pressure_default": False, "playability_default": "enhanced"},
    "sports": {"cn": "体育", "flavor": "比赛、训练、团队、竞争", "atmosphere": "这个剧本的场景、人物、道具应该体现什么样的体育特色？", "time_pressure_default": False, "playability_default": "enhanced"},
    "detective": {"cn": "侦探", "flavor": "案件、证物、证词、调查", "atmosphere": "这个剧本的场景、人物、道具应该体现什么样的侦探特色？", "time_pressure_default": True, "playability_default": "enhanced"},
    "espionage": {"cn": "谍战", "flavor": "身份隐藏、情报、双面间谍、信号", "atmosphere": "这个剧本的场景、人物、道具应该体现什么样的谍战特色？", "time_pressure_default": True, "playability_default": "enhanced"},
}


# ============================================================
# 难度规则（推理链长度，好坏人数量由模型动态划分）
# ============================================================
DIFFICULTY_RULES: Dict[str, List[str]] = {
    "easy": [
        "[难度-easy] 推理链 2 步: 找到线索 A, 从 A 推出真相.",
        "[难度-easy] 好人偏多, 坏人/嫌疑人偏少 (具体人数由规模决定).",
        "[难度-easy] 线索 desc 直接可读, 无需多步推理.",
        "[难度-easy] 红鲱鱼 0-1 个.",
    ],
    "normal": [
        "[难度-normal] 推理链 3-6 步: 2-3 个线索需比对/出示.",
        "[难度-normal] 好人与坏人/嫌疑人各半.",
        "[难度-normal] 线索需 1-2 步推理.",
        "[难度-normal] 红鲱鱼 1-2 个, 部分与 truth 相关.",
    ],
    "hard": [
        "[难度-hard] 推理链 7+ 步: 3+ 个线索需比对/出示/交叉验证.",
        "[难度-hard] 坏人/嫌疑人偏多, 好人偏少.",
        "[难度-hard] 线索需 2-3 步推理, 部分线索需 NPC 话语真伪判断.",
        "[难度-hard] 红鲱鱼 2-3 个, 且与 truth 相关 (增加推理难度).",
    ],
}


# ============================================================
# 可玩性档位规则（玩家交互/线索收集丰富度）
# ============================================================
PLAYABILITY_RULES: Dict[str, List[str]] = {
    "basic": [
        "[可玩性-basic] 通用硬规则 (防死锁、唯一性、时间系统).",
        "[可玩性-basic] truth_leads 每条 fact 必须有 via.",
        "[可玩性-basic] endings 至少 1 个.",
    ],
    "enhanced": [
        "[可玩性-enhanced] truth_leads 每个核心事实建议至少 2 条独立 via.",
        "[可玩性-enhanced] 建议至少 1 个 bio 或 both 锁, bio_holder 初始不在该门禁内.",
        "[可玩性-enhanced] 建议至少 1 个 either 锁, 给玩家二选一.",
        "[可玩性-enhanced] 建议至少 1 个 NPC 持有与 global_truth 相关的线索物 (carried=true).",
        "[可玩性-enhanced] 建议至少 1 个交互道具 (撬锁器/镇静剂), desc 写明用途.",
        "[可玩性-enhanced] 建议至少 1 条 truth_lead 依赖 NPC 在特定 phase 的 location 或 timed_event.",
        "[可玩性-enhanced] endings 建议至少 3 个: 真相达成 / 部分达成 / 失败.",
        "[可玩性-enhanced] initial_items 建议至少 1 个能打开某锁或推进某 truth_lead.",
    ],
}


# ============================================================
# 通用总原则
# ============================================================
GENERAL_PRINCIPLES = """[总原则]
1. 解析玩家要求中的题材/关系/氛围/节奏/边界/禁用项; 风格适配题材, 不套固定模板.
2. 可推演性优先: global_truth 每个可获取的关键事实必须在 truth_leads 有玩家可执行的获取路径. 部分事实永远不可知 (证据销毁/死人带秘密/时间流逝), truth_leads 不必覆盖全部.
3. 逻辑自洽: 人物动机/时间/位置/物品归属/获取链必须自洽; NPC 不得说出未登记事实.
4. 规模由玩家要求 + 题材复杂度 + 推理链长度决定.
5. 唯一性: 物品只能登记在 scenes.items/props/initial_items 三者之一; 场景名/物品名/NPC 名全局唯一.
6. 真实感: 世界应有不可知信息、冗余细节、环境一致性、允许无剧情日常、允许摆烂."""


# ============================================================
# 通用硬规则
# ============================================================
GENERAL_HARD_RULES = f"""[硬规则]
1. truth_leads 每条 via 必须写成玩家可执行路径: 地点+对象(已登记)+动作(调查/打开/询问/出示/比对)+玩家可读到/听到/取得什么.
2. 每个场景物品的 desc 必须包含空间定位信息; 被锁时 locked:true 并写 key_name.
3. locked:false 表示可直接调查/取得, 不得写 key_name. locked:true 必须写 key_name, 钥匙必须在门禁外有明确获取途径.
4. 影响胜负/关键分支的机制, 必须在物品desc/NPC对话条件中有玩家可接触的落点.
5. ending_goal 中凡需做某件事, 场景或物品中必须有对应可操作落点.
6. 对话线索必须写清: NPC+场景+触发条件+玩家出示或询问什么+NPC 会透露什么.
7. 适当设伪线索链,根据难度选择. 伪线索或者可疑细节不用必须与 truth_leads 相关.
10. NPC 有关的地点必须是 scenes[].name 中已登记的场景名.
11. 门禁的 key_name 必须是实际存在的物品, bio_holder 必须是实际存在的 NPC. 禁止"钥匙被锁在门禁内部".
12. 如果某场景 locked=true, 持有其解锁凭证的 NPC 初始 location 不能是该场景.
13. 同一物品不能在 props 和 scenes.items 中同时出现.
14. time_start/time_limit 只能取: {"/".join(VALID_TIME_PHASES)}.
15. 适当增加与主线无关的日常物品 (如手帕、茶杯、旧报纸等), 用于混淆视听, 增加调查难度. 这些物品 desc 中不应包含真相相关线索.
16. 场景 desc 应包含"冗余细节"(墙上涂鸦、食物、工具等不服务解谜的物品), 填充生活感. 单场景不要增加太多的无关物品.
17. 环境应与 NPC 性格/秘密对应, 风格统一, 不强行放违和物品埋线索.
18. 允许"无线索"的日常场景, 不是每个场景都要埋伏笔.
19. endings 应包含至少 1 个坏结局/无赢家结局/没答案的结局 (如果剧本需要多结局)."""


# ============================================================
# 字段说明
# ============================================================
FIELD_SPEC = """[字段说明]
backstory: 200-300字开场白, 交代玩家身份/处境/为何在此, 结尾给1-2句开局可尝试的方向. 应允许玩家什么主线都不做, 世界照常运转.
ending_goal: 玩家看到的目标. 只写要达成什么, 不写判定方法, 不泄露答案.
endings: 数组[{"id":"","name":"","desc":"","condition":""}]. name结局名, desc结局画面(80字内), condition达成条件. 应包含坏结局/无赢家/没答案的结局 (如果剧本需要多结局).
global_truth: 玩家需推理出的完整事实, 后台判定用, 不向玩家明示. 可以包含永远不可知的事实 (证据销毁/死人带秘密/时间流逝).
truth_leads: 数组[{"fact":"确定事实(禁可能/也许/疑似)","via":"玩家可执行获取路径"}]. 不必覆盖global_truth的全部事实, 部分事实永远不可知.
initial_scene: 开局场景名, 必须在scenes[].name中.
initial_items: 数组[{"name":"","desc":""}]. 只放玩家开局已持有或可立即使用的物品. 不得设为locked, 不得为容器.
scenes: 数组[{"name":"","desc":"","locked":false,"lock_mode":"","key_name":"","bio_holder":"","items":[]}].
  desc写环境细节. locked=true时用lock_mode/key_name/bio_holder描述解锁方式.
  desc应包含"冗余细节"(墙上涂鸦、食物、工具等不服务解谜的物品), 填充生活感. 
  items: 数组[{"name":"","desc":"","locked":false,"lock_mode":"","key_name":"","bio_holder":""}].
    只生成能被玩家拾取的实体小物品(信件、钥匙、药品、手机、书、衣服等). 严禁家具/冰箱/箱子等大型物体.
    平铺生成, 严禁content嵌套. 物品的额外信息写入desc. desc必须包含空间位置描述.
    locked=true时lock_mode指定解锁方式: physical=物理锁(key_name填钥匙/工具名); bio=生物锁(bio_holder填NPC名); both=双重锁; either=二选一锁.
    钥匙/工具类物品自身不得设locked.
    万能道具(可0个): 撬锁工具(撬锁器/万能钥匙/门禁卡, 可撬physical锁), 安抚药物(安眠药/镇静剂, 可平息NPC情绪). desc写明"可用于撬锁/安抚".
npcs: 数组[{"id":"npc_1","name":"","role":"","location":""}]. id严格npc_1/npc_2连续. name全局唯一. role直指主线职能. location必须是scenes[].name. NPC必须是活人.
props: 数组[{"name":"","holder":"","location":"","carried":true,"desc":"","locked":false,"lock_mode":"","key_name":"","bio_holder":""}].
  holder必须是npcs[].name之一(无主留空). location等于holder的location.
  desc写明物品状态. 可读物品追加原文.
  生物凭证=NPC本人(虹膜/指纹/声纹), 不生成凭证物品. bio锁的bio_holder填写需要在场验证的NPC名(表示该NPC本人必须在场并同意帮忙).
time_start/time_limit/time_turns: 有时限才填, 否则留空. 时段只能取__TIME_PHASES__.
total_rounds: 整数, 剧本体量估算的总对话轮数. 由LLM根据规模自行决定."""
FIELD_SPEC = FIELD_SPEC.replace("__TIME_PHASES__", "/".join(VALID_TIME_PHASES))


# ============================================================
# 输出格式
# ============================================================
OUTPUT_FORMAT = """[输出格式-严格JSON]
{
  "backstory": "",
  "ending_goal": "",
  "global_truth": "",
  "truth_leads": [{"fact": "", "via": ""}],
  "initial_scene": "",
  "initial_items": [{"name": "", "desc": ""}],
  "endings": [{"id": "ending_1", "name": "", "desc": "", "condition": ""}],
  "scenes": [{"name": "", "desc": "", "locked": false, "lock_mode": "", "key_name": "", "bio_holder": "", "items": [{"name": "", "desc": "", "locked": false, "lock_mode": "", "key_name": "", "bio_holder": ""}]}],
  "props": [{"name": "", "holder": "", "location": "", "carried": true, "desc": "", "locked": false, "lock_mode": "", "key_name": "", "bio_holder": ""}],
  "time_start": "",
  "time_limit": "",
  "time_turns": 0,
  "total_rounds": 30,
  "npcs": [{"id": "npc_1", "name": "", "role": "", "location": ""}]
}"""


# ============================================================
# 内部自检
# ============================================================
SELF_CHECK = """[输出前内部自检(不输出)]
逐条truth_leads问"玩家在哪个场景, 点哪个已登记对象, 读到/取得什么, 推出什么"; 答不出就重写. 每个ending_goal动作是否有可操作落点."""


# ============================================================
# 辅助函数
# ============================================================
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


def _get_profile(style_profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    sp = style_profile if isinstance(style_profile, dict) else {}
    # 向后兼容: primary_genre -> tone
    tone = str(sp.get("tone", "") or sp.get("primary_genre", "") or "").strip().lower()
    if tone not in TONES:
        tone = ""
    setting = str(sp.get("setting", "") or "").strip().lower()
    if setting not in SETTINGS:
        setting = ""
    scale = str(sp.get("scale", "") or "").strip().lower()
    if scale not in VALID_SCALES:
        scale = "medium"
    pace = str(sp.get("pace", "") or "").strip().lower()
    if pace not in VALID_PACES:
        pace = "medium"
    playability = str(sp.get("playability", "") or "").strip().lower()
    if playability not in VALID_PLAYABILITIES:
        playability = ""
    difficulty = str(sp.get("difficulty", "") or "").strip().lower()
    if difficulty not in VALID_DIFFICULTIES:
        difficulty = "normal"
    time_pressure = sp.get("time_pressure", None)
    if not isinstance(time_pressure, bool):
        time_pressure = None
    boundaries = sp.get("boundaries", [])
    if not isinstance(boundaries, list):
        boundaries = [str(boundaries)]
    boundaries = [str(b).strip() for b in boundaries if str(b).strip()]
    dynamic_events = bool(sp.get("dynamic_events", False))
    return {
        "tone": tone,
        "setting": setting,
        "scale": scale,
        "pace": pace,
        "playability": playability,
        "difficulty": difficulty,
        "time_pressure": time_pressure,
        "boundaries": boundaries,
        "dynamic_events": dynamic_events,
    }


def _scale_hint(scale: str) -> str:
    if scale == "small":
        return "规模: small (NPC 1-3人, 场景 2-4个)"
    if scale == "large":
        return "规模: large (NPC 8+人, 场景 10+个)"
    return "规模: medium (NPC 3-8人, 场景 5-7个)"


def _pace_hint(pace: str) -> str:
    if pace == "slow":
        return "节奏: slow (慢节奏, 重氛围)"
    if pace == "fast":
        return "节奏: fast (快节奏, 重推进)"
    return "节奏: medium (平衡)"


def _boundaries_section(boundaries: List[str]) -> str:
    if not boundaries:
        return ""
    lines = ["[玩家边界/禁用项 (必须遵守)]"]
    for b in boundaries:
        lines.append(f"  - {b}")
    return "\n".join(lines)


def _style_section(profile: Dict[str, Any]) -> str:
    tone = profile.get("tone", "")
    setting = profile.get("setting", "")
    parts = []
    if tone and tone in TONES:
        g = TONES[tone]
        parts.append(f"[主风格: {g['cn']}]")
        parts.append(f"flavor: {g['flavor']}")
        parts.append(f"atmosphere: {g['atmosphere']}")
    if setting and setting in SETTINGS:
        g = SETTINGS[setting]
        parts.append(f"[主类型: {g['cn']}]")
        parts.append(f"flavor: {g['flavor']}")
        parts.append(f"atmosphere: {g['atmosphere']}")
    if not parts:
        parts.append("[风格/类型: 未指定, 由导演根据玩家要求自行适配]")
    return "\n".join(parts)


def build_world_prompt(task: str, rag_reference: str, style_profile: Optional[Dict[str, Any]] = None) -> str:
    profile = _get_profile(style_profile)
    tone = profile["tone"]
    setting = profile["setting"]

    # time_pressure 默认值
    time_pressure = profile["time_pressure"]
    if time_pressure is None and tone in TONES:
        time_pressure = TONES[tone]["time_pressure_default"]
    if time_pressure is None and setting in SETTINGS:
        time_pressure = SETTINGS[setting]["time_pressure_default"]
    if time_pressure is None:
        time_pressure = False

    # playability 默认值
    playability = profile["playability"]
    if not playability and tone in TONES:
        playability = TONES[tone]["playability_default"]
    if not playability and setting in SETTINGS:
        playability = SETTINGS[setting]["playability_default"]
    if not playability:
        playability = "basic"

    difficulty = profile["difficulty"]

    # time_pressure 提示
    if time_pressure:
        time_hint = "[时间系统] 启用: 必须填写 time_start/time_limit/time_turns."
    else:
        time_hint = "[时间系统] 不限时: time_limit 与 time_turns 留空/0, time_start 默认清晨."

    sections = []
    sections.append("你是互动叙事导演. 按玩家要求直接生成剧本世界.")
    sections.append("[铁律] 只输出纯净JSON, 不要思考过程/开场白/解释/markdown代码块标记. JSON必须合法: 双引号、无尾逗号、无注释.")
    sections.append(f"[玩家要求] {task}")
    if rag_reference:
        sections.append(rag_reference.strip())
    sections.append(GENERAL_PRINCIPLES)
    sections.append(_style_section(profile))
    sections.append(_scale_hint(profile["scale"]))
    sections.append(_pace_hint(profile["pace"]))
    sections.append(time_hint)
    bs = _boundaries_section(profile["boundaries"])
    if bs:
        sections.append(bs)
    sections.append(FIELD_SPEC)
    sections.append(GENERAL_HARD_RULES)
    # 难度
    diff_rules = DIFFICULTY_RULES.get(difficulty, DIFFICULTY_RULES["normal"])
    sections.append("[难度: " + difficulty + "]\n" + "\n".join(diff_rules))
    # 可玩性
    pl_rules = PLAYABILITY_RULES.get(playability, PLAYABILITY_RULES["basic"])
    sections.append("[可玩性: " + playability + "]\n" + "\n".join(pl_rules))
    sections.append(OUTPUT_FORMAT)
    sections.append(SELF_CHECK)

    return "\n\n".join(s for s in sections if s)


def get_tone_list() -> List[str]:
    return sorted(TONES.keys())


def get_setting_list() -> List[str]:
    return sorted(SETTINGS.keys())


def get_tone_info(tone: str) -> Optional[Dict[str, Any]]:
    return TONES.get(str(tone or "").strip().lower())


def get_setting_info(setting: str) -> Optional[Dict[str, Any]]:
    return SETTINGS.get(str(setting or "").strip().lower())
