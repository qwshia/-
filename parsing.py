"""纯解析层：玩家指令 → 结构化意图。零 LLM / 零 stream_bus / 零 AgentState 依赖，仅依赖 re。"""
import re


_ATTIRE_STRIP = " ，,。！？、()（）"
_ATTIRE_PUT_ON = [
    re.compile(r"(?:把|将)?\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})\s*(?:穿上|穿起来|穿戴|套上|披上|戴上)"),
    re.compile(r"(?:穿上|穿起来|穿戴|套上|披上|戴上)\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})"),
]
_ATTIRE_TAKE_OFF = [
    re.compile(r"(?:把|将)?\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})\s*(?:脱掉|脱下|拿下|卸下|除下)"),
    re.compile(r"(?:脱掉|脱下|拿下|卸下|除下)\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})"),
    re.compile(r"(?:脱光光|脱光|全脱|全部脱掉|脱个精光)"),
]

_PLAYER_ATTIRE_PAT = [
    re.compile(r"(?:我|玩家).{0,4}(?:把|将)?\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})\s*(?:脱掉|脱下|穿上|穿起)"),
]

_ITEM_REQ_PAT = [
    re.compile(r"(?:把|给|交|拿).{0,6}?([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})"),
]

_MOVE_PAT = [
    re.compile(r"(?:让|叫|请)?\s*@(?:子|npc_)?\d*\s*(?:去|前往|走到|来到|移动到)\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})"),
    re.compile(r"(?:去|前往|走到|来到|移动到)\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})"),
]


def _parse_attire_change(msg: str):
    # 脱光
    if re.search(r"脱光光|脱光|全脱|全部脱掉|脱个精光", msg):
        return "take_off", "全部"
    for pat in _ATTIRE_PUT_ON:
        m = pat.search(msg)
        if m:
            txt = m.group(1).strip(_ATTIRE_STRIP)
            if txt:
                return "put_on", txt
    for pat in _ATTIRE_TAKE_OFF:
        m = pat.search(msg)
        if m:
            try:
                txt = m.group(1).strip(_ATTIRE_STRIP)
            except IndexError:
                txt = "全部"
            if txt:
                return "take_off", txt
    return None


def _parse_player_attire(msg: str):
    for pat in _PLAYER_ATTIRE_PAT:
        m = pat.search(msg)
        if m:
            item = m.group(1).strip(_ATTIRE_STRIP)
            if item:
                act = "take_off" if re.search(r"脱", msg) else "put_on"
                return act, item
    return None


def _parse_item_request(msg: str, props: list, holder_name: str) -> str | None:
    # 优先：props 中该 NPC 持有的物品名直接出现在 msg 中
    for p in props:
        if p.get("holder") == holder_name and p.get("name") and p.get("name") in msg:
            return p.get("name")
    # 兜底：正则
    for pat in _ITEM_REQ_PAT:
        for m in pat.finditer(msg):
            name = m.group(1).strip(_ATTIRE_STRIP)
            if not name:
                continue
            for p in props:
                if p.get("name") == name and p.get("holder") == holder_name:
                    return name
            for p in props:
                if p.get("holder") == holder_name and p.get("name") and (name in p.get("name") or p.get("name") in name):
                    return p.get("name")
    return None


def _parse_npc_move(msg: str, avail: list) -> str | None:
    for pat in _MOVE_PAT:
        m = pat.search(msg)
        if m:
            cand = m.group(1).strip(_ATTIRE_STRIP)
            for s in avail:
                if cand == s or cand in s or s in cand:
                    return s
    return None


def _apply_attire(cur: str, action: str, item: str) -> str:
    item = item.strip(_ATTIRE_STRIP)
    if action == "take_off":
        if item in ("衣服", "衣", "全部", "所有", "全身", "全部衣服"):
            return ""
        parts = [s for s in re.split(r"[，,、;；]", cur) if item not in s]
        res = "，".join(parts).strip("，,、;； ")
        return res
    if item in cur:
        return cur
    return (cur + "，" + item) if cur else item


_NPC_CMD_PREFIX = re.compile(r"^\s*[!！]\s*@(?:子|npc_)?\d+\s*")


def _is_npc_command(msg: str) -> bool:
    """是否以 !令 @N 开头（NPC 指令的唯一显式入口）。"""
    return bool(_NPC_CMD_PREFIX.match(msg or ""))


def _strip_npc_cmd_prefix(msg: str) -> str:
    """剥掉 !令 @N 前缀，返回剩余内容（给 _parse_* 识别与 prompt 用）。"""
    return _NPC_CMD_PREFIX.sub("", msg or "", count=1).strip()

