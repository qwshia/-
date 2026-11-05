"""流式输出总线：NPC 生成端 → 播放端 的跨协程队列。

npc_worker 用 llm.astream 边生成边 push chunk；main 的播放器协程
消费队列并按 NPC 排队播放（同一 NPC 的 chunk 连续播完才切下一个），
因此多个 NPC 并行生成也不会串线。

协议元素：
  ("start", npc_id)           生成开始（注册/接管播放）
  ("chunk", npc_id, text)     增量文本
  ("end", npc_id)             生成结束

注册/结束计数用于播放器判断"全部播完"：main 在 astream 结束后
调用 set_done()，播放器在 done 且 ended >= registered 且无当前播放
且队列空时退出。
"""
import asyncio

_queue: asyncio.Queue | None = None
_registered = 0
_ended = 0
_done = False


def _get() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


def reset() -> None:
    """新一轮开始前清空状态（也可不调用：注册数只在单轮内比较）"""
    global _queue, _registered, _ended, _done
    _queue = None
    _registered = 0
    _ended = 0
    _done = False


def inc_registered() -> None:
    global _registered
    _registered += 1


async def push_start(npc_id: str) -> None:
    await _get().put(("start", npc_id))


async def push_chunk(npc_id: str, text: str) -> None:
    await _get().put(("chunk", npc_id, text))


async def push_end(npc_id: str) -> None:
    global _ended
    _ended += 1
    await _get().put(("end", npc_id))


def set_done() -> None:
    global _done
    _done = True


def is_finished() -> bool:
    return _done and (_registered == 0 or _ended >= _registered)
