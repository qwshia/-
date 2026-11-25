"""通用 NPC 执行器：npc_worker。"""
import re
import asyncio

from langchain_core.runnables import RunnableConfig
from langchain_core.messages import HumanMessage

from config import actor_llm
from state import AgentState
from stream_bus import inc_registered, push_chunk, push_end, push_start
from parsing import _apply_attire, _is_npc_command, _strip_npc_cmd_prefix
from npc_utils import parse_npc_output
from rag_manager import search_npc_dialog, add_npc_dialog
from item_utils import prop_location, key_name_of, bio_holder_of, unlock_hint
from utils import _format_attire, _get_item_names


def _safe_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def _cut_at_marks(s: str, is_ripple_phase: bool = False) -> str:
    """返回 s 中第一个标签之前的部分。无论什么阶段，遇到 RIPPLE 分隔符都切断。"""
    cut = len(s)
    m_rip = re.search(r"\|+|\s*RIP", s, re.IGNORECASE)
    if m_rip and m_rip.start() < cut:
        cut = m_rip.start()
    for mk in ("[aff:", "【aff:", "[do:", "【do:", "[do_items", "【do_items", "[weakness:", "【weakness:", "[surrender:", "【surrender:", "[options:", "【options:", "[emotion:", "【emotion:", "[intent:", "【intent:"):
        i = s.find(mk)
        if i != -1 and i < cut:
            cut = i
    return s[:cut]


def _fact_spoken(fact: str, content: str) -> bool:
    """判断秘密事实是否真的被 NPC 说进了本轮台词（而不是只触发后台判定）。"""
    fact = (fact or "").strip()
    content = (content or "").strip()
    if not fact or not content:
        return False
    # 取事实中长度>=2 的独立片段，命中一半以上即认为已说出
    tokens = [t for t in re.split(r"[，。；、,.;:：\s]+", fact) if len(t) >= 2]
    if not tokens:
        return fact in content
    hits = sum(1 for t in tokens if t in content)
    return hits >= max(1, int(len(tokens) * 0.5))


async def _npc_respond(prompt: str, npc_id: str, requested_items=None, thread_id: str = "") -> tuple:
    inc_registered()
    await push_start(npc_id)
    full = ""
    pushed = 0
    half_beat_done = False
    try:
        async for chunk in actor_llm.astream([HumanMessage(content=prompt)]):
            t = chunk.content if isinstance(chunk.content, str) else ""
            if not t:
                continue
            full += t
            # 一段话回复：清洗可能残留的 RIPPLE 分隔符后整段推送
            safe = re.sub(r"\|\|\|\s*RIPPLE\s*\|\|\|?", "", full, flags=re.IGNORECASE)
            safe = _cut_at_marks(safe)
            if not half_beat_done and safe.strip():
                half_beat_done = True
                await asyncio.sleep(0.6)
            if len(safe) > pushed:
                await push_chunk(npc_id, safe[pushed:])
                pushed = len(safe)

        await push_end(npc_id)
    except Exception:
        await push_end(npc_id)
        return "（网络异常，没有说话。）", False, 0, False, [], [], None, [], [], "", None

    parsed = parse_npc_output(full)
    content = parsed["content"]
    ripple = parsed.get("ripple", "")
    aff_change = parsed["aff_change"]
    consent = parsed["consent"]

    # 同意判定修正：consent=True 才给物；do_items 指定给哪些，do:y 无 do_items 时默认给全部索要物品。
    # 之前这里"拒绝却把 requested_items 全塞进 consented_items"导致拒绝被判成同意（bug）。
    if consent:
        consented_items = list(parsed["consented_items"]) if parsed["consented_items"] else list(requested_items or [])
    else:
        consented_items = []

    # 文本兜底：标签判了拒绝，但台词明确说"给你/拿去"时，仍按同意处理
    if not consent and not consented_items:
        if any(w in full for w in ("给你", "拿去", "拿去吧", "这就给")):
            consent = True
            consented_items = list(requested_items or [])

    return content, True, aff_change, consent, consented_items, parsed["options"], parsed["weakness_hit"], parsed["surrender_items"], parsed.get("emotion_tags", []), ripple, parsed.get("intent")


async def npc_worker(state: AgentState, config: RunnableConfig) -> dict:
    thread_id = config.get("configurable", {}).get("thread_id", "") if isinstance(config, dict) else getattr(config, "configurable", {}).get("thread_id", "")
    npc_id = state.get("npc_id", "")
    npcs = state.get("npcs") or {}
    info = npcs.get(npc_id) or {}
    scene = state.get("current_scene", "未知")
    if info.get("location") != scene:
        return {}

    # NPC 状态检查：dead/absent 不回应
    _npc_status = str(info.get("status", "alive") or "alive").strip().lower()
    if _npc_status in ("dead", "absent"):
        return {}

    user_msg = state.get("user_msg", "")

    is_observer = state.get("is_observer", False)
    if is_observer:
        from bystander_node import bystander_worker
        return await bystander_worker(state, config)

    is_cmd = _is_npc_command(user_msg)
    if is_cmd:
        user_msg = _strip_npc_cmd_prefix(user_msg)

    player_intent = state.get("player_intent") or {}
    intent_type = player_intent.get("intent_type", "chat")
    intent_command = player_intent.get("command")
    clean_msg = player_intent.get("clean_msg") or user_msg

    attire_change = None
    requested_items = []
    offered_items = []
    move_target = None

    if isinstance(intent_command, dict):
        ctype = intent_command.get("type")
        citem = intent_command.get("item")
        cscene = intent_command.get("scene")
        caction = intent_command.get("action")
        if ctype in ("wear", "attire", "attire_change"):
            if isinstance(citem, list):
                attire_items = [str(x) for x in citem if x]
            elif isinstance(citem, str) and citem:
                attire_items = [citem]
            else:
                attire_items = []
            if attire_items:
                action = caction or ("take_off" if "全部" in attire_items else "put_on")
                if "全部" in attire_items:
                    attire_change = (action, "全部")
                else:
                    attire_change = (action, attire_items)
        if ctype in ("give_item", "item", "give"):
            if isinstance(citem, list):
                requested_items = [str(x) for x in citem if x]
            elif isinstance(citem, str) and citem:
                requested_items = [citem]
            else:
                requested_items = []
        if ctype in ("move", "go"):
            move_target = cscene
        if ctype in ("give_to_npc", "player_give", "hand_over"):
            if isinstance(citem, list):
                offered_items = [str(x) for x in citem if x]
            elif isinstance(citem, str) and citem:
                offered_items = [citem]

    user_msg = clean_msg
    player_items = _get_item_names(state.get("player_items") or [])
    name = info.get("name", npc_id)
    role = info.get("role", "路人")
    personality = info.get("personality", "普通")
    appearance = info.get("appearance", "普通")
    attire_raw = info.get("attire", {})
    if isinstance(attire_raw, str):
        attire = {"全装": attire_raw.strip()} if attire_raw.strip() and attire_raw.strip() not in ("", "无") else {}
    elif isinstance(attire_raw, dict):
        attire = dict(attire_raw)
    else:
        attire = {}
    attire_text = _format_attire(attire)
    core_drive = info.get("core_drive") or {}
    short_term_plan = info.get("short_term_plan") or {}
    mood = info.get("mood") or {}
    daily_routine = info.get("daily_routine") or []
    life_events = info.get("life_events") or []
    current_action = info.get("current_action") or {}
    cognition = info.get("cognition") or []
    fears = info.get("fears") or []
    desires = info.get("desires") or []
    perspective = str(info.get("perspective", "") or "").strip()
    contradictions = info.get("contradictions") or []
    impulsivity = info.get("impulsivity", 0.5)
    # 从 cognition 中提取个人经历作为关键记忆
    _key_mem_entries = [c for c in cognition if isinstance(c, dict) and c.get("content") and (
        (c.get("type") == "secret" and not c.get("reveal_condition")) or
        (c.get("type") == "fact")
    )]
    key_memories_txt = "；".join([f"{c.get('content', '')}" for c in _key_mem_entries]) or "（无）"
    status = str(info.get("status", "alive") or "alive").strip()

    core_drive_txt = f"长期执念：{core_drive.get('goal', '')}（服务：{core_drive.get('serves', 'self')}，优先级：{core_drive.get('priority', 100)}）" if core_drive and core_drive.get("goal") else "（无长期执念）"
    short_plan_txt = f"近期盘算：{short_term_plan.get('goal', '')}" if short_term_plan and short_term_plan.get("goal") else "（无近期盘算）"
    mood_txt = f"当前情绪：{mood.get('current', '平静')}（原因：{mood.get('reason', '')}，稳定性：{mood.get('stability', 'stable')}）"
    action_txt = current_action.get("action", "") or "发呆"
    routine_txt = "；".join([f"{r.get('phase', '')}: {','.join(r.get('actions', []))}" for r in daily_routine if isinstance(r, dict)]) or "（无）"
    recent_life = "；".join([str(e.get('event', '')) for e in life_events[-5:] if isinstance(e, dict)]) or "（无）"

    # cognition 提取
    _cog_facts = [str(c.get("content", "")).strip() for c in cognition if isinstance(c, dict) and c.get("type") == "fact" and c.get("content")]
    _cog_secrets = [c for c in cognition if isinstance(c, dict) and c.get("type") == "secret" and c.get("content")]
    _cog_beliefs = [c for c in cognition if isinstance(c, dict) and c.get("type") == "belief" and c.get("content")]
    _cog_forbidden = [c for c in cognition if isinstance(c, dict) and c.get("type") == "forbidden" and c.get("content")]

    facts_txt = "\n".join(f"- {c}" for c in _cog_facts) or "（无）"
    secrets_txt = "\n".join(f"- {c.get('content', '')}（透露条件：{c.get('reveal_condition', '无')}）" for c in _cog_secrets) or "（无）"
    beliefs_txt = "\n".join(f"- {c.get('content', '')}（置信度：{c.get('confidence', 50)}%）" for c in _cog_beliefs) or "（无）"
    forbidden_txt = "\n".join(f"- {c.get('content', '')}（原因：{c.get('reason', '不愿透露')}）" for c in _cog_forbidden) or "（无）"
    fears_txt = "、".join(str(f).strip() for f in fears if str(f).strip()) or "（无）"
    desires_txt = "、".join(str(d).strip() for d in desires if str(d).strip()) or "（无）"
    contradictions_txt = "；".join(str(c).strip() for c in contradictions if str(c).strip()) or "（无）"
    
    flesh_section = f"""\n[人物特质] {personality}
【表演铁律】性格影响你的“表演方式”，不影响“做不做”。比如多疑的人同意了也会先试探、答应得勉强、嘴上答应手上不动。
[当前状态] {mood_txt} | 你此刻正在：{action_txt} | 身体状态：{physical_state} | 状态：{status}
你的日常习惯：{routine_txt}
[近期经历] {recent_life}
[关键记忆] {key_memories_txt}
[心里盘算] {core_drive_txt}；{short_plan_txt}
[认知状态]
确知事实：{facts_txt}
秘密：{secrets_txt}
推断/信念：{beliefs_txt}
拒绝话题：{forbidden_txt}
[恐惧] {fears_txt}
[渴望] {desires_txt}
[内在矛盾] {contradictions_txt}
[解读倾向] {perspective or '（无特殊倾向）'}
"""
    goal = info.get("goal", "") or info.get("life_goal", "")
    aff = _safe_int(info.get("affection", 100), 100)

    _weak = info.get("weakness") if isinstance(info.get("weakness"), dict) else {}
    weakness_trigger = str(_weak.get("trigger", "") or "").strip()
    weakness_reaction = str(_weak.get("reaction", "") or "").strip()
    weakness_state = str(_weak.get("state", "closed") or "closed").strip()
    weakness_kws = _weak.get("trigger_keywords") if isinstance(_weak.get("trigger_keywords"), list) else []
    weakness_kws = [str(k).strip() for k in weakness_kws if str(k).strip()]

    mem = info.get("memory") or {"l1": []}
    if not isinstance(mem, dict):
        mem = {"l1": []}
    l1 = list(mem.get("l1") or [])

    recent_dialogue = "\n".join(l1) if l1 else "（无）"

    props = state.get("props") or []
    private_items = [p.get("name") for p in props if p.get("holder") == name and p.get("name")]
    private_txt = "、".join(private_items) if private_items else "（无）"

    # ==== 秘密后台松口判定（无显式触发词：好感/关系/递物/话题命中自然累积，动态判定）====
    _secret_objs = [c for c in _cog_secrets]  # cognition 中 type=="secret" 的条目
    _revealed_facts = {str(r.get("fact", "") or "").strip() for r in (state.get("secrets_revealed") or []) if isinstance(r, dict) and r.get("npc") == name}
    _available_secrets = [s for s in _secret_objs if str(s.get("content", "") or "").strip() not in _revealed_facts]
    probe_outcome = None  # {"reveal": dict} 或 None（未松口则不注入任何东西，NPC 正常对话）
    probe_section = ""
    if _available_secrets:
        import random as _random
        _weak_broken = weakness_state == "broken"
        _bribed = bool(offered_items)
        _p = 0.02  # 背景松口率：关系升温、聊得多了，NPC 自然漏出秘密
        _p += max(0.0, (aff - 80) / 300.0)  # 好感：80→0，230→+0.5
        if _weak_broken:
            _p += 0.6
        if _bribed:
            _p += 0.3
        # 玩家话语命中该秘密的透露条件（如"欠债""孩子"），显著提高松口概率
        _cond = str(_available_secrets[0].get("reveal_condition", "") or "")
        if _cond:
            _cond_words = [w for w in re.split(r"[,，。;；、\s]+", _cond) if len(w) >= 2]
            _hit_n = sum(1 for w in _cond_words if w in (clean_msg or user_msg or ""))
            if _hit_n >= 2:
                _p += 0.35
        _p = min(0.95, max(0.0, _p))
        if _random.random() < _p:
            probe_outcome = {"reveal": _available_secrets[0]}

    player_name = state.get("player_name", "")
    player_location = scene
    npc_location = info.get("location", scene)
    time_phase = state.get("time_phase", "")
    events = state.get("world_events") or []
    recent_events = "；".join(events[-3:]) if events else "（无）"

    available_scenes = state.get("available_scenes") or []
    move_blocked = False
    if move_target and move_target not in available_scenes:
        move_target = None
    if move_target:
        _sdef = (state.get("scenes") or {}).get(move_target, {})
        if _sdef.get("locked") and move_target not in (state.get("opened_scenes") or []):
            _kn = str(_sdef.get("key_name", "") or "").strip()
            _bh = str(_sdef.get("bio_holder", "") or "").strip()
            _has_key = bool(_kn and any(p.get("name") == _kn and p.get("holder") == name for p in props))
            _self_bio = bool(_bh and _bh == name)
            if _kn and not _has_key and not _self_bio:
                move_blocked = True
                move_target = None

    props = state.get("props") or []
    scenes = state.get("scenes") or {}

    available_scenes = state.get("available_scenes") or []
    all_npcs = state.get("npcs") or {}
    
    # 此时计算当前场景有哪些人（必须在 all_npcs 定义之后）
    present_names = [i.get("name", "") for i in all_npcs.values() if i.get("location") == scene and i.get("name")]
    present_txt = "、".join(present_names) if present_names else "（无）"

    available_txt = "、".join(available_scenes) if available_scenes else "（无）"
    _scene_items = (scenes.get(scene) or {}).get("items") or []
    _scene_items_names = [it.get("name", "") for it in _scene_items if isinstance(it, dict) and it.get("name")]
    scene_items_txt = "、".join(_scene_items_names) if _scene_items_names else "（无）"

    items = _get_item_names(state.get("player_items") or [])
    player_items = list(items)
    desc_map = state.get("item_desc_map") or {}
    _snap_items = state.get("npc_private_items", {}).get(npc_id, [])
    lookup_lines = []
    if any(w in user_msg for w in ("衣服", "穿着", "穿", "脱", "外套")):
        lookup_lines.append(f"玩家问到了你的穿着，你当前的真实衣着是：{attire_text}。")
    if move_target:
        sdef = scenes.get(move_target, {})
        lookup_lines.append(f"场景「{move_target}」：{sdef.get('desc', '')}")
    for nid2, ninfo2 in all_npcs.items():
        nname2 = ninfo2.get("name", "")
        if nname2 and nname2 in user_msg and nid2 != npc_id:
            _nloc = ninfo2.get('location','')
            _npos = "在当前场景" if _nloc == scene else f"在{_nloc}"
            lookup_lines.append(f"NPC「{nname2}」：身份{ninfo2.get('role','')}，外貌{ninfo2.get('appearance','')}，性格{ninfo2.get('personality','')}，衣着{_format_attire(ninfo2.get('attire', {}))}，{_npos}")
    if not is_cmd and len(all_npcs) > 1:
        _mentioned_other = any(info.get("name") in user_msg for nid, info in all_npcs.items() if nid != npc_id)
        if not _mentioned_other:
            _names_hint = "、".join(f"{info.get('name')}" for nid, info in all_npcs.items())
            lookup_lines.append(f"[提示:如果玩家提到了某人,可能是名单中的:{_names_hint}]")
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
    _all_names = set(items) | {p.get("name") for p in props if p.get("name")} | set(_snap_items)
    for nm in _all_names:
        if nm and nm in user_msg and nm not in offered_items:
            if nm in _snap_items:
                still_has = any(p.get("name") == nm and p.get("holder") == name for p in props)
                if still_has:
                    lookup_lines.append(f"物品「{nm}」：你的私有物品，仍在你手中")
                else:
                    lookup_lines.append(f"物品「{nm}」：你的私有物品，但已不在你手中（可能被取走）")
                continue
            pp = next((p for p in props if p.get("name") == nm), None)
            if pp:
                if pp.get("container"):
                    _pos = f"在「{pp.get('container')}」内部"
                else:
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
            else:
                lookup_lines.append(f"物品「{nm}」：{desc_map.get(nm, '')}")
    if offered_items:
        for _oi in offered_items:
            _od = str(desc_map.get(_oi, "") or "").strip()
            _o_prop = next((p for p in props if p.get("name") == _oi), None)
            _status = ""
            if _o_prop:
                if _o_prop.get("locked"):
                    _status = f"（{unlock_hint(_o_prop)}）"
            _full = _od
            if _status and _status not in _full:
                _full = (_full + _status) if _full else _status
            lookup_lines.append(f"玩家递给你物品「{_oi}」：{_full or '（无描述）'}")
    if lookup_lines:
        lookup_section = "[本次检索结果]\n" + "\n".join(lookup_lines)
    else:
        lookup_section = ""

    cmd_parts = []
    if attire_change:
        act = "穿上" if attire_change[0] == "put_on" else "脱掉"
        _attire_items = attire_change[1]
        _attire_txt = "、".join(str(x) for x in _attire_items) if isinstance(_attire_items, (list, tuple)) else str(_attire_items)
        cmd_parts.append(f"- 脱掉衣物：{_attire_txt}")
        
    if requested_items:
        items_txt = "、".join(requested_items)
        cmd_parts.append(f"- 索要物品：{items_txt}")
    if offered_items:
        items_txt = "、".join(offered_items)
        cmd_parts.append(f"- 玩家给你物品：{items_txt}")
        cmd_parts.append(f"- 收礼好感：根据你对该物品的真实喜好/需求动态调整好感——喜欢+10~20，无感0，厌恶/嫌弃-5~-10，并在 [aff:] 标签中体现（收下物品≠必须涨好感）")
        
    if move_target:
        cmd_parts.append(f"- 前往地点：{move_target}")
        
    if cmd_parts:
        cmd_section = (
            "\n[本轮指令]\n"
            "玩家正在对你下达以下指令（可能包含多项）：\n" + "\n".join(cmd_parts) + "\n"
            "请针对每一项指令单独做出判断。\n"
            "【指令标签铁律】系统只读取标签，自然语言不算数！\n"
            "1. 无论你在对话里说什么，系统只认 [do:y] / [do:n] / [do_items: 物品A, 物品B] 三种标签。\n"
            "2. 只要本轮涉及以下任意一项，你必须输出对应标签：\n"
            "   - 玩家索要你的物品（NPC→玩家）：接受输出 [do_items: 物品名]；拒绝输出 [do:n]。\n"
            "   - 玩家给你物品（玩家→NPC）：接受输出 [do_items: 物品名]；拒绝输出 [do:n]。\n"
            "   - 玩家要你脱衣：接受输出 [do_items: 衣物名]；拒绝输出 [do:n]。\n"
            "   - 玩家让你移动：接受输出 [do:y]；拒绝输出 [do:n]。\n"
            "3. 即使你嘴上说了「好/收下/行/可以」，也必须同时输出 [do_items:] 标签；即使你说「不要/免了/心领了/不需要」，也必须同时输出 [do:n]。\n"
            "4. 本轮缺失标签视为「拒绝所有指令」，即使你嘴上说同意。\n"
            "5. 移动同意只能用 [do:y]，绝对不能把地点名字写进 [do_items]。\n"
            "6. 本轮没有出现在 [本轮指令] 列表里的物品，绝对不能输出 [do_items:]。\n"
        )
    else:
        cmd_section = (
            "\n[本轮无指令]\n"
            "玩家本轮没有对你下达任何显式指令。你只需进行正常的剧情对话。\n"
        )

    intent_section = ""
    if intent_type == "mixed":
        intent_section = "玩家的话里既有情绪也有指令，请先回应情绪，再根据人设决定是否执行指令。\n"
    elif intent_type == "command":
        intent_section = (
            "【本轮为指令执行模式】玩家下达了明确的指令。主响应必须极简："
            "只输出一句 25 字以内的简短应答（可带一个轻动作/神态），"
            "禁止展开剧情对话、禁止长篇解释、禁止反问（若拒绝也要极简如「（摇头）我不去。」）。\n"
        )

    # RAG 智障拦截：指令、极短句、动作类、索要/递交类不查记忆
    _skip_rag = (intent_type == "command") or (len(clean_msg) <= 4) or (move_target is not None) or bool(requested_items) or bool(offered_items) or bool(attire_change)
    _meaningless_words = ["宝宝", "亲爱的", "你好", "在吗", "谢谢", "喜欢你", "爱你", "给我吧", "给我", "给你", "拿去", "收下"]
    if not _skip_rag:
        _clean_for_check = clean_msg
        for w in _meaningless_words:
            _clean_for_check = _clean_for_check.replace(w, "")
        if len(_clean_for_check.strip()) <= 2:
            _skip_rag = True

    if _skip_rag:
        rag_history = ""
    else:
        rag_history = await search_npc_dialog(thread_id, npc_id, clean_msg, top_k=2)
    rag_section = "\n[RAG 相关历史记忆]\n" + rag_history + "\n" if rag_history else ""

    weakness_section = ""
    if weakness_trigger:
        if weakness_state == "broken":
            weakness_section = (
                "\n[心理状态:已破防]\n"
                f"你的软肋({weakness_trigger})已经被玩家击穿。你无法再嘴硬或隐瞒，只能主动示弱、坦白、配合玩家，"
                "必要时主动交出底牌(用 [surrender: 物品名] 标签交给玩家)。\n"
            )
        else:
            weakness_section = (
                "\n[心理状态:防线完整]\n"
                f"你极力维护的软肋是:{weakness_trigger}。玩家一旦精准命中，你会动摇失态、露出破绽。\n"
                "【标签铁律】若玩家本轮的话精准命中软肋，你必须输出 [weakness: hit]；若擦边或未命中，输出 [weakness: miss]；若本轮玩家没有对你施压，不要输出该标签。\n"
                "命中后你的反应模板：" + weakness_reaction + "\n"
                "命中后你可以主动交出底牌，用 [surrender: 物品名] 标签交给玩家(仅限你实际持有的物品)；不交则不用该标签。\n"
            )

    if probe_outcome and probe_outcome.get("reveal"):
        _probe_fact = str(probe_outcome["reveal"].get("content", "") or "").strip()
        probe_section = (
            "\n[松口判定-你守不住了]\n"
            f"聊到这份上，你的心理防线已松动/权衡后决定松口。你必须把下面这条秘密的核心信息直接说进本轮台词里（用第一人称，事实准确完整，不要省略关键信息）：{_probe_fact}\n"
            "保持你的人设与情绪，让秘密顺着当前话题自然说出来，不要突兀广播。\n"
        )

    if cmd_parts:
        tail_rules = """
[尾部标签区-指令模式]
主响应写完, 另起一行, 只输出以下两个标签:
[aff:数字] [do:y|do:n|do_items: 物品A, 物品B]
[禁令] 严禁 [options:] / [intent:]. 缺 [do:] 视为拒绝.

示例:
好.
[aff:5] [do:y]
"""
    else:
        tail_rules = """
[尾部标签区-普通对话]
主响应写完, 另起一行, 输出:
[aff:数字] [intent: 类型 | 场景名/物品名 | 细节]
[禁令] 严禁 [do:y] / [do:n] / [do_items:] / [options:].

[意图池-只选1种]
- idle: 发呆/无明确目标. 默认选项, 无剧情推动时必须用此.
- move | 场景名: 剧情明确推动主动去某处
- take | 物品名: 剧情明确推动主动拿某物
- give | 物品名: 剧情明确推动主动交某物
- put_on | 衣物名: 剧情明确推动主动穿衣
- take_off | 衣物名: 剧情明确推动主动脱衣
- chat | NPC名: 剧情明确推动主动找某人攀谈
- find | 人物/物品名: 剧情明确推动主动寻找某物或某人
- follow | 无: 剧情明确推动主动跟随玩家行动
- trade | 物品名: 剧情明确推动主动提出交换物品
- guard | 人物/物品/地点: 剧情明确推动主动看守某人或某物
- flee | 场景名: 剧情明确推动主动逃离某场景
[禁令] 意图池仅作参考, 严禁照抄示例. 无明确推动一律 idle.

示例:
我不想让你看那个包. 求你了.
[aff:-5] [intent: idle]
"""

    prompt = f"""你是互动叙事中的NPC. 任务: 针对[玩家当前对你说]生成一句符合人设的角色回复 + 尾部标签.
[禁令] 不要代写玩家; 不要开启新场景; 不要描述其他NPC行为.

[你的角色]
姓名:{name} | 身份:{role} | 衣着:{attire_text}
性格:{personality} | 外貌:{appearance}
人生目标:{goal}
{flesh_section}

[认知状态]
确知事实:
{facts_txt}
秘密:
{secrets_txt}
推断/信念:
{beliefs_txt}
拒绝话题:
{forbidden_txt}
恐惧: {fears_txt}
渴望: {desires_txt}
内在矛盾: {contradictions_txt}
解读倾向: {perspective or '（无特殊倾向）'}
私有物品:{private_txt}
{weakness_section}
{probe_section}

[对玩家好感度]{aff}
好感无上下限. 越高越顺从主动, 越低越冷淡抗拒. 回复末尾必须给 [aff:数字], 单次≤±20, 平淡给0.

[世界动态]
时间:{time_phase} | 玩家名:{player_name} | 玩家在:{player_location} | 你在:{npc_location}
同场景NPC:{present_txt} | 可去场景:{available_txt}
场景物品:{scene_items_txt} | 你的私有物品:{private_txt}
最近世界事件:{recent_events}

[历史记录]
近期对话:
{recent_dialogue}
{rag_section}

[查表规则]
1. 只能依据[本次检索结果]里的事实回答. 没查到的一律回"不知道/没听过", 绝不瞎编.
2. 他人物品只陈述归属和位置, 不透露具体内容.
3. 询问物品用途: 在你身上且无锁可说明; 有锁则说打不开.
4. 若[本次检索结果]明确说该物品在你身上, 绝对不能撒谎说"不在我身上/早就不在了/我不知道". 你可以拒绝交出(输出[do:n]), 但必须承认东西在你手里.
5. 若玩家要的是场景物品且上了锁, 请告知需要什么钥匙才能打开.
6. [帮忙开锁] 若玩家递给你一个带生物锁的物品(提示语含"需要你本人验证"或"生物锁"), 而你恰好是其生物锁持有者: 你愿意时可在回复中输出同意(标签[do:y]+[do_items:物品名]), 系统会让你用自己的虹膜/指纹帮他打开; 不愿意则输出[do:n]并说明. 生物凭证长在自己身上, 谁也偷不走、索要不去.

{lookup_section}

[交互红线]
1. 严禁编造未登记的人物/物品/地点. 动作只能发生在当前场景.
2. 严禁复读上一轮句式/神态, 严禁连续使用万能词(如'眼神闪烁'), 必须换新角度.
3. 动作必须基于当前真实衣着. 若无衣物, 严禁出现衣物摩擦描写.

{cmd_section}
{intent_section}
[玩家当前对你说]
{user_msg}

[输出格式-一段话+尾部标签]
主响应: 直接说台词, 自然口语, 一段话说完, 40-120字. 严禁用括号描写动作/神态/小动作, 严禁添加口头禅, 直接说话即可.
{tail_rules}
[注意] 玩家输入含问题时, 先回答问题再处理指令. 所有标签统一在最后一行, 绝不与台词混排."""

    content, ok, aff_change, consent, consented_items, _options, weakness_hit, surrender_items, emotion_tags, _ripple, npc_intent = await _npc_respond(prompt, npc_id, requested_items, thread_id)
    # 无显式指令时，强行清空所有指令标签，防止幻觉或越界
    if not is_cmd and not cmd_parts:
        consent = False
        consented_items = []
    # 脱衣/穿衣指令：intent 视为 idle，衣物绝不入意图池
    if attire_change:
        npc_intent = None
        _target_attire = attire_change[1]
        _attire_targets = [_target_attire] if isinstance(_target_attire, str) else _target_attire
        
        # 穿衣服：完全隔离物品池。只要模型输出了 wear/put_on，就忽略其附带的 do_items，防止去 props 里找不到报错
        if attire_change[0] == "put_on":
            consented_items = [x for x in consented_items if x not in _attire_targets]
        # 脱衣服：如果模型输出了 consented_items，但没包含 attire_change 的目标，补充进去保证生效
        elif attire_change[0] == "take_off":
            for _ta in _attire_targets:
                if _ta and _ta not in consented_items and _ta != "全部":
                    consented_items.append(_ta)

    # 兜底：玩家给了物品，模型未标 [do_items:]/[do:n]，从自然语言判断接受/拒绝
    if offered_items and ok and not consent and not consented_items:
        _acc_words = ("收下了", "我收下", "收下吧", "笑纳", "接受", "谢谢", "这就拿", "这就收")
        _rej_words = ("不要", "不收", "不用", "免了", "心领", "不接", "拿回去", "退给你", "还给你", "别给我", "不需要")
        _has_acc = any(w in content for w in _acc_words)
        _has_rej = any(w in content for w in _rej_words)
        if _has_acc and not _has_rej:
            consent = True
            consented_items = list(offered_items)

    # weakness 判定：标签优先，缺失时用 trigger_keywords 兜底（仅 closed 态有效）
    _weak_effective_hit = False
    if weakness_trigger and ok:
        if weakness_state == "broken":
            _weak_effective_hit = False
        elif weakness_hit is True:
            _weak_effective_hit = True
        elif weakness_hit is None and weakness_kws:
            if any(k in user_msg for k in weakness_kws):
                _weak_effective_hit = True

    round_entry = f"玩家：{user_msg}"
    if ok:
        round_entry += f"\n{name}：{content}"
    l1.append(round_entry)

    if len(l1) > 10:
        l1 = l1[-10:]
    new_mem = {"l1": l1}

    npc_patch = {npc_id: {
        "memory": new_mem,
        "round_count": (info.get("round_count") or 0) + (1 if ok else 0),
        "affection": max(-100, min(500, aff + aff_change)),
        "current_intent": npc_intent,
    }}

    ui_events = []
    if emotion_tags:
        _current_tags = list(info.get("emotion_tags") or [])
        for _et in emotion_tags:
            _clean_et = str(_et).strip()
            if _clean_et:
                _current_tags.append({"target": "玩家", "tag": _clean_et, "tick": int(state.get("world_tick") or 0)})
                ui_events.append({"type": "emotion_shift", "npc_id": npc_id, "npc_name": name, "tag": _clean_et, "category": "对玩家"})
        # FIFO 截断到 5 条
        if len(_current_tags) > 5:
            _current_tags = _current_tags[-5:]
        npc_patch[npc_id]["emotion_tags"] = _current_tags

    out = {
        "history": [{"speaker": npc_id, "content": content}],
        "npcs": npc_patch,
    }
    sys_msgs = []

    if weakness_trigger and ok:
        if _weak_effective_hit:
            _new_weak = dict(_weak)
            _new_weak["state"] = "broken"
            npc_patch.setdefault(npc_id, {})["weakness"] = _new_weak
            sys_msgs.append(f"【系统】{name}的心理防线崩溃了。")
            ui_events.append({
                "type": "mental_break",
                "npc_id": npc_id,
                "npc_name": name,
                "reaction": weakness_reaction,
            })  # 2D frontend
        elif weakness_hit is False and weakness_state != "broken":
            ui_events.append({
                "type": "mental_guard",
                "npc_id": npc_id,
                "npc_name": name,
            })  # 2D frontend

    if probe_outcome and probe_outcome.get("reveal"):
        _fact = str(probe_outcome["reveal"].get("content", "") or "").strip()
        # 只有真的在聊天里说出口才算透露，才记入线索；同时按事实文本去重，避免同一条秘密重复进线索
        if _fact_spoken(_fact, content):
            _clue_text = f"秘密（{name}透露）：{_fact}"
            _existing_clues = {str(c).strip() for c in (state.get("player_revealed_clues") or [])}
            _existing_facts = {str(r.get("fact", "") or "").strip() for r in (state.get("secrets_revealed") or []) if isinstance(r, dict)}
            if _fact not in _existing_facts:
                out["secrets_revealed"] = [{"npc": name, "fact": _fact}]
            if _clue_text not in _existing_clues:
                out["player_revealed_clues"] = [_clue_text]
            sys_msgs.append(f"【系统】{name}松口了，你得到一条秘密，已记入线索笔记。")
            ui_events.append({"type": "secret_revealed", "npc_name": name})
            # 从 cognition 中移除已透露的秘密（秘密已暴露，不再是秘密）
            _revealed_secret_content = _fact
            _updated_cog = [c for c in (info.get("cognition") or []) if not (isinstance(c, dict) and c.get("type") == "secret" and str(c.get("content", "")).strip() == _revealed_secret_content)]
            # 如果秘密内容不完全匹配，按 reveal_condition 匹配
            if len(_updated_cog) == len(info.get("cognition") or []):
                _probe_entry = probe_outcome["reveal"]
                _probe_content = str(_probe_entry.get("content", "")).strip()
                _updated_cog = [c for c in (info.get("cognition") or []) if not (isinstance(c, dict) and c.get("type") == "secret" and str(c.get("content", "")).strip() == _probe_content)]
            if len(_updated_cog) < len(info.get("cognition") or []):
                npc_patch.setdefault(npc_id, {})["cognition"] = _updated_cog

    if surrender_items and ok and weakness_trigger:
        _held_names = {p.get("name") for p in props if p.get("name") and p.get("holder") == name}
        _valid_surrender = [x for x in surrender_items if x in _held_names]
        if _valid_surrender:
            out.setdefault("item_transfers", [])
            for _si in _valid_surrender:
                out["item_transfers"].append({"item": _si, "from": npc_id})
            sys_msgs.append(f"【系统】{name}主动交出了「{'、'.join(_valid_surrender)}」。")

    if move_target and not move_blocked and ok and consent:
        npc_patch[npc_id]["location"] = move_target
        npc_patch[npc_id]["current_intent"] = None  # 指令已执行完毕，清空意图，防 arbiter_node 二次执行（"悄悄去了"重复）
        if move_target not in (state.get("opened_scenes") or []):
            out["opened_scenes"] = [move_target]
        sys_msgs.append(f"【系统】{name}去了{move_target}。")
        ui_events.append({"type": "npc_move", "npc": name, "target_scene": move_target, "from_scene": scene})  # 2D frontend
    elif move_blocked:
        npc_patch[npc_id]["current_intent"] = None  # 玩家指令已尝试失败，清空意图，防 arbiter_node 二次偷偷执行
        sys_msgs.append(f"【系统】{name}无法前往该场景（缺少凭证）。")
        ui_events.append({"type": "action_feedback", "target": name, "action": "threatened", "shake_screen": True})  # 2D frontend

    if attire_change and ok and consent:
        new_attire = _apply_attire(attire, attire_change[0], attire_change[1])
        npc_patch[npc_id]["attire"] = new_attire
        sys_msgs.append(f"【系统】{name}的穿着更新为：{_format_attire(new_attire)}。")
        ui_events.append({"type": "attire_change", "npc": name, "action": attire_change[0], "current_attire": new_attire})  # 2D frontend

    if (requested_items or consented_items) and ok:
        if consented_items:
            agreed_list = [str(x).strip() for x in consented_items if str(x).strip()]
        elif consent:
            agreed_list = [str(x).strip() for x in (requested_items or []) if str(x).strip()]
        else:
            agreed_list = []
        # 去重保序
        _seen = set()
        agreed_list = [x for x in agreed_list if not (x in _seen or _seen.add(x))]
        if offered_items:
            agreed_list = [x for x in agreed_list if x not in offered_items]
        consented_set = set(agreed_list)
        # 提取当前NPC的衣物值列表，用于排除
        attire_values = list(attire.values()) if isinstance(attire, dict) else []
        if attire_change and isinstance(attire_change[1], (list, tuple)):
            attire_values.extend(attire_change[1])
        # 过滤地点乱入
        if move_target:
            agreed_list = [x for x in agreed_list if x != move_target and x != f"前往{move_target}"]
        transfers = []
        missing_items = []
        _attire_off_names = set()
        if attire_change and attire_change[0] == "take_off":
            _ai = attire_change[1]
            if isinstance(_ai, str):
                _attire_off_names.add(_ai)
            elif isinstance(_ai, (list, tuple)):
                for _x in _ai:
                    _attire_off_names.add(str(_x))

        for it in agreed_list:
            # 玩家要脱的衣服，无论模型怎么标 [do_items:]，一律不进物品转移
            if _attire_off_names and (it in _attire_off_names or any(it in n or n in it for n in _attire_off_names)):
                continue
            # 穿衣动作涉及的物品，直接跳过，不进入物品转移
            if attire_change and attire_change[0] == "put_on":
                if any(it in n or n in it for n in _attire_targets):
                    continue
            # 索要物品列表里显式包含的，才允许进入转移
            if attire_change and it not in requested_items and (
                it in attire_values or any(it in str(val) for val in attire_values)
            ):
                continue
            # （物品已平铺，无容器子物品，无需穿透拦截）
            if it not in consented_set:
                continue
            tp = next((p for p in props if p.get("name") == it), None)
            if tp is None and it not in (_snap_items or []):
                missing_items.append(it)
                continue
            locked = bool(tp and tp.get("locked"))
            if locked:
                _bh = bio_holder_of(tp) if tp else ""
                if _bh == name:
                    # 生物锁且 bio_holder 是本人：同意帮忙 → 用自己的生物凭证（身体特征）开生物部分
                    transfers.append({"item": it, "from": npc_id, "auto_open": True})
                    ui_events.append({"type": "item_transfer", "item": it, "count": 1, "from_npc": name, "to_player": True})
                else:
                    transfers.append({"item": it, "from": npc_id})
                    ui_events.append({"type": "item_transfer", "item": it, "count": 1, "from_npc": name, "to_player": True})
            else:
                transfers.append({"item": it, "from": npc_id})
                ui_events.append({"type": "item_transfer", "item": it, "count": 1, "from_npc": name, "to_player": True})
        if missing_items:
            sys_msgs.append(f"【系统】{name}身上似乎没有「{'、'.join(missing_items)}】。")
        for tr in transfers:
            sys_msgs.append(f"【系统】{name}把「{tr['item']}」交给了你。")
        if transfers:
            out["item_transfers"] = transfers

    if offered_items and ok:
        _consented = set(str(x).strip() for x in (consented_items or []))
        accepted_from_player = [x for x in offered_items if x in _consented]
        if not accepted_from_player and consent and len(offered_items) == 1:
            accepted_from_player = offered_items[:1]
        _mem_action = ""
        if accepted_from_player:
            out["player_gives"] = [{"item": x, "to": npc_id, "from": "玩家"} for x in accepted_from_player]
            sys_msgs.append(f"【系统】{name}收下了「{'、'.join(accepted_from_player)}」。")
            _mem_action = f"（{name}收下了玩家给的{'、'.join(accepted_from_player)}）"
            for _ax in accepted_from_player:
                ui_events.append({"type": "item_transfer", "item": _ax, "count": 1, "from_player": True, "to_npc": name})  # 2D frontend
        else:
            _pl = state.get("player_items") or []
            _bio_rej = [
                x for x in offered_items
                for _it in _pl
                if isinstance(_it, dict) and _it.get("name") == x and _it.get("locked")
                and str(_it.get("bio_holder", "") or "").strip() == name
            ]
            if _bio_rej:
                sys_msgs.append(f"【系统】{name}拒绝帮你打开「{'、'.join(_bio_rej)}」，物品原样留在你的背包。")
            else:
                sys_msgs.append(f"【系统】{name}没有收下「{'、'.join(offered_items)}」，但 TA 已经看到了。")
            _mem_action = f"（{name}拒绝了玩家给的{'、'.join(offered_items)}，但已经看到了）"
        if _mem_action:
            _cur_mem = npc_patch.get(npc_id, {}).get("memory", {}) or {}
            _cur_l1 = list(_cur_mem.get("l1") or [])
            if _cur_l1:
                _cur_l1[-1] = _cur_l1[-1] + "\n" + _mem_action
            else:
                _cur_l1 = [_mem_action]
            npc_patch.setdefault(npc_id, {})["memory"] = {"l1": _cur_l1}
    if aff_change <= -15:
        ui_events.append({"type": "action_feedback", "target": name, "action": "angry", "shake_screen": True})  # 2D frontend

    if sys_msgs:
        existing = {e.get("content") for e in (out.get("history") or []) if isinstance(e, dict)}
        for m in sys_msgs:
            if m not in existing:
                out["history"].append({"speaker": "system", "content": m})
                existing.add(m)

    if ui_events:
        out["ui_events"] = ui_events

    try:
        meta = {"scene": scene, "time": state.get("time_phase", "")}
        add_npc_dialog(thread_id, npc_id, f"玩家对我说：{user_msg}", meta)
        if ok and content:
            add_npc_dialog(thread_id, npc_id, f"我回答：{content}", meta)
    except Exception as e:
        print(f"[RAG写入NPC对话失败] {e}")

    return out
