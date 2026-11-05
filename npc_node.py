"""通用 NPC 执行器：npc_worker。"""
import re

from langchain_core.messages import HumanMessage

from config import actor_llm
from state import AgentState
from stream_bus import inc_registered, push_chunk, push_end, push_start
from parsing import _parse_attire_change, _parse_player_attire, _parse_item_request, _parse_npc_move, _apply_attire, _is_npc_command, _strip_npc_cmd_prefix
from memory import _compress, _compress_two
from item_utils import key_owner_npc, split_keys, player_has_key, prop_location
from utils import _parse_content


def _safe_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def _cut_at_marks(s: str) -> str:
    """返回 s 中第一个 [aff:/【aff:/[do:/【do: 标记之前的部分（流式推送用，避免把标记推给播放器）。"""
    cut = len(s)
    for mk in ("[aff:", "【aff:", "[do:", "【do:"):
        i = s.find(mk)
        if i != -1 and i < cut:
            cut = i
    return s[:cut]


async def _npc_respond(prompt: str, npc_id: str) -> tuple[str, bool, int, bool]:
    inc_registered()
    await push_start(npc_id)
    full = ""
    pushed = 0
    try:
        async for chunk in actor_llm.astream([HumanMessage(content=prompt)]):
            t = chunk.content if isinstance(chunk.content, str) else ""
            if t:
                full += t
                safe = full
                safe = _cut_at_marks(safe)
                if len(safe) > pushed:
                    await push_chunk(npc_id, safe[pushed:])
                    pushed = len(safe)
        await push_end(npc_id)
    except Exception:
        await push_end(npc_id)
        return "（网络异常，没有说话。）", False, 0, False
    m = re.search(r"[\[【]\s*aff\s*[:：]\s*([+-]?\d+)\s*[\]】]", full)
    aff_change = int(m.group(1)) if m else 0
    aff_change = max(-40, min(40, aff_change))
    m_do = re.search(r"[\[【]\s*do\s*[:：]\s*([yn])\s*[\]】]", full, re.I)
    consent = bool(m_do) and m_do.group(1).lower() == "y"
    content = re.sub(r"[\[【]\s*(?:aff\s*[:：]\s*[+-]?\d+|do\s*[:：]\s*[yn])\s*[\]】]", "", full, flags=re.I).strip()
    content = re.sub(r"[\[【][^\]】]*$", "", content).strip()
    content = re.sub(r"[\[【]\s*$", "", content).strip()
    if not content:
        content = "（没有说话。）"
    return content, True, aff_change, consent


# NPC 执行器：生成回复、更新好感/衣着/位置/记忆
async def npc_worker(state: AgentState) -> dict:
    npc_id = state.get("npc_id", "")
    npcs = state.get("npcs") or {}
    info = npcs.get(npc_id) or {}
    scene = state.get("current_scene", "未知")
    if info.get("location") != scene:
        return {}

    user_msg = state.get("user_msg", "")
    is_cmd = _is_npc_command(user_msg)
    if is_cmd:
        user_msg = _strip_npc_cmd_prefix(user_msg)
    player_items = list(state.get("player_items") or [])
    name = info.get("name", npc_id)
    role = info.get("role", "路人")
    personality = info.get("personality", "普通")
    appearance = info.get("appearance", "普通")
    attire = info.get("attire", "常服")
    goal = info.get("goal", "")
    secrets = [str(s) for s in (info.get("secrets") or [])]
    forbidden = [str(f) for f in (info.get("forbidden") or [])]
    hallucinations = [str(h) for h in (info.get("hallucinations") or [])]
    aff = _safe_int(info.get("affection", 100), 100)

    mem = info.get("memory") or {"l1": [], "l2": None, "l3": None}
    if not isinstance(mem, dict):
        mem = {"l1": [], "l2": None, "l3": None}
    l1 = list(mem.get("l1") or [])
    l2 = mem.get("l2")
    l3 = mem.get("l3")

    long_summary = l3 or "（无）"
    recent_summary = l2 or "（无）"
    recent_dialogue = "\n".join(l1) if l1 else "（无）"

    props = state.get("props") or []
    private_items = [p.get("name") for p in props if p.get("holder") == name and p.get("name")]
    private_txt = "、".join(private_items) if private_items else "（无）"
    secrets_txt = "\n".join(f"- {s}" for s in secrets) or "（无）"
    forbidden_txt = "\n".join(f"- {f}" for f in forbidden) or "（无）"
    hallucinations_txt = "\n".join(f"- {h}" for h in hallucinations) or "（无）"

    player_name = state.get("player_name", "")
    player_attire = state.get("player_attire", "普通衣着")
    player_location = scene
    npc_location = info.get("location", scene)
    events = state.get("world_events") or []
    recent_events = "；".join(events[-3:]) if events else "（无）"

    player_attire_change = _parse_player_attire(user_msg)
    if player_attire_change:
        player_attire = _apply_attire(player_attire, player_attire_change[0], player_attire_change[1])
    attire_change = _parse_attire_change(user_msg)

    available_scenes = state.get("available_scenes") or []
    move_target = _parse_npc_move(user_msg, available_scenes) if is_cmd else None
    move_blocked = False
    if move_target:
        rk = str((state.get("scenes") or {}).get(move_target, {}).get("requires_key", "") or "").strip()
        if rk:
            need = [k.strip() for k in rk.split("+") if k.strip()]
            if move_target in (state.get("opened_scenes") or []):
                need = []
            player_items = state.get("player_items") or []
            missing = [k for k in need if k not in player_items]
            if missing:
                npc_has = all(any(p.get("name") == k and p.get("holder") == name for p in props) for k in missing)
                if not npc_has:
                    move_blocked = True
                    move_target = None

    props = state.get("props") or []
    scenes = state.get("scenes") or {}

    requested_item = _parse_item_request(user_msg, props, name)

    available_scenes = state.get("available_scenes") or []
    all_npcs = state.get("npcs") or {}
    items = state.get("player_items") or []
    player_items = list(items)
    desc_map = state.get("item_desc_map") or {}
    _snap_items = state.get("npc_private_items", {}).get(npc_id, [])
    lookup_lines = []
    if any(w in user_msg for w in ("衣服", "穿着", "穿", "脱", "外套")):
        lookup_lines.append(f"玩家问到了你的穿着，你当前的真实衣着是：{attire}。")
    if move_target:
        sdef = scenes.get(move_target, {})
        lookup_lines.append(f"场景「{move_target}」：{sdef.get('desc', '')}")
    for nid2, ninfo2 in all_npcs.items():
        nname2 = ninfo2.get("name", "")
        if nname2 and nname2 in user_msg and nid2 != npc_id:
            _nloc = ninfo2.get('location','')
            _npos = "在当前场景" if _nloc == scene else f"在{_nloc}"
            lookup_lines.append(f"NPC「{nname2}」：身份{ninfo2.get('role','')}，外貌{ninfo2.get('appearance','')}，性格{ninfo2.get('personality','')}，衣着{ninfo2.get('attire','')}，{_npos}")
    if not is_cmd and len(all_npcs) > 1:
        _mentioned_other = any(info.get("name") in user_msg for nid, info in all_npcs.items() if nid != npc_id)
        if not _mentioned_other:
            _names_hint = "、".join(f"{info.get('name')}" for nid, info in all_npcs.items())
            lookup_lines.append(f"【提示：如果玩家提到了某人，可能是名单中的：{_names_hint}】")
    _ask_item = any(w in user_msg for w in ("身上有什么", "带了什么", "拿着什么", "身上有", "随身带"))
    if not is_cmd and _ask_item:
        for _p in props:
            if _p.get("name") and _p.get("holder"):
                if _p.get("holder") == name or _p.get("holder") in user_msg:
                    if _p.get("carried"):
                        _pos = f"在{_p.get('holder')}身上"
                    else:
                        _pos = f"在{_p.get('location', '某处')}"
                    lookup_lines.append(f"物品「{_p.get('name')}」：{_p.get('desc','')}，{_pos}")
    for nm in set(items) | {p.get("name") for p in props if p.get("name")} | set(_snap_items):
        if nm and nm in user_msg:
            if nm in _snap_items:
                still_has = any(p.get("name") == nm and p.get("holder") == name for p in props)
                if still_has:
                    lookup_lines.append(f"物品「{nm}」：你的私有物品，仍在你手中")
                else:
                    lookup_lines.append(f"物品「{nm}」：你的私有物品，但已不在你手中（可能被取走）")
                continue
            pp = next((p for p in props if p.get("name") == nm), None)
            if pp:
                _loc = prop_location(pp, all_npcs)
                if _loc == "player":
                    _pos = "在你背包"
                elif _loc.startswith("npc:"):
                    _pos = f"在{_loc[4:]}身上"
                elif _loc.startswith("scene:"):
                    _pos = f"在{_loc[6:]}"
                else:
                    _pos = _loc
                _holder = pp.get("holder", "")
                if _holder and _holder not in (name, "玩家", "玩家身上", ""):
                    lookup_lines.append(f"物品「{nm}」：这是{_holder}的东西，{_pos}，具体用途不详")
                else:
                    lookup_lines.append(f"物品「{nm}」：{pp.get('desc','')}，持有者{_holder}，{_pos}")
                _parsed_c = _parse_content(pp.get("content"))
                if _parsed_c:
                    _sub = "；".join(f"{c['name']}" + (f"：{c['text']}" if c.get("text") else "") for c in _parsed_c)
                    lookup_lines.append(f"（内含：{_sub}）")
            else:
                lookup_lines.append(f"物品「{nm}」：{desc_map.get(nm, '')}")
    if lookup_lines:
        lookup_section = "【本次检索结果】\n" + "\n".join(lookup_lines)
    else:
        lookup_section = ""

    cmd_parts = []
    if attire_change:
        act = "穿上" if attire_change[0] == "put_on" else "脱掉"
        cmd_parts.append(f"让你{act}「{attire_change[1]}」")
    if requested_item:
        cmd_parts.append(f"让你把「{requested_item}」交给玩家")
    if move_target:
        cmd_parts.append(f"让你前往「{move_target}」")
    if is_cmd and cmd_parts:
        cmd_section = (
            "\n[本轮指令]\n"
            f"玩家正在对你下达指令：{'；'.join(cmd_parts)}。\n"
            "请根据你的好感度、性格、当前情景判断是否执行；"
            "在回复末尾额外输出[do:y]（执行）或[do:n]（不执行）；未输出视为不执行。\n"
        )
    else:
        cmd_section = ""

    prompt = f"""你是互动叙事中的NPC.当前这句话是回应对象;同时参考下方全部信息来保持关系,承诺,语气与状态连续.请你阅读全部提示词并思考后回答.只输出角色回复,不代写玩家,不开启新场景/新事件.本设定只用于生成你的回话或你的即时状态变化(好感度,衣着),不用于判定,生成场景或其他NPC行为.

[你的角色]
姓名:{name} | 身份:{role} | 当前衣着:{attire}
性格:{personality} | 外貌:{appearance}
真实目标:{goal}

[认知状态]
确知事实:{secrets_txt}
极力隐瞒:{forbidden_txt}
错误认知:{hallucinations_txt}
你的私有物品:{private_txt}

[对玩家好感度]{aff}
好感无上限无下限:越高越顺从,主动;越低越冷淡,抗拒;远高于100时几乎不会推诿玩家的任何要求,仍须符合身份与当场场景.回复末尾用[aff:数字]（单次不超过±20，平淡给0）。

[世界动态]
玩家名字:{player_name}
玩家当前在:{player_location}
你在此处:{npc_location}
最近世界事件:{recent_events}

[历史记录]
长期摘要:{long_summary}
近期摘要:{recent_summary}
近期对话:
{recent_dialogue}

[查表规则]
1. 查表结果是你唯一的事实依据。没查到的，一律以“不知道/没听过”回应，绝不瞎编。
2. 提到他人物品，只陈述归属和位置，不透露具体内容。
3. 询问物品用途时，在你身上且无锁可说明；有锁则说打不开。

{lookup_section}

[交互红线]
1. 实体限制：严禁编造未登记的人物/物品/地点；动作只能发生在当前场景。
2. 新鲜感：严禁复读上一轮的句式或神态，严禁连续使用万能词汇（如眼神闪烁），每次必须换新角度。
3. 状态同步：动作必须基于当前真实衣着！若无衣物，严禁出现衣物摩擦描写。

{cmd_section}
[玩家当前对你说]
{user_msg}
请以角色口吻回答.格式:`(动作/神态) 说话内容`,字数控制在40~100字.末尾必须标注[aff:数字]"""

    content, ok, aff_change, consent = await _npc_respond(prompt, npc_id)
    if not is_cmd:
        consent = False

    round_entry = f"玩家：{user_msg}"
    if ok:
        round_entry += f"\n{name}：{content}"
    l1.append(round_entry)

    old_l2 = l2
    if len(l1) >= 8:
        front = l1[:5]
        l1 = l1[5:]
        if l3 is None:
            if old_l2 is not None:
                l3 = old_l2
        else:
            if old_l2:
                l3 = await _compress_two(old_l2, l3)
        l2 = await _compress(front)

    new_mem = {"l1": l1, "l2": l2, "l3": l3}

    npc_patch = {npc_id: {
        "memory": new_mem,
        "round_count": (info.get("round_count") or 0) + (1 if ok else 0),
        "affection": max(-100, min(500, aff + aff_change)),
    }}

    out = {
        "history": [{"speaker": npc_id, "content": content}],
        "npcs": npc_patch,
    }

    sys_msgs = []
    if move_target and not move_blocked and ok and consent:
        npc_patch[npc_id]["location"] = move_target
        if move_target not in (state.get("opened_scenes") or []):
            out["opened_scenes"] = [move_target]
        sys_msgs.append(f"【系统】{name}去了{move_target}。")
    elif move_blocked:
        sys_msgs.append(f"【系统】{name}无法前往该场景（缺少凭证）。")

    if attire_change and ok and consent:
        new_attire = _apply_attire(attire, attire_change[0], attire_change[1])
        npc_patch[npc_id]["attire"] = new_attire
        sys_msgs.append(f"【系统】{name}的穿着：{new_attire if new_attire else '无'}。")
    if player_attire_change:
        out["player_attire"] = _apply_attire(player_attire, player_attire_change[0], player_attire_change[1])
        sys_msgs.append(f"【系统】玩家衣着已更新。")

    if requested_item and ok and consent:
        _target_p = next((p for p in props if p.get("name") == requested_item), None)
        if _target_p and _target_p.get("locked"):
            _keys = split_keys(_target_p.get("key_name", ""))
            _can_npc_open = all(key_owner_npc(k, props) == name for k in _keys if not player_has_key(k, player_items, props))
            if _can_npc_open:
                out["item_transfers"] = [{"item": requested_item, "from": npc_id, "auto_open": True}]
                sys_msgs.append(f"【系统】{name}用自己的凭证打开了{requested_item}，里面的东西已放入背包。")
            else:
                out["item_transfers"] = [{"item": requested_item, "from": npc_id}]
                sys_msgs.append(f"【系统】{requested_item}（锁着）在背包。")
        else:
            out["item_transfers"] = [{"item": requested_item, "from": npc_id}]
            sys_msgs.append(f"【系统】{requested_item}在背包。")

    if sys_msgs:
        out["history"] = out["history"] + [{"speaker": "system", "content": m} for m in sys_msgs]

    return out
