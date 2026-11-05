"""多智能体互动叙事游戏 · 世界引擎版 CLI"""
import os
import sys
import json
import asyncio

for stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from graph import build_graph, load_checkpointer
from stream_bus import set_done, reset as reset_stream, _get as _sb_queue

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkpoints.db")

C_GRAY = "\033[90m"
C_CYAN = "\033[96m"
C_YELLOW = "\033[93m"
C_GREEN = "\033[92m"
C_RESET = "\033[0m"


def _npc_name(npcs: dict, npc_id: str) -> str:
    return (npcs or {}).get(npc_id, {}).get("name", npc_id)


def _print_status(result: dict) -> None:
    print(f"\n{C_CYAN}========== 状态面板 =========={C_RESET}")
    print(f"{C_CYAN}[当前场景]{C_RESET}", result.get("current_scene", "未知"))
    if result.get("player_name"):
        print(f"{C_CYAN}[你的名字]{C_RESET}", result.get("player_name"))
    if result.get("time_phase"):
        print(f"{C_CYAN}[当前时间]{C_RESET}", result.get("time_phase"))
    if result.get("time_limit"):
        print(f"{C_CYAN}[关键时间]{C_RESET}", result.get("time_limit"))
    avail = result.get("available_scenes") or []
    if avail:
        print(f"{C_CYAN}[可去场景]{C_RESET}", "、".join(avail))
    npcs = result.get("npcs") or {}
    scene = result.get("current_scene", "")
    present = [(nid, info) for nid, info in npcs.items() if info.get("location") == scene]
    if present:
        names = "、".join(f"{info.get('name','')}（{info.get('role','')}）" for _, info in present)
        print(f"{C_CYAN}[场景人物]{C_RESET}", names)
    else:
        print(f"{C_CYAN}[场景人物]{C_RESET}", "（这里没有其他人）")

    items = result.get("player_items") or []
    if items:
        desc_map = result.get("item_desc_map") or {}
        bag_lines = []
        for i in items:
            _desc = desc_map.get(i, '用途不明')
            _content_key = f"__content__:{i}"
            if _content_key in desc_map:
                _subs_list = desc_map[_content_key].split(",")
                _sub_strs = []
                for _sub in _subs_list:
                    _sub = _sub.strip()
                    if not _sub:
                        continue
                    _sub_desc = desc_map.get(_sub, "")
                    if _sub_desc:
                        _sub_strs.append(f"\n    - {_sub}：{_sub_desc}")
                    else:
                        _sub_strs.append(f"\n    - {_sub}")
                bag_lines.append(f"{i}（{_desc}）\n  [包含子物品]{''.join(_sub_strs)}")
            else:
                bag_lines.append(f"{i}（{_desc}）")
        bag = "；\n".join(bag_lines)
        print(f"{C_CYAN}[背包]{C_RESET}\n{bag}")
    else:
        print(f"{C_CYAN}[背包]{C_RESET}（空）")
    print(f"{C_CYAN}[对话轮数]{C_RESET}", result.get("analysis_counter", 0))
    clues = result.get("player_revealed_clues") or []
    if clues:
        print(f"{C_CYAN}[已知线索]{C_RESET}", "；".join(clues[-5:]))
    affs = {nid: info.get("affection", 0) for nid, info in npcs.items()}
    if affs:
        print(f"{C_CYAN}[好感度]{C_RESET}", {_npc_name(npcs, k): v for k, v in affs.items()})
    events = result.get("world_events") or []
    if events:
        print(f"{C_CYAN}[世界事件]{C_RESET}", "；".join(events[-3:]))


def _print_roster(result: dict) -> None:
    print("\n" + "=" * 40)
    print("【人物名单】")
    npcs = result.get("npcs") or {}
    for npc_id, info in npcs.items():
        print(f"  - {info.get('name','')}（{npc_id}）：{info.get('role','')} | 性格{info.get('personality','?')} | "
              f"外貌{info.get('appearance','?')} | 在 {info.get('location','?')} | 穿{info.get('attire','常服')}")
    print("=" * 40)


async def _stream_player(names: dict) -> None:
    """消费 stream_bus 队列，按 NPC 排队流式播放。

    同一时刻只播放一个 NPC 的流：播放中收到其他 NPC 的 chunk 先存 buffer，
    当前 NPC 播完（end）才切下一个——多个 NPC 并行生成也不会串线。
    状态机要点：
    - header 只在"真的有内容要播"时打一次（header_shown），空回复不显示空名字；
    - 不用 playing 锁：NPC 失败重试/兜底的 start→chunk→end 序列到达时，
      只要 chunk 有内容就继续播（header 不重复），杜绝"打了名字却没内容"的假死。
    """
    import stream_bus

    q = _sb_queue()
    current: str | None = None
    buffers: dict[str, dict] = {}

    def _header(nid: str) -> None:
        display = names.get(nid, nid)
        color = C_YELLOW if str(nid).endswith("1") else C_CYAN
        print(f"\n{color}[{display}]{C_RESET} ", end="", flush=True)

    def _try_play() -> None:
        nonlocal current
        if current is not None:
            return
        for nid, buf in list(buffers.items()):
            if buf["chunks"]:
                if not buf["header_shown"]:
                    buf["header_shown"] = True
                    _header(nid)
                current = nid
                while buf["chunks"]:
                    print(buf["chunks"].pop(0), end="", flush=True)
                if buf["ended"]:
                    print()
                    current = None
                    _try_play()
                return

    while True:
        if (current is None and stream_bus.is_finished()
                and q.empty() and all(b["ended"] for b in buffers.values())):
            break
        try:
            item = await asyncio.wait_for(q.get(), timeout=0.3)
        except asyncio.TimeoutError:
            continue
        kind = item[0]
        if kind == "start":
            nid = item[1]
            buffers.setdefault(nid, {"chunks": [], "ended": False, "header_shown": False})
            if current is None:
                _try_play()
        elif kind == "chunk":
            _, nid, text = item
            buf = buffers.setdefault(nid, {"chunks": [], "ended": False, "header_shown": False})
            if nid == current:
                print(text, end="", flush=True)
            else:
                buf["chunks"].append(text)
                if current is None:
                    _try_play()
        elif kind == "end":
            nid = item[1]
            buf = buffers.setdefault(nid, {"chunks": [], "ended": False, "header_shown": False})
            buf["ended"] = True
            if nid == current and not buf["chunks"]:
                print()
                current = None
                _try_play()
            elif current is None:
                _try_play()
    print()


async def _stream_round(app, msg, config, names: dict) -> dict:
    reset_stream()
    print(f"{C_GRAY}（思考中……）{C_RESET}", flush=True)

    player = asyncio.create_task(_stream_player(names))
    async for item in app.astream({"user_msg": msg}, config, stream_mode="updates"):
        for node_name, update in (item.items() if isinstance(item, dict) else []):
            if not isinstance(update, dict):
                continue
            if node_name == "npc_worker":
                hist = update.get("history") or []
                for e in hist:
                    if e.get("speaker") == "system" and e.get("content"):
                        print(f"\n{C_GRAY}{e['content']}{C_RESET}", flush=True)
                continue
            elif node_name == "action_engine":
                hist = update.get("history") or []
                for e in hist:
                    if e.get("speaker") == "system" and e.get("content"):
                        print(f"\n{C_GRAY}{e['content']}{C_RESET}", flush=True)
            elif node_name == "item_node":
                hist = update.get("history") or []
                for e in hist:
                    if e.get("speaker") != "user" and e.get("content"):
                        print(f"\n{C_GRAY}{e['content']}{C_RESET}", flush=True)
                continue
            elif node_name == "record_input":
                hist = update.get("history") or []
                for e in hist:
                    if e.get("speaker") == "system" and e.get("content"):
                        print(f"\n{C_GRAY}{e['content']}{C_RESET}", flush=True)
            elif node_name == "p2p_node" and update.get("p2p_transcript"):
                print(f"\n{C_GRAY}【旁听】{C_RESET}{update['p2p_transcript']}", flush=True)
            elif node_name == "hint_node":
                hist = update.get("history") or []
                for e in hist:
                    if e.get("speaker") == "system" and e.get("content"):
                        print(f"\n{C_GREEN}[导演提示]{C_RESET} {e['content']}", flush=True)
            elif node_name == "judge_ending":
                hist = update.get("history") or []
                for e in hist:
                    if e.get("speaker") == "system" and e.get("content"):
                        print("\n" + "=" * 50)
                        print(e.get("content"))
                        print("=" * 50)
                        print(f"{C_GRAY}（结局已揭晓，你仍可继续探索与对话）{C_RESET}", flush=True)

    set_done()
    await player
    print("\n")
    final_state = (await app.aget_state(config)).values
    return final_state


# 主入口：初始化图，循环读取输入并执行
async def main() -> None:
    async with load_checkpointer(DB_PATH) as (checkpointer, ckpt_type):
        app = build_graph(checkpointer=checkpointer)

        thread_id = input("会话 ID（默认 script-design-session；开始新任务请换一个）：\n> ").strip()
        if not thread_id:
            thread_id = "script-design-session"
        config = {"configurable": {"thread_id": thread_id}}

        print("=" * 60)
        print("剧本设计游戏 · 世界引擎版（动态 NPC + 调查 + 确定性解锁）")
        print("=" * 60)

        snapshot = await app.aget_state(config)
        result = snapshot.values

        if result.get("npcs") and not result.get("game_over"):
            print("\n[恢复] 该会话已有剧本，直接继续：")
            print("  背景：", (result.get("backstory") or "无")[:120])
            _print_roster(result)
            _print_status(result)
        else:
            task = input(
                "\n请给导演一句话的剧本要求（可指定NPC人数，如“两个嫌疑人”）。\n"
                "提示：可指定NPC名字与风格（如“女主叫周砚，民国风”）；不指定则由导演自行生成。\n> "
            ).strip()
            result = await app.ainvoke({"task": task}, config)

            print("\n" + "=" * 40)
            print("【剧本背景】")
            print(result.get("backstory", "无"))
            print("\n【主线目标】")
            print(result.get("ending_goal", "无"))
            sc = (result.get("scenes") or {}).get(result.get("current_scene", ""), {})
            if sc.get("desc"):
                print(f"\n【{result.get('current_scene')}】{sc['desc']}")
            init_items = result.get("player_items") or []
            if init_items:
                desc_map = result.get("item_desc_map") or {}
                print("\n【你随身携带】")
                for it in init_items:
                    print(f"  - {it}：{desc_map.get(it, '用途不明')}")
            print("=" * 40 + "\n")

            _print_roster(result)
            _print_status(result)

        while True:
            # 主循环：读取玩家输入，路由到图，打印状态面板
            if result.get("game_over"):
                print("\n[系统] 剧本已结束。输入 quit 退出。")
                break

            try:
                msg = input(
                    "\n[行动]（@数字/@全部 对话 | @1和@2交流 | !@N 穿上/脱掉/给我/去 物品或地点（NPC指令，需NPC同意） | !去 地点 | !调查 | !提示 | !偷 物品 | 结局是：XXX | quit退出）：\n> "
                ).strip()
            except EOFError:
                break
            if msg.lower() in ("quit", "exit", "退出") or msg.lstrip().lstrip("!！").lower() in ("quit", "exit", "退出"):
                print("\n[系统] 已退出。")
                break
            if not msg:
                continue

            npcs = result.get("npcs") or {}
            names = {nid: info.get("name", nid) for nid, info in npcs.items()}
            result = await _stream_round(app, msg, config, names)
            _print_status(result)


if __name__ == "__main__":
    asyncio.run(main())
