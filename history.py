"""
剧本设计游戏 · 会话历史查看与管理（增删改查）
=============================================
独立模块：不改动 graph.py / main.py 原有代码，只读取 / 整理 checkpoints.db。
读取直接构造 langgraph 官方 AsyncSqliteSaver（与 main.py 同一数据文件），
增/删/改用 langgraph 官方序列化器回写最新 checkpoint，不破坏图运行。

用法（在项目目录下，用 .venv 的 python 运行）：
  python history.py list                         # 查：列出所有会话（任务 / 状态 / 轮数）
  python history.py show <会话ID> [--full]       # 查：逐条查看某会话每一轮历史（带 [N] 条目编号）
  python history.py add <会话ID> <speaker> <内容> [--target 子1|子2|全部]
  python history.py edit <会话ID> <条目号> <新内容>
  python history.py delete <会话ID>              # 删：删除整个会话（二次确认）
  python history.py delete <会话ID> --entry <条目号>

speaker 取值：user / 子1 / 子2（同 user / sub1 / sub2；用户条目可用 --target 指定目标）
条目号 = show 命令输出的 [N] 编号，与 show 显示一一对应。
"""
import os
import sys
import json
import asyncio
import argparse
import sqlite3

for stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "checkpoints.db")

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from graph import build_graph  # 仅用于读取状态（不执行节点，不依赖其 load_checkpointer）

_SERDE = JsonPlusSerializer()

SPEAKER_MAP = {
    "user": "user", "用户": "user", "玩家": "user",
    "system": "system", "系统": "system", "导演": "system",
    "sub1": "sub1", "子1": "sub1",
    "sub2": "sub2", "子2": "sub2",
}
for _i in range(1, 30):
    SPEAKER_MAP[f"npc_{_i}"] = f"npc_{_i}"
    SPEAKER_MAP[str(_i)] = f"npc_{_i}"
TARGET_MAP = {"子1": "sub1", "sub1": "sub1", "子2": "sub2", "sub2": "sub2", "全部": "全部"}
for _i in range(1, 30):
    TARGET_MAP[f"npc_{_i}"] = f"npc_{_i}"
    TARGET_MAP[str(_i)] = f"npc_{_i}"


def _clip(s, full):
    if full or len(s) <= 200:
        return s
    return s[:200] + "…(截断，加 --full 查看全文)"


def _all_thread_ids():
    if not os.path.exists(DB):
        return []
    conn = sqlite3.connect(DB, timeout=10)
    tids = [r[0] for r in conn.execute("select distinct thread_id from checkpoints").fetchall()]
    conn.close()
    return tids


def _get_history(vals) -> list:
    """取共享交互历史：新 schema 用 history；旧 schema（sub_messages）回退合成"""
    hist = vals.get("history")
    if hist is not None:
        return list(hist)
    legacy = vals.get("sub_messages") or {}
    merged = []
    for sid in ("sub1", "sub2"):
        for m in legacy.get(sid, []):
            if m.get("role") == "assistant":
                merged.append({"speaker": sid, "content": m.get("content", "")})
            else:
                merged.append({"speaker": "user", "target": sid, "content": m.get("content", "")})
    return merged


async def _read_state(tid):
    """用官方 AsyncSqliteSaver 读取最新状态，返回 (values, 最新 checkpoint_id)；
    会话不存在/为空时返回 (None, None)"""
    conn = await aiosqlite.connect(DB, check_same_thread=False)
    try:
        saver = AsyncSqliteSaver(conn)
        app = build_graph(checkpointer=saver)
        snap = await app.aget_state({"configurable": {"thread_id": tid}})
        if not snap.values.get("npcs") and not snap.values.get("subtasks") and not snap.values.get("backstory"):
            return None, None
        cid = snap.config["configurable"].get("checkpoint_id")
        return snap.values, cid
    finally:
        await conn.close()


async def _write_history(tid, cid, new_hist):
    """把修改后的 history 回写该会话最新 checkpoint（langgraph 官方 serde 往返，只改 history 通道）"""
    conn = sqlite3.connect(DB, timeout=10)
    try:
        row = conn.execute(
            "select type, checkpoint from checkpoints where thread_id=? and checkpoint_ns='' and checkpoint_id=?",
            (tid, cid),
        ).fetchone()
        if not row:
            raise RuntimeError("找不到该会话的最新 checkpoint 记录")
        typ, blob = row
        obj = _SERDE.loads_typed((typ, blob))
        obj.setdefault("channel_values", {})["history"] = new_hist
        new_type, new_blob = _SERDE.dumps_typed(obj)
        conn.execute(
            "update checkpoints set type=?, checkpoint=? where thread_id=? and checkpoint_ns='' and checkpoint_id=?",
            (new_type, new_blob, tid, cid),
        )
        conn.commit()
    finally:
        conn.close()


async def cmd_list():
    tids = _all_thread_ids()
    if not tids:
        print("（暂无会话。运行 main.py 对话后会自动生成）")
        return
    print(f"共 {len(tids)} 个会话：")
    for tid in tids:
        vals, _ = await _read_state(tid)
        if vals is None:
            print(f"\n- thread_id: {tid}（状态读取失败或为空）")
            continue
        hist = _get_history(vals)
        rounds = sum(1 for e in hist if e.get("speaker") == "user")
        print(f"\n- thread_id: {tid}")
        print(f"  任务: {(vals.get('task') or '(未分配)')[:60]}")
        npcs = vals.get("npcs") or {}
        if npcs:
            npc_line = "、".join(f"{info.get('name','')}({nid})" for nid, info in npcs.items())
            print(f"  NPC: {npc_line}")
        print(f"  对话轮数: {rounds}（历史 {len(hist)} 条）")


async def cmd_show(tid, full):
    vals, cid = await _read_state(tid)
    if vals is None:
        print(f"会话不存在或为空: {tid}")
        return
    print(f"== 会话 {tid} ==")
    print(f"任务: {vals.get('task')}")
    print(f"当前场景: {vals.get('current_scene', '未知')}")
    print(f"当前时间: {vals.get('time_phase', '清晨')} | 关键时间: {vals.get('time_limit') or '（不限时）'}")
    print(f"持有道具: {vals.get('player_items') or []}")
    print("\n-- NPC 名单 --")
    npcs = vals.get("npcs") or {}
    for nid, info in npcs.items():
        if isinstance(info, dict):
            print(f"  {info.get('name', nid)}（{nid}）: {info.get('role', '?')} | 在 {info.get('location', '?')} | 好感 {info.get('affection', '?')}")
        else:
            print(f"  {nid}: {info}")
    hist = _get_history(vals)
    if not hist:
        print("\n（暂无历史交互）")
        return
    print("\n-- 历史记录（[N] 为条目号，edit/delete --entry 使用）--")
    for i, e in enumerate(hist, 1):
        speaker = e.get("speaker", "?")
        if speaker == "user":
            tgt = e.get("target", "?")
            print(f"[{i}] 用户(对{tgt}): {_clip(e.get('content', ''), full)}")
        else:
            print(f"[{i}] {speaker}: {_clip(e.get('content', ''), full)}")


async def cmd_add(tid, speaker, content, target):
    vals, cid = await _read_state(tid)
    if vals is None:
        print(f"会话不存在或为空: {tid}（请先运行 main.py 创建会话）")
        return
    spk = SPEAKER_MAP.get(speaker)
    if spk is None:
        print(f"speaker 只能取: {list(SPEAKER_MAP.keys())}（收到 {speaker!r}）")
        return
    entry = {"speaker": spk, "content": content}
    if spk == "user":
        entry["target"] = TARGET_MAP.get(target, "全部")
    hist = _get_history(vals)
    hist.append(entry)
    await _write_history(tid, cid, hist)
    print(f"已追加到会话 {tid}，新条目号 [{len(hist)}]：")
    print(f"  {entry.get('speaker')} (对{entry.get('target', '-')}): {_clip(content, True)}")


async def cmd_edit(tid, idx, content):
    vals, cid = await _read_state(tid)
    if vals is None:
        print(f"会话不存在或为空: {tid}")
        return
    hist = _get_history(vals)
    if not 1 <= idx <= len(hist):
        print(f"条目号越界：有效范围 1-{len(hist)}（收到 {idx}）")
        return
    old = hist[idx - 1]
    old["content"] = content
    await _write_history(tid, cid, hist)
    print(f"已修改会话 {tid} 条目 [{idx}]：")
    print(f"  {old.get('speaker')} (对{old.get('target', '-')}): {_clip(content, True)}")


async def cmd_delete(tid, entry):
    if entry is not None:
        vals, cid = await _read_state(tid)
        if vals is None:
            print(f"会话不存在或为空: {tid}")
            return
        hist = _get_history(vals)
        if not 1 <= entry <= len(hist):
            print(f"条目号越界：有效范围 1-{len(hist)}（收到 {entry}）")
            return
        e = hist[entry - 1]
        print(f"将删除会话 {tid} 条目 [{entry}]：")
        print(f"  {e.get('speaker')} (对{e.get('target', '-')}): {_clip(e.get('content', ''), True)}")
        ans = input("确认删除？(y/N): ").strip().lower()
        if ans != "y":
            print("已取消。")
            return
        del hist[entry - 1]
        await _write_history(tid, cid, hist)
        print("已删除该条记录。")
        return

    vals, cid = await _read_state(tid)
    if vals is None:
        print(f"会话不存在或为空: {tid}")
        return
    hist = _get_history(vals)
    rounds = sum(1 for e in hist if e.get("speaker") == "user")
    print(f"将删除整个会话 {tid}：")
    print(f"  任务: {(vals.get('task') or '')[:60]}")
    print(f"  对话轮数: {rounds}（任务/角色/对话/状态将一并删除，不可恢复）")
    ans = input("确认删除？(y/N): ").strip().lower()
    if ans != "y":
        print("已取消。")
        return
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute("delete from checkpoints where thread_id=?", (tid,))
    conn.execute("delete from writes where thread_id=?", (tid,))
    conn.commit()
    conn.close()
    print("已删除。")
    _tidy_db()


def _tidy_db():
    """删除整会话后，如果 checkpoints 表为空，顺手清理空 db"""
    try:
        conn = sqlite3.connect(DB)
        n = conn.execute("select count(*) from checkpoints").fetchone()[0]
        conn.close()
    except Exception:
        return
    if n == 0:
        try:
            os.remove(DB)
        except OSError:
            pass


async def main():
    p = argparse.ArgumentParser(description="剧本设计游戏 · 会话历史查看/管理（增删改查）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出所有会话")

    s_show = sub.add_parser("show", help="逐条查看某会话历史")
    s_show.add_argument("thread_id")
    s_show.add_argument("--full", action="store_true", help="显示完整内容，不截断")

    s_add = sub.add_parser("add", help="追加一条历史记录")
    s_add.add_argument("thread_id")
    s_add.add_argument("speaker", help="user / system / npc_1 / npc_2 / 子1 / 子2")
    s_add.add_argument("content")
    s_add.add_argument("--target", default=None, help="用户条目的目标：npc_1 / npc_2 / 全部（默认全部）")

    s_edit = sub.add_parser("edit", help="修改指定条目的内容")
    s_edit.add_argument("thread_id")
    s_edit.add_argument("idx", type=int, help="条目号（show 输出的 [N]）")
    s_edit.add_argument("content")

    s_del = sub.add_parser("delete", help="删除整个会话或单条记录")
    s_del.add_argument("thread_id")
    s_del.add_argument("--entry", type=int, default=None, help="只删除该条目号（默认删除整个会话）")

    args = p.parse_args()
    if args.cmd == "list":
        await cmd_list()
    elif args.cmd == "show":
        await cmd_show(args.thread_id, args.full)
    elif args.cmd == "add":
        await cmd_add(args.thread_id, args.speaker, args.content, args.target)
    elif args.cmd == "edit":
        await cmd_edit(args.thread_id, args.idx, args.content)
    elif args.cmd == "delete":
        await cmd_delete(args.thread_id, args.entry)


if __name__ == "__main__":
    asyncio.run(main())
