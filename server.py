import asyncio
import hashlib
import json
import os
from pathlib import Path
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from graph import build_graph
from state import load_checkpointer
from rag_manager import add_script_summary
from scene_layout_genesis import ASSET_TAGS, WALL_TAGS, WALL_DECOR_TAGS, generate_layout
from debug_log import log_thread, read_thread, list_threads, log_snapshot

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkpoints.db")
LAYOUT_CACHE_DIR = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "剧本设计游戏" / "scene_layout_cache"
state = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时加载图
    cm = load_checkpointer(DB_PATH)
    checkpointer, ckpt_type = await cm.__aenter__()
    state["app"] = build_graph(checkpointer=checkpointer)
    state["cm"] = cm
    print(f"[OK] Backend started, using {ckpt_type} storage")
    yield
    # 关闭时释放
    await cm.__aexit__(None, None, None)

app = FastAPI(title="剧本游戏 API", lifespan=lifespan)

thread_history_len = {}
thread_ui_events_len = {}
thread_affection_snap = {}
thread_world_events_len = {}
thread_step = {}
scene_layout_locks = {}


def _clean_obstacle(obstacle: dict) -> dict:
    base_fields = {"asset_tag", "grid_x", "grid_y", "grid_w", "grid_h"}
    if not isinstance(obstacle, dict) or not base_fields.issubset(obstacle):
        raise RuntimeError("场景布局缓存格式错误：障碍物字段不完整")
    if any(type(obstacle.get(key)) is not int for key in ("grid_x", "grid_y", "grid_w", "grid_h")):
        raise RuntimeError("场景布局缓存格式错误：障碍物坐标和尺寸必须为整数")
    if obstacle["asset_tag"] not in ASSET_TAGS:
        raise RuntimeError("场景布局缓存格式错误：asset_tag 不在素材白名单内")
    if (
        obstacle["grid_x"] < 0
        or obstacle["grid_y"] < 0
        or obstacle["grid_w"] < 1
        or obstacle["grid_h"] < 1
        or obstacle["grid_x"] + obstacle["grid_w"] > 30
        or obstacle["grid_y"] + obstacle["grid_h"] > 17
    ):
        raise RuntimeError("场景布局缓存格式错误：障碍物坐标超出网格范围")

    is_door = obstacle.get("asset_tag") == "door"
    if is_door:
        return {key: obstacle[key] for key in ("asset_tag", "grid_x", "grid_y", "grid_w", "grid_h")}

    interact_item = obstacle.get("interact_item", "")
    if not isinstance(interact_item, str):
        raise RuntimeError("场景布局缓存格式错误：interact_item 必须是字符串")
    return {
        "asset_tag": obstacle["asset_tag"],
        "grid_x": obstacle["grid_x"],
        "grid_y": obstacle["grid_y"],
        "grid_w": obstacle["grid_w"],
        "grid_h": obstacle["grid_h"],
        "interact_item": interact_item,
        "lean_wall": bool(obstacle.get("lean_wall", False)),
    }


def _validate_scene_layout(layout: dict) -> dict:
    if not isinstance(layout, dict):
        raise RuntimeError("场景布局缓存格式错误：顶层必须是对象")
    bg_prompt = layout.get("bg_prompt")
    obstacles = layout.get("obstacles", [])
    items = layout.get("items", [])
    walls = layout.get("walls", [])
    if not isinstance(bg_prompt, str) or not isinstance(obstacles, list):
        raise RuntimeError("场景布局缓存格式错误：需要 bg_prompt 字符串和 obstacles 数组")
    if not isinstance(items, list):
        raise RuntimeError("场景布局缓存格式错误：items 必须是数组")
    if not isinstance(walls, list):
        raise RuntimeError("场景布局缓存格式错误：walls 必须是数组")
    # 兼容旧缓存：从 obstacles 中剥离 door
    if "door" not in layout and "doors" not in layout:
        door_candidates = [ob for ob in obstacles if isinstance(ob, dict) and ob.get("asset_tag") == "door"]
        obstacles = [ob for ob in obstacles if not (isinstance(ob, dict) and ob.get("asset_tag") == "door")]
        layout = dict(layout)
        layout["door"] = door_candidates[0] if door_candidates else None

    # 窗(window)等墙体附带品：从 obstacles 剥离到 wall_decor
    raw_wall_decor = layout.get("wall_decor", [])
    if not isinstance(raw_wall_decor, list):
        raw_wall_decor = []
    window_obs = [ob for ob in obstacles if isinstance(ob, dict) and ob.get("asset_tag") == "window"]
    obstacles = [ob for ob in obstacles if not (isinstance(ob, dict) and ob.get("asset_tag") == "window")]
    layout = dict(layout)
    layout["wall_decor"] = raw_wall_decor + window_obs

    cleaned_obstacles = []
    for obstacle in obstacles:
        if isinstance(obstacle, dict) and obstacle.get("asset_tag") == "wall":
            continue
        cleaned = _clean_obstacle(obstacle)
        if cleaned["asset_tag"] == "door":
            continue
        cleaned_obstacles.append(cleaned)

    # walls 清洗
    cleaned_walls = []
    for wall in walls:
        if not isinstance(wall, dict):
            continue
        required = {"grid_x", "grid_y", "grid_w", "grid_h"}
        if not required.issubset(wall):
            continue
        if any(type(wall.get(k)) is not int for k in ("grid_x", "grid_y", "grid_w", "grid_h")):
            continue
        if (
            wall["grid_x"] < 0 or wall["grid_y"] < 0 or wall["grid_w"] < 1 or wall["grid_h"] < 1
            or wall["grid_x"] + wall["grid_w"] > 30 or wall["grid_y"] + wall["grid_h"] > 17
        ):
            continue
        wall_tag = str(wall.get("asset_tag", "") or "").strip()
        if wall_tag not in WALL_TAGS:
            wall_tag = "wall_h"
        cleaned_walls.append({
            "grid_x": wall["grid_x"],
            "grid_y": wall["grid_y"],
            "grid_w": wall["grid_w"],
            "grid_h": wall["grid_h"],
            "asset_tag": wall_tag,
        })


    # 墙体附带品（窗户等）清洗
    decor_required = {"grid_x", "grid_y", "grid_w", "grid_h"}
    cleaned_wall_decor = []
    for d in layout.get("wall_decor", []):
        if not isinstance(d, dict) or not decor_required.issubset(d):
            continue
        tag = str(d.get("asset_tag", "") or "").strip()
        if tag not in WALL_DECOR_TAGS:
            continue
        if any(type(d.get(key)) is not int for key in ("grid_x", "grid_y", "grid_w", "grid_h")):
            continue
        if (
            d["grid_x"] < 0 or d["grid_y"] < 0 or d["grid_w"] < 1 or d["grid_h"] < 1
            or d["grid_x"] + d["grid_w"] > 30 or d["grid_y"] + d["grid_h"] > 17
        ):
            continue
        cleaned_wall_decor.append({
            "asset_tag": tag,
            "grid_x": d["grid_x"],
            "grid_y": d["grid_y"],
            "grid_w": d["grid_w"],
            "grid_h": d["grid_h"],
        })

    item_required = {"name", "grid_x", "grid_y", "grid_w", "grid_h"}
    cleaned_items = []
    for item in items:
        if not isinstance(item, dict) or not item_required.issubset(item):
            raise RuntimeError("场景布局缓存格式错误：物品字段不完整")
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            raise RuntimeError("场景布局缓存格式错误：物品 name 必须是非空字符串")
        if any(type(item.get(key)) is not int for key in ("grid_x", "grid_y", "grid_w", "grid_h")):
            raise RuntimeError("场景布局缓存格式错误：物品坐标和尺寸必须为整数")
        if (
            item["grid_x"] < 0
            or item["grid_y"] < 0
            or item["grid_w"] < 1
            or item["grid_h"] < 1
            or item["grid_x"] + item["grid_w"] > 30
            or item["grid_y"] + item["grid_h"] > 17
        ):
            raise RuntimeError("场景布局缓存格式错误：物品坐标超出网格范围")
        cleaned_items.append({
            "name": item["name"].strip(),
            "grid_x": item["grid_x"],
            "grid_y": item["grid_y"],
            "grid_w": item["grid_w"],
            "grid_h": item["grid_h"],
        })

    door_raw = layout.get("door", layout.get("doors"))
    if isinstance(door_raw, list):
        door_raw = door_raw[0] if door_raw else None
    cleaned_door = None
    if isinstance(door_raw, dict):
        cleaned = _clean_obstacle(door_raw)
        if cleaned["asset_tag"] != "door":
            cleaned["asset_tag"] = "door"
        cleaned_door = cleaned

    return {
        "bg_prompt": bg_prompt,
        "walls": cleaned_walls,
        "obstacles": cleaned_obstacles,
        "wall_decor": cleaned_wall_decor,
        "items": cleaned_items,
        "door": cleaned_door,
    }


async def _get_or_generate_scene_layout(thread_id: str, game_state: dict, scene_name: str) -> dict:
    if not scene_name:
        return {"bg_prompt": "", "obstacles": [], "items": [], "door": None}

    scenes = game_state.get("scenes") or {}
    scene_info = scenes.get(scene_name)
    if not isinstance(scene_info, dict):
        raise RuntimeError(f"当前场景「{scene_name}」未在 scenes 中登记，无法生成布局")

    cache_key = hashlib.sha256(f"{thread_id}\0{scene_name}".encode("utf-8")).hexdigest()
    cache_path = LAYOUT_CACHE_DIR / f"{cache_key}.json"
    cache_id = str(cache_path)
    lock = scene_layout_locks.get(cache_id)
    if lock is None:
        lock = scene_layout_locks.setdefault(cache_id, asyncio.Lock())
    async with lock:
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(f"读取场景「{scene_name}」布局缓存失败：{error}") from error
            return _validate_scene_layout(cached)

        items = []
        for item in (scene_info.get("items") or []):
            if (isinstance(item, dict) and item.get("name")) or (isinstance(item, str) and item.strip()):
                items.append(item)
        generated = _validate_scene_layout(await generate_layout(
            scene_name,
            str(scene_info.get("desc", "") or ""),
            items,
        ))
        temporary_path = cache_path.with_suffix(".tmp")
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(generated, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary_path.replace(cache_path)
        except OSError as error:
            if temporary_path.exists():
                temporary_path.unlink()
            raise RuntimeError(f"写入场景「{scene_name}」布局缓存失败：{error}") from error
        return generated

# 允许跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class NewGameReq(BaseModel):
    task: str
    thread_id: str
    player_name: str = ""
    style_profile: dict = {}

class ActReq(BaseModel):
    thread_id: str
    msg: str
    view: str = "full"

def _build_npc_view(state: dict, info: dict, view: str = "full") -> dict:
    """把 NPC 身上的 emotion_tags / 可偷物 / 可见物整理成前端消费的结构。"""
    _emotion_tags_raw = list(info.get("emotion_tags") or [])
    _emotion_tags_struct = []
    for _t in _emotion_tags_raw:
        if isinstance(_t, dict):
            _emotion_tags_struct.append({
                "target": str(_t.get("target", "玩家")),
                "tag": str(_t.get("tag", "")),
                "tick": int(_t.get("tick", 0))
            })
        else:
            # 兼容老格式字符串
            _s = str(_t or "").strip()
            if not _s:
                continue
            if "：" in _s:
                _target, _tag = _s.split("：", 1)
            else:
                _target, _tag = "玩家", _s
            _emotion_tags_struct.append({"target": _target.strip(), "tag": _tag.strip(), "tick": 0})

    _npc_name = info.get("name", "")
    _desc_map = state.get("item_desc_map", {}) or {}
    _stealable_items = []
    _visible_items = []
    for _p in (state.get("props") or []):
        if not (isinstance(_p, dict) and _p.get("holder") == _npc_name):
            continue
        _pname = str(_p.get("name", "") or "").strip()
        if not _pname:
            continue
        _carried = bool(_p.get("carried"))
        # NPC 随身物品前端不可被发现：一律不进可见/可偷列表。
        # 只有"落在场景里、由该 NPC 看管"的未随身物品才对外可见。
        if _carried:
            continue
        _pdesc = str(_desc_map.get(_pname, _p.get("desc", "")) or "")
        _entry = {"name": _pname, "desc": _pdesc}
        _locked = bool(_p.get("locked"))
        _in_container = bool(_p.get("container"))
        if view == "strict":
            # strict（偷窃/聊天过滤）只关心随身可偷物；随身已不可见，故恒空
            continue
        _stealable_items.append(dict(_entry))
        if not _in_container:
            _visible_items.append(dict(_entry))

    _pending_summary = []
    for pi in (info.get("pending_intent") or []):
        _pending_summary.append(f"{pi.get('kind')}:{pi.get('payload')}")

    return {
        "emotion_tags": _emotion_tags_struct,
        "stealable_items": _stealable_items,
        "visible_items": _visible_items,
        "has_pending": bool(info.get("pending_intent") or []),
        "pending_summary": _pending_summary,
        "current_intent": info.get("current_intent"),
    }


async def build_ui_state(thread_id: str, state: dict, view: str = "full") -> dict:
    # 1. 场景与时间
    current_scene = state.get("current_scene", "")
    time_phase = state.get("time_phase", "")
    current_scene_layout = await _get_or_generate_scene_layout(thread_id, state, current_scene)
    door_raw = current_scene_layout.get("door")
    if isinstance(door_raw, list):
        scene_doors = [ob for ob in door_raw if isinstance(ob, dict)]
    elif isinstance(door_raw, dict):
        scene_doors = [door_raw]
    else:
        scene_doors = [ob for ob in (current_scene_layout.get("obstacles") or []) if isinstance(ob, dict) and ob.get("asset_tag") == "door"]
    
    # 2. 背包提纯
    player_items = []
    desc_map = state.get("item_desc_map", {})
    for it in state.get("player_items", []):
        if isinstance(it, dict):
            name = it.get("name", "")
            raw_desc = desc_map.get(name, it.get("desc", ""))
            # 清洗重复拼接的子串
            if isinstance(raw_desc, str) and "；" in raw_desc:
                parts = [p.strip() for p in raw_desc.split("；") if p.strip()]
                seen = set()
                unique_parts = []
                for p in parts:
                    if p not in seen:
                        seen.add(p)
                        unique_parts.append(p)
                raw_desc = "；".join(unique_parts)
            
            player_items.append({
                "name": name,
                "count": it.get("count", 1),
                "desc": raw_desc,
                "locked": bool(it.get("locked")),
                "lock_mode": str(it.get("lock_mode", "") or "").strip(),
                "key_name": str(it.get("key_name", "") or "").strip(),
                "bio_holder": str(it.get("bio_holder", "") or "").strip(),
            })
            
    # 3. 场景结构体
    available_scenes = []
    scenes_dict = state.get("scenes", {})
    opened_scenes = state.get("opened_scenes", [])
    for s_name in state.get("available_scenes", []):
        sdef = scenes_dict.get(s_name, {})
        locked = bool(sdef.get("locked")) and (s_name not in opened_scenes)
        available_scenes.append({
            "name": s_name,
            "locked": locked,
            "lock_mode": sdef.get("lock_mode", ""),
            "key_name": sdef.get("key_name", ""),
            "bio_holder": sdef.get("bio_holder", ""),
            "lock_hint": unlock_hint(sdef) if locked else ""
        })
        
    # 4. 在场 NPC 与好感度差分
    present_npcs = []
    all_npcs = state.get("npcs", {})
    last_aff_snap = thread_affection_snap.get(thread_id, {})
    current_aff_snap = {}
    
    for nid, info in all_npcs.items():
        if info.get("location") == current_scene:
            aff = info.get("affection", 100)
            current_aff_snap[nid] = aff
            delta = aff - last_aff_snap.get(nid, aff) # 首次默认不变
            _w = info.get("weakness") if isinstance(info.get("weakness"), dict) else {}
            _npc_view = _build_npc_view(state, info, view=view)
            present_npcs.append({
                "id": nid,
                "name": info.get("name", nid),
                "role": info.get("role", ""),
                "appearance": info.get("appearance", ""),
                "attire": info.get("attire", {}),
                "personality": info.get("personality", ""),
                "life_goal": info.get("life_goal") or info.get("goal") or "",
                "background": info.get("background", ""),
                "cognition": info.get("cognition") or [],
                "perspective": info.get("perspective", ""),
                "fears": info.get("fears") or [],
                "desires": info.get("desires") or [],
                "contradictions": info.get("contradictions") or [],
                "impulsivity": info.get("impulsivity", 0.5),
                "core_drive": info.get("core_drive"),
                "short_term_plan": info.get("short_term_plan"),
                "physical_state": info.get("physical_state", "healthy"),
                "social_initiative": info.get("social_initiative", 0.4),
                "status": info.get("status", "alive"),
                "affection": aff,
                "affection_delta": delta,
                "weakness_state": str(_w.get("state", "") or ""),
                "emotion_tags": _npc_view["emotion_tags"],
                "stealable_items": _npc_view["stealable_items"],
                "visible_items": _npc_view["visible_items"],
                "has_pending": _npc_view["has_pending"],
                "pending_summary": _npc_view["pending_summary"],
                "current_intent": _npc_view["current_intent"],
                "life_goal": info.get("life_goal") or info.get("goal") or "",
                "background": info.get("background", ""),
                "cognition": info.get("cognition") or [],
                "perspective": info.get("perspective", ""),
                "fears": info.get("fears") or [],
                "desires": info.get("desires") or [],
                "contradictions": info.get("contradictions") or [],
                "impulsivity": info.get("impulsivity", 0.5),
                "core_goal": info.get("core_drive") or info.get("core_goal"),
                "short_term_goal": info.get("short_term_plan") or info.get("short_term_goal"),
                "mood": info.get("mood") or {},
                "current_action": info.get("current_action") or {"action": "", "since_tick": 0},
                "daily_routine": info.get("daily_routine") or [],
                "life_events": info.get("life_events") or [],
                "physical_state": info.get("physical_state", "healthy"),
                "social_initiative": info.get("social_initiative", 0.4),
                "status": info.get("status", "alive"),
            })
    thread_affection_snap[thread_id] = current_aff_snap

    # 5. 增量对话 (new_dialogs)
    history = state.get("history", [])
    last_h_len = thread_history_len.get(thread_id, 0)
    new_dialogs = []
    if len(history) > last_h_len:
        for entry in history[last_h_len:]:
            _dlg = {
                "speaker": entry.get("speaker", ""),
                "content": entry.get("content", "")
            }
            if "target" in entry:
                _dlg["target"] = entry["target"]
            new_dialogs.append(_dlg)
    thread_history_len[thread_id] = len(history)

    # 6. 增量 UI 事件 (ui_events)
    all_ui_events = state.get("ui_events", [])
    last_ui_len = thread_ui_events_len.get(thread_id, 0)
    new_ui_events = []
    if len(all_ui_events) > last_ui_len:
        new_ui_events = list(all_ui_events[last_ui_len:])
    thread_ui_events_len[thread_id] = len(all_ui_events)
    
    # 6.1 NPC 进场/离场提示：npc_move 相对玩家当前场景补系统提示
    for _ev in new_ui_events:
        if not isinstance(_ev, dict) or _ev.get("type") != "npc_move":
            continue
        _ev_name = str(_ev.get("npc", "") or "").strip()
        if not _ev_name:
            continue
        _target = str(_ev.get("target_scene", "") or "")
        _from = str(_ev.get("from_scene", "") or "")
        if _target == current_scene:
            new_dialogs.append({"speaker": "system", "content": f"【系统】{_ev_name} 来到了这里。"})
        elif _from == current_scene and _target and _target != current_scene:
            new_dialogs.append({"speaker": "system", "content": f"【系统】{_ev_name} 离开了这里，去了{_target}。"})

    # 追加好感度变化事件
    for npc in present_npcs:
        if npc["affection_delta"] != 0:
            new_ui_events.append({
                "type": "affection_change",
                "payload": {"npc_id": npc["id"], "delta": npc["affection_delta"]}
            })

    scenes_dict = state.get("scenes", {})
    scene_info = scenes_dict.get(current_scene, {})
    current_scene_desc = scene_info.get("desc", "")

    # 当前场景可交互物品清单：合并剧本物品（desc/locked）与布局坐标（grid_x/y/w/h）
    # 布局物品坐标索引（name -> layout item）
    layout_items_by_name = {}
    for _li in (current_scene_layout.get("items") or []):
        if isinstance(_li, dict) and str(_li.get("name", "")).strip():
            layout_items_by_name[str(_li["name"]).strip()] = _li

    # 已被玩家/NPC 持有的物品，绝不能当作场景物品渲染（场景只渲染固定场景物品）
    _held_names = set()
    for _pi in (state.get("player_items") or []):
        if isinstance(_pi, dict) and _pi.get("name"):
            _held_names.add(str(_pi["name"]).strip())
    for _pr in (state.get("props") or []):
        if isinstance(_pr, dict) and _pr.get("name") and (_pr.get("carried") or _pr.get("holder")):
            _held_names.add(str(_pr["name"]).strip())

    scene_items = []
    _seen_item_names = set()
    # 只取场景剧本物品（带 desc/locked），并 join 布局坐标；布局里多出的旧物品不再补入场景
    for _it in (scene_info.get("items") or []):
        if isinstance(_it, dict) and _it.get("name"):
            _si_name = str(_it.get("name", "") or "").strip()
            if not _si_name or _si_name in _seen_item_names or _si_name in _held_names:
                continue
            _seen_item_names.add(_si_name)
            _locked = bool(_it.get("locked"))
            _entry = {
                "name": _si_name,
                "desc": str(_it.get("desc", "") or "").strip(),
                "locked": _locked,
                "lock_mode": str(_it.get("lock_mode", "") or "").strip(),
                "key_name": str(_it.get("key_name", "") or "").strip(),
                "bio_holder": str(_it.get("bio_holder", "") or "").strip(),
                "lock_hint": unlock_hint(_it) if _locked else "",
            }
            _li = layout_items_by_name.get(_si_name)
            if _li:
                _entry["grid_x"] = _li["grid_x"]
                _entry["grid_y"] = _li["grid_y"]
                _entry["grid_w"] = _li["grid_w"]
                _entry["grid_h"] = _li["grid_h"]
            scene_items.append(_entry)

    backstory = state.get("backstory", "")
    ending_goal = state.get("ending_goal", "")
    clues = state.get("player_revealed_clues", [])
    world_events = state.get("world_events", [])
    
    all_npcs_detail = []
    for nid, info in all_npcs.items():
        _w2 = info.get("weakness") if isinstance(info.get("weakness"), dict) else {}
        _npc_view2 = _build_npc_view(state, info, view=view)
        all_npcs_detail.append({
            "id": nid, 
            "name": info.get("name", nid), 
            "role": info.get("role", ""),
            "location": info.get("location", ""),
            "affection": info.get("affection", 100),
                "weakness_state": str(_w2.get("state", "") or ""),
            "emotion_tags": _npc_view2["emotion_tags"],
            "stealable_items": _npc_view2["stealable_items"],
            "visible_items": _npc_view2["visible_items"],
            "has_pending": _npc_view2["has_pending"],
            "pending_summary": _npc_view2["pending_summary"],
            "current_intent": _npc_view2["current_intent"],
            "appearance": info.get("appearance", ""), 
            "attire": info.get("attire", {}),
            "personality": info.get("personality", ""),
            "life_goal": info.get("life_goal") or info.get("goal") or "",
                "background": info.get("background", ""),
            "cognition": info.get("cognition") or [],
            "perspective": info.get("perspective", ""),
            "fears": info.get("fears") or [],
            "desires": info.get("desires") or [],
            "contradictions": info.get("contradictions") or [],
            "impulsivity": info.get("impulsivity", 0.5),
            "core_goal": info.get("core_drive") or info.get("core_goal"),
            "short_term_goal": info.get("short_term_plan") or info.get("short_term_goal"),
            "mood": info.get("mood") or {},
            "current_action": info.get("current_action") or {"action": "", "since_tick": 0},
            "daily_routine": info.get("daily_routine") or [],
            "life_events": info.get("life_events") or [],
                "social_initiative": info.get("social_initiative", 0.4),
                "status": info.get("status", "alive"),
        })
    
    return {
        "world_tick": state.get("world_tick", 0),
        "current_scene": current_scene,
        "current_scene_desc": current_scene_desc,
        "current_scene_layout": current_scene_layout,
        "bg_prompt": current_scene_layout["bg_prompt"],
        "walls": current_scene_layout.get("walls", []),
        "obstacles": current_scene_layout["obstacles"],
        "doors": scene_doors,
        "scene_items": scene_items,
        "time_phase": time_phase,
        "player_items": player_items,
        "available_scenes": available_scenes,
        "present_npcs": present_npcs,
        "new_dialogs": new_dialogs,
        "ui_events": new_ui_events,
        "backstory": backstory,
        "ending_goal": ending_goal,
        "endings": state.get("endings", []),
        "clues": clues,
        "world_events": world_events,
        "all_npcs_detail": all_npcs_detail,
        "active_crisis": state.get("active_crisis") or {},
        "secrets_revealed": state.get("secrets_revealed") or [],
        "p2p_transcript": state.get("p2p_transcript", ""),
        "social_graph": state.get("social_graph") or {},
    }

def _snapshot_text(step_no: int, player_msg: str, ui_state: dict, new_dialogs: list, new_world_events: list) -> str:
    lines = [f"第 {step_no} 步 · 玩家行动：{player_msg}"]
    _scene = str(ui_state.get("current_scene", "") or "").strip()
    _phase = str(ui_state.get("time_phase", "") or "").strip()
    if _phase or _scene:
        lines.append(f"时间 {_phase}，地点 {_scene}")
    for d in (new_dialogs or []):
        if not isinstance(d, dict):
            continue
        _spk = str(d.get("speaker", "") or "").strip()
        _txt = str(d.get("content", "") or "").strip()
        if not _txt:
            continue
        lines.append(f"  {_spk}：{_txt}" if _spk else f"  {_txt}")
    for ev in (new_world_events or []):
        lines.append(f"  【动态】{ev}")
    return "\n".join(lines)


def _take_snapshot(thread_id: str, player_msg: str, ui_state: dict) -> None:
    step_no = int(thread_step.get(thread_id, 0)) + 1
    thread_step[thread_id] = step_no
    all_wevs = ui_state.get("world_events", [])
    if not isinstance(all_wevs, list):
        all_wevs = []
    last_wev = int(thread_world_events_len.get(thread_id, 0))
    new_world_events = list(all_wevs[last_wev:])
    thread_world_events_len[thread_id] = len(all_wevs)
    new_dialogs = ui_state.get("new_dialogs", [])
    if not isinstance(new_dialogs, list):
        new_dialogs = []
    log_snapshot(thread_id, _snapshot_text(step_no, player_msg, ui_state, new_dialogs, new_world_events))


@app.post("/game/new")
async def new_game(req: NewGameReq):
    graph_app = state["app"]
    config = {"configurable": {"thread_id": req.thread_id}}
    
    snapshot = await graph_app.aget_state(config)
    if snapshot.values.get("npcs"):
        return {"status": "exists", "message": "该会话已存在，直接用 act 接口"}
    
    try:
        # 生成剧本世界（可能耗时 30 秒以上）
        result = await graph_app.ainvoke({"task": req.task, "player_name": req.player_name, "style_profile": req.style_profile or {}}, config)
        # 新增拦截：如果触发保底，直接抛错
        if result.get("backstory") == "故事开始了。" or (result.get("npcs") and "NPC甲" in result["npcs"]):
            raise Exception("触发保底剧本！说明模型返回的 JSON 解析失败，请查看后端黑窗口报错。")
        try:
            summary_text = f"玩家要求：{req.task}\n背景：{result.get('backstory', '')}\n真相：{result.get('global_truth', '')}"
            add_script_summary(req.thread_id, req.task, summary_text)
        except Exception as e:
            print(f"[RAG剧本写入失败] {e}")
        ui_state = await build_ui_state(req.thread_id, result)
        _take_snapshot(req.thread_id, f"开局：{req.task}", ui_state)
        return {"status": "success", "ui_state": ui_state}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/game/act")
async def act(req: ActReq):
    graph_app = state["app"]
    config = {"configurable": {"thread_id": req.thread_id}}
    
    try:
        # 玩家行动，全量返回最新状态（前端做全量刷新）
        result = await graph_app.ainvoke({"user_msg": req.msg}, config)
        ui_state = await build_ui_state(req.thread_id, result, view=req.view)
        _take_snapshot(req.thread_id, req.msg, ui_state)
        return {"status": "success", "ui_state": ui_state}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/game/state/{thread_id}")
async def get_state(thread_id: str, view: str = "full"):
    graph_app = state["app"]
    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = await graph_app.aget_state(config)
        ui_state = await build_ui_state(thread_id, snapshot.values, view=view)
        return {"status": "success", "ui_state": ui_state}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/debug/logs/{thread_id}")
async def debug_logs(thread_id: str, tail: int = 0):
    """按需读取测试日志全文（前端"日志"按钮触发）。tail>0 只返回末尾 N 行。"""
    content = read_thread(thread_id, tail_lines=tail)
    return {"status": "success", "thread_id": thread_id, "content": content, "length": len(content)}


@app.get("/debug/logs")
def debug_log_list():
    return {"status": "success", "threads": list_threads()}


@app.get("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run("server:app", host="127.0.0.1", port=8888, reload=False)