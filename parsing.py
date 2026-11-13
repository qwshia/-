"""纯解析层：玩家指令 → 结构化意图。零 LLM / 零 stream_bus / 零 AgentState 依赖，仅依赖 re。"""
import re


_ATTIRE_STRIP = " ，,。！？、()（）"
_ATTIRE_PUT_ON = [
    re.compile(r"(?:把|将)?\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})\s*(?:穿上|穿起来|穿戴|套上|披上|戴上)"),
    re.compile(r"(?:穿上|穿起来|穿戴|套上|披上|戴上)\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})"),
]
_ATTIRE_TAKE_OFF = [
    re.compile(r"(?:把|将)?\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})\s*(?:脱掉|脱下|拿下|卸下|除下|脱了)"),
    re.compile(r"(?:脱掉|脱下|拿下|卸下|除下|脱了)\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})"),
    re.compile(r"(?:脱光光|脱光|全脱|全部脱掉|脱个精光)"),
]

_ITEM_REQ_PAT = [
    re.compile(r"(?:把|给|交|拿).{0,6}?([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})"),
]

_MOVE_PAT = [
    re.compile(r"(?:让|叫|请)?\s*@(?:子|npc_)?\d*\s*(?:去|前往|走到|移动到)\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})"),
    re.compile(r"(?:去|前往|走到|移动到)\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})"),
]


_CONJUNCTIONS = ("和", "与", "跟", "及", "、")

def _parse_attire_change(msg: str):
    if any(c in (msg or "") for c in _CONJUNCTIONS):
        return None
    # 脱光
    if re.search(r"脱光光|脱光|全脱|全部脱掉|脱个精光", msg):
        return "take_off", "全部"
    if "全" in (msg or "") and re.search(r"脱", msg or ""):
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


def _parse_item_request(msg: str, props: list, holder_name: str) -> str | None:
    if any(c in (msg or "") for c in ("和", "与", "跟")):
        return None
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


def _parse_player_give(msg: str, player_items: list) -> str | None:
    """玩家把背包物品交给当前 NPC。协议：!@N 给 X（前端拖拽拼出）。
    也兼容自然语言「把 X 给你」「给你 X」「X 给你」。
    「给我」「给我 X」不算（那是 NPC→玩家的 give_item）。返回匹配到的背包物品名，未匹配返回 None。"""
    if not msg:
        return None
    items = [str(x) for x in (player_items or []) if x]
    if not items:
        return None
    text = msg.strip()
    has_gei_wo = bool(re.search(r"(?:给|交给|递给|送)\s*我", text))
    has_gei_npc = bool(re.search(r"(?:给|交给|递给|送)\s*(?:你|您|ta|TA|她|他)", text))
    if has_gei_wo and not has_gei_npc:
        return None

    cands = []
    m = re.match(r"^(?:给|交给|递给|送)\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})\s*$", text)
    if m:
        cands.append(m.group(1).strip(_ATTIRE_STRIP))
    m = re.search(r"(?:把|将)\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})\s*(?:给|交给|递给|送)\s*(?:你|您|ta|TA|她|他)", text)
    if m:
        cands.append(m.group(1).strip(_ATTIRE_STRIP))
    m = re.search(r"(?:给|交给|递给|送)\s*(?:你|您|ta|TA|她|他)\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})", text)
    if m:
        cands.append(m.group(1).strip(_ATTIRE_STRIP))
    m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})\s*(?:给|交给|递给|送)\s*(?:你|您|ta|TA|她|他)", text)
    if m:
        cands.append(m.group(1).strip(_ATTIRE_STRIP))

    for cand in cands:
        if not cand:
            continue
        if cand in items:
            return cand
        for it in items:
            if cand in it or it in cand:
                return it
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
def _parse_player_give(msg: str, player_items: list) -> str | None:
    """玩家把背包物品交给当前 NPC。协议：!@N 给 X（前端拖拽拼出）。
    兼容「把 X 给你」「给你 X」「X 给你」；「给我」「给我 X」不算（那是反向 give_item）。返回背包中匹配到的物品名；未匹配返回 None。"""
    if not msg:
        return None
    items = [str(x) for x in (player_items or []) if x]
    if not items:
        return None
    text = msg.strip()
    has_gei_wo = bool(re.search(r"(?:给|交给|递给|送)\s*我", text))
    has_gei_npc = bool(re.search(r"(?:给|交给|递给|送)\s*(?:你|您|ta|TA|她|他)", text))
    if has_gei_wo and not has_gei_npc:
        return None

    cands = []
    m = re.match(r"^(?:给|交给|递给|送)\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})\s*$", text)
    if m:
        cands.append(m.group(1).strip(_ATTIRE_STRIP))
    m = re.search(r"(?:把|将)\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})\s*(?:给|交给|递给|送)\s*(?:你|您|ta|TA|她|他)", text)
    if m:
        cands.append(m.group(1).strip(_ATTIRE_STRIP))
    m = re.search(r"(?:给|交给|递给|送)\s*(?:你|您|ta|TA|她|他)\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})", text)
    if m:
        cands.append(m.group(1).strip(_ATTIRE_STRIP))
    m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12})\s*(?:给|交给|递给|送)\s*(?:你|您|ta|TA|她|他)", text)
    if m:
        cands.append(m.group(1).strip(_ATTIRE_STRIP))

    for cand in cands:
        if not cand:
            continue
        if cand in items:
            return cand
        for it in items:
            if cand in it or it in cand:
                return it
    return None


_ATTIRE_PREFIXES = ("外罩", "内穿", "身着", "穿着", "足蹬", "头戴", "手持", "披着", "套着", "外披", "内衬")


def _strip_attire_affixes(s: str) -> str:
    s = s or ""
    for w in _ATTIRE_PREFIXES:
        s = s.replace(w, "")
    return s.strip(_ATTIRE_STRIP)


def _apply_attire(cur, action: str, item) -> dict:
    if not isinstance(cur, dict):
        if isinstance(cur, str):
            cur = {"全装": cur.strip()} if cur.strip() and cur.strip() not in ("", "无") else {}
        else:
            cur = {}
    cur = dict(cur)
    if isinstance(item, (list, tuple)):
        items = [str(x) for x in item]
    else:
        items = [item]

    if action == "take_off":
        for it in items:
            it_s = (it or "").strip() if isinstance(it, str) else str(it).strip()
            it_core = _strip_attire_affixes(it_s) or it_s
            if any(k in it_s for k in ("全部", "所有", "衣服")) or it_core in ("衣服", "衣", "全部", "所有", "全身", "全部衣服"):
                return {}
        for key in list(cur.keys()):
            val = str(cur.get(key, "") or "")
            val_core = _strip_attire_affixes(val) or val
            for it in items:
                it_s = (it or "").strip() if isinstance(it, str) else str(it).strip()
                it_core = _strip_attire_affixes(it_s) or it_s
                matched = False
                if it_s and (it_s in val or val in it_s or it_s in key or key in it_s
                             or it_core in val_core or val_core in it_core):
                    matched = True
                # 长句包含：value（或其核心词）出现在长句中
                if not matched:
                    # 滑动窗口：val_core 中任意长度>=2 的子串出现在 it_s 中
                    for L in range(min(len(val_core), 4), 1, -1):
                        for i in range(len(val_core) - L + 1):
                            sub = val_core[i:i+L]
                            if sub and sub in it_s:
                                matched = True
                                break
                        if matched:
                            break
                if matched:
                    del cur[key]
                    break
        return cur

    # put_on
    for it in items:
        it_s = (it or "").strip() if isinstance(it, str) else str(it).strip()
        if it_s and it_s not in [str(v) for v in cur.values()]:
            cur["外衣"] = it_s
    return cur


_NPC_CMD_PREFIX = re.compile(r"^\s*[!！]\s*@(?:子|npc_)?\d+\s*")


def _is_npc_command(msg: str) -> bool:
    """是否以 !令 @N 开头（NPC 指令的唯一显式入口）。"""
    return bool(_NPC_CMD_PREFIX.match(msg or ""))


def _strip_npc_cmd_prefix(msg: str) -> str:
    """剥掉 !令 @N 前缀，返回剩余内容（给 _parse_* 识别与 prompt 用）。"""
    return _NPC_CMD_PREFIX.sub("", msg or "", count=1).strip()
