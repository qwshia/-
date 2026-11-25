"""动态社交网络核心模块：初始化、更新、查询、Prompt 注入。

设计：
- 图结构独立于 npcs，避免 reducer 冲突。
- NPC 为节点，NPC 间关系为有向边（A→B 与 B→A 各存一份态度），
  共享元数据（交互次数、最近轮次、连接类型）挂在 canonical key 上。
- 秘密知情为有向边：知情者 → 秘密节点。
- 共识区域、阵营、舆论、声誉、恩怨、债务为独立信息对象，沿图传播。
- known_by 单向扩展（秘密一旦透露不可收回）。
- 关系权重单次变化 ≤ ±0.02（阻尼），最终 ∈ [0,1]。
"""
import copy
import hashlib
import uuid
from typing import Any, Dict, List, Optional

from constants import (
    FLOAT_FIELD_MIN, FLOAT_FIELD_MAX,
    VALID_CONNECTION_TYPES, VALID_FACTION_RELATION_TYPES,
    VALID_RUMOR_TYPES, VALID_CONSENSUS_VISIBILITY, VALID_SOCIAL_UPDATE_OPS,
    VALID_EDGE_ATTRS, VALID_INTERACTION_TYPES,
)


def _clamp(x, lo=FLOAT_FIELD_MIN, hi=FLOAT_FIELD_MAX):
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return lo


def _canon(a: str, b: str) -> str:
    return f"{min(a, b)}||{max(a, b)}"


def secret_key(owner_id: str, fact: str) -> str:
    return f"{owner_id}::{hashlib.sha1(fact.encode('utf-8')).hexdigest()[:12]}"


def _now_tick(state: dict) -> int:
    return int((state or {}).get("world_tick") or 0)


# ============================================================
# 基础结构
# ============================================================
def empty_social_graph(npcs: Optional[Dict[str, dict]] = None) -> dict:
    graph = {
        "schema_version": 1,
        "npcs": {},
        "relationships": {},   # canonical key -> edge dict
        "secrets": {},         # secret_key -> secret dict
        "consensus_regions": {},
        "factions": {},
        "rumors": {},
        "reputation": {"global": 0.5, "factions": {}},
        "grudges": {},
        "debts": {},
    }
    for nid, info in (npcs or {}).items():
        graph["npcs"][nid] = {"name": (info or {}).get("name", nid)}
        graph["grudges"].setdefault(nid, [])
        graph["debts"].setdefault(nid, [])
    return graph


def init_secrets_from_npcs(graph: dict, npcs: Optional[Dict[str, dict]] = None) -> dict:
    g = copy.deepcopy(graph or empty_social_graph(npcs))
    for nid, info in (npcs or {}).items():
        for c in ((info or {}).get("cognition") or []):
            if not (isinstance(c, dict) and c.get("type") == "secret"):
                continue
            fact = str(c.get("content", "") or "").strip()
            if not fact:
                continue
            kb = [str(x).strip() for x in (c.get("known_by") or []) if str(x).strip()]
            if nid not in kb:
                kb = [nid] + kb
            k = secret_key(nid, fact)
            g.setdefault("secrets", {})[k] = {
                "owner": nid,
                "fact": fact,
                "known_by": list(dict.fromkeys(kb)),
                "player_knows": "player" in kb,
                "revealed_tick": None,
                "reveal_condition": str(c.get("reveal_condition", "") or "").strip(),
            }
    return g


def merge_social_graph(left: Optional[dict], right: Optional[dict]) -> dict:
    """Reducer：深合并社交图，known_by 取并集，权重取 max（单调防覆盖）。"""
    if not left:
        left = {}
    if not right:
        right = {}
    out = copy.deepcopy(left)
    # npcs 索引
    for nid, info in (right.get("npcs") or {}).items():
        out.setdefault("npcs", {}).setdefault(nid, {})
        out["npcs"][nid].update(info or {})
    # relationships
    for key, e in (right.get("relationships") or {}).items():
        cur = out.setdefault("relationships", {}).get(key)
        if cur is None:
            out["relationships"][key] = copy.deepcopy(e)
            continue
        merged = copy.deepcopy(cur)
        for attr in VALID_EDGE_ATTRS:
            if attr in (e or {}):
                merged[attr] = max(float(merged.get(attr, 0.0)), float(e.get(attr, 0.0)))
        for k in ("interactions", "last_tick"):
            if k in (e or {}):
                merged[k] = max(int(merged.get(k, 0)), int(e.get(k, 0)))
        # connection_types 并集
        ct = set(merged.get("connection_types") or [])
        ct |= set((e or {}).get("connection_types") or [])
        merged["connection_types"] = sorted(ct)
        # history 并集去重保序
        hist = list(merged.get("history") or [])
        for h in (e or {}).get("history") or []:
            if h not in hist:
                hist.append(h)
        merged["history"] = hist[-10:]
        out["relationships"][key] = merged
    # secrets：known_by 取并集
    for key, s in (right.get("secrets") or {}).items():
        cur = out.setdefault("secrets", {}).get(key)
        if cur is None:
            out["secrets"][key] = copy.deepcopy(s)
            continue
        merged = copy.deepcopy(cur)
        kb = list(merged.get("known_by") or [])
        for k in (s or {}).get("known_by") or []:
            if k not in kb:
                kb.append(k)
        merged["known_by"] = kb
        merged["player_knows"] = bool(merged.get("player_knows")) or bool((s or {}).get("player_knows"))
        if (s or {}).get("revealed_tick") is not None:
            merged["revealed_tick"] = (s or {}).get("revealed_tick")
        if (s or {}).get("reveal_condition"):
            merged["reveal_condition"] = (s or {}).get("reveal_condition")
        out["secrets"][key] = merged
    # consensus_regions
    for cid, c in (right.get("consensus_regions") or {}).items():
        cur = out.setdefault("consensus_regions", {}).get(cid)
        if cur is None:
            out["consensus_regions"][cid] = copy.deepcopy(c)
            continue
        merged = copy.deepcopy(cur)
        mems = list(merged.get("members") or [])
        for m in (c or {}).get("members") or []:
            if m not in mems:
                mems.append(m)
        merged["members"] = mems
        if "level" in (c or {}):
            merged["level"] = max(float(merged.get("level", 0.0)), float((c or {}).get("level", 0.0)))
        out["consensus_regions"][cid] = merged
    # factions
    for fid, f in (right.get("factions") or {}).items():
        cur = out.setdefault("factions", {}).get(fid)
        if cur is None:
            out["factions"][fid] = copy.deepcopy(f)
            continue
        merged = copy.deepcopy(cur)
        for k in ("members", "allies", "hostiles"):
            lst = list(merged.get(k) or [])
            for x in (f or {}).get(k) or []:
                if x not in lst:
                    lst.append(x)
            merged[k] = lst
        for k in ("goal", "name"):
            if (f or {}).get(k):
                merged[k] = (f or {}).get(k)
        out["factions"][fid] = merged
    # rumors
    for rid, r in (right.get("rumors") or {}).items():
        if rid not in (out.get("rumors") or {}):
            out.setdefault("rumors", {})[rid] = copy.deepcopy(r)
    # reputation
    if isinstance(right.get("reputation"), dict):
        cur_rep = out.setdefault("reputation", {"global": 0.5, "factions": {}})
        if "global" in right["reputation"]:
            cur_rep["global"] = right["reputation"]["global"]
        for fid, v in (right["reputation"].get("factions") or {}).items():
            cur_rep.setdefault("factions", {})[fid] = v
    # grudges / debts：按 owner 追加去重
    for field in ("grudges", "debts"):
        for owner, items in (right.get(field) or {}).items():
            cur_list = out.setdefault(field, {}).setdefault(owner, [])
            for it in (items or []):
                key = (it.get("target"), it.get("reason"))
                if not any((x.get("target"), x.get("reason")) == key for x in cur_list):
                    cur_list.append(copy.deepcopy(it))
    return out


# ============================================================
# 边操作
# ============================================================
def _edge_default(a, b):
    return {
        "a": min(a, b), "b": max(a, b),
        "familiarity": 0.1, "trust": 0.2, "tension": 0.0,
        "affinity": 0.15, "respect": 0.2, "fear": 0.0,
        "obligation": 0.0, "resentment": 0.0, "curiosity": 0.2,
        "dependency": 0.0,
        "interactions": 0, "last_tick": 0, "last_interaction_type": "",
        "connection_types": [], "history": [],
    }


def get_relationship(graph: dict, a: str, b: str) -> dict:
    key = _canon(a, b)
    e = (graph or {}).get("relationships", {}).get(key)
    if e is None:
        return _edge_default(a, b)
    out = _edge_default(a, b)
    out.update(e)
    return out


def record_interaction(graph, a, b, tick=0, df=0.0, dt=0.0, dtension=0.0,
                      daffinity=0.0, drespect=0.0, dfear=0.0, dobligation=0.0,
                      dresentment=0.0, dcuriosity=0.0, ddependency=0.0,
                      interaction_type="", note=None, damping=0.02,
                      connection_type=None):
    g = copy.deepcopy(graph or {})
    key = _canon(a, b)
    rels = g.setdefault("relationships", {})
    e = rels.get(key)
    if e is None:
        e = _edge_default(a, b)
        rels[key] = e
    # 阻尼
    def _d(v):
        return max(-damping, min(damping, v))
    e["familiarity"] = _clamp(e.get("familiarity", 0.1) + _d(df))
    e["trust"] = _clamp(e.get("trust", 0.2) + _d(dt))
    e["tension"] = _clamp(e.get("tension", 0.0) + _d(dtension))
    e["affinity"] = _clamp(e.get("affinity", 0.15) + _d(daffinity if daffinity else (df + dt - dtension) / 3.0))
    e["respect"] = _clamp(e.get("respect", 0.2) + _d(drespect))
    e["fear"] = _clamp(e.get("fear", 0.0) + _d(dfear))
    e["obligation"] = _clamp(e.get("obligation", 0.0) + _d(dobligation))
    e["resentment"] = _clamp(e.get("resentment", 0.0) + _d(dresentment))
    e["curiosity"] = _clamp(e.get("curiosity", 0.2) + _d(dcuriosity))
    e["dependency"] = _clamp(e.get("dependency", 0.0) + _d(ddependency))
    e["interactions"] = int(e.get("interactions", 0)) + 1
    e["last_tick"] = tick
    if interaction_type:
        e["last_interaction_type"] = interaction_type
    if connection_type and connection_type in VALID_CONNECTION_TYPES:
        ct = set(e.get("connection_types") or [])
        ct.add(connection_type)
        e["connection_types"] = sorted(ct)
    if note:
        hist = list(e.get("history") or [])
        hist.append(str(note)[:80])
        e["history"] = hist[-10:]
    return g


def set_relationship(graph, a, b, **fields):
    g = copy.deepcopy(graph or {})
    key = _canon(a, b)
    e = g.setdefault("relationships", {}).get(key)
    if e is None:
        e = _edge_default(a, b)
        g["relationships"][key] = e
    for k in VALID_EDGE_ATTRS:
        if k in fields:
            e[k] = _clamp(fields[k])
    if "connection_types" in fields:
        ct = set(e.get("connection_types") or [])
        ct |= set(fields["connection_types"] or [])
        e["connection_types"] = sorted(ct)
    if "note" in fields and fields["note"]:
        hist = list(e.get("history") or [])
        hist.append(str(fields["note"])[:80])
        e["history"] = hist[-10:]
    if "last_interaction_type" in fields:
        e["last_interaction_type"] = str(fields["last_interaction_type"])
    return g


def connect(graph, a, b, connection_type, tick=0, strength=0.5, note=None):
    if connection_type not in VALID_CONNECTION_TYPES:
        return graph
    g = record_interaction(graph, a, b, tick=tick, connection_type=connection_type, note=note)
    # 强度写入对应属性（若提供）
    if strength is not None:
        g = set_relationship(g, a, b, **{connection_type if connection_type in VALID_EDGE_ATTRS else "familiarity": _clamp(strength)})
    return g


def disconnect(graph, a, b, tick=0, note=None, dormant=True):
    g = copy.deepcopy(graph or {})
    key = _canon(a, b)
    e = g.get("relationships", {}).get(key)
    if e is None:
        return g
    if dormant:
        e["dormant"] = True
        e["last_tick"] = tick
        if note:
            hist = list(e.get("history") or [])
            hist.append(str(note)[:80])
            e["history"] = hist[-10:]
    else:
        g.get("relationships", {}).pop(key, None)
    return g


# ============================================================
# 秘密知情
# ============================================================
def expand_known_by(graph, owner_id, fact, new_knower, tick=None):
    g = copy.deepcopy(graph or {})
    k = secret_key(owner_id, fact)
    sec = g.setdefault("secrets", {}).setdefault(k, {
        "owner": owner_id, "fact": fact, "known_by": [owner_id],
        "player_knows": False, "revealed_tick": None,
        "reveal_condition": "",
    })
    kb = list(sec.get("known_by") or [])
    if new_knower and new_knower not in kb:
        kb.append(new_knower)
    sec["known_by"] = kb
    if new_knower == "player":
        sec["player_knows"] = True
        sec["revealed_tick"] = tick
    return g


def who_knows(graph, owner_id, fact):
    k = secret_key(owner_id, fact)
    return list((graph.get("secrets", {}).get(k) or {}).get("known_by") or [])


# ============================================================
# 共识区域
# ============================================================
def add_consensus_region(graph, theme, members, tick=0, level=0.5, visibility="secret"):
    g = copy.deepcopy(graph or {})
    cid = f"consensus::{hashlib.sha1(theme.encode('utf-8')).hexdigest()[:10]}"
    g.setdefault("consensus_regions", {})[cid] = {
        "id": cid, "theme": theme,
        "members": list(dict.fromkeys(members or [])),
        "level": _clamp(level), "visibility": visibility,
        "formed_tick": tick, "history": [],
    }
    return g


def update_consensus_region(graph, cid, members=None, level=None, visibility=None, tick=0, note=None):
    g = copy.deepcopy(graph or {})
    c = g.get("consensus_regions", {}).get(cid)
    if c is None:
        return g
    if members is not None:
        mems = list(c.get("members") or [])
        for m in members:
            if m not in mems:
                mems.append(m)
        c["members"] = mems
    if level is not None:
        c["level"] = _clamp(level)
    if visibility is not None and visibility in VALID_CONSENSUS_VISIBILITY:
        c["visibility"] = visibility
    if note:
        hist = list(c.get("history") or [])
        hist.append(str(note)[:80])
        c["history"] = hist[-10:]
    return g


def remove_from_consensus(graph, cid, member, tick=0, note=None):
    g = copy.deepcopy(graph or {})
    c = g.get("consensus_regions", {}).get(cid)
    if c is None:
        return g
    c["members"] = [m for m in (c.get("members") or []) if m != member]
    if note:
        hist = list(c.get("history") or [])
        hist.append(str(note)[:80])
        c["history"] = hist[-10:]
    # 成员空则删除区域
    if not c["members"]:
        g.get("consensus_regions", {}).pop(cid, None)
    return g


# ============================================================
# 阵营
# ============================================================
def add_faction(graph, faction_id, name, goal="", members=None, allies=None, hostiles=None, tick=0):
    g = copy.deepcopy(graph or {})
    g.setdefault("factions", {})[faction_id] = {
        "id": faction_id, "name": name, "goal": goal,
        "members": list(dict.fromkeys(members or [])),
        "allies": list(dict.fromkeys(allies or [])),
        "hostiles": list(dict.fromkeys(hostiles or [])),
        "formed_tick": tick, "history": [],
    }
    return g


def faction_join(graph, npc_id, faction_id, tick=0, loyalty=0.5):
    g = copy.deepcopy(graph or {})
    f = g.get("factions", {}).get(faction_id)
    if f is None:
        return g
    if npc_id not in (f.get("members") or []):
        f.setdefault("members", []).append(npc_id)
    g.setdefault("npcs", {}).setdefault(npc_id, {})["faction_id"] = faction_id
    g["npcs"][npc_id]["faction_loyalty"] = _clamp(loyalty)
    hist = list(f.get("history") or [])
    hist.append(f"{npc_id} 加入 (tick={tick})")
    f["history"] = hist[-10:]
    return g


def faction_leave(graph, npc_id, faction_id, tick=0, note=None):
    g = copy.deepcopy(graph or {})
    f = g.get("factions", {}).get(faction_id)
    if f is not None:
        f["members"] = [m for m in (f.get("members") or []) if m != npc_id]
    if g.get("npcs", {}).get(npc_id, {}).get("faction_id") == faction_id:
        g["npcs"][npc_id].pop("faction_id", None)
        g["npcs"][npc_id].pop("faction_loyalty", None)
    if f is not None:
        hist = list(f.get("history") or [])
        hist.append(f"{npc_id} 离开 (tick={tick})" + (f": {note}" if note else ""))
        f["history"] = hist[-10:]
    return g


def set_faction_relation(graph, faction_a, faction_b, relation_type, tick=0):
    if relation_type not in VALID_FACTION_RELATION_TYPES:
        return graph
    g = copy.deepcopy(graph or {})
    fa = g.get("factions", {}).get(faction_a)
    fb = g.get("factions", {}).get(faction_b)
    if fa is None or fb is None:
        return g
    if relation_type == "ally":
        if faction_b not in (fa.get("allies") or []):
            fa.setdefault("allies", []).append(faction_b)
        if faction_a not in (fb.get("allies") or []):
            fb.setdefault("allies", []).append(faction_a)
        fa.setdefault("hostiles", []).remove(faction_b) if faction_b in (fa.get("hostiles") or []) else None
        fb.setdefault("hostiles", []).remove(faction_a) if faction_a in (fb.get("hostiles") or []) else None
    elif relation_type == "hostile":
        if faction_b not in (fa.get("hostiles") or []):
            fa.setdefault("hostiles", []).append(faction_b)
        if faction_a not in (fb.get("hostiles") or []):
            fb.setdefault("hostiles", []).append(faction_a)
        fa.setdefault("allies", []).remove(faction_b) if faction_b in (fa.get("allies") or []) else None
        fb.setdefault("allies", []).remove(faction_a) if faction_a in (fb.get("allies") or []) else None
    return g


# ============================================================
# 舆论
# ============================================================
def create_rumor(graph, content, source, tick=0, credibility=0.5, rumor_type="fact", targets=None):
    g = copy.deepcopy(graph or {})
    rid = f"rumor::{uuid.uuid4().hex[:10]}"
    g.setdefault("rumors", {})[rid] = {
        "id": rid, "content": str(content)[:200], "source": source,
        "credibility": _clamp(credibility), "type": rumor_type,
        "heat": 1.0, "created_tick": tick,
        "known_by": list(dict.fromkeys([source] + (targets or []))),
        "history": [],
    }
    return g, rid


def spread_rumor(graph, rumor_id, from_npc, to_npc, tick=0, distortion=0.0):
    g = copy.deepcopy(graph or {})
    r = g.get("rumors", {}).get(rumor_id)
    if r is None:
        return g
    if to_npc not in (r.get("known_by") or []):
        r["known_by"] = list(r.get("known_by") or []) + [to_npc]
    # 可信度衰减 + 失真
    r["credibility"] = _clamp(r.get("credibility", 0.5) - 0.05 - abs(distortion) * 0.1)
    if distortion and abs(distortion) > 0.3:
        r["type"] = "distorted"
    r["heat"] = min(1.0, r.get("heat", 0.0) + 0.1)
    hist = list(r.get("history") or [])
    hist.append(f"{from_npc}->{to_npc} (tick={tick})")
    r["history"] = hist[-10:]
    return g


def decay_rumors(graph, tick=0, half_life=5.0):
    g = copy.deepcopy(graph or {})
    for rid, r in (g.get("rumors") or {}).items():
        age = max(0, tick - int(r.get("created_tick", tick)))
        r["heat"] = max(0.0, r.get("heat", 1.0) * (0.5 ** (age / max(1.0, half_life))))
        if r["heat"] < 0.05 and r.get("type") != "archived":
            r["type"] = "archived"
    return g


# ============================================================
# 声誉（全局/群体层）
# ============================================================
def update_reputation(graph, scope="global", faction_id=None, delta=0.0, tick=0):
    g = copy.deepcopy(graph or {})
    rep = g.setdefault("reputation", {"global": 0.5, "factions": {}})
    if scope == "global":
        rep["global"] = _clamp(rep.get("global", 0.5) + delta)
    elif scope == "faction" and faction_id:
        cur = rep.setdefault("factions", {}).get(faction_id, 0.5)
        rep["factions"][faction_id] = _clamp(cur + delta)
    return g


# ============================================================
# 恩怨 / 债务
# ============================================================
def add_grudge(graph, owner, target, reason, severity=0.5, tick=0):
    g = copy.deepcopy(graph or {})
    lst = g.setdefault("grudges", {}).setdefault(owner, [])
    lst.append({
        "target": target, "reason": str(reason)[:120],
        "severity": _clamp(severity), "tick": tick,
    })
    g["grudges"][owner] = lst[-20:]
    return g


def add_debt(graph, owner, target, reason, severity=0.5, tick=0):
    g = copy.deepcopy(graph or {})
    lst = g.setdefault("debts", {}).setdefault(owner, [])
    lst.append({
        "target": target, "reason": str(reason)[:120],
        "severity": _clamp(severity), "tick": tick,
    })
    g["debts"][owner] = lst[-20:]
    return g


def resolve_grudge(graph, owner, target, reason=None, amount=0.3):
    g = copy.deepcopy(graph or {})
    lst = g.get("grudges", {}).get(owner, [])
    new_lst = []
    for it in lst:
        if it.get("target") == target and (reason is None or it.get("reason") == reason):
            new_sev = _clamp(it.get("severity", 0.5) - amount)
            if new_sev > 0.05:
                it["severity"] = new_sev
                new_lst.append(it)
        else:
            new_lst.append(it)
    g.setdefault("grudges", {})[owner] = new_lst
    return g


def resolve_debt(graph, owner, target, reason=None, amount=0.3):
    g = copy.deepcopy(graph or {})
    lst = g.get("debts", {}).get(owner, [])
    new_lst = []
    for it in lst:
        if it.get("target") == target and (reason is None or it.get("reason") == reason):
            new_sev = _clamp(it.get("severity", 0.5) - amount)
            if new_sev > 0.05:
                it["severity"] = new_sev
                new_lst.append(it)
        else:
            new_lst.append(it)
    g.setdefault("debts", {})[owner] = new_lst
    return g


# ============================================================
# 情绪传染 / 地位层级（图层辅助）
# ============================================================
def emotion_contagion(graph, npcs, tick=0, damping=0.05):
    """基于亲密关系传播 mood，返回 mood patch dict {npc_id: new_mood_str}。"""
    patches = {}
    for nid, info in (npcs or {}).items():
        mems = []
        for key, e in (graph.get("relationships") or {}).items():
            if e.get("a") == nid or e.get("b") == nid:
                other = e["b"] if e.get("a") == nid else e["a"]
                if (e.get("familiarity", 0) >= 0.5 or e.get("trust", 0) >= 0.5):
                    mems.append((other, e))
        # 简化：取最亲密的对方 mood
        if not mems:
            continue
        mems.sort(key=lambda x: (x[1].get("familiarity", 0) + x[1].get("trust", 0)), reverse=True)
        other_id = mems[0][0]
        other_info = (npcs or {}).get(other_id) or {}
        other_mood = (other_info.get("mood") or {}).get("current")
        if other_mood and other_mood != (info.get("mood") or {}).get("current"):
            patches[nid] = other_mood
    return patches


def compute_status(npcs, graph=None):
    """从 role/self_interest/social_initiative 推导地位分数 0-1。"""
    scores = {}
    for nid, info in (npcs or {}).items():
        role = str((info or {}).get("role", "") or "")
        si = float((info or {}).get("self_interest", 0.5) or 0.5)
        si_soc = float((info or {}).get("social_initiative", 0.4) or 0.4)
        att = float((info or {}).get("attachment", 0.5) or 0.5)
        score = (si * 0.3 + si_soc * 0.3 + att * 0.2 + 0.2)
        # role 关键词加成
        if any(w in role for w in ("长官", "主管", "老板", "队长", "首领", "主", "长")):
            score = min(1.0, score + 0.2)
        scores[nid] = _clamp(score)
    return scores


# ============================================================
# 更新指令分发
# ============================================================
def apply_update(graph, update, npcs=None, tick=0):
    if not isinstance(update, dict):
        return graph
    op = str(update.get("op", "")).strip().lower()
    if op not in VALID_SOCIAL_UPDATE_OPS:
        return graph
    npcs = npcs or {}
    if op == "interact":
        return record_interaction(
            graph, update.get("a"), update.get("b"), tick=tick,
            df=float(update.get("df", 0.0)), dt=float(update.get("dt", 0.0)),
            dtension=float(update.get("dtension", 0.0)),
            interaction_type=update.get("interaction_type", ""),
            note=update.get("note"), connection_type=update.get("connection_type"),
        )
    if op == "reveal":
        return expand_known_by(graph, update.get("owner"), update.get("fact"), update.get("knower"), tick=tick)
    if op == "set":
        return set_relationship(graph, update.get("a"), update.get("b"), **(update.get("fields") or {}))
    if op == "connect":
        return connect(graph, update.get("a"), update.get("b"), update.get("connection_type"),
                      tick=tick, strength=update.get("strength", 0.5), note=update.get("note"))
    if op == "disconnect":
        return disconnect(graph, update.get("a"), update.get("b"), tick=tick, note=update.get("note"),
                         dormant=update.get("dormant", True))
    if op == "consensus_add":
        return add_consensus_region(graph, update.get("theme"), update.get("members", []),
                                   tick=tick, level=update.get("level", 0.5),
                                   visibility=update.get("visibility", "secret"))
    if op == "consensus_remove":
        return remove_from_consensus(graph, update.get("consensus_id"), update.get("member"), tick=tick, note=update.get("note"))
    if op == "faction_join":
        return faction_join(graph, update.get("npc"), update.get("faction"), tick=tick, loyalty=update.get("loyalty", 0.5))
    if op == "faction_leave":
        return faction_leave(graph, update.get("npc"), update.get("faction"), tick=tick, note=update.get("note"))
    if op == "rumor_create":
        g, _ = create_rumor(graph, update.get("content"), update.get("source"), tick=tick,
                           credibility=update.get("credibility", 0.5), rumor_type=update.get("rumor_type", "fact"),
                           targets=update.get("targets"))
        return g
    if op == "rumor_spread":
        return spread_rumor(graph, update.get("rumor_id"), update.get("from"), update.get("to"), tick=tick,
                          distortion=float(update.get("distortion", 0.0)))
    if op == "reputation_change":
        return update_reputation(graph, scope=update.get("scope", "global"), faction_id=update.get("faction"),
                               delta=float(update.get("delta", 0.0)), tick=tick)
    if op == "debt_add":
        return add_debt(graph, update.get("owner"), update.get("target"), update.get("reason", ""),
                       severity=float(update.get("severity", 0.5)), tick=tick)
    if op == "grudge_add":
        return add_grudge(graph, update.get("owner"), update.get("target"), update.get("reason", ""),
                         severity=float(update.get("severity", 0.5)), tick=tick)
    if op == "player_catalyst":
        # 玩家介入：创建/加强边
        return record_interaction(graph, update.get("a"), update.get("b"), tick=tick,
                                df=float(update.get("df", 0.05)), dt=float(update.get("dt", 0.03)),
                                note=update.get("note", "玩家催化"), connection_type=update.get("connection_type"))
    return graph


def apply_social_updates(graph, updates, npcs=None, tick=0, processed=None):
    processed = set(processed or [])
    new_processed = []
    g = graph
    for u in (updates or []):
        if not isinstance(u, dict):
            continue
        uid = str(u.get("id", "") or "")
        if uid and uid in processed:
            continue
        g = apply_update(g, u, npcs=npcs, tick=tick)
        if uid:
            new_processed.append(uid)
    return g, new_processed


# ============================================================
# 查询 / Prompt 注入
# ============================================================
def social_context_for_npc(graph, npc_id, npcs, tick=None):
    lines = []
    rels = (graph or {}).get("relationships") or {}
    for key, e in rels.items():
        if e.get("a") != npc_id and e.get("b") != npc_id:
            continue
        other = e["b"] if e.get("a") == npc_id else e["a"]
        oname = (npcs or {}).get(other, {}).get("name", other)
        ct = ",".join(e.get("connection_types") or []) or "无"
        lines.append(
            f"- {oname}：熟悉{e.get('familiarity',0):.2f}/信任{e.get('trust',0):.2f}"
            f"/紧张{e.get('tension',0):.2f}/亲和{e.get('affinity',0):.2f}"
            f"（互动{e.get('interactions',0)}次，连接：{ct}）"
        )
    sec_lines = []
    for sid, s in ((graph or {}).get("secrets") or {}).items():
        if s.get("owner") == npc_id:
            others = [x for x in (s.get("known_by") or []) if x != npc_id]
            sec_lines.append(f"- 你自己的秘密「{str(s.get('fact',''))[:24]}…」已知者：{', '.join(others) or '仅自己'}")
        elif npc_id in (s.get("known_by") or []):
            oname = (npcs or {}).get(s.get("owner"), {}).get("name", s.get("owner"))
            sec_lines.append(f"- {oname}的秘密「{str(s.get('fact',''))[:24]}…」：你知道")
    out = []
    if lines:
        out.append("[社交网络-你认识的人]\n" + "\n".join(lines))
    if sec_lines:
        out.append("[社交网络-秘密知情]\n" + "\n".join(sec_lines))
    # 共识区域
    cr_lines = []
    for cid, c in ((graph or {}).get("consensus_regions") or {}).items():
        if npc_id in (c.get("members") or []):
            cr_lines.append(f"- 共识「{c.get('theme','')}」（成员{len(c.get('members') or [])}人，一致度{c.get('level',0):.2f}）")
    if cr_lines:
        out.append("[社交网络-你所属的共识]\n" + "\n".join(cr_lines))
    # 阵营
    npc_info = (graph or {}).get("npcs", {}).get(npc_id, {})
    fid = npc_info.get("faction_id")
    if fid:
        f = (graph or {}).get("factions", {}).get(fid, {})
        out.append(f"[社交网络-阵营] {f.get('name', fid)}（忠诚度{npc_info.get('faction_loyalty', 0.5):.2f}）")
    # 恩怨/债务
    gr = [g for g in ((graph or {}).get("grudges", {}).get(npc_id) or []) if g.get("severity", 0) > 0.1]
    db = [d for d in ((graph or {}).get("debts", {}).get(npc_id) or []) if d.get("severity", 0) > 0.1]
    if gr:
        out.append("[社交网络-恩怨]\n" + "\n".join(f"- 对{g['target']}：{g['reason']}（强度{g['severity']:.2f}）" for g in gr[:5]))
    if db:
        out.append("[社交网络-债务]\n" + "\n".join(f"- 欠{d['target']}：{d['reason']}（强度{d['severity']:.2f}）" for d in db[:5]))
    return "\n".join(out)


def create_update(op, **kwargs):
    """构造一条社交更新指令，自动分配 id。"""
    u = {"id": f"{op}::{uuid.uuid4().hex[:10]}", "op": op}
    u.update(kwargs)
    return u