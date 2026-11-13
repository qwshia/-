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
from rag_manager import search_script_summary, add_script_summary
from langchain_core.runnables import RunnableConfig


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


async def world_genesis(state: AgentState, config: RunnableConfig = None) -> dict:
    task = state.get("task", "")
    rag_reference = ""
    try:
        similar_script = await search_script_summary(task, top_k=1)
        if similar_script:
            rag_reference = f"\n[过往剧本参考(仅供参考，不要照抄)]\n{similar_script}\n"
            print("[导演提示] 成功检索到相似剧本，已注入参考。")
    except Exception as e:
        print(f"[剧本检索失败] {e}")
    _PROMPT_TEMPLATE = """你是互动叙事导演.请按玩家要求直接生成剧本世界.不要输出任何思考过程、开场白或解释,必须且只能直接输出纯净的 JSON 格式数据,连 markdown 的 ```json 标记都不要加.
玩家要求:{task}
{rag_reference}
[总原则]
1. 先解析玩家要求中的题材,关系,氛围,节奏,边界和禁用项;风格要适配,不套固定模板.聊天氛围可让 NPC 对话承载线索,推理氛围增加物证链,情感氛围围绕关系秘密,但都必须满足"玩家可实际获得关键信息".
2. 可推演性优先:global_truth 的每个关键事实,必须在 truth_leads 有玩家可执行的获取路径,并在场景 desc,scenes.items,props,initial_items 或明确对话条件中留下可读/可听/可取得的内容.只存在 global_truth,NPC secrets,裸物品名中的事实视为无效.
3. 逻辑常理:人物动机,时间,位置,物品归属,获取链必须自洽;NPC 不得说出未登记的事实;谎言必须留下可验证矛盾点,且不得把未登记专名,地址,类型,时间当作真相.
4. 规模按玩家要求(玩家要求优先),题材复杂度,推理链长度和交互方式决定,不设固定场景/NPC/truth_leads数量;以支撑推理,多线和可操作落点为准,避免冗余,无落点线索和为空而空.
5. 物品与名字唯一性:物品只能登记在 scenes.items、props 或 initial_items 之一中;场景名、物品名、NPC名必须全局唯一,严禁跨类别重名(如物品叫"客厅").

[必须字段]
backstory:200-300字开场白,交代玩家身份,处境,为何在此,结尾给1~2句开局可尝试的方向.不得把未登记的可疑细节写成诱导性伪线索.
player_name:玩家全名,简洁,与NPC名区分(有新意).
ending_goal:玩家看到的目标,只写要达成什么,不写判定方法,不泄露答案.
global_truth:玩家需推理出的完整事实,后台判定用,不向玩家明示;若题材天然多结局,各线路胜负条件融入其中,不向玩家明示.玩家要求"时间不限"时,不写"必须在某时前"作为硬胜负条件.
truth_leads:真相拼图数组,每项 {fact, via}.fact 写确定事实,不写"可能";via 写玩家获取路径.
initial_scene:开局场景名,须在 scenes[].name 中.
scenes:场景数组,每项 {name, desc},可选 requires_key / items;desc 写可感知环境细节并埋线索,但关键可调查物必须登记到 items 或 props.
npcs:NPC 数组,每项 {id,name,role,personality,appearance,attire,location,affection?};name 必须唯一,不得重名;id 必须严格为 npc_1、npc_2、npc_3… 的格式(以 npc_ 开头紧跟从 1 开始的连续整数),不得使用语义化命名(如 npc_xiaoxi、char_lin 等),不得重复.
  NPC 必须是活人;死者不登记为 NPC,尸体/遗物作为场景物品或 props 登记.
  attire:必须是 JSON 对象(字典),键名只能包含(如:内搭、外套、鞋履、头饰)。严禁包含配饰、首饰、挂件！值代表具体的衣物名称.严禁输出一长串带逗号的字符串.;location:所在场景(必须是 scenes[].name 中的值);
  personality:一句话定位(如"酒馆里混了十年的老江湖"),不是性格特质列表,特质列表由后续角色铸造环节生成;
  affection:初始好感度(可选,整数,默认100,根据玩家与 NPC 关系设定,关系好更高,关系差更低).

[可选字段]
initial_items:开局道具数组 {name,desc},只放玩家开局已持有或可立即使用的物品,不得把场景中 NPC 物,他人物品塞进来.
scenes.requires_key:场景准入凭证,值必须是已登记物品的具体 name,多凭证写 "A+B",禁泛称(虹膜/指纹/密码/门禁卡/钥匙).
scenes.items:场景调查道具 {name,desc,locked?,key_name?,content?};收无主可调查物,信息载体,容器或场景设施.关键可调查物必须在此登记,不能只在场景 desc 里提一句.key_name 必须是已登记物品具体 name,禁泛称.content数组如果放长文本,必须是 {"name": "名字", "text": "具体内容"} 格式.
props:NPC 持有物品表 {name,holder,location,carried,desc,locked?,key_name?,content?};holder 必须且只能是 npcs[].name 中的名字,严禁填玩家名字.违反此条的 props 条目将被系统丢弃并报警.holder 为 npcs[].name;carried 布尔,true 时 location 必等于 holder.location,false 时 location 为该物实际所在场景,可与 该NPC 不同.key_name 必须具体 name;生物凭证(指纹/虹膜/密码)也实体化为 props 一条:name:"X的指纹"、holder:X、carried:true、desc 写"开启Y的唯一凭证,仅X本人可开";不再重复登记到 scenes.items/initial_items,不可转移,只能本人同意开.content数组如果放长文本,必须是 {"name": "名字", "text": "具体内容"} 格式.
time_start/time_limit/time_turns:有时限才填,否则留空(""/""/0);时段用清晨/上午/正午/午后/黄昏/夜晚/深夜/凌晨.玩家说时间不限时,三项留空/0;剧情内可写 NPC 的计划时间作为线索,但不得作为硬性超时胜负.
timed_events:时间事件数组(无 time_limit 则必须输出 []),每项 {id, at_phase, scene, new_desc, new_desc_failed, condition, effects, hint}。
at_phase 须在 time_start 之后;scene 须在 scenes 中且每场景限 1 个事件;生成 1~3 个,必须与 truth_leads 关联。
new_desc:条件成立时的全量新描述;new_desc_failed:条件不成立时的全量新描述(若留空则事件跳过,场景不变)。
condition 结构:{npc_id, at_scene, has_items},不依赖 NPC 时 npc_id 填空字符串,不依赖物品时 has_items 填 []。
effects 数组(可为空):
  - {"type":"npc_move","npc_id":"npc_1","to":"场景"}
  - {"type":"item_move","item":"物名","to":"npc:NPC名 或 scene:场景名"}
hint:1 句玩家视角的模糊感知。

[剧本可推演性硬规则]
1. truth_leads 每条 via 必须写成玩家可执行路径,包含:地点;对象(必须已登记于 scenes.items / props / initial_items,或场景 desc 中有足够具体的可感知细节,或明确 NPC 对话条件);动作(调查/打开/询问/出示/展示/比对/触发对话等);玩家可读到,听到或取得什么;推出对应 fact.不得只写后台事实或未登记载体.
2. 若 truth_leads 或 global_truth 依赖某条信息,该信息必须在已登记对象的 desc/content 中可读,或在明确对话条件中可听;不得只存在于 global_truth,NPC secrets,裸物品名中.仅作为实体凭证,交付物,准入物使用的物品,不要求写详细内容,但必须有可识别特征,取得方式和用途落点.
3. content 必须是 JSON 数组(列表).数组元素只能是两种:字符串(普通子物品名)或对象 {"name": "...", "text": "..."}(文本道具).禁止把 content 写成字符串,禁止嵌套数组.同一个物品名不得同时出现在 content 子物品和 props 顶层(二选一).
   严禁将长段文字(含冒号、引号的长句)直接作为字符串塞入 content 数组,防止被当作物品名.
   content 条目按实际用途写清,不强制所有 content 都写可读文本:
   - 凭证型:写名称/识别特征/用途或可交付对象,不要求写详细内容;
   - 信息型:写关键可读原文或足够精确的摘要,玩家能据此推出事实;
   - 容器型:写已登记的子物,避免与子物重复登记.
   若 content 中的信息被 truth_leads 或 global_truth 使用,两边细节必须一致;不得把未登记信息写成玩家可读内容.
4. locked:false 表示可直接调查/打开/取得,可有 content,表示直接可见/可得内容,不得写 key_name.无需钥匙必须 locked:false,禁止用 key_name 写"直接拉开/无需钥匙"这类占位;locked:true 的 key_name 必须具体 name.
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
12. [防门禁死锁]：场景的 requires_key 必须是实际存在的物品（含NPC的生物凭证），且该物品必须有明确的获取途径（在场景中可调查，或在NPC身上可索要）。严禁出现“开启该门禁的钥匙，本身被锁在门禁内部”的套娃死锁。
13. [道具唯一性]：同一个物品绝对不能在 props（NPC持有）和 scenes.items（场景调查物）中同时出现，杜绝状态冲突。
14. [穿着与物品绝对隔离]：严禁同一个名词同时出现在 npc.attire 和 props/scenes.items 中。如果某件配饰（如红宝石发卡）是需要玩家获取的物品，那么该 NPC 的 attire 中绝对不能写出这个名字，并把该发卡作为独立物品登记在 props 中。凡是玩家能拿走的，绝对不能穿在身上当衣服。
[输出格式]严格输出 JSON,无其他文字.必须格式化输出:每个字段单独一行,缩进 2 空格,不要压缩到一行.示例结构:
{
  "backstory": "",
  "player_name": "",
  "ending_goal": "",
  "global_truth": "",
  "truth_leads": [{"fact": "", "via": ""}],
  "initial_scene": "",
  "initial_items": [{"name": "", "desc": ""}],
  "scenes": [{"name": "", "desc": "", "requires_key": "A+B", "items": [{"name": "", "desc": "", "locked": true, "key_name": "", "content": ["子物品名", {"name": "文本道具名", "text": "文本内容"}]}]}],
  "props": [{"name": "", "holder": "", "location": "", "carried": true, "desc": "", "locked": true, "key_name": "", "content": [""]}],
  "time_start": "",
  "time_limit": "",
  "time_turns": 0,
  "timed_events": [{"id": "evt_1", "at_phase": "黄昏", "scene": "", "new_desc": "", "new_desc_failed": "", "condition": {"npc_id": "npc_1", "at_scene": "", "has_items": []}, "effects": [{"type": "npc_move", "npc_id": "npc_1", "to": ""}, {"type": "item_move", "item": "", "to": "scene:"}], "hint": ""}],
  "npcs": [{"id": "npc_1", "name": "", "role": "", "personality": "", "appearance": "", "attire": {}, "location": "", "affection": 100}]
}"""
    prompt = _PROMPT_TEMPLATE.replace("{task}", task).replace("{rag_reference}", rag_reference)
    _raw_output_for_debug = ""
    try:
        response = await world_llm.ainvoke([HumanMessage(content=prompt)])
        _raw_output_for_debug = response.content if isinstance(response.content, str) else str(response.content)
        parsed = _parse_json(response.content)
        if not parsed:
            print(f"\n[模型输出解析失败] 原始输出类型: {type(response.content)}")
            print(f"原始输出长度: {len(_raw_output_for_debug)} 字符")
            print(f"原始输出尾部 300 字符: {_raw_output_for_debug[-300:]}\n", file=sys.stderr)
    except Exception as e:
        import traceback
        print(f"[导演生成失败] {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        traceback.print_exc()
        parsed = {}

    if not parsed:
        import os as _os
        _debug_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "debug_world_output.txt")
        try:
            with open(_debug_path, "w", encoding="utf-8") as _f:
                _f.write(_raw_output_for_debug)
            print(f"[导演调试] 原始输出已写入 {_debug_path}", file=sys.stderr)
        except Exception as _e:
            print(f"[导演调试] 写调试文件失败: {_e}", file=sys.stderr)
        raise RuntimeError(
            "[world_genesis] 导演输出 JSON 解析失败。原始输出已写入 debug_world_output.txt，"
            "请检查末尾是否被截断（可能是 max_tokens 不足或输出超限）。"
        )

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
        raw_attire = item.get("attire", {})
        if isinstance(raw_attire, str):
            s = raw_attire.strip()
            raw_attire = {"全装": s} if s and s not in ("", "无") else {}
        elif not isinstance(raw_attire, dict):
            raw_attire = {}
        attire = {}
        for k, v in (raw_attire or {}).items():
            if v not in (None, ""):
                attire[str(k)] = str(v)
        if "配饰" in attire:
            del attire["配饰"]
        npcs[npc_id] = {
            "name": item.get("name", npc_id),
            "role": item.get("role", "路人"),
            "personality": item.get("personality", "普通"),
            "appearance": item.get("appearance", "普通"),
            "attire": attire if attire else {"全装": "常服"},
            "location": _match_scene(item.get("location"), scene_names, init_scene),
            "action": item.get("action", "发呆"),
            "affection": _safe_aff(item.get("affection", 100)),
            "memory": {"l1": [], "l2": None, "l3": None},
            "emotion_tags": [],
            "pending_intent": [],
            "life_events": [],
            "current_action": {"action": "", "since_tick": 0},
            "round_count": 0,
        }
    if not npcs:
        raise RuntimeError(
            "[world_genesis] 导演输出中 npcs 为空。剧本生成失败，请重新生成。"
        )

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

    # === id 重编号兜底：强制 npc_1 / npc_2 / ... 标准格式 ===
    _old_to_new = {}
    _new_npcs = {}
    for _idx, (_old_id, _info) in enumerate(list(npcs.items()), 1):
        _new_id = f"npc_{_idx}"
        _old_to_new[_old_id] = _new_id
        _new_npcs[_new_id] = _info
        if _old_id != _new_id:
            print(f"[导演校验] NPC id '{_old_id}' 非标准格式，已重编号为 '{_new_id}'", file=sys.stderr)
    npcs = _new_npcs

    # 同步替换 timed_events 里引用的旧 id（condition.npc_id / effects[].npc_id / effects[].to 的 npc: 前缀）
    if isinstance(parsed.get("timed_events"), list):
        for _ev in parsed["timed_events"]:
            if not isinstance(_ev, dict):
                continue
            _cond = _ev.get("condition")
            if isinstance(_cond, dict) and _cond.get("npc_id") in _old_to_new:
                _cond["npc_id"] = _old_to_new[_cond["npc_id"]]
            _effects = _ev.get("effects")
            if isinstance(_effects, list):
                for _ef in _effects:
                    if not isinstance(_ef, dict):
                        continue
                    if _ef.get("type") == "npc_move" and _ef.get("npc_id") in _old_to_new:
                        _ef["npc_id"] = _old_to_new[_ef["npc_id"]]
                    elif _ef.get("type") == "item_move":
                        _to = _ef.get("to", "")
                        if isinstance(_to, str) and _to.startswith("npc:"):
                            _old_owner = _to[4:].strip()
                            if _old_owner in _old_to_new:
                                _ef["to"] = "npc:" + _old_to_new[_old_owner]

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
    if any(w in task for w in _NO_TIME_WORDS) and time_limit:
        print("[导演校验] 玩家要求'时间不限'，强制清空时限。", file=sys.stderr)
        time_limit, time_turns = "", 0
        
    # 兜底：如果没有开局时间，默认“清晨”，让世界时钟转起来
    if not time_start:
        time_start = "清晨"
    initial_raw = parsed.get("initial_items", []) if isinstance(parsed.get("initial_items"), list) else []
    _npc_names_set = {info.get("name") for info in npcs.values()}
    _p_name = parsed.get("player_name", "")
    props = []
    for p in parsed.get("props", []) if isinstance(parsed.get("props"), list) else []:
        if isinstance(p, dict) and p.get("name"):
            if p.get("holder") and p["holder"] not in _npc_names_set:
                if p["holder"] == _p_name:
                    print(f"[导演校验] props「{p.get('name')}」holder=玩家名，已转进背包", file=sys.stderr)
                    initial_raw.append({"name": p.get("name"), "desc": p.get("desc", "")})
                    continue
                else:
                    print(f"[导演校验] props「{p.get('name')}」holder「{p['holder']}」不存在，已转为无主场景物品", file=sys.stderr)
                    p["holder"] = ""
                    p["carried"] = False
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

    initial_names = []
    initial_items = []
    for it in initial_raw:
        if isinstance(it, str) and it.strip():
            nm = it.strip()
            initial_names.append(nm)
            initial_items.append({"name": nm, "desc": "", "count": 1})
        elif isinstance(it, dict) and it.get("name"):
            nm = str(it["name"]).strip()
            desc = str(it.get("desc", "") or "").strip()
            initial_names.append(nm)
            initial_items.append({"name": nm, "desc": desc, "count": 1})

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
                    if not item_desc_map.get(c["name"]):
                        item_desc_map[c["name"]] = ""
    for p in props:
        if p.get("name"):
            d = p.get("desc", "")
            if not p.get("locked"):
                texts = [c.get("text", "") for c in _parse_content(p.get("content")) if c.get("text")]
                if texts:
                    d = (d + "；可读：" + " / ".join(texts)).strip("；")
            item_desc_map.setdefault(p["name"], d)
        for c in _parse_content(p.get("content")):
            if not item_desc_map.get(c["name"]):
                item_desc_map[c["name"]] = ""
    for it in initial_raw:
        if isinstance(it, dict) and it.get("name"):
            item_desc_map[it["name"]] = it.get("desc", item_desc_map.get(it["name"], ""))

    # content 格式兜底：非 list 转成单元素数组
    for _sdef in list(scenes.values()) + [{"items": props}]:
        for _it in (_sdef.get("items") or []):
            if isinstance(_it, dict) and "content" in _it and _it["content"] is not None:
                if not isinstance(_it["content"], list):
                    print(f"[导演校验] 「{_it.get('name')}」content 不是数组，已转成单元素数组", file=sys.stderr)
                    _it["content"] = [_it["content"]]

    # 保护容器 content 中的子物品：收集所有容器 content 中的子物品名
    _container_sub_names = set()
    def _collect_container_subs(items_list):
        for it in (items_list or []):
            if isinstance(it, dict) and isinstance(it.get("content"), list):
                for c in it["content"]:
                    cn = c.get("name") if isinstance(c, dict) else c
                    cn = cn.strip() if isinstance(cn, str) else str(cn or "").strip()
                    if cn:
                        _container_sub_names.add(cn)
    for sdef in scenes.values():
        _collect_container_subs(sdef.get("items"))
    _collect_container_subs(props)

    def _dup_name(a, b):
        a = a.strip() if isinstance(a, str) else str(a or "").strip()
        b = b.strip() if isinstance(b, str) else str(b or "").strip()
        return bool(a and b and (a in b or b in a))

    # 缓存被剔除的 props 的 desc（容器子物品），供 desc_map 初始化使用
    _removed_props_desc = {}
    for _p in props:
        if isinstance(_p, dict) and _p.get("name"):
            _nm = str(_p["name"]).strip()
            if _nm and any(_dup_name(_nm, s) for s in _container_sub_names):
                _d = _p.get("desc", "")
                if not _p.get("locked"):
                    _texts = [c.get("text", "") for c in _parse_content(_p.get("content")) if c.get("text")]
                    if _texts:
                        _d = (_d + "；可读：" + " / ".join(_texts)).strip("；")
                _removed_props_desc[_nm] = _d

    # 容器子物品若被 LLM 误登记为独立 props，移除 props 中的重复顶层物品，保留容器内容
    props = [p for p in props if not (isinstance(p, dict) and any(_dup_name(str(p.get("name", "")), s) for s in _container_sub_names))]

    # 把缓存的被剔除 props 的 desc 合并到 item_desc_map
    for _nm, _d in _removed_props_desc.items():
        if _d and not item_desc_map.get(_nm):
            item_desc_map[_nm] = _d

    _prop_names = {p.get("name") for p in props if isinstance(p, dict) and p.get("name")}

    # 清洗冲突：NPC随身携带的物品，绝不能出现在场景中（仅顶层场景物品；容器 content 子物品受保护）
    for sdef in scenes.values():
        _new_items = []
        for it in (sdef.get("items") or []):
            if not isinstance(it, dict):
                _new_items.append(it)
                continue
            _hit_name = str(it.get("name", "") or "").strip()
            if any(_pn and _dup_name(_pn, _hit_name) for _pn in _prop_names):
                continue
            _new_items.append(it)
        sdef["items"] = _new_items

    # 清洗冲突：如果是容器的子物品，绝不能同时作为场景顶层物品出现（去重，保留容器内的）
    for sdef in scenes.values():
        _new_items = []
        for it in (sdef.get("items") or []):
            if not isinstance(it, dict):
                _new_items.append(it)
                continue
            _hit_name = str(it.get("name", "") or "").strip()
            # 如果这个名字在容器子物品里，且它自己不是容器（没有content），就踢掉它
            if _hit_name in _container_sub_names and not it.get("content"):
                print(f"[导演校验] 场景物品「{_hit_name}」与容器子物品重名，已从顶层移除", file=sys.stderr)
                continue
            _new_items.append(it)
        sdef["items"] = _new_items

    npc_private_items = {}
    for nid, info in npcs.items():
        npc_private_items[nid] = [p.get("name") for p in props if p.get("holder") == info.get("name") and p.get("name")]
    # 容器内子物品：若容器在 NPC 身上，子物品显示为在容器内部，不独立显示在 NPC 身上
    for p in props:
        if not (isinstance(p, dict) and p.get("content")):
            continue
        holder = p.get("holder")
        carried = bool(p.get("carried"))
        on_npc = carried and holder and holder not in ("玩家", "玩家身上")
        if not on_npc:
            continue
        cname = p.get("name")
        for c in _parse_content(p.get("content")):
            sub = str(c.get("name", "") or "").strip() if isinstance(c, dict) else str(c or "").strip()
            if not sub:
                continue
            for q in props:
                if q.get("name") == sub and q.get("holder") == holder:
                    q["container"] = cname
            if not item_desc_map.get(sub):
                item_desc_map[sub] = f"在「{cname}」内部"

    # ========== timed_events 解析 + 硬校验 ==========
    from utils import TIME_PHASES as _TP
    _TIME_PHASES_SET = set(_TP)
    _npc_id_by_name = {info.get("name", ""): nid for nid, info in npcs.items()}
    _prop_by_name = {p.get("name"): p for p in props if isinstance(p, dict) and p.get("name")}

    def _phase_forward_dist(_start: str, _target: str) -> int:
        """_start → _target 的环形向前距离；任一非法返回 -1"""
        if _start not in _TP or _target not in _TP:
            return -1
        _n = len(_TP)
        _si = _TP.index(_start)
        _ti = _TP.index(_target)
        return (_ti - _si) % _n

    _timed_events_raw = parsed.get("timed_events", []) if isinstance(parsed.get("timed_events"), list) else []
    timed_events = []

    if not time_limit:
        if _timed_events_raw:
            print("[导演校验] time_limit 未设置，timed_events 已清空", file=sys.stderr)
        _timed_events_raw = []

    _total_span = _phase_forward_dist(time_start, time_limit) if (time_start and time_limit) else -1
    _seen_event_scenes = set()
    _evt_idx = 0
    for _ev in _timed_events_raw:
        if not isinstance(_ev, dict):
            continue
        _at = str(_ev.get("at_phase", "") or "").strip()
        _scene = str(_ev.get("scene", "") or "").strip()
        _new_desc = str(_ev.get("new_desc", "") or "").strip()
        _new_desc_failed = str(_ev.get("new_desc_failed", "") or "").strip()
        _hint = str(_ev.get("hint", "") or "").strip()
        _cond_raw = _ev.get("condition", {}) if isinstance(_ev.get("condition"), dict) else {}
        _effects_raw = _ev.get("effects", []) if isinstance(_ev.get("effects"), list) else []

        if _at not in _TIME_PHASES_SET:
            print(f"[导演校验] timed_event at_phase='{_at}' 非法，已丢弃", file=sys.stderr)
            continue
        _dist = _phase_forward_dist(time_start, _at)
        if _dist <= 0 or (_total_span > 0 and _dist > _total_span):
            print(f"[导演校验] timed_event at_phase='{_at}' 不在 {time_start}~{time_limit} 区间，已丢弃", file=sys.stderr)
            continue
        if _scene not in scenes:
            print(f"[导演校验] timed_event scene='{_scene}' 不在 scenes 中，已丢弃", file=sys.stderr)
            continue
        if _scene in _seen_event_scenes:
            print(f"[导演校验] 场景 '{_scene}' 重复出现在多个 timed_events，仅保留首个，本次丢弃", file=sys.stderr)
            continue
        if not _new_desc or not _hint:
            print(f"[导演校验] timed_event id={_ev.get('id', '?')} 缺少 new_desc 或 hint，已丢弃", file=sys.stderr)
            continue

        # condition 校验
        _cond_npc_id = str(_cond_raw.get("npc_id", "") or "").strip()
        _cond_at_scene = str(_cond_raw.get("at_scene", "") or "").strip()
        _cond_has_items = _cond_raw.get("has_items", []) if isinstance(_cond_raw.get("has_items"), list) else []
        _cond_has_items = [str(x).strip() for x in _cond_has_items if str(x).strip()]

        _cond_ok = True
        if _cond_npc_id:
            if _cond_npc_id not in npcs:
                print(f"[导演校验] timed_event condition.npc_id='{_cond_npc_id}' 不存在，已丢弃", file=sys.stderr)
                _cond_ok = False
            elif _cond_at_scene and _cond_at_scene not in scenes:
                print(f"[导演校验] timed_event condition.at_scene='{_cond_at_scene}' 不在 scenes 中，已丢弃", file=sys.stderr)
                _cond_ok = False
            elif _cond_has_items:
                _all_known_names = set()
                for _p in props:
                    if isinstance(_p, dict) and _p.get("name"):
                        _all_known_names.add(str(_p["name"]).strip())
                for _it0 in initial_raw:
                    if isinstance(_it0, dict) and _it0.get("name"):
                        _all_known_names.add(str(_it0["name"]).strip())
                for _sdef0 in scenes.values():
                    for _it0 in (_sdef0.get("items") or []):
                        if isinstance(_it0, dict) and _it0.get("name"):
                            _all_known_names.add(str(_it0["name"]).strip())
                _all_known_names |= _container_sub_names
                _missing = [it for it in _cond_has_items if it not in _all_known_names]
                if _missing:
                    print(f"[导演校验] timed_event condition.has_items 中 {_missing} 不在剧本任何物品中，已丢弃", file=sys.stderr)
                    _cond_ok = False
        elif _cond_has_items:
            print(f"[导演校验] timed_event condition.has_items 非空但缺 npc_id，已丢弃", file=sys.stderr)
            _cond_ok = False

        if not _cond_ok:
            continue

        _valid_effects = []
        for _ef in _effects_raw:
            if not isinstance(_ef, dict):
                continue
            _etype = str(_ef.get("type", "") or "").strip()
            if _etype == "npc_move":
                _nid = str(_ef.get("npc_id", "") or "").strip()
                _to = str(_ef.get("to", "") or "").strip()
                if _nid not in npcs:
                    print(f"[导演校验] npc_move npc_id='{_nid}' 不存在，跳过", file=sys.stderr)
                    continue
                if _to not in scenes:
                    print(f"[导演校验] npc_move to='{_to}' 不在 scenes 中，跳过", file=sys.stderr)
                    continue
                _valid_effects.append({"type": "npc_move", "npc_id": _nid, "to": _to})
            elif _etype == "item_move":
                _item = str(_ef.get("item", "") or "").strip()
                _to = str(_ef.get("to", "") or "").strip()
                if _item not in _prop_by_name:
                    print(f"[导演校验] item_move 物品 '{_item}' 不在 props 中，跳过", file=sys.stderr)
                    continue
                if not (_to.startswith("npc:") or _to.startswith("scene:")):
                    print(f"[导演校验] item_move to='{_to}' 格式非法，跳过", file=sys.stderr)
                    continue
                if _to.startswith("npc:"):
                    _to_owner = _to[4:].strip()
                    if _to_owner not in _npc_id_by_name:
                        print(f"[导演校验] item_move to='{_to}' 的 NPC 不存在，跳过", file=sys.stderr)
                        continue
                elif _to.startswith("scene:"):
                    _to_scene = _to[6:].strip()
                    if _to_scene not in scenes:
                        print(f"[导演校验] item_move to='{_to}' 的场景不存在，跳过", file=sys.stderr)
                        continue
                _valid_effects.append({"type": "item_move", "item": _item, "to": _to})
            else:
                print(f"[导演校验] timed_event effect type='{_etype}' 不支持，跳过", file=sys.stderr)

        _evt_idx += 1
        _evt_id = str(_ev.get("id", "") or "").strip() or f"evt_{_evt_idx}"
        timed_events.append({
            "id": _evt_id,
            "at_phase": _at,
            "scene": _scene,
            "new_desc": _new_desc,
            "new_desc_failed": _new_desc_failed,
            "condition": {
                "npc_id": _cond_npc_id,
                "at_scene": _cond_at_scene,
                "has_items": _cond_has_items,
            },
            "effects": _valid_effects,
            "hint": _hint,
        })
        _seen_event_scenes.add(_scene)

    timed_events.sort(key=lambda e: _phase_forward_dist(time_start, e.get("at_phase", "")))

    # ==================== 新增：写入 RAG 剧本摘要 ====================
    try:
        thread_id = "default"
        if config and isinstance(config, dict):
            thread_id = config.get("configurable", {}).get("thread_id", "default")
            
        summary_text = f"玩家要求：{task}\n背景：{parsed.get('backstory', '')}\n真相：{parsed.get('global_truth', '')}"
        add_script_summary(thread_id, task, summary_text)
    except Exception as e:
        print(f"[RAG剧本写入失败] {e}")
    # ================================================================

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
        "timed_events": timed_events,
        "fired_time_events": [],
        "current_scene": init_scene,
        "time_phase": time_start,
        "time_progress": 0.0,
        "world_tick": 0,
        "player_items": initial_items,
        "player_attire": str(parsed.get("player_attire", "") or "").strip() or "普通衣着",
        "player_intent": {},
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
