"""物品位置/归属/可获取性判定（纯函数）。集中所有位置、归属、锁规则，供各节点调用，保证同步一致。

锁规则：locked + lock_mode(physical/bio/both/either) + key_name + bio_holder。
- physical 物理锁: 需 key_name（钥匙/工具）。
- bio 生物锁: 需 bio_holder NPC 本人验证（虹膜/指纹/声纹等身体特征即生物凭证）。
  生物凭证随身携带、不可偷窃、不可索要；bio_holder 必须在当前场景且同意帮忙才可打开。
- both 双重锁: key_name AND bio_holder 同时满足。NPC 开完生物部分后物品仍锁定（差物理钥匙），
  绝不出现"只靠生物部分就附带打开物理部分"的情况。
- either 二选一锁: key_name OR bio_holder 任一满足。
"""


def find_npc_scene(holder_name, npcs):
    if not holder_name or not npcs:
        return None
    for info in npcs.values():
        if info.get("name") == holder_name:
            return info.get("location")
    return None


def prop_location(p, npcs):
    if p.get("holder") in ("玩家", "玩家身上") or p.get("location") == "玩家身上":
        return "player"
    holder = p.get("holder")
    if p.get("carried") and holder and holder not in ("玩家", "玩家身上") and holder in (info.get("name") for info in (npcs or {}).values()):
        return f"npc:{holder}"
    return f"scene:{p.get('location', '')}"


def is_locked(p):
    return bool(p.get("locked"))


def lock_mode_of(p):
    return str(p.get("lock_mode") or "").strip()


def key_name_of(p):
    return str(p.get("key_name") or "").strip()


def bio_holder_of(p):
    return str(p.get("bio_holder") or "").strip()


def npc_in_scene(npc_name, npcs, current_scene):
    """bio_holder NPC 是否在当前场景（生物凭证=NPC 本人，在场即可验证）。"""
    if not npc_name or not npcs:
        return False
    for info in (npcs or {}).values():
        if info.get("name") == npc_name:
            return str(info.get("location") or "") == str(current_scene or "")
    return False


def bio_available(p, npcs, current_scene):
    """该物品的生物锁部分当前是否可被验证（bio_holder 在场）。"""
    bh = bio_holder_of(p)
    return npc_in_scene(bh, npcs, current_scene) if bh else False


def player_has_item(name, player_items, props):
    if not name:
        return False
    for it in (player_items or []):
        n = it.get("name") if isinstance(it, dict) else it
        if str(n or "").strip() == name:
            # 背包里锁着的物品不能算持有（锁着不算可用）
            if isinstance(it, dict) and it.get("locked"):
                continue
            return True
    for p in (props or []):
        if p.get("name") == name and p.get("holder") in ("玩家", "玩家身上"):
            if p.get("locked"):
                continue
            return True
    return False


def player_has_key(key, player_items, props):
    return player_has_item(key, player_items, props)


def unlock_missing(p, player_items, props, npcs=None, current_scene=""):
    """返回未满足的解锁条件列表（空=可解锁）。bio 条件=bio_holder NPC 在场。"""
    if not is_locked(p):
        return []
    mode = lock_mode_of(p)
    key_name = key_name_of(p)
    bio_holder = bio_holder_of(p)
    has_key = player_has_item(key_name, player_items, props) if key_name else False
    has_bio = npc_in_scene(bio_holder, npcs, current_scene) if bio_holder else False
    if mode == "physical":
        return [] if has_key else ([key_name] if key_name else ["钥匙"])
    if mode == "bio":
        return [] if has_bio else ([f"{bio_holder}本人验证"] if bio_holder else ["生物验证"])
    if mode == "both":
        miss = []
        if not has_key:
            miss.append(key_name or "钥匙")
        if not has_bio:
            miss.append(f"{bio_holder}本人验证" if bio_holder else "生物验证")
        return miss
    if mode == "either":
        if has_key or has_bio:
            return []
        return [f"{key_name} 或 {bio_holder}本人验证"]
    return []


def unlock_hint(p):
    """返回锁定物品的解锁提示文字（用于 desc 展示）。"""
    if not is_locked(p):
        return ""
    mode = lock_mode_of(p)
    key_name = key_name_of(p)
    bio_holder = bio_holder_of(p)
    if mode == "physical":
        return f"锁着，需要{key_name or '钥匙'}"
    if mode == "bio":
        return f"生物锁，需要{bio_holder or '某NPC'}本人验证"
    if mode == "both":
        return f"双重锁，需要{key_name or '钥匙'}和{bio_holder or '某NPC'}本人验证"
    if mode == "either":
        return f"锁着，需要{key_name or '钥匙'}或{bio_holder or '某NPC'}本人验证"
    return "锁着"


def open_bio_part(p, npc_name):
    """NPC 用自己的生物凭证（本人验证）打开物品的生物锁部分。
    返回新的锁字段 dict；NPC 不是 bio_holder 或物品未锁时返回 None。
    bio/either→完全解锁；both→生物部分完成、仍锁定（只差物理钥匙），绝不附带开物理锁。
    """
    mode = lock_mode_of(p)
    bh = bio_holder_of(p)
    if not is_locked(p) or not bh or bh != npc_name:
        return None
    if mode in ("bio", "either"):
        return {"locked": False, "lock_mode": "", "key_name": "", "bio_holder": ""}
    if mode == "both":
        return {"locked": True, "lock_mode": "physical", "key_name": key_name_of(p), "bio_holder": ""}
    return None


def can_acquire(p, npcs, player_items, props, current_scene):
    # 第一步：锁判定——拾取只认玩家自己的钥匙；bio 部分必须 NPC 帮忙（在场不算同意），故 bio 恒判缺
    missing = unlock_missing(p, player_items, props, None, "")
    if missing:
        return ("locked", "+".join(missing))
    # 第二步：位置判定
    loc = prop_location(p, npcs)
    if loc == "player":
        return ("free", "")
    if loc.startswith("npc:"):
        # NPC 随身物品前端不可被发现：调查时一律当作不存在，不泄露物品名与归属。
        # 玩家只能通过对话索要 / 偷窃指令主动获取，不能靠调查"看见"。
        return ("unknown", "")
    # 第三步：scene
    if loc.startswith("scene:"):
        scene_name = loc[6:]
        if scene_name != current_scene:
            return ("unknown", f"不在当前场景({scene_name})")
        holder = p.get("holder") or ""
        if holder and holder not in ("玩家", "玩家身上", ""):
            holder_scene = find_npc_scene(holder, npcs)
            if holder_scene == current_scene:
                return ("npc_present", holder)
        return ("free", "")
    # 第四步兜底
    return ("unknown", "")
