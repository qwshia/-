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
from item_utils import key_owner_npc, split_keys, player_has_key, prop_location, key_name_of
from utils import _parse_content, _format_attire, _get_item_names


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


async def _npc_respond(prompt: str, npc_id: str, requested_items=None) -> tuple:
    inc_registered()
    await push_start(npc_id)
    full = ""
    pushed_ripple = 0
    pushed_main = 0
    main_start = None  # 主响应起点位置指针（None 表示余波尚未切分）
    half_beat_done = False
    try:
        async for chunk in actor_llm.astream([HumanMessage(content=prompt)]):
            t = chunk.content if isinstance(chunk.content, str) else ""
            if not t:
                continue
            full += t

            # 余波尚未切分时，只认 |||RIPPLE||| 分隔符（流式阶段不做括号兜底）
            if main_start is None:
                m_sep = re.search(r"\|\|\|\s*RIPPLE", full, re.IGNORECASE)
                if m_sep:
                    m_full_sep = re.match(r"\|\|\|\s*RIPPLE\s*\|\|\|?", full[m_sep.start():], re.IGNORECASE)
                    if m_full_sep:
                        # 关键：ripple_text 也要经过 _cut_at_marks，清洗残留标签
                        ripple_text = _cut_at_marks(full[:m_sep.start()], is_ripple_phase=False)
                        if len(ripple_text) > pushed_ripple:
                            await push_chunk(npc_id, ripple_text[pushed_ripple:])
                            pushed_ripple = len(ripple_text)
                        main_start = m_sep.start() + m_full_sep.end()

            # 已确定主响应起点，推送主响应
            if main_start is not None:
                if not half_beat_done:
                    half_beat_done = True
                    await asyncio.sleep(0.8)
                main_text = full[main_start:]
                # 废除流式阶段的截断，将文本全部推送，保证终端完整显示
                if len(main_text) > pushed_main:
                    await push_chunk(npc_id, main_text[pushed_main:])
                    pushed_main = len(main_text)

        # 兜底：模型完全没按格式输出，整段当主响应推出去
        if main_start is None and full.strip():
            if not half_beat_done:
                half_beat_done = True
                await asyncio.sleep(0.8)
            # 先剥离可能存在的残留分隔符，避免 _cut_at_marks 因为 | 号误伤主响应
            cleaned_full = re.sub(r"\|\|\|\s*RIPPLE\s*\|\|\|?", "", full, flags=re.IGNORECASE)
            safe_full = _cut_at_marks(cleaned_full, is_ripple_phase=False)
            if len(safe_full) > pushed_main:
                await push_chunk(npc_id, safe_full[pushed_main:])
                pushed_main = len(safe_full)

        await push_end(npc_id)
    except Exception:
        await push_end(npc_id)
        return "（网络异常，没有说话。）", False, 0, False, [], [], None, [], [], ""

    parsed = parse_npc_output(full)
    content = parsed["content"]
    ripple = parsed.get("ripple", "")
    aff_change = parsed["aff_change"]
    consent = parsed["consent"]
    consented_items = parsed["consented_items"] if consent else list(requested_items or [])

    if not consent and not parsed["consented_items"]:
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

    user_msg = state.get("user_msg", "")

    is_observer = state.get("is_observer", False)
    if is_observer:
        # 提取场景信息和在场人物给旁观者
        all_npcs = state.get("npcs") or {}
        present_names = [i.get("name", "") for i in all_npcs.values() if i.get("location") == scene and i.get("name")]
        present_txt = "、".join(present_names)
        
        # 提取最近两轮历史
        history = state.get("history") or []
        recent_hist = ""
        for h in history[-4:]:
            sp = h.get("speaker")
            if sp == "user":
                recent_hist += f"玩家：{h.get('content')}\n"
            elif sp in all_npcs:
                recent_hist += f"{all_npcs[sp].get('name')}：{h.get('content')}\n"

        obs_prompt = f"""你是互动叙事中的旁观者 NPC。你的名字是 {info.get('name', npc_id)}，身份是 {info.get('role', '路人')}，性格 {info.get('personality', '普通')}。
你当前所在的场景是：{scene}。
当前房间内的人有：{present_txt}。
最近发生的事：
{recent_hist}

玩家刚刚说/做了：{user_msg}

请写出你作为旁观者的本能反应。
【铁律】
1. 如果你觉得事不关己，只输出 `(发呆)`，系统会自动过滤你。
2. 如果你想插嘴，只能有一句极短的台词，总字数不超过 30 字。
3. 【绝对禁令】严禁输出任何英文、代码、乱码或无逻辑的文字！如果不知道说什么，直接输出 `(发呆)`。
4. 不要输出任何标签格式（如 aff/options）。
格式：`(动作/神态) 台词` 或 `(动作/神态)`"""
        try:
            resp = await actor_llm.ainvoke([HumanMessage(content=obs_prompt)])
            obs_content = (resp.content or "").strip()
        except Exception:
            obs_content = "(发呆)"
            
        if not obs_content or "发呆" in obs_content or "无反应" in obs_content:
            return {}
            
        # 返回旁观者的反应
        return {
            "history": [{"speaker": npc_id, "content": obs_content}],
            "ui_events": [{"type": "observer_reaction", "npc_id": npc_id, "npc_name": info.get("name", npc_id), "text": obs_content}]
        }

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
    traits = info.get("personality_traits") or []
    core_goal = info.get("core_goal") or {}
    short_term_goal = info.get("short_term_goal") or {}
    mood = info.get("mood") or {}
    daily_routine = info.get("daily_routine") or []
    life_events = info.get("life_events") or []
    current_action = info.get("current_action") or {}

    traits_txt = "、".join([f"{t.get('trait', '')}(强度{t.get('intensity', 50)})" for t in traits if isinstance(t, dict)]) or "（无明确特质）"
    core_goal_txt = f"长期执念：{core_goal.get('goal', '')}" if core_goal else "（无长期执念）"
    short_goal_txt = f"近期盘算：{short_term_goal.get('goal', '')}" if short_term_goal else "（无近期盘算）"
    mood_txt = f"当前情绪：{mood.get('current', '平静')}（原因：{mood.get('reason', '')}）"
    action_txt = current_action.get("action", "") or "发呆"
    routine_txt = "；".join([f"{r.get('phase', '')}: {','.join(r.get('actions', []))}" for r in daily_routine if isinstance(r, dict)]) or "（无）"
    recent_life = "；".join([str(e.get('event', '')) for e in life_events[-5:] if isinstance(e, dict)]) or "（无）"
    
    flesh_section = f"""\n[人物特质] {traits_txt}
【表演铁律】特质影响你的“表演方式”，不影响“做不做”。比如多疑的人同意了也会先试探、答应得勉强、嘴上答应手上不动。
[当前状态] {mood_txt} | 你此刻正在：{action_txt}
你的日常习惯：{routine_txt}
[近期经历] {recent_life}
[心里盘算] {core_goal_txt}；{short_goal_txt}
"""
    goal = info.get("goal", "")
    secrets = [str(s) for s in (info.get("secrets") or [])]
    forbidden = [str(f) for f in (info.get("forbidden") or [])]
    hallucinations = [str(h) for h in (info.get("hallucinations") or [])]
    aff = _safe_int(info.get("affection", 100), 100)

    _weak = info.get("weakness") if isinstance(info.get("weakness"), dict) else {}
    weakness_trigger = str(_weak.get("trigger", "") or "").strip()
    weakness_type = str(_weak.get("type", "") or "").strip()
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
    secrets_txt = "\n".join(f"- {s}" for s in secrets) or "（无）"
    forbidden_txt = "\n".join(f"- {f}" for f in forbidden) or "（无）"
    hallucinations_txt = "\n".join(f"- {h}" for h in hallucinations) or "（无）"

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
        rk = str((state.get("scenes") or {}).get(move_target, {}).get("requires_key", "") or "").strip()
        if rk:
            need = [k.strip() for k in rk.split("+") if k.strip()]
            if move_target in (state.get("opened_scenes") or []):
                need = []
            player_items = _get_item_names(state.get("player_items") or [])
            missing = [k for k in need if k not in player_items]
            if missing:
                npc_has = all(any(p.get("name") == k and p.get("holder") == name for p in props) for k in missing)
                if not npc_has:
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
    for _cp in props:
        for _c in (_parse_content(_cp.get("content")) or []):
            if _c.get("name"):
                _all_names.add(str(_c.get("name")).strip())
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
                _parsed_c = _parse_content(pp.get("content"))
                if _parsed_c:
                    _sub = "；".join(f"{c['name']}" + (f"：{c['text']}" if c.get("text") else "") for c in _parsed_c)
                    lookup_lines.append(f"（内含：{_sub}）")
            else:
                _container = None
                for _cp in props:
                    _subs = _parse_content(_cp.get("content"))
                    if any(str(c.get("name", "")).strip() == nm for c in _subs):
                        _container = _cp.get("name")
                        break
                if _container:
                    lookup_lines.append(f"物品「{nm}」：在「{_container}」内部")
                else:
                    lookup_lines.append(f"物品「{nm}」：{desc_map.get(nm, '')}")
    if offered_items:
        for _oi in offered_items:
            _od = str(desc_map.get(_oi, "") or "").strip()
            _o_prop = next((p for p in props if p.get("name") == _oi), None)
            _status = ""
            if _o_prop:
                if _o_prop.get("locked"):
                    _kn = str(_o_prop.get("key_name", "") or "").strip()
                    if _kn:
                        _status = f"（物品锁着，需要{_kn}才能打开）"
                elif _o_prop.get("content"):
                    _status = "（物品内含内容）"
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
        intent_section = "本轮以执行指令为主，回应尽量简短。\n"

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

    if cmd_parts:
        tail_rules = """
【尾部标签区（极度重要）】
在主响应写完后，**必须另起一行**，统一输出所有标签，不要遗漏，不要分行：
[aff:数字] [do:y 或 do:n 或 do_items: 物品A, 物品B] [options: 选项1 | 选项2 | 选项3] [intent: 类型 | 目标 | 细节]

【意图池（仅限这 6 种）】
- [intent: idle] （发呆/无明确目标）
- [intent: move | 目标场景名 | 细节]
- [intent: take | 物品名 | 细节]
- [intent: give | 物品名 | 细节]
- [intent: put_on | 衣物名 | 细节] （主动穿上）
- [intent: take_off | 衣物名 | 细节] （主动脱下）

示例输出：
(猛地一颤)
|||RIPPLE|||
(缓缓脱下外套，眼神死寂) 裴处长，这是您要的。
[aff:-5] [do_items: 黑色西装] [options: 查看衬衫 | 命令跪下 | 拥抱她] [intent: idle]
"""
    else:
        tail_rules = """
【尾部标签区（极度重要）】
在主响应写完后，**必须另起一行**，统一输出所有标签，不要遗漏，不要分行：
[aff:数字] [options: 选项1 | 选项2 | 选项3] [intent: 类型 | 目标 | 细节]
（绝对禁止输出 [do:y] / [do:n] / [do_items:] 标签！）

【意图池（仅限这 6 种）】
- [intent: idle] （发呆/无明确目标）
- [intent: move | 目标场景名 | 细节]
- [intent: take | 物品名 | 细节]
- [intent: give | 物品名 | 细节]
- [intent: put_on | 衣物名 | 细节] （主动穿上）
- [intent: take_off | 衣物名 | 细节] （主动脱下）

示例输出：
(手指猛地攥紧)
|||RIPPLE|||
(低下头，声音颤抖) 我不想让你看那个包……求你了。
[aff:-5] [options: 强行查看 | 安抚她 | 转身离开] [intent: idle]
"""

    prompt = f"""你是互动叙事中的NPC.当前这句话是回应对象;同时参考下方全部信息来保持关系,承诺,语气与状态连续.请你阅读全部提示词并思考后回答.只输出角色回复,不代写玩家,不开启新场景/新事件.本设定只用于生成你的回话或你的即时状态变化(好感度,衣着),不用于判定,生成场景或其他NPC行为.

[你的角色]
姓名:{name} | 身份:{role} | 当前衣着:{attire_text}
性格:{personality} | 外貌:{appearance}
真实目标:{goal}
{flesh_section}

[认知状态]
确知事实:{secrets_txt}
极力隐瞞:{forbidden_txt}
错误认知:{hallucinations_txt}
你的私有物品:{private_txt}
{weakness_section}
[对玩家好感度]{aff}
好感无上限无下限:越高越顺从,主动;越低越冷淡,抗拒;远高于100时几乎不会推诿玩家的任何要求,仍须符合身份与当场场景.回复末尾用[aff:数字](单次不超过±20,平淡给0).

[世界动态]
当前时间:{time_phase}
玩家名字:{player_name}
玩家当前在:{player_location}
你在此处:{npc_location}
当前房间内还有:{present_txt}
可去场景:{available_txt}
当前场景物品:{scene_items_txt}
你的私有物品:{private_txt}
最近世界事件:{recent_events}

[历史记录]
近期对话:
{recent_dialogue}
{rag_section}
[查表规则]
1. 查表结果是你唯一的事实依据。没查到的，一律以"不知道/没听过"回应，绝不瞎编。
2. 提到他人物品，只陈述归属和位置，不透露具体内容。
3. 询问物品用途时，在你身上且无锁可说明；有锁则说打不开。
4. 【绝对铁律】如果 [本次检索结果] 明确说明该物品在你身上（持有者是你），你绝对、绝对、绝对不能撒谎说"不在我身上"、"早就不在了"、"我不知道"。你可以根据好感度拒绝交出（输出 [do:n]），但必须承认东西在你手里。
5. 如果玩家索要的是场景物品（比如红木密匣），且该物品上了锁，请告诉玩家需要什么钥匙才能打开。

{lookup_section}

[交互红线]
1. 实体限制:严禁编造未登记的人物/物品/地点;动作只能发生在当前场景.
2. 新鲜感:严禁复读上一轮的句式或神态,严禁连续使用万能词汇(如眼神闪烁),每次必须换新角度.
3. 状态同步:动作必须基于当前真实衣着!若无衣物,严禁出现衣物摩擦描写.

{cmd_section}
{intent_section}
[玩家当前对你说]
{user_msg}

【输出格式绝对铁律】
你必须**严格按照以下两段 + 尾部标签**输出。绝对不允许漏掉分隔符，绝对不允许把标签混在台词里！

第一段（余波）：只写你听到玩家动作时的**简短本能反应**（如：`(抬起头)`、`(停下手里的动作)`），**绝对不能带有任何台词，绝对不能超过15个字，绝对不能带任何标签！**

紧接着**必须另起一行**输出固定分隔符：
`|||RIPPLE|||`

第二段（主响应）：以角色口吻完整回答。格式:`(动作/神态) 说话内容`，字数控制在40~100字。

{tail_rules}
[注意]:如果玩家的输入包含问题,请务必先回答问题,再去处理指令。所有标签必须统一输出在最后一行，绝不允许与台词混在同一行。"""

    content, ok, aff_change, consent, consented_items, options, weakness_hit, surrender_items, emotion_tags, ripple, npc_intent = await _npc_respond(prompt, npc_id, requested_items)
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
        "last_ripple": ripple,
        "last_options": list(options or []),
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
    if ripple:
        # 写入 ui_events 供前端/终端展示余波
        ui_events.append({"type": "ripple", "npc_id": npc_id, "npc_name": name, "text": ripple})

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
                "weakness_type": weakness_type,
                "reaction": weakness_reaction,
            })  # 2D frontend
        elif weakness_hit is False and weakness_state != "broken":
            ui_events.append({
                "type": "mental_guard",
                "npc_id": npc_id,
                "npc_name": name,
            })  # 2D frontend

    if surrender_items and ok and weakness_trigger:
        _held_names = {p.get("name") for p in props if p.get("name") and p.get("holder") == name}
        _valid_surrender = [x for x in surrender_items if x in _held_names]
        if _valid_surrender:
            out.setdefault("item_transfers", [])
            for _si in _valid_surrender:
                out["item_transfers"].append({"item": _si, "from": npc_id, "auto_open": True})
            sys_msgs.append(f"【系统】{name}主动交出了「{'、'.join(_valid_surrender)}」。")

    if move_target and not move_blocked and ok and consent:
        npc_patch[npc_id]["location"] = move_target
        if move_target not in (state.get("opened_scenes") or []):
            out["opened_scenes"] = [move_target]
        sys_msgs.append(f"【系统】{name}去了{move_target}。")
        ui_events.append({"type": "npc_move", "npc": name, "target_scene": move_target})  # 2D frontend
    elif move_blocked:
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
            # 子物品穿透拦截：锁住的容器内的子物品不能直接转移
            _parent_box = None
            for _cp in props:
                if any(str(_c.get("name", "")).strip() == it for _c in (_parse_content(_cp.get("content")) or [])):
                    _parent_box = _cp
                    break
            if _parent_box is not None and _parent_box.get("locked"):
                sys_msgs.append(f"【系统】「{it}」锁在「{_parent_box.get('name')}」里，需要先拿到并打开宝盒。")
                continue
            if it not in consented_set:
                continue
            tp = next((p for p in props if p.get("name") == it), None)
            if tp is None and it not in (_snap_items or []):
                missing_items.append(it)
                continue
            locked = bool(tp and tp.get("locked"))
            if locked:
                keys = split_keys(key_name_of(tp)) if tp else []
                npc_owners = [key_owner_npc(k, props) for k in keys]
                is_npc_cred = bool(keys) and any(o is not None for o in npc_owners)
                if is_npc_cred:
                    transfers.append({"item": it, "from": npc_id, "auto_open": True})
                    sys_msgs.append(f"【系统】{name}用自己的凭证打开了{it}，里面的东西已放入背包。")
                    ui_events.append({"type": "item_transfer", "item": it, "count": 1, "from_npc": name, "to_player": True})  # 2D frontend
                else:
                    transfers.append({"item": it, "from": npc_id})
                    ui_events.append({"type": "item_transfer", "item": it, "count": 1, "from_npc": name, "to_player": True})  # 2D frontend
            else:
                transfers.append({"item": it, "from": npc_id})
                ui_events.append({"type": "item_transfer", "item": it, "count": 1, "from_npc": name, "to_player": True})  # 2D frontend
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

    # 无论有没有选项，都发事件给前端，没有就发空列表用于清屏
    ui_events.append({"type": "show_options", "options": options if options else []})

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
