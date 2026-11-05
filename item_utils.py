"""物品位置/归属/可获取性判定（纯函数）。乙方案：props/scenes/player_items 三处并存，集中所有位置与归属规则，供各节点调用，保证同步一致。"""
import re


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


def key_name_of(p):
    return str(p.get("key_name") or "").strip()


def split_keys(key_name):
    if not key_name:
        return []
    return [k.strip() for k in re.split(r"[+＋]", key_name) if k.strip()]


def player_has_key(key, player_items, props):
    if not key:
        return False
    if key in player_items:
        return True
    for p in (props or []):
        if p.get("name") == key and p.get("holder") in ("玩家", "玩家身上"):
            return True
    return False


def npc_carries_key(key, npc_name, props):
    if not key or not npc_name:
        return False
    for p in (props or []):
        if p.get("name") == key and p.get("holder") == npc_name and p.get("carried"):
            return True
    return False


def key_owner_npc(key, props):
    if not key:
        return None
    for p in (props or []):
        if (p.get("name") == key and p.get("carried")
                and p.get("holder") and p.get("holder") not in ("玩家", "玩家身上")):
            return p.get("holder")
    return None


def can_acquire(p, npcs, player_items, props, current_scene):
    # 第一步：锁判定
    if is_locked(p):
        keys = split_keys(key_name_of(p))
        missing = [k for k in keys if not player_has_key(k, player_items, props)]
        if missing:
            return ("locked", "+".join(missing))
    # 第二步：位置判定
    loc = prop_location(p, npcs)
    if loc == "player":
        return ("free", "")
    if loc.startswith("npc:"):
        holder = loc[4:]
        holder_scene = find_npc_scene(holder, npcs)
        if holder_scene == current_scene:
            return ("npc_present", holder)
        else:
            return ("free", "")
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
