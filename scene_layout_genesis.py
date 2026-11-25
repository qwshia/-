"""Generate and validate a single scene's grid layout."""
import json
import sys
from collections import deque

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from config import ACTOR_API_KEY, ACTOR_BASE_URL, _thinking_extra
from utils import _parse_json


GRID_WIDTH = 30
GRID_HEIGHT = 17
SPAWN_X = 15
SPAWN_Y = 8.5
ASSET_TAGS = (
    "door", "window", "bed", "table", "chair", "sofa", "bookshelf",
    "cabinet", "tv", "plant", "tree", "rock", "chest", "counter",
)
# 墙体附带品：贴墙渲染、不参与碰撞，不作为障碍物
WALL_DECOR_TAGS = ("window",)
WALL_TAGS = ("wall_h", "wall_v")
# 障碍物固定占地尺寸：(grid_w, grid_h)。door 不在内（door 需嵌墙，尺寸由 LLM 判断）。
ASSET_SIZES = {
    "window": (4, 4),
    "bed": (6, 4),
    "table": (4, 4),
    "chair": (2, 2),
    "sofa": (6, 4),
    "bookshelf": (4, 6),
    "cabinet": (4, 4),
    "tv": (4, 2),
    "plant": (2, 2),
    "tree": (4, 4),
    "rock": (4, 4),
    "chest": (2, 2),
    "counter": (6, 4),
}
BG_STYLE_SUFFIX = "像素艺术风格,2D伪2.5D微倾斜俯视视角,2D游戏背景,纯背景地形与墙面底图,绝对空旷,无任何家具/障碍物/可拾取物品,下半部分是连续地板且可带地砖拼花或地毯等地面装饰,地面材质纹理清晰."
BG_PROMPT_TEMPLATE = ""
SCENE_LAYOUT_MODEL = "doubao-seed-evolving"
scene_layout_llm = ChatOpenAI(
    model=SCENE_LAYOUT_MODEL,
    api_key=ACTOR_API_KEY,
    base_url=ACTOR_BASE_URL,
    temperature=0.2,
    timeout=300,
    max_retries=3,
    max_tokens=4096,
    extra_body=_thinking_extra(SCENE_LAYOUT_MODEL, ACTOR_BASE_URL),
    stream_usage=True,
)


def _item_names(items: list) -> list:
    names = []
    seen = set()
    for item in items or []:
        raw_name = item.get("name") if isinstance(item, dict) else item
        name = str(raw_name or "").strip()
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


def _integer(value, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _normalize_obstacles(raw_obstacles: list, item_names: list) -> list:
    obstacles = []
    bound_items = set()
    for raw in raw_obstacles if isinstance(raw_obstacles, list) else []:
        if not isinstance(raw, dict):
            continue
        asset_tag = raw.get("asset_tag")
        if asset_tag not in ASSET_TAGS:
            asset_tag = "cabinet"

        if asset_tag == "door":
            grid_w = max(1, min(GRID_WIDTH, _integer(raw.get("grid_w"), 1)))
            grid_h = max(1, min(GRID_HEIGHT, _integer(raw.get("grid_h"), 1)))
        else:
            grid_w, grid_h = ASSET_SIZES.get(asset_tag, (1, 1))
            grid_w = max(1, min(GRID_WIDTH, grid_w))
            grid_h = max(1, min(GRID_HEIGHT, grid_h))
        grid_x = max(0, min(GRID_WIDTH - grid_w, _integer(raw.get("grid_x"), 0)))
        grid_y = max(0, min(GRID_HEIGHT - grid_h, _integer(raw.get("grid_y"), 0)))
        if grid_x + grid_w > GRID_WIDTH:
            grid_x = GRID_WIDTH - grid_w
        if grid_y + grid_h > GRID_HEIGHT:
            grid_y = GRID_HEIGHT - grid_h

        interact_item = str(raw.get("interact_item", "") or "").strip()
        if interact_item not in item_names or interact_item in bound_items:
            interact_item = ""
        if interact_item:
            bound_items.add(interact_item)
        obstacles.append({
            "asset_tag": asset_tag,
            "grid_x": grid_x,
            "grid_y": grid_y,
            "grid_w": grid_w,
            "grid_h": grid_h,
            "interact_item": interact_item,
            "lean_wall": bool(raw.get("lean_wall", False)),
        })

    return obstacles


def _normalize_wall_decor(raw_decor: list) -> list:
    """墙体附带品（窗户等）：贴墙渲染、无碰撞，只保留 asset_tag 与网格坐标。"""
    decor = []
    for raw in raw_decor if isinstance(raw_decor, list) else []:
        if not isinstance(raw, dict):
            continue
        asset_tag = raw.get("asset_tag")
        if asset_tag not in WALL_DECOR_TAGS:
            continue
        grid_w, grid_h = ASSET_SIZES.get(asset_tag, (1, 1))
        grid_w = max(1, min(GRID_WIDTH, grid_w))
        grid_h = max(1, min(GRID_HEIGHT, grid_h))
        grid_x = max(0, min(GRID_WIDTH - grid_w, _integer(raw.get("grid_x"), 0)))
        grid_y = max(0, min(GRID_HEIGHT - grid_h, _integer(raw.get("grid_y"), 0)))
        decor.append({
            "asset_tag": asset_tag,
            "grid_x": grid_x,
            "grid_y": grid_y,
            "grid_w": grid_w,
            "grid_h": grid_h,
        })
    return decor


def _normalize_walls(raw_walls: list) -> list:
    walls = []
    for raw in raw_walls if isinstance(raw_walls, list) else []:
        if not isinstance(raw, dict):
            continue
        grid_w = max(1, min(GRID_WIDTH, _integer(raw.get("grid_w"), 1)))
        grid_h = max(1, min(GRID_HEIGHT, _integer(raw.get("grid_h"), 1)))
        grid_x = max(0, min(GRID_WIDTH - 1, _integer(raw.get("grid_x"), 0)))
        grid_y = max(0, min(GRID_HEIGHT - 1, _integer(raw.get("grid_y"), 0)))
        if grid_x + grid_w > GRID_WIDTH:
            grid_x = GRID_WIDTH - grid_w
        if grid_y + grid_h > GRID_HEIGHT:
            grid_y = GRID_HEIGHT - grid_h

        # 墙类型按朝向自动判定：横向墙 / 纵向墙，前端据此选贴图
        asset_tag = "wall_h" if grid_w >= grid_h else "wall_v"
        walls.append({
            "grid_x": grid_x,
            "grid_y": grid_y,
            "grid_w": grid_w,
            "grid_h": grid_h,
            "asset_tag": asset_tag,
        })
    return walls


def _normalize_items(raw_items: list, item_names: list, obstacles: list, walls: list = None) -> list:
    raw_by_name = {}
    for raw in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "") or "").strip()
        if name in item_names and name not in raw_by_name:
            raw_by_name[name] = raw

    normalized = []
    reachable = _reachable_cells(obstacles, walls)
    walls = walls or []
    for item_name in item_names:
        raw = raw_by_name.get(item_name, {})
        bound = next((ob for ob in obstacles if ob.get("interact_item") == item_name), None)
        if bound:
            default_x, default_y = bound["grid_x"], bound["grid_y"]
        else:
            default_x, default_y = 14, 14

        grid_x = max(0, min(GRID_WIDTH - 1, _integer(raw.get("grid_x"), default_x)))
        grid_y = max(0, min(GRID_HEIGHT - 1, _integer(raw.get("grid_y"), default_y)))
        # items are strictly 1x1
        grid_w = 1
        grid_h = 1
        if grid_x + grid_w > GRID_WIDTH:
            grid_x = GRID_WIDTH - grid_w
        if grid_y + grid_h > GRID_HEIGHT:
            grid_y = GRID_HEIGHT - grid_h

        item_rect = {"grid_x": grid_x, "grid_y": grid_y, "grid_w": 1, "grid_h": 1}
        needs_relocate = False
        non_bound_obstacles = [ob for ob in obstacles if ob is not bound]
        if any(_rectangles_overlap(item_rect, ob) for ob in non_bound_obstacles):
            needs_relocate = True
        if bound is None and (grid_x, grid_y) not in reachable:
            needs_relocate = True
        if bound is not None:
            in_bound = (
                bound["grid_x"] <= grid_x < bound["grid_x"] + bound["grid_w"]
                and bound["grid_y"] <= grid_y < bound["grid_y"] + bound["grid_h"]
            )
            if not in_bound:
                needs_relocate = True

        if needs_relocate:
            valid_positions = []
            for y in range(GRID_HEIGHT):
                for x in range(GRID_WIDTH):
                    rect = {"grid_x": x, "grid_y": y, "grid_w": 1, "grid_h": 1}
                    if bound is None:
                        if any(_rectangles_overlap(rect, ob) for ob in obstacles):
                            continue
                        if any(_rectangles_overlap(rect, w) for w in walls):
                            continue
                        if (x, y) not in reachable:
                            continue
                    else:
                        if any(_rectangles_overlap(rect, ob) for ob in non_bound_obstacles):
                            continue
                        if any(_rectangles_overlap(rect, w) for w in walls):
                            continue
                        if not (
                            bound["grid_x"] <= x < bound["grid_x"] + bound["grid_w"]
                            and bound["grid_y"] <= y < bound["grid_y"] + bound["grid_h"]
                        ):
                            continue
                    valid_positions.append((x, y))
            if not valid_positions:
                raise RuntimeError(f"[scene_layout_genesis] 无法为物品「{item_name}」找到合法坐标")
            grid_x, grid_y = min(
                valid_positions,
                key=lambda pos: ((pos[0] - grid_x) ** 2 + (pos[1] - grid_y) ** 2, pos[1], pos[0]),
            )

        normalized.append({
            "name": item_name,
            "grid_x": grid_x,
            "grid_y": grid_y,
            "grid_w": 1,
            "grid_h": 1,
        })
    return normalized


def _cells(obstacle: dict):
    for grid_y in range(obstacle["grid_y"], obstacle["grid_y"] + obstacle["grid_h"]):
        for grid_x in range(obstacle["grid_x"], obstacle["grid_x"] + obstacle["grid_w"]):
            yield grid_x, grid_y


def _occupied_cells(obstacles: list) -> set:
    return {cell for obstacle in obstacles for cell in _cells(obstacle)}


def _interaction_cells(obstacle: dict) -> set:
    x0 = obstacle["grid_x"]
    y0 = obstacle["grid_y"]
    x1 = x0 + obstacle["grid_w"] - 1
    y1 = y0 + obstacle["grid_h"] - 1
    return {
        (x, y)
        for y in range(max(0, y0 - 1), min(GRID_HEIGHT, y1 + 2))
        for x in range(max(0, x0 - 1), min(GRID_WIDTH, x1 + 2))
        if not (x0 <= x <= x1 and y0 <= y <= y1)
    }


def _covers_spawn(obstacle: dict) -> bool:
    # 出生点格子 (SPAWN_X,8) 与 (SPAWN_X,9)，障碍物不得覆盖
    spawn_cells = {(SPAWN_X, 8), (SPAWN_X, 9)}
    return bool(set(_cells(obstacle)) & spawn_cells)


def _reachable_cells(obstacles: list, walls: list = None) -> set:
    blocked = set()
    for wall in walls or []:
        blocked.update(_cells(wall))
    for obstacle in obstacles:
        if obstacle.get("asset_tag") == "door":
            continue
        blocked.update(_cells(obstacle))
    # door cells are passable even if embedded in walls
    for obstacle in obstacles:
        if obstacle.get("asset_tag") == "door":
            for cell in _cells(obstacle):
                blocked.discard(cell)
    spawn_cells = {(SPAWN_X, 8), (SPAWN_X, 9)}
    reachable = {cell for cell in spawn_cells if cell not in blocked}
    queue = deque(reachable)
    while queue:
        x, y = queue.popleft()
        for next_cell in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            nx, ny = next_cell
            if (
                0 <= nx < GRID_WIDTH
                and 0 <= ny < GRID_HEIGHT
                and next_cell not in blocked
                and next_cell not in reachable
            ):
                reachable.add(next_cell)
                queue.append(next_cell)
    return reachable


def _unreachable_interactables(obstacles: list, reachable: set = None, walls: list = None) -> list:
    reachable = _reachable_cells(obstacles, walls) if reachable is None else reachable
    return [
        index
        for index, obstacle in enumerate(obstacles)
        if obstacle.get("interact_item", "") and not (_interaction_cells(obstacle) & reachable)
    ]


def _rectangles_overlap(left: dict, right: dict) -> bool:
    return not (
        left["grid_x"] + left["grid_w"] <= right["grid_x"]
        or right["grid_x"] + right["grid_w"] <= left["grid_x"]
        or left["grid_y"] + left["grid_h"] <= right["grid_y"]
        or right["grid_y"] + right["grid_h"] <= left["grid_y"]
    )


def _nearest_placement(obstacles: list, index: int, preserve_indices: set, allow_overlap: bool = False, walls: list = None):
    walls = walls or []
    original = obstacles[index]
    original = obstacles[index]
    sizes = []
    width, height = original["grid_w"], original["grid_h"]
    while True:
        sizes.append((width, height))
        if width == 1 and height == 1:
            break
        if width >= height and width > 1:
            width -= 1
        elif height > 1:
            height -= 1
        else:
            width -= 1

    for width, height in sizes:
        positions = [
            (x, y)
            for y in range(GRID_HEIGHT - height + 1)
            for x in range(GRID_WIDTH - width + 1)
        ]
        positions.sort(
            key=lambda pos: (
                (pos[0] + width / 2 - SPAWN_X) ** 2 + (pos[1] + height / 2 - SPAWN_Y) ** 2,
                pos[1],
                pos[0],
            )
        )
        for x, y in positions:
            candidate = {**original, "grid_x": x, "grid_y": y, "grid_w": width, "grid_h": height}
            other_obstacles = [item for other_index, item in enumerate(obstacles) if other_index != index]
            if not allow_overlap and any(_rectangles_overlap(candidate, other) for other in other_obstacles):
                continue
            if any(_rectangles_overlap(candidate, w) for w in walls):
                continue
            if _covers_spawn(candidate):
                continue
            proposed = list(obstacles)
            proposed[index] = candidate
            reachable = _reachable_cells(proposed, walls)
            if not (_interaction_cells(candidate) & reachable):
                continue
            if any(
                not (_interaction_cells(proposed[other_index]) & reachable)
                for other_index in preserve_indices
                if other_index != index
            ):
                continue
            return x, y, width, height
    return None


def _nearest_nonoverlap_position(obstacles: list, index: int, walls: list = None):
    walls = walls or []
    obstacle = obstacles[index]
    width, height = obstacle["grid_w"], obstacle["grid_h"]
    positions = [
        (x, y)
        for y in range(GRID_HEIGHT - height + 1)
        for x in range(GRID_WIDTH - width + 1)
    ]
    positions.sort(
        key=lambda pos: (
            (pos[0] + width / 2 - SPAWN_X) ** 2 + (pos[1] + height / 2 - SPAWN_Y) ** 2,
            pos[1],
            pos[0],
        )
    )
    others = [other for other_index, other in enumerate(obstacles) if other_index != index]
    for x, y in positions:
        candidate = {**obstacle, "grid_x": x, "grid_y": y}
        if any(_rectangles_overlap(candidate, other) for other in others):
            continue
        if any(_rectangles_overlap(candidate, w) for w in walls):
            continue
        if _covers_spawn(candidate):
            continue
        return x, y
    return None


def _resolve_overlaps(obstacles: list, walls: list = None) -> list:
    walls = walls or []
    obstacles = [dict(obstacle) for obstacle in obstacles]
    index = 0
    while index < len(obstacles):
        obstacle = obstacles[index]
        overlaps_other = any(
            _rectangles_overlap(obstacle, other)
            for other_index, other in enumerate(obstacles)
            if other_index != index
        )
        overlaps_wall = (
            obstacle.get("asset_tag") != "door"
            and any(_rectangles_overlap(obstacle, wall) for wall in walls)
        )
        covers_spawn = (
            obstacle.get("asset_tag") != "door"
            and _covers_spawn(obstacle)
        )
        if not overlaps_other and not overlaps_wall and not covers_spawn:
            index += 1
            continue

        if obstacle.get("interact_item", ""):
            reachable = _reachable_cells(obstacles, walls)
            preserve = {
                other_index
                for other_index, other in enumerate(obstacles)
                if other_index != index
                and other.get("interact_item", "")
                and _interaction_cells(other) & reachable
            }
            placement = _nearest_placement(obstacles, index, preserve, walls=walls)
            if placement:
                x, y, width, height = placement
                obstacles[index].update({
                    "grid_x": x,
                    "grid_y": y,
                    "grid_w": width,
                    "grid_h": height,
                })
            index += 1
            continue

        if obstacle.get("asset_tag") == "door":
            index += 1
            continue

        position = _nearest_nonoverlap_position(obstacles, index, walls=walls)
        if position:
            obstacles[index]["grid_x"], obstacles[index]["grid_y"] = position
            index += 1
        elif obstacle.get("asset_tag") == "door":
            index += 1
        else:
            print("[场景布局校验] 无法无重叠放置非交互障碍物，已移除", file=sys.stderr)
            del obstacles[index]
    return obstacles


def _remove_route_blockers(obstacles: list, walls: list = None) -> list:
    walls = walls or []
    obstacles = [dict(obstacle) for obstacle in obstacles]
    while _unreachable_interactables(obstacles, None, walls):
        removable = [
            index for index, obstacle in enumerate(obstacles)
            if not obstacle.get("interact_item", "") and obstacle.get("asset_tag") not in ("door",)
        ]
        if not removable:
            break

        current_reachable = _reachable_cells(obstacles, walls)
        current_unreachable = len(_unreachable_interactables(obstacles, current_reachable, walls))
        best_index = None
        best_score = None
        for index in removable:
            candidate = obstacles[:index] + obstacles[index + 1:]
            candidate_reachable = _reachable_cells(candidate, walls)
            candidate_unreachable = len(_unreachable_interactables(candidate, candidate_reachable, walls))
            score = (
                current_unreachable - candidate_unreachable,
                len(candidate_reachable) - len(current_reachable),
            )
            if best_score is None or score > best_score:
                best_index, best_score = index, score

        del obstacles[best_index]
        print(
            f"[场景布局校验] 移除阻路且无交互物品的障碍物 ({best_score})",
            file=sys.stderr,
        )
    return obstacles


def _repack_interactables(obstacles: list, walls: list = None) -> list:
    walls = walls or []
    repacked = []
    for obstacle in obstacles:
        if not obstacle.get("interact_item", ""):
            continue
        repacked.append(dict(obstacle))
        index = len(repacked) - 1
        preserve = {
            other_index for other_index, other in enumerate(repacked)
            if other_index != index and other.get("interact_item", "")
        }
        placement = _nearest_placement(repacked, index, preserve, walls=walls)
        if placement is None:
            placement = _nearest_placement(repacked, index, preserve, allow_overlap=True, walls=walls)
        if placement is None:
            raise RuntimeError(
                f"[scene_layout_genesis] 无法为交互物品「{obstacle.get('interact_item', '')}」找到可达位置"
            )
        x, y, width, height = placement
        repacked[index].update({
            "grid_x": x,
            "grid_y": y,
            "grid_w": width,
            "grid_h": height,
        })
    return repacked


def _ensure_reachable(obstacles: list, walls: list = None) -> list:
    walls = walls or []
    obstacles = _remove_route_blockers(obstacles, walls)
    for _ in range(max(1, len(obstacles) * 2)):
        reachable = _reachable_cells(obstacles, walls)
        unreachable = _unreachable_interactables(obstacles, reachable, walls)
        if not unreachable:
            return obstacles
        moved = False
        for index in unreachable:
            reachable = _reachable_cells(obstacles, walls)
            preserve = {
                other_index
                for other_index, obstacle in enumerate(obstacles)
                if other_index != index
                and obstacle.get("interact_item", "")
                and _interaction_cells(obstacle) & reachable
            }
            placement = _nearest_placement(obstacles, index, preserve, walls=walls)
            if placement is None:
                placement = _nearest_placement(obstacles, index, preserve, allow_overlap=True, walls=walls)
            if placement is None:
                continue
            x, y, width, height = placement
            obstacles[index].update({
                "grid_x": x,
                "grid_y": y,
                "grid_w": width,
                "grid_h": height,
            })
            moved = True
        if not moved:
            break

    if _unreachable_interactables(obstacles, None, walls):
        obstacles = _repack_interactables(obstacles, walls)
    if _unreachable_interactables(obstacles, None, walls):
        missing = [obstacles[index].get("interact_item", "") for index in _unreachable_interactables(obstacles, None, walls)]
        raise RuntimeError(f"[scene_layout_genesis] 交互物品仍不可达：{missing}")
    return obstacles

def _snap_wall_decor(decor: list, walls: list) -> list:
    """把墙体附带品（窗户等）整体放进最近的墙立面，不悬空。"""
    if not walls:
        return decor
    for d in decor:
        gw = d.get("grid_w", 1)
        gh = d.get("grid_h", 1)
        cx = d["grid_x"] + gw / 2.0
        cy = d["grid_y"] + gh / 2.0

        best_wall = None
        best_dist = float("inf")
        for w in walls:
            wcx = w["grid_x"] + w["grid_w"] / 2.0
            wcy = w["grid_y"] + w["grid_h"] / 2.0
            dist = (cx - wcx) ** 2 + (cy - wcy) ** 2
            if dist < best_dist:
                best_dist = dist
                best_wall = w
        if best_wall is None:
            continue

        w = best_wall
        wcx = w["grid_x"] + w["grid_w"] / 2.0
        wcy = w["grid_y"] + w["grid_h"] / 2.0
        new_x, new_y = d["grid_x"], d["grid_y"]

        if w["grid_w"] >= w["grid_h"] * 2:
            if wcy < cy:
                new_y = w["grid_y"] + w["grid_h"] - gh  # 贴上方墙：窗底=墙底
            else:
                new_y = w["grid_y"]  # 贴下方墙：窗顶=墙顶
        else:
            if wcx < cx:
                new_x = w["grid_x"] + w["grid_w"] - gw  # 贴左墙：窗右边=墙右
            else:
                new_x = w["grid_x"]  # 贴右墙：窗左边=墙左

        d["grid_x"] = max(0, min(GRID_WIDTH - gw, new_x))
        d["grid_y"] = max(0, min(GRID_HEIGHT - gh, new_y))
    return decor


async def generate_layout(scene_name: str, scene_desc: str, items: list) -> dict:
    item_names = _item_names(items)
    prompt_context = {
        "scene_name": str(scene_name or ""),
        "scene_desc": str(scene_desc or ""),
        "items": item_names,
    }
    prompt = f"""你是2D游戏场景布局设计师,只为当前单个场景生成布局,不得引用其他场景.
[输出铁律] 只输出严格JSON,无解释/注释/markdown/额外字段.
[单场景资料]
{json.dumps(prompt_context, ensure_ascii=False, indent=2)}

[坐标与网格]
画布30列*17行,原点左上,坐标整数,x范围0-30,y范围0-17,矩形左上角,grid_w/grid_h至少为1,且grid_x+grid_w<=30,grid_y+grid_h<=17.
asset_tag白名单:{json.dumps(ASSET_TAGS, ensure_ascii=False)}.obstacles禁用door,door单独输出;每个输入物品在items中必须且只能出现一次,name与输入完全一致.

[interact_item字段]
- interact_item:该障碍物绑定(携带)的可调查物品名,物品放在障碍物格内,玩家靠近即可调查;一个障碍物最多绑一个物品,不绑填空字符串"".door不输出此字段.

[lean_wall字段]
- lean_wall:该障碍物是否贴最后面的上方横墙,布尔值true/false,每个障碍物必须输出.
- 仅当障碍物顶边与某段上方横墙底边相接(grid_y = 该横墙grid_y + 该横墙grid_h)时为true;贴侧墙、贴下方墙、不靠墙一律false.
- lean_wall=true时不得绑定可调查物品,即interact_item必须为"".

[空间类型]
按场景描述选择空间类型并生成墙(walls).所有墙都必须是"单段直墙"(单段矩形:横墙grid_w>grid_h或竖墙grid_h>grid_w),严禁生成L形、拐角、T形或任何多段组合墙;确实需要拐角时必须拆成两段独立直墙输出:
1. room封闭室内(宿舍/办公室):仅1段上方横墙(w=30充满宽度,h=4~6),不要任何侧墙或下墙,其余边界由画面自然封闭.
2. corridor走廊:仅上下2段独立平行横墙,各为单段直墙,h=5~6,通行区约占高度1/3,禁止在两端补竖墙.
3. alley巷子:仅左右2段独立平行竖墙,各为单段直墙,w=8~10,通行区约占宽度1/3,禁止在两端补横墙.
4. open室外(操场/天台):walls为空数组.

[门(door)规则]
必须生成1个门,是否嵌入墙自行判断;嵌入时水平墙门底与墙底对齐、垂直墙门边与墙边对齐;open场景门随机.

[障碍物(obstacles)规则]
- 种类/数量/位置按场景描述自行判断;不得与墙、门重叠,不得覆盖出生点(15,8.5),彼此留至少1格走道,不可堵死门或出生点.
- 带interact_item的障碍物,其必须能够从出生点到达.
- 尺寸由系统按asset_tag自动套用(只需给asset_tag、grid_x、grid_y):床6*4,桌4*4,椅2*2,沙发6*4,书架4*6,柜4*4,电视4*2,植物2*2,树4*4,岩石4*4,箱子2*2,柜台6*4.
- 障碍物可以贴上方横墙(lean_wall=true), 贴墙障碍物不得绑定可携带可调查物品(interact_item="").    

[调查物品(items)规则]
严格1*1,优先放障碍物表面或空地边缘;从出生点(15,8.5)必须可达,不可在墙内.

[输出结构]
{{
  "bg_prompt": "场景背景描述",
  "walls": [
    {{"grid_x": 0, "grid_y": 0, "grid_w": 30, "grid_h": 2}}
  ],
  "door": {{"asset_tag": "door", "grid_x": 15, "grid_y": 0, "grid_w": 1, "grid_h": 2}},
  "obstacles": [
    {{"asset_tag": "table", "grid_x": 12, "grid_y": 9, "grid_w": 4, "grid_h": 4, "interact_item": "", "lean_wall": false}}
  ],
  
  "items": [
    {{"name": "物品名", "grid_x": 22, "grid_y": 11, "grid_w": 1, "grid_h": 1}}
  ]
}}"""

    try:
        response = await scene_layout_llm.ainvoke([HumanMessage(content=prompt)])
        parsed = _parse_json(response.content)
    except Exception as error:
        print(f"[场景布局生成失败] {type(error).__name__}: {str(error)[:300]}", file=sys.stderr)
        raise RuntimeError(f"[scene_layout_genesis] 场景「{scene_name}」布局生成失败：{error}") from error

    if not isinstance(parsed, dict):
        raise RuntimeError("[scene_layout_genesis] 返回格式错误：顶层必须是JSON对象")
    raw_walls = parsed.get("walls", [])
    if not isinstance(raw_walls, list):
        raw_walls = []
    walls = _normalize_walls(raw_walls)

    raw_bg_prompt = parsed.get("bg_prompt")
    raw_obstacles = parsed.get("obstacles")
    if not isinstance(raw_obstacles, list):
        raise RuntimeError("[scene_layout_genesis] 返回格式错误：obstacles必须是数组")

    # 窗(window)等墙体附带品从障碍物中剥离，单独走 wall_decor
    normal_raw = [
        ob for ob in raw_obstacles
        if not (isinstance(ob, dict) and ob.get("asset_tag") in ("door", "window"))
    ]
    obstacles = _normalize_obstacles(normal_raw, item_names)

    raw_decor = parsed.get("wall_decor", [])
    if not isinstance(raw_decor, list):
        raw_decor = []
    # 兼容：LLM 若仍把 window 写进 obstacles，也归入 wall_decor
    obstacle_windows = [
        ob for ob in raw_obstacles
        if isinstance(ob, dict) and ob.get("asset_tag") == "window"
    ]
    wall_decor = _normalize_wall_decor(raw_decor + obstacle_windows)

    raw_door = parsed.get("door", parsed.get("doors"))
    if isinstance(raw_door, list):
        raw_door = raw_door[0] if raw_door else None
    door_raw = [raw_door] if isinstance(raw_door, dict) else []
    door_obstacles = _normalize_obstacles(door_raw, [])
    for ob in door_obstacles:
        ob["asset_tag"] = "door"
        ob.pop("interact_item", None)
    if not door_obstacles:
        raise RuntimeError("[scene_layout_genesis] 返回格式错误：door必须是对象")

    all_obstacles = obstacles + door_obstacles
    all_obstacles = _resolve_overlaps(all_obstacles, walls)
    all_obstacles = _ensure_reachable(all_obstacles, walls)

    wall_decor = _snap_wall_decor(wall_decor, walls)

    obstacles_result = [dict(ob) for ob in all_obstacles if ob.get("asset_tag") != "door"]
    door_result = next((dict(ob) for ob in all_obstacles if ob.get("asset_tag") == "door"), dict(door_obstacles[0]))
    door_result.pop("interact_item", None)

    raw_items = parsed.get("items")
    if not isinstance(raw_items, list):
        raw_items = parsed.get("item_positions") if isinstance(parsed.get("item_positions"), list) else parsed.get("item_layouts")
    if not isinstance(raw_items, list):
        raw_items = []
    items_layout = _normalize_items(raw_items, item_names, all_obstacles, walls)

    if BG_PROMPT_TEMPLATE.strip():
        bg_prompt = f"{BG_PROMPT_TEMPLATE.strip()} {str(scene_desc or '').strip()}".strip()
    else:
        raw_bg = str(raw_bg_prompt or "").strip()
        if not raw_bg:
            raise RuntimeError("[scene_layout_genesis] 返回格式错误：bg_prompt必须是非空字符串")
        bg_prompt = f"{raw_bg} {BG_STYLE_SUFFIX}".strip()

    return {
        "bg_prompt": bg_prompt,
        "walls": walls,
        "obstacles": obstacles_result,
        "wall_decor": wall_decor,
        "items": items_layout,
        "door": door_result,
    }
