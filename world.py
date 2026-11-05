"""世界引擎节点：world_genesis —— 导演按玩家要求一次生成整个剧本世界。

输出：backstory / global_truth / ending_goal / scenes / npcs 实体库
（固定字段），并兜底"开局场景至少一个 NPC 在场"。

结局机制：不设剧情节点。导演设计真相（global_truth）与结局判定标准（ending_goal），
玩家通过调查与对话形成推理，输入"结局是：推理"由 judge_ending 对比判定。
"""
import sys
from typing import Dict

from langchain_core.messages import HumanMessage

from config import world_llm
from state import AgentState
from utils import _parse_json, _parse_content


def _safe_aff(v):
    try:
        return int(v)
    except Exception:
        return 100


def _match_scene(loc, scene_names, fallback):
    if not loc:
        return fallback
    if loc in scene_names:
        return loc
    for s in scene_names:
        if loc in s or s in loc:
            return s
    return fallback


def _validate_truth_leads(truth_leads, scene_names, npcs, props, item_names) -> list:
    warnings = []
    npc_names = {info.get("name") for info in (npcs or {}).values()}
    prop_names = {p.get("name") for p in (props or [])}
    entities = set(scene_names) | npc_names | prop_names | set(item_names)
    for lead in truth_leads or []:
        if not isinstance(lead, dict):
            continue
        fact, via = str(lead.get("fact", "")), str(lead.get("via", ""))
        if not via:
            warnings.append(f"真相拼图「{fact}」缺少获取途径")
        elif not any(e and e in via for e in entities):
            warnings.append(f"真相拼图「{fact}」的途径可能不指向任何真实场景/NPC/道具：{via}")
    return warnings


async def world_genesis(state: AgentState) -> dict:
    task = state.get("task", "")
    _PROMPT_TEMPLATE = """你是互动叙事导演。请按玩家要求直接生成剧本世界。不要输出任何思考过程、开场白或解释，必须且只能直接输出纯净的 JSON 格式数据，连 markdown 的 ```json 标记都不要加。
玩家要求:{task}

[总原则]
1. 先解析玩家要求中的题材,关系,氛围,节奏,边界和禁用项;风格要适配,不套固定模板.聊天氛围可让 NPC 对话承载线索,推理氛围增加物证链,情感氛围围绕关系秘密,但都必须满足"玩家可实际获得关键信息".
2. 可推演性优先:global_truth 的每个关键事实,必须在 truth_leads 有玩家可执行的获取路径,并在场景 desc,scenes.items,props,initial_items 或明确对话条件中留下可读/可听/可取得的内容.只存在 global_truth,NPC secrets,裸物品名中的事实视为无效.
3. 逻辑常理:人物动机,时间,位置,物品归属,获取链必须自洽;NPC 不得说出未登记的事实;谎言必须留下可验证矛盾点,且不得把未登记专名,地址,类型,时间当作真相.
4. 规模按玩家要求(玩家要求优先),题材复杂度,推理链长度和交互方式决定,不设固定场景/NPC/truth_leads数量;以支撑推理,多线和可操作落点为准,避免冗余,无落点线索和为空而空.
5. 物品与名字唯一性：物品只能登记在 scenes.items、props 或 initial_items 之一中；场景名、物品名、NPC名必须全局唯一，严禁跨类别重名（如物品叫"客厅"）。

[必须字段]
backstory:200-300字开场白,交代玩家身份,处境,为何在此,结尾给1~2句开局可尝试的方向.不得把未登记的可疑细节写成诱导性伪线索.
player_name:玩家全名,简洁,与NPC名区分(有新意).
ending_goal:玩家看到的目标,只写要达成什么,不写判定方法,不泄露答案.
global_truth:玩家需推理出的完整事实,后台判定用,不向玩家明示;若题材天然多结局,各线路胜负条件融入其中,不向玩家明示.玩家要求"时间不限"时,不写"必须在某时前"作为硬胜负条件.
truth_leads:真相拼图数组,每项 {fact, via}.fact 写确定事实,不写"可能";via 写玩家获取路径.
initial_scene:开局场景名,须在 scenes[].name 中.
scenes:场景数组,每项 {name, desc},可选 requires_key / items;desc 写可感知环境细节并埋线索,但关键可调查物必须登记到 items 或 props.
npcs:NPC 数组,每项 {id,name,role,personality,appearance,attire,location,goal,secrets,forbidden,hallucinations?,affection?};name 必须唯一，不得重名.
  NPC 必须是活人;死者不登记为 NPC,尸体/遗物作为场景物品或 props 登记.
  attire:衣着(详细描述,当前所穿,可随剧情变化,只能是服装,没有其他多余补充);location:所在场景(必须是 scenes[].name 中的值);
  goal:真实目的;secrets:确知的事实(数组);forbidden:隐瞒的事(数组);
  hallucinations:错误认知(可选,无则省略或 []);
  affection:初始好感度(可选,整数,默认100,根据玩家与 NPC 关系设定,关系好更高,关系差更低).

[可选字段]
initial_items:开局道具数组 {name,desc},只放玩家开局已持有或可立即使用的物品,不得把场景中 NPC 物,他人物品塞进来.
scenes.requires_key:场景准入凭证,值必须是已登记物品的具体 name,多凭证写 "A+B",禁泛称（虹膜/指纹/密码/门禁卡/钥匙）.
scenes.items:场景调查道具 {name,desc,locked?,key_name?,content?};收无主可调查物,信息载体,容器或场景设施.关键可调查物必须在此登记,不能只在场景 desc 里提一句.key_name 必须是已登记物品具体 name，禁泛称.content数组如果放长文本，必须是 {"name": "名字", "text": "具体内容"} 格式。
props:NPC 持有物品表 {name,holder,location,carried,desc,locked?,key_name?,content?};holder 必须且只能是 npcs[].name 中的名字，严禁填玩家名字。holder 为 npcs[].name;carried 布尔,true 时 location 必等于 holder.location,false 时 location 为该物实际所在场景,可与 该NPC 不同.key_name 必须具体 name；生物凭证（指纹/虹膜/密码）也实体化为 props 一条：name:"X的指纹"、holder:X、carried:true、desc 写"开启Y的唯一凭证，仅X本人可开"；不再重复登记到 scenes.items/initial_items，不可转移，只能本人同意开.content数组如果放长文本，必须是 {"name": "名字", "text": "具体内容"} 格式。
time_start/time_limit/time_turns:有时限才填,否则留空(""/""/0);时段用清晨/上午/正午/午后/黄昏/夜晚/深夜/凌晨.玩家说时间不限时,三项留空/0;剧情内可写 NPC 的计划时间作为线索,但不得作为硬性超时胜负.

[剧本可推演性硬规则]
1. truth_leads 每条 via 必须写成玩家可执行路径,包含:地点;对象(必须已登记于 scenes.items / props / initial_items,或场景 desc 中有足够具体的可感知细节,或明确 NPC 对话条件);动作(调查/打开/询问/出示/展示/比对/触发对话等);玩家可读到,听到或取得什么;推出对应 fact.不得只写后台事实或未登记载体.
2. 若 truth_leads 或 global_truth 依赖某条信息,该信息必须在已登记对象的 desc/content 中可读,或在明确对话条件中可听;不得只存在于 global_truth,NPC secrets,裸物品名中.仅作为实体凭证,交付物,准入物使用的物品,不要求写详细内容,但必须有可识别特征,取得方式和用途落点.
3. content 数组的元素必须按以下两种格式之一书写:
   - 普通子物品：直接写物品名字符串（如 "黄铜钥匙"）.
   - 文本型道具（信件、字条、日记内容等）：必须写成对象 {"name": "字条", "text": "字条上的具体内容"}.
   严禁将长段文字（含冒号、引号的长句）直接作为字符串塞入 content 数组，防止被当作物品名.
   content 条目按实际用途写清,不强制所有 content 都写可读文本:
   - 凭证型:写名称/识别特征/用途或可交付对象,不要求写详细内容;
   - 信息型:写关键可读原文或足够精确的摘要,玩家能据此推出事实;
   - 容器型:写已登记的子物,避免与子物重复登记.
   若 content 中的信息被 truth_leads 或 global_truth 使用,两边细节必须一致;不得把未登记信息写成玩家可读内容.
4. locked:false 表示可直接调查/打开/取得,可有 content,表示直接可见/可得内容,不得写 key_name.无需钥匙必须 locked:false，禁止用 key_name 写"直接拉开/无需钥匙"这类占位；locked:true 的 key_name 必须具体 name.
   locked:true 必须写 key_name,content 为开锁后可得;钥匙必须在门禁外有明确获取途径,不得循环,不得只存在于该门禁场景内部.
5. 影响胜负,关键分支或核心判断的机制,条件,可操作动作和结果,必须在场景 desc,物品 desc,content 可读信息或明确 NPC 对话条件中有玩家可接触的落点;不得只存 global_truth.落点形式按题材和风格决定,不固定为某类物,某类话术或某类动作.
6. ending_goal 中凡需做某件事,场景或物品中必须有对应可操作落点:相关物品可拿取且可展示/提交/使用,相关 NPC 可接触,相关门禁有凭证链,相关设备可操作.不能让玩家知道要干却无处下手.
7. 对话线索必须写清:NPC,场景,触发条件,玩家出示或询问什么,NPC 会透露的关键信息;该信息须在 NPC secrets 中有对应事实.NPC 可以撒谎,但谎言只能制造可验证矛盾,不得引入未登记关键专名作为真相.
8. [最高优先级硬规则,任何题材,风格,规模,模型下不得违反]不设伪线索链.任何可疑细节,误导性细节或氛围描写,必须与 truth_leads 相关,或明确为不诱导核心推理的氛围描写;不得让玩家把核心推理引向未登记或与 global_truth 无关的方向.
truth_leads.fact 必须是确定,可判定事实,不得用"可能""也许""疑似"等不确定表述作为关键事实.此条忘掉任何其他规则也不能违反.
9. NPC secrets 中提及的具名物品,必须已登记于 props 或 scenes.items;secrets 不能替代玩家可见落点.
10. 初始场景中至少有一个可调查对象或可对话推进点;关键证据不得只放在初始场景 desc 的氛围句里.
11. 输出前内部自检,不输出自检内容:逐条 truth_leads 问"玩家在哪个场景,点哪个已登记对象,读到/听到/取得什么,推出什么";答不出就重写.再检查每个 ending_goal 动作是否有可操作落点,每个关键证据是否可拿取/可查看/可展示/可触发.
NPC 有关的地点必须是 scenes[].name 中已登记的场景名,不得发明新地点.
[输出格式]严格输出 JSON,无其他文字.必须格式化输出:每个字段单独一行,缩进 2 空格,不要压缩到一行.示例结构:
{
  "backstory": "",
  "player_name": "",
  "ending_goal": "",
  "global_truth": "",
  "truth_leads": [{"fact": "", "via": ""}],
  "initial_scene": "",
  "initial_items": [{"name": "", "desc": ""}],
  "scenes": [{"name": "", "desc": "", "requires_key": "A+B", "items": [{"name": "", "desc": "", "locked": true, "key_name": "", "content": [""]}]}],
  "props": [{"name": "", "holder": "", "location": "", "carried": true, "desc": "", "locked": true, "key_name": "", "content": [""]}],
  "time_start": "",
  "time_limit": "",
  "time_turns": 0,
  "npcs": [{"id": "npc_1", "name": "", "role": "", "personality": "", "appearance": "", "attire": "", "location": "", "goal": "", "secrets": [""], "forbidden": [""], "hallucinations": [""]}]
}"""
    prompt = _PROMPT_TEMPLATE.replace("{task}", task)
    try:
        response = await world_llm.ainvoke([HumanMessage(content=prompt)])
        parsed = _parse_json(response.content)
    except Exception as e:
        print(f"[导演生成失败] {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        parsed = {}

    init_scene = parsed.get("initial_scene", "大厅")
    scene_names = [s for s in parsed.get("available_scenes", []) if isinstance(s, str)]
    if not scene_names:
        for s in (parsed.get("scenes", []) if isinstance(parsed.get("scenes"), list) else []):
            if isinstance(s, dict) and s.get("name"):
                scene_names.append(s["name"])
    if init_scene not in scene_names:
        scene_names.append(init_scene)
    if not scene_names:
        scene_names = [init_scene]

    npcs: Dict[str, dict] = {}
    for item in parsed.get("npcs", []) if isinstance(parsed.get("npcs"), list) else []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        npc_id = str(item["id"])
        npcs[npc_id] = {
            "name": item.get("name", npc_id),
            "role": item.get("role", "路人"),
            "personality": item.get("personality", "普通"),
            "appearance": item.get("appearance", "普通"),
            "attire": item.get("attire", "常服"),
            "location": _match_scene(item.get("location"), scene_names, init_scene),
            "action": item.get("action", "发呆"),
            "goal": item.get("goal", ""),
            "secrets": item.get("secrets", []),
            "forbidden": item.get("forbidden", []),
            "hallucinations": item.get("hallucinations", []),
            "affection": _safe_aff(item.get("affection", 100)),
            "memory": {"l1": [], "l2": None, "l3": None},
            "round_count": 0,
        }
    if not npcs:
        npcs = {
            "npc_1": {"name": "NPC甲", "role": "路人", "personality": "普通", "appearance": "普通",
                      "attire": "常服", "location": init_scene, "action": "发呆", "goal": "",
                      "secrets": [], "forbidden": [], "hallucinations": [],
                      "affection": 100, "memory": [], "round_count": 0},
            "npc_2": {"name": "NPC乙", "role": "路人", "personality": "普通", "appearance": "普通",
                      "attire": "常服", "location": init_scene, "action": "发呆", "goal": "",
                      "secrets": [], "forbidden": [], "hallucinations": [],
                      "affection": 100, "memory": [], "round_count": 0},
        }

    _seen_names = set()
    for nid, info in npcs.items():
        nm = str(info.get("name", "") or "").strip()
        if nm and nm in _seen_names:
            new_nm = nm + "_2"
            print(f"[导演校验] NPC 重名，已重命名为 {new_nm}", file=sys.stderr)
            info["name"] = new_nm
            _seen_names.add(new_nm)
        elif nm:
            _seen_names.add(nm)

    scenes: Dict[str, dict] = {}
    for s in parsed.get("scenes", []) if isinstance(parsed.get("scenes"), list) else []:
        if isinstance(s, dict) and s.get("name"):
            scenes[s["name"]] = {
                "desc": s.get("desc", ""),
                "items": s.get("items", []),
                "requires_key": s.get("requires_key", ""),
            }

    if init_scene not in scenes:
        if scenes:
            init_scene = next(iter(scenes))
        else:
            scenes[init_scene] = {"desc": "", "items": [], "requires_key": ""}

    _NO_KEY_WORDS = ("拉链", "直接拉开", "拉开", "无需钥匙", "无钥匙", "直接打开", "直接掀开")
    for sdef in scenes.values():
        for it in (sdef.get("items") or []):
            if isinstance(it, dict) and it.get("locked"):
                kn = str(it.get("key_name", "") or "").strip()
                if not kn or any(w in kn for w in _NO_KEY_WORDS):
                    it["locked"] = False
                    it.pop("key_name", None)

    time_start = str(parsed.get("time_start", "") or "").strip()
    time_limit = str(parsed.get("time_limit", "") or "").strip()
    try:
        time_turns = max(0, int(parsed.get("time_turns", 0) or 0))
    except Exception:
        time_turns = 0
    _NO_TIME_WORDS = ("时间不限", "不限时", "不限时间", "没有时间限制", "无时间限制", "时间上没有限制", "不限定时限")
    if any(w in task for w in _NO_TIME_WORDS) and (time_start or time_limit or time_turns):
        print("[导演校验] 玩家要求'时间不限'，强制清空导演生成的时间系统。", file=sys.stderr)
        time_start, time_limit, time_turns = "", "", 0
    _npc_names_set = {info.get("name") for info in npcs.values()}
    _p_name = parsed.get("player_name", "")
    props = []
    for p in parsed.get("props", []) if isinstance(parsed.get("props"), list) else []:
        if isinstance(p, dict) and p.get("name"):
            if p.get("holder") and p["holder"] not in _npc_names_set:
                if p["holder"] == _p_name:
                    initial_raw.append({"name": p.get("name"), "desc": p.get("desc", "")})
                continue
            raw_loc = p.get("location") or ""
            _c = p.get("carried", False)
            carried = _c if isinstance(_c, bool) else str(_c).strip().lower() in ("true", "1", "yes", "是")
            prop = {
                "name": p.get("name"),
                "holder": p.get("holder") or "",
                "carried": carried,
                "location": _match_scene(raw_loc, scene_names, raw_loc),
                "desc": p.get("desc", ""),
            }
            if carried and prop["holder"]:
                hloc = None
                for nid, info in npcs.items():
                    if info.get("name") == prop["holder"]:
                        hloc = info.get("location")
                        break
                if hloc:
                    prop["location"] = hloc
            if isinstance(p.get("content"), list) and p.get("content"):
                prop["content"] = list(p["content"])
                prop["locked"] = bool(p.get("locked", False))
                kn = str(p.get("key_name", "") or "").strip()
                if kn:
                    prop["key_name"] = kn
            props.append(prop)

    for p in props:
        if not isinstance(p, dict) or not p.get("name") or not p.get("content"):
            continue
        if p.get("locked"):
            kn = str(p.get("key_name", "") or "").strip()
            if not kn or any(w in kn for w in _NO_KEY_WORDS):
                p["locked"] = False
                p.pop("key_name", None)

    initial_raw = parsed.get("initial_items", []) if isinstance(parsed.get("initial_items"), list) else []
    initial_names = []
    for it in initial_raw:
        if isinstance(it, str) and it.strip():
            initial_names.append(it.strip())
        elif isinstance(it, dict) and it.get("name"):
            initial_names.append(it["name"])

    truth_leads = parsed.get("truth_leads", []) if isinstance(parsed.get("truth_leads"), list) else []
    for _w in _validate_truth_leads(truth_leads, scene_names, npcs, props, initial_names):
        print(f"[真相可达性警告] {_w}", file=sys.stderr)

    item_desc_map = {}
    for sdef in scenes.values():
        for it in sdef.get("items", []) or []:
            if isinstance(it, dict) and it.get("name"):
                d = it.get("desc", "")
                # 无锁且 content 有文本时，把可读内容追加到 desc
                if not it.get("locked"):
                    texts = [c.get("text", "") for c in _parse_content(it.get("content")) if c.get("text")]
                    if texts:
                        d = (d + "；可读：" + " / ".join(texts)).strip("；")
                item_desc_map[it["name"]] = d
                for c in _parse_content(it.get("content")):
                    item_desc_map.setdefault(c["name"], "")
    for p in props:
        if p.get("name"):
            d = p.get("desc", "")
            if not p.get("locked"):
                texts = [c.get("text", "") for c in _parse_content(p.get("content")) if c.get("text")]
                if texts:
                    d = (d + "；可读：" + " / ".join(texts)).strip("；")
            item_desc_map.setdefault(p["name"], d)
        for c in _parse_content(p.get("content")):
            item_desc_map.setdefault(c["name"], "")
    for it in initial_raw:
        if isinstance(it, dict) and it.get("name"):
            item_desc_map[it["name"]] = it.get("desc", item_desc_map.get(it["name"], ""))

    npc_private_items = {}
    for nid, info in npcs.items():
        npc_private_items[nid] = [p.get("name") for p in props if p.get("holder") == info.get("name") and p.get("name")]

    # 清洗冲突：NPC随身携带的物品，绝不能出现在场景中
    _prop_names = {p.get("name") for p in props}
    for sdef in scenes.values():
        _new_items = []
        for it in (sdef.get("items") or []):
            _hit_name = it.get("name", "")
            if any(_pn and (_pn in _hit_name or _hit_name in _pn) for _pn in _prop_names if _pn):
                continue
            # 如果场景物品里有 content，也清洗一遍
            if isinstance(it.get("content"), list):
                _new_content = []
                for c in it["content"]:
                    _cname = c.get("name") if isinstance(c, dict) else c
                    _cname_hit = _cname if isinstance(_cname, str) else str(_cname or "")
                    if not any(_pn and (_pn in _cname_hit or _cname_hit in _pn) for _pn in _prop_names if _pn):
                        _new_content.append(c)
                it["content"] = _new_content
            _new_items.append(it)
        sdef["items"] = _new_items
    return {
        "backstory": parsed.get("backstory", "故事开始了。"),
        "player_name": str(parsed.get("player_name", "") or "").strip(),
        "ending_goal": parsed.get("ending_goal", "查明真相。"),
        "global_truth": parsed.get("global_truth", "真相未知。"),
        "truth_leads": parsed.get("truth_leads", []) if isinstance(parsed.get("truth_leads"), list) else [],
        "allowed_entities": parsed.get("allowed_entities", []),
        "available_scenes": scene_names,
        "scenes": scenes,
        "npcs": npcs,
        "props": props,
        "time_start": time_start,
        "time_limit": time_limit,
        "time_turns": time_turns,
        "current_scene": init_scene,
        "time_phase": time_start,
        "time_progress": 0.0,
        "player_items": initial_names,
        "player_attire": str(parsed.get("player_attire", "") or "").strip() or "普通衣着",
        "item_desc_map": item_desc_map,
        "item_transfers": [],
        "processed_transfers": [],
        "analysis_counter": 0,
        "last_clue_idx": 0,
        "player_revealed_clues": [],
        "world_events": [],
        "game_over": False,
        "p2p_transcript": "",
        "opened_scenes": [],
        "steal_attempts": {},
        "npc_private_items": npc_private_items,
        "run_story": False,
    }
