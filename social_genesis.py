"""社交网络生成节点：social_genesis

在 NPC 血肉生成之后运行，读取剧本大纲与 NPC 名单，生成初始社交网络：
- 关系边（含多种连接类型）
- 共识区域
- 阵营
- 恩怨 / 债务
- 舆论种子

秘密知情边由 social_network.init_secrets_from_npcs 确定性生成，不依赖 LLM。
"""
import json
import sys
from typing import Any, Dict, List

from langchain_core.messages import HumanMessage

from config import world_llm
from state import AgentState
from utils import _parse_json
from social_network import empty_social_graph, init_secrets_from_npcs


async def social_genesis(state: AgentState) -> dict:
    npcs = state.get("npcs") or {}
    if not npcs:
        return {}

    backstory = state.get("backstory", "")
    global_truth = state.get("global_truth", "")
    ending_goal = state.get("ending_goal", "")

    roster = []
    for nid, info in npcs.items():
        roster.append({
            "id": nid,
            "name": (info or {}).get("name", nid),
            "role": (info or {}).get("role", ""),
            "location": (info or {}).get("location", ""),
        })

    # 提取秘密摘要供 LLM 参考
    secret_lines = []
    for nid, info in npcs.items():
        for c in ((info or {}).get("cognition") or []):
            if isinstance(c, dict) and c.get("type") == "secret":
                secret_lines.append(f"- {nid}({(info or {}).get('name', nid)})：{c.get('content', '')}")

    prompt = f"""你是社交网络设计师.给定剧本与 NPC 名单,生成初始社交网络 JSON.

[剧本背景]
backstory: {backstory}
global_truth: {global_truth}
ending_goal: {ending_goal}

[NPC名单]
{json.dumps(roster, ensure_ascii=False, indent=2)}

[NPC秘密(参考)]
{chr(10).join(secret_lines) if secret_lines else "（无）"}

[任务]
生成初始社交网络,只输出严格 JSON,不要额外文字.字段:
- relationships: 数组,每项 {{"a":"npc_id","b":"npc_id","familiarity":0-1,"trust":0-1,"tension":0-1,"connection_types":["alliance"等],"note":"简短说明"}}
- factions: 数组,每项 {{"id":"faction_1","name":"阵营名","goal":"目标","members":["npc_id"],"allies":[],"hostiles":[]}}
- grudges: 数组,每项 {{"owner":"npc_id","target":"npc_id","reason":"原因","severity":0-1}}
- debts: 数组,每项 {{"owner":"npc_id","target":"npc_id","reason":"原因","severity":0-1}}
- consensus_regions: 数组,每项 {{"theme":"主题","members":["npc_id"],"level":0-1,"visibility":"secret/public/faction_only"}}
- rumors: 数组,每项 {{"content":"内容","source":"npc_id","credibility":0-1,"targets":["npc_id"]}}

[规则]
1. 只生成剧本中有依据的关系/阵营/恩怨/债务/共识/舆论，不要编造。
2. 没有依据的字段输出空数组。
3. npc_id 必须来自名单。
4. familiarity/trust/tension/severity/level/credibility 为 0-1 浮点。
5. connection_types 可选值:secret/shared_goal/common_enemy/alliance/rivalry/debt/grudge/kinship/info_channel/witness/consensus/faction。

[输出格式-严格JSON]
{{"relationships":[],"factions":[],"grudges":[],"debts":[],"consensus_regions":[],"rumors":[]}}"""

    try:
        response = await world_llm.ainvoke([HumanMessage(content=prompt)])
        parsed = _parse_json(response.content)
    except Exception as e:
        import traceback
        print(f"[social_genesis 生成失败] {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        traceback.print_exc()
        parsed = {}

    if not isinstance(parsed, dict):
        parsed = {}

    graph = empty_social_graph(npcs)
    graph = init_secrets_from_npcs(graph, npcs)

    # relationships
    for r in (parsed.get("relationships") or []):
        if not isinstance(r, dict):
            continue
        a = str(r.get("a", "") or "").strip()
        b = str(r.get("b", "") or "").strip()
        if a not in npcs or b not in npcs or a == b:
            continue
        from social_network import set_relationship, record_interaction
        graph = record_interaction(
            graph, a, b, tick=0,
            df=float(r.get("familiarity", 0.3)) - 0.1,
            dt=float(r.get("trust", 0.3)) - 0.2,
            dtension=float(r.get("tension", 0.0)),
            note=str(r.get("note", "") or "").strip(),
            connection_type=None,
        )
        ct = [str(x).strip() for x in (r.get("connection_types") or []) if str(x).strip()]
        if ct:
            graph = set_relationship(graph, a, b, connection_types=ct)
        # 覆盖初始值为 LLM 建议值
        graph = set_relationship(graph, a, b,
                                familiarity=float(r.get("familiarity", 0.3)),
                                trust=float(r.get("trust", 0.3)),
                                tension=float(r.get("tension", 0.0)))

    # factions
    for f in (parsed.get("factions") or []):
        if not isinstance(f, dict):
            continue
        fid = str(f.get("id", "") or "").strip()
        if not fid:
            fid = f"faction_{len(graph.get('factions', {})) + 1}"
        name = str(f.get("name", fid) or fid).strip()
        goal = str(f.get("goal", "") or "").strip()
        members = [str(x).strip() for x in (f.get("members") or []) if str(x).strip() in npcs]
        allies = [str(x).strip() for x in (f.get("allies") or []) if str(x).strip()]
        hostiles = [str(x).strip() for x in (f.get("hostiles") or []) if str(x).strip()]
        from social_network import add_faction
        graph = add_faction(graph, fid, name, goal=goal, members=members, allies=allies, hostiles=hostiles, tick=0)

    # grudges
    for g_item in (parsed.get("grudges") or []):
        if not isinstance(g_item, dict):
            continue
        owner = str(g_item.get("owner", "") or "").strip()
        target = str(g_item.get("target", "") or "").strip()
        if owner not in npcs or target not in npcs:
            continue
        from social_network import add_grudge
        graph = add_grudge(graph, owner, target, str(g_item.get("reason", "") or "").strip(),
                          severity=float(g_item.get("severity", 0.5)), tick=0)

    # debts
    for d_item in (parsed.get("debts") or []):
        if not isinstance(d_item, dict):
            continue
        owner = str(d_item.get("owner", "") or "").strip()
        target = str(d_item.get("target", "") or "").strip()
        if owner not in npcs or target not in npcs:
            continue
        from social_network import add_debt
        graph = add_debt(graph, owner, target, str(d_item.get("reason", "") or "").strip(),
                        severity=float(d_item.get("severity", 0.5)), tick=0)

    # consensus_regions
    for c in (parsed.get("consensus_regions") or []):
        if not isinstance(c, dict):
            continue
        theme = str(c.get("theme", "") or "").strip()
        if not theme:
            continue
        members = [str(x).strip() for x in (c.get("members") or []) if str(x).strip() in npcs]
        if not members:
            continue
        vis = str(c.get("visibility", "secret") or "secret").strip()
        if vis not in ("public", "secret", "faction_only"):
            vis = "secret"
        from social_network import add_consensus_region
        graph = add_consensus_region(graph, theme, members, tick=0, level=float(c.get("level", 0.5)), visibility=vis)

    # rumors
    for r in (parsed.get("rumors") or []):
        if not isinstance(r, dict):
            continue
        content = str(r.get("content", "") or "").strip()
        source = str(r.get("source", "") or "").strip()
        if not content or source not in npcs:
            continue
        targets = [str(x).strip() for x in (r.get("targets") or []) if str(x).strip() in npcs]
        from social_network import create_rumor
        graph, _ = create_rumor(graph, content, source, tick=0,
                               credibility=float(r.get("credibility", 0.5)),
                               rumor_type="fact", targets=targets)

    return {"social_graph": graph}