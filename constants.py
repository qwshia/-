"""全局常量表:白名单、枚举、数值范围等,供所有节点查询.

放数据的就放数据,做逻辑的就做逻辑.
不要把常量放在 utils.py 或 world.py 中(utils.py 负责通用工具函数,world.py 负责世界生成逻辑).
新增内容只需在此文件中添加,消费方自动生效.
"""

# ============================================================
# World 相关白名单
# ============================================================
VALID_LOCK_MODES = {"physical", "bio", "both", "either"}
VALID_DIFFICULTIES = {"easy", "normal", "hard"}
VALID_PLAYABILITIES = {"basic", "enhanced"}
VALID_SCALES = {"small", "medium", "large"}
VALID_PACES = {"slow", "medium", "fast"}

# 无钥匙词(world 专用,用于校验 key_name)
NO_KEY_WORDS = {"拉链", "直接拉开", "拉开", "无需钥匙", "无钥匙", "直接打开", "直接掀开"}

# ============================================================
# 跨模块共享白名单
# ============================================================
VALID_TIME_PHASES = ["清晨", "上午", "正午", "午后", "黄昏", "夜晚", "深夜", "凌晨"]

# ============================================================
# NPC 相关白名单
# ============================================================
VALID_MOODS = {"平静", "焦虑", "烦躁", "愉悦", "恐惧", "压抑"}
VALID_STABILITY = {"stable", "volatile", "fragile"}
VALID_STATUSES = {"alive", "absent", "dead", "captured"}
VALID_PHYSICAL_STATES = {"healthy", "injured", "exhausted", "sick"}
VALID_COGNITION_TYPES = {"fact", "secret", "belief", "forbidden"}
VALID_SERVES = {"self", "other", "faction", "group"}

# ============================================================
# 意图池白名单(供 arbiter_node / npc_node / intent_node 查询)
# ============================================================
VALID_INTENT_KINDS = {
    "idle", "move", "take", "give", "put_on", "take_off",
    "chat", "find", "follow", "trade", "guard", "flee",
}

# ============================================================
# 数值范围
# ============================================================
AFFECTION_MIN = -100
AFFECTION_MAX = 500
INITIAL_AFFECTION_MIN = -100
INITIAL_AFFECTION_MAX = 200
FLOAT_FIELD_MIN = 0.0
FLOAT_FIELD_MAX = 1.0

# ============================================================
# 社交网络相关白名单（供 social_network / social_node / social_genesis 查询）
# ============================================================
# NPC 间连接类型（一条边上可同时存在多种）
VALID_CONNECTION_TYPES = {
    "secret", "shared_goal", "common_enemy", "alliance", "rivalry",
    "debt", "grudge", "kinship", "info_channel", "witness",
    "consensus", "faction",
}
# 阵营间关系类型
VALID_FACTION_RELATION_TYPES = {"ally", "hostile", "neutral", "nested", "merged"}
# 舆论类型
VALID_RUMOR_TYPES = {"fact", "distorted", "fabricated", "archived"}
# 共识区域对外态度
VALID_CONSENSUS_VISIBILITY = {"public", "secret", "faction_only"}
# 社交更新指令操作类型
VALID_SOCIAL_UPDATE_OPS = {
    "interact", "reveal", "set", "connect", "disconnect", "consensus_add",
    "consensus_remove", "faction_join", "faction_leave", "rumor_create",
    "rumor_spread", "reputation_change", "debt_add", "grudge_add",
    "emotion_contagion", "status_change", "player_catalyst",
}
# 关系边数值属性名（0.0-1.0）
VALID_EDGE_ATTRS = {
    "familiarity", "trust", "tension", "affinity", "respect", "fear",
    "obligation", "resentment", "curiosity", "dependency",
}
# 关系边交互类型
VALID_INTERACTION_TYPES = {
    "dialog", "p2p", "bg_chat", "contest", "help", "betrayal",
    "witness", "introduction",
}