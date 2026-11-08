import os
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from graph import build_graph
from state import load_checkpointer

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkpoints.db")
state = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时加载图
    cm = load_checkpointer(DB_PATH)
    checkpointer, ckpt_type = await cm.__aenter__()
    state["app"] = build_graph(checkpointer=checkpointer)
    state["cm"] = cm
    print(f"✅ 后端引擎启动，使用 {ckpt_type} 存储")
    yield
    # 关闭时释放
    await cm.__aexit__(None, None, None)

app = FastAPI(title="剧本游戏 API", lifespan=lifespan)

thread_history_len = {}
thread_ui_events_len = {}
thread_affection_snap = {}

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

class ActReq(BaseModel):
    thread_id: str
    msg: str

def build_ui_state(thread_id: str, state: dict) -> dict:
    # 1. 场景与时间
    current_scene = state.get("current_scene", "")
    time_phase = state.get("time_phase", "")
    
    # 2. 背包提纯
    player_items = []
    desc_map = state.get("item_desc_map", {})
    for it in state.get("player_items", []):
        if isinstance(it, dict):
            name = it.get("name", "")
            player_items.append({
                "name": name,
                "count": it.get("count", 1),
                "desc": desc_map.get(name, it.get("desc", ""))
            })
            
    # 3. 场景结构体
    available_scenes = []
    scenes_dict = state.get("scenes", {})
    opened_scenes = state.get("opened_scenes", [])
    for s_name in state.get("available_scenes", []):
        sdef = scenes_dict.get(s_name, {})
        req = sdef.get("requires_key", "")
        locked = bool(req) and (s_name not in opened_scenes)
        available_scenes.append({
            "name": s_name,
            "locked": locked,
            "requires": req if locked else ""
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
            present_npcs.append({
                "id": nid,
                "name": info.get("name", nid),
                "role": info.get("role", ""),
                "appearance": info.get("appearance", ""),
                "attire": info.get("attire", {}),
                "personality": info.get("personality", ""),
                "affection": aff,
                "affection_delta": delta
            })
    thread_affection_snap[thread_id] = current_aff_snap

    # 5. 增量对话 (new_dialogs)
    history = state.get("history", [])
    last_h_len = thread_history_len.get(thread_id, 0)
    new_dialogs = []
    if len(history) > last_h_len:
        for entry in history[last_h_len:]:
            new_dialogs.append({
                "speaker": entry.get("speaker", ""),
                "content": entry.get("content", "")
            })
    thread_history_len[thread_id] = len(history)

    # 6. 增量 UI 事件 (ui_events)
    all_ui_events = state.get("ui_events", [])
    last_ui_len = thread_ui_events_len.get(thread_id, 0)
    new_ui_events = []
    if len(all_ui_events) > last_ui_len:
        new_ui_events = list(all_ui_events[last_ui_len:])
    thread_ui_events_len[thread_id] = len(all_ui_events)
    
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

    backstory = state.get("backstory", "")
    ending_goal = state.get("ending_goal", "")
    clues = state.get("player_revealed_clues", [])
    world_events = state.get("world_events", [])
    
    return {
        "current_scene": current_scene,
        "current_scene_desc": current_scene_desc,
        "time_phase": time_phase,
        "player_items": player_items,
        "available_scenes": available_scenes,
        "present_npcs": present_npcs,
        "new_dialogs": new_dialogs,
        "ui_events": new_ui_events,
        "backstory": backstory,
        "ending_goal": ending_goal,
        "clues": clues,
        "world_events": world_events
    }

@app.post("/game/new")

async def new_game(req: NewGameReq):
    graph_app = state["app"]
    config = {"configurable": {"thread_id": req.thread_id}}
    
    snapshot = await graph_app.aget_state(config)
    if snapshot.values.get("npcs"):
        return {"status": "exists", "message": "该会话已存在，直接用 act 接口"}
    
    try:
        # 生成剧本世界（可能耗时 30 秒以上）
        result = await graph_app.ainvoke({"task": req.task}, config)
        # 新增拦截：如果触发保底，直接抛错
        if result.get("backstory") == "故事开始了。" or (result.get("npcs") and "NPC甲" in result["npcs"]):
            raise Exception("触发保底剧本！说明模型返回的 JSON 解析失败，请查看后端黑窗口报错。")
        ui_state = build_ui_state(req.thread_id, result)
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
        ui_state = build_ui_state(req.thread_id, result)
        return {"status": "success", "ui_state": ui_state}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/game/state/{thread_id}")
async def get_state(thread_id: str):
    graph_app = state["app"]
    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = await graph_app.aget_state(config)
        ui_state = build_ui_state(thread_id, snapshot.values)
        return {"status": "success", "ui_state": ui_state}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run("server:app", host="127.0.0.1", port=8888, reload=False)