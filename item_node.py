"""道具状态机节点：item_node —— 确定性处理物品给予/开箱/偷窃。"""
import re

from config import actor_llm
from langchain_core.messages import HumanMessage
from state import AgentState
from utils import _parse_content
from item_utils import can_acquire, split_keys, player_has_key, key_name_of, key_owner_npc

_STEAL_RE = re.compile(r"(?:偷|窃取|顺走|拿走)(?:\s*([\u4e00-\u9fa5A-Za-z0-9（）()]{1,12}))?")

def is_steal_cmd(msg: str) -> bool:
    return msg.lstrip().startswith(("!", "！")) and bool(_STEAL_RE.search(msg.lstrip().lstrip("!！")))

async def item_node(state: AgentState) -> dict:
    transfers = state.get("item_transfers") or []
    scene = state.get("current_scene", "")
    player_items = list(state.get("player_items") or [])
    props = [dict(p) for p in (state.get("props") or [])]
    desc_map = dict(state.get("item_desc_map") or {})
    done = list(state.get("processed_transfers") or [])
    events = []
    sys_msgs = []

    def _land(name, tp, mode):
        parsed = _parse_content(tp.get("content"))
        desc = str(tp.get("desc", "") or "").strip()
        locked = bool(tp.get("locked"))
        keys = split_keys(key_name_of(tp)) if locked else []
        got_names = [c["name"] for c in parsed]
        npc_owners = [key_owner_npc(k, props) for k in keys] if keys else []
        is_npc_cred = locked and bool(keys) and any(o is not None for o in npc_owners)
        content_present = bool(tp.get("content"))

        if content_present:
            if locked and is_npc_cred:
                if mode == "transfer":
                    sys_msgs.append(f"「{name}」该物品需 NPC 本人打开")
                    return
                if mode == "steal":
                    sys_msgs.append("这东西被生物锁保护，你拿不走。")
                    return
            if locked and mode != "auto_open":
                if name not in player_items:
                    player_items.append(name)
                desc_map[name] = f"{desc}（锁着，需要{key_name_of(tp)}）"
                desc_map[f"__content__:{name}"] = ",".join(got_names)
                props[:] = [p for p in props if p.get("name") != name]
                if mode == "transfer":
                    events.append(f"{name}到了玩家手中")
                return

            # 如果是 auto_open，或者是无锁，都会走到这里，直接展开
            if name not in player_items:
                player_items.append(name)
            desc_map[name] = f"{desc}（已开启）"
            for c in parsed:
                cn = c["name"]
                if cn not in player_items:
                    player_items.append(cn)
                if cn not in desc_map:
                    desc_map[cn] = ""
                if c.get("text"):
                    desc_map[cn] = (desc_map.get(cn, "") + "；" + c["text"]).strip("；")
            props[:] = [p for p in props if p.get("name") != name]
            if mode in ("transfer", "auto_open"):
                events.append(f"{name}到了玩家手中")
            return
            # 无锁
            if name not in player_items:
                player_items.append(name)
            desc_map[name] = f"{desc}（已开启）"
            for c in parsed:
                cn = c["name"]
                if cn not in player_items:
                    player_items.append(cn)
                if cn not in desc_map:
                    desc_map[cn] = ""
                if c.get("text"):
                    desc_map[cn] = (desc_map.get(cn, "") + "；" + c["text"]).strip("；")
            props[:] = [p for p in props if p.get("name") != name]
            if mode == "transfer":
                events.append(f"{name}到了玩家手中")
            return
        # 无 content
        if name not in player_items:
            player_items.append(name)
        desc_map[name] = desc
        props[:] = [p for p in props if p.get("name") != name]
        if mode == "transfer":
            events.append(f"{name}到了玩家手中")
        return

    avail = set(state.get("available_scenes") or [])
    for tr in transfers:
        name = tr.get("item")
        sig = f"{tr.get('from')}::{name}"
        if not name or sig in done:
            continue
        done.append(sig)
        tp = next((p for p in props if p.get("name") == name), None)
        if tp is None:
            continue
        loc = (tp or {}).get("location", "") or ""
        if loc in avail and loc != scene:
            sys_msgs.append(f"「{name}」在{loc}，NPC没法在这里给你——需要先到{loc}，再让她转交。")
            continue
        _mode = "auto_open" if tr.get("auto_open") else "transfer"
        _land(name, tp, _mode)

    msg = state.get("user_msg", "")

    steal_attempts = dict(state.get("steal_attempts") or {})
    if is_steal_cmd(msg):
        steal_target = None
        for p in props:
            if p.get("name") and p.get("name") in msg:
                steal_target = p.get("name")
                break
        if not steal_target:
            for p in props:
                if p.get("holder") not in ("玩家", "玩家身上", "") and p.get("name"):
                    steal_target = p.get("name")
                    break
        if not steal_target:
            return {
                "history": [{"speaker": "system", "content": "【系统】这里没什么可偷的东西。"}],
                "processed_transfers": done,
                "steal_attempts": steal_attempts,
            }
        tp = next((p for p in props if p.get("name") == steal_target), None)
        holder = (tp or {}).get("holder", "")
        holder_npc = next((nid for nid, info in (state.get("npcs") or {}).items() if info.get("name") == holder), None)
        holder_info = (state.get("npcs") or {}).get(holder_npc, {}) if holder_npc else {}
        aff = int(holder_info.get("affection", 50))
        attempts = steal_attempts.get(steal_target, 0) + 1
        steal_attempts[steal_target] = attempts
        PITY = 5
        if attempts >= PITY:
            success = True
        else:
            import random
            base = 0.2 + aff / 500.0
            base = min(0.7, max(0.05, base))
            success = random.random() < base

        if success:
            tp = next((p for p in props if p.get("name") == steal_target), None)
            _is_bio = False
            if tp and tp.get("locked"):
                _keys = split_keys(tp.get("key_name", ""))
                if any(key_owner_npc(k, props) is not None for k in _keys):
                    _is_bio = True

            if _is_bio:
                sys_msgs.append(f"【系统】你试图偷取「{steal_target}」，但它是生物凭证锁，你拿不走。")
                steal_attempts[steal_target] = 0
                out = {
                    "player_items": player_items,
                    "props": props,
                    "item_desc_map": desc_map,
                    "processed_transfers": done,
                    "steal_attempts": steal_attempts,
                }
                if sys_msgs:
                    out["history"] = [{"speaker": "system", "content": "【系统】" + "\n".join(sys_msgs)}]
                return out

            sys_msgs.append(f"【系统】你成功偷到了「{steal_target}」，它已进入背包。")
            if tp is not None:
                _land(steal_target, tp, "steal")
            steal_attempts[steal_target] = 0
            out = {
                "player_items": player_items,
                "props": props,
                "item_desc_map": desc_map,
                "processed_transfers": done,
                "steal_attempts": steal_attempts,
            }
            if sys_msgs:
                out["history"] = [{"speaker": "system", "content": "【系统】" + "\n".join(sys_msgs)}]
            return out
        else:
            penalty = 15 if aff < 50 else 8 if aff < 80 else 3
            npcs_patch = {}
            if holder_npc:
                npcs_patch = {holder_npc: {"affection": max(-100, int(holder_info.get("affection", 50)) - penalty)}}
            sys_msgs.append(f"【系统】偷窃「{steal_target}」失败，{holder}似乎有所察觉。")
            events_out = [f"玩家偷窃{steal_target}失败，{holder}产生警觉"]
            try:
                r = await actor_llm.ainvoke([HumanMessage(content=f"你是{holder}，性格{holder_info.get('personality','')}，对玩家好感度{aff}。玩家试图偷你的{steal_target}但失败了，用一句话反应（40字内），好感低时更愤怒。")])
                npc_reply = (r.content or "").strip()
            except Exception:
                npc_reply = "（没有说话。）"
            return {
                "player_items": player_items,
                "props": props,
                "item_desc_map": desc_map,
                "processed_transfers": done,
                "steal_attempts": steal_attempts,
                "npcs": npcs_patch,
                "history": [
                    {"speaker": "user", "content": msg},
                    {"speaker": holder_npc or "system", "content": npc_reply},
                    {"speaker": "system", "content": "【系统】" + "\n".join(sys_msgs)},
                ],
                "world_events": events_out,
                "analysis_counter": int(state.get("analysis_counter") or 0) + 1,
                "run_story": True,
            }

    out = {
        "player_items": player_items,
        "props": props,
        "item_desc_map": desc_map,
        "processed_transfers": done,
        "steal_attempts": steal_attempts,
    }
    if events:
        out["world_events"] = events
    if sys_msgs:
        out["history"] = [{"speaker": "system", "content": m} for m in sys_msgs]
    return out