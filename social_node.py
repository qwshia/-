"""社交网络串行节点：social_node

消费 social_updates 指令，应用到 social_graph；
同时从高层状态自动提取秘密透露、p2p 交流、玩家催化等更新。
"""
from social_network import apply_social_updates, empty_social_graph, init_secrets_from_npcs
from state import AgentState


async def social_node(state: AgentState) -> dict:
    npcs = state.get("npcs") or {}
    graph = state.get("social_graph")
    if not isinstance(graph, dict):
        graph = init_secrets_from_npcs(empty_social_graph(npcs), npcs)

    tick = int(state.get("world_tick") or 0)
    processed = state.get("processed_social_updates") or []

    # 1) 消费手动更新指令
    graph, new_processed = apply_social_updates(
        graph, state.get("social_updates") or [], npcs=npcs, tick=tick, processed=processed
    )

    # 2) 从高层状态自动提取：秘密透露给玩家
    for r in (state.get("secrets_revealed") or []):
        if not isinstance(r, dict):
            continue
        npc_name = str(r.get("npc", "") or "").strip()
        fact = str(r.get("fact", "") or "").strip()
        nid = None
        for k, v in (npcs or {}).items():
            if (v or {}).get("name") == npc_name:
                nid = k
                break
        if nid and fact:
            from social_network import expand_known_by
            graph = expand_known_by(graph, nid, fact, "player", tick=tick)

    # 3) p2p 交流自动记录交互
    transcript = state.get("p2p_transcript") or ""
    bg = state.get("bg_chat_targets") or []
    if transcript and isinstance(bg, list) and len(bg) == 2:
        a, b = bg[0], bg[1]
        from social_network import record_interaction
        graph = record_interaction(graph, a, b, tick=tick, df=0.02, dt=0.01,
                                  note="p2p交流", interaction_type="p2p")

    # 4) 舆论衰减
    from social_network import decay_rumors
    graph = decay_rumors(graph, tick=tick)

    out = {"social_graph": graph}
    if new_processed:
        out["processed_social_updates"] = new_processed
    return out