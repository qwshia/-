"""导演提示节点（hint_node）：玩家迷茫时主动求助才调用，不进历史。

- 你掌握完整的主线目标（ending_goal）与底层真相（global_truth），据此判断玩家当前最需要做什么；
- 给足：玩家/NPC 对话（排除系统移动/调查消息）、全部场景描述、全部物品表（锁箱不显示内容）、
  玩家已掌握线索、当前场景；
- 提示自由生成、点到为止：绝不直接说真相/答案/物品用途，重要容器只暗示"想办法打开"。
"""
from langchain_core.messages import HumanMessage

from config import actor_llm
from state import AgentState


async def hint_node(state: AgentState) -> dict:
    scene = state.get("current_scene", "未知")
    history = state.get("history", []) or []
    npcs = state.get("npcs") or {}
    scenes = state.get("scenes") or {}
    props = state.get("props") or []
    items = state.get("player_items") or []
    clues = state.get("player_revealed_clues") or []
    ending_goal = state.get("ending_goal", "")
    global_truth = state.get("global_truth", "")

    dialog = []
    for e in history:
        sp = e.get("speaker")
        if sp == "system":
            continue
        content = e.get("content", "")
        if content == "（对方垂下眼帘，没有说话。）":
            continue
        if sp == "user":
            dialog.append(f"玩家：{content}")
        else:
            dialog.append(f"{(npcs.get(sp) or {}).get('name', sp)}：{content}")
    dialog_txt = "\n".join(dialog[-10:]) or "（暂无）"

    scene_txt = "\n".join(f"{i}. {n}：{d.get('desc', '')}"
                          for i, (n, d) in enumerate(scenes.items(), 1)) or "（无）"

    desc_map = state.get("item_desc_map") or {}
    owned = set(items) | {p.get("name") for p in props if p.get("name")}
    item_lines = []
    for n, d in scenes.items():
        for it in (d.get("items", []) if isinstance(d.get("items"), list) else []):
            if not (isinstance(it, dict) and it.get("name")):
                continue
            if it["name"] in owned:
                continue  # 已获得，见"玩家持有"
            item_lines.append(f"- {it['name']}（{n}，调查可得）：{it.get('desc', '用途不明')}")
    for p in props:
        if not p.get("name"):
            continue
        item_lines.append(f"- {p['name']}（在{p.get('location', '?')}，{p.get('holder', '?')}持有）：{p.get('desc', '用途不明')}")
    if items:
        holder_txt = "、".join(f"{i}（{desc_map.get(i) or '用途不明'}）" for i in items)
        item_lines.append(f"- 玩家持有：{holder_txt}")
    item_txt = "\n".join(item_lines) or "（无）"

    clues_txt = "\n".join(f"- {c}" for c in clues) or "（无）"

    prompt = (
        "你是导演,玩家感到迷茫,向你求助.掌握主线目标与底层真相(后台),提示只能点到为止.\n"
        "1. 自由输出2-4句提示:结合处境,线索,场景,物品,判断最需要做什么(找谁追问,调查哪里,验证疑点,想办法打开某物),不局限于形式.\n"
        "2. 不直接说出真相/答案/物品用途;容器可暗示'那里面似乎有东西,想办法打开',不说里面是什么.\n"
        "3. 线索足够时暗示可整理思路,用'结局是:推理'下结论.\n"
        "4. 只提示[场景一览][物品一览][玩家已掌握线索]里真实存在的东西,严禁提示列表外地点/物品/人物/机关.\n"
        "5. 提示中物品名必须来自[物品一览];场景环境描述除非已登记为可获取物品,不得写进提示.\n\n"
        f"[主线目标]{ending_goal}\n\n"
        f"[底层真相(后台,不透露)]{global_truth}\n\n"
        f"[当前场景]{scene}\n\n"
        f"[玩家已掌握线索]\n{clues_txt}\n\n"
        f"[场景一览]\n{scene_txt}\n\n"
        f"[物品一览]\n{item_txt}\n\n"
        f"[近期对话]\n{dialog_txt}\n\n"
        "直接输出提示文字,不要 JSON."
    )

    resp = await actor_llm.ainvoke([HumanMessage(content=prompt)])
    return {"history": [{"speaker": "system", "content": resp.content}]}
