"""剧情判定节点（story_keeper）：统一线索提取。"""
import difflib
import re

from langchain_core.messages import HumanMessage

from config import judge_llm
from state import AgentState
from utils import _parse_json, is_scene_move_cmd


def _too_similar(new: str, existing: list) -> bool:
    def _chars(s: str) -> set:
        return set(re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", s))
    for old in existing:
        if new == old:
            return True
        if new in old or old in new:
            return True
        a, b = _chars(new), _chars(old)
        if a and b and (len(a & b) / len(a | b)) >= 0.6:
            return True
        if difflib.SequenceMatcher(None, new, old).ratio() >= 0.6:
            return True
    return False


def _dialog_window(history: list, start_idx: int, npcs: dict) -> tuple[str, bool]:
    lines = []
    has_npc = False
    for e in history[start_idx:]:
        sp = e.get("speaker")
        if sp == "system":
            continue
        content = e.get("content", "")
        if content == "（对方垂下眼帘，没有说话。）":
            continue
        if sp == "user":
            lines.append(f"玩家：{content}")
        else:
            nname = (npcs.get(sp) or {}).get("name", sp)
            lines.append(f"{nname}：{content}")
            has_npc = True
    return "\n".join(lines), has_npc


def _clues_txt(clues: list) -> str:
    return "\n".join(f"- {c}" for c in clues) or "（无）"


def _story_prompt(window: str, existing_clues: list, global_truth: str, ending_goal: str, recent_events: list) -> str:
    return (
        "你是剧情判定官。请从[近期对话]中提取新线索和世界事件。\n"
        "[核心铁律]：线索和事件必须与[主线目标]或[真相]直接相关！日常动作、寒暄、进门、身体接触（如牵手、拥抱）属于无意义交互，绝对不能算作线索。宁可输出空值，也绝不记录与结局无关的废话！\n"
        "player_revealed_clues:新线索,一句话,只写真正新增的已知信息(真相/身份/地点/物品用途/机制/约定),15字以内;\n"
        "world_event:客观已发生的行动事实——谁见了谁,谁去了哪,谁达成约定,只写和结局有关的事实.一句话,15字以内;\n"
        "查重:必须与[最近已知线索]比对,同义/包含/高度相似一律丢弃.\n"
        "参考结局真相(后台)以判断哪些信息与结局相关:\n"
        f"{global_truth}\n"
        f"主线目标:{ending_goal}\n\n"
        f"[最近已知线索]\n{_clues_txt(existing_clues[-6:])}\n\n"
        f"[最近已发生事件]\n{(';'.join(recent_events[-3:])) or '（无）'}\n\n"
        f"[近期对话]\n{window}\n\n"
        '只输出JSON:{"player_revealed_clues": [], "world_event": ""}'
    )


async def story_keeper(state: AgentState) -> dict:
    history = state.get("history", []) or []
    npcs = state.get("npcs") or {}
    counter = int(state.get("analysis_counter") or 0)
    last_idx = int(state.get("last_clue_idx") or 0)
    user_msg = state.get("user_msg", "")

    is_command = (
        user_msg.lstrip().startswith(("调查", "打开"))
        or is_scene_move_cmd(user_msg, state)
    )

    run = False
    window = ""
    if not is_command:
        if counter > 0 and counter % 6 == 0:
            window, has_npc = _dialog_window(history, last_idx, npcs)
            if has_npc and window:
                run = True

    out = {"analysis_counter": counter, "run_story": False}
    if not run:
        return out

    existing = list(state.get("player_revealed_clues") or [])
    prompt = _story_prompt(window, existing, state.get("global_truth", ""), state.get("ending_goal", ""), state.get("world_events") or [])
    try:
        resp = await judge_llm.ainvoke([HumanMessage(content=prompt)])
        parsed = _parse_json(resp.content)
    except Exception:
        parsed = {}

    raw_clues = parsed.get("player_revealed_clues", [])
    if not isinstance(raw_clues, list):
        raw_clues = []
    world_event = str(parsed.get("world_event", "") or "").strip()

    clues = [c.strip() for c in raw_clues
             if isinstance(c, str) and c.strip() and not _too_similar(c.strip(), list(existing))]
    if clues:
        out["player_revealed_clues"] = clues
    if world_event:
        out["world_events"] = [world_event]
    out["last_clue_idx"] = len(history)
    return out
