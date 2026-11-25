import re


def parse_npc_output(full: str) -> dict:
    """解析模型输出的标签与双段分隔符。"""
    res = {
        "aff_change": 0,
        "consent": False,
        "consented_items": [],
        "options": [],
        "weakness_hit": None,
        "surrender_items": [],
        "emotion_tags": [],
        "ripple": "",
        "intent": None,
    }

    # 一段话回复（无余波）：整段作为主响应，仅清洗可能的残留 RIPPLE 分隔符
    main_raw = re.sub(r"^\s*\|\|\|\s*RIPPLE\s*\|\|\|?\s*", "", full, flags=re.IGNORECASE).strip()
    res["ripple"] = ""

    # 2. 标签解析使用完整原始输出 full（防止标签写在余波段漏检）
    # aff
    m = re.search(r"[\[【(（]\s*aff\s*[:：]\s*([+-]?\d+)\s*[\]】)）]", full)
    if m:
        res["aff_change"] = max(-40, min(40, int(m.group(1))))

    # do_items / do
    m_items = re.search(r"[\[【(（]\s*do_items\s*[:：]\s*([^\]】)）]+)[\]】)）]", full, re.I)
    if m_items:
        res["consented_items"] = [x.strip() for x in re.split(r"[,，、\s]+", m_items.group(1)) if x.strip()]
        res["consent"] = True
    else:
        m_do = re.search(r"[\[【(（]\s*do\s*[:：]\s*([yn])\s*[\]】)）]", full, re.I)
        res["consent"] = bool(m_do) and m_do.group(1).lower() == "y"

    # weakness: hit / miss
    m_w = re.search(r"[\[【(（]\s*weakness\s*[:：]\s*(hit|miss)\s*[\]】)）]", full, re.I)
    if m_w:
        res["weakness_hit"] = (m_w.group(1).lower() == "hit")

    # surrender: 物品A, 物品B
    m_s = re.search(r"[\[【(（]\s*surrender\s*[:：]\s*([^\]】)）]+)[\]】)）]", full, re.I)
    if m_s:
        res["surrender_items"] = [x.strip() for x in re.split(r"[,，、\s]+", m_s.group(1)) if x.strip()]

    # emotion
    m_emo = re.search(r"[\[【(（]\s*emotion\s*[:：]\s*([^\]】)）]+)[\]】)）]", full, re.I)
    if m_emo:
        res["emotion_tags"] = [x.strip() for x in re.split(r"[,，、|｜]+", m_emo.group(1)) if x.strip()]

    # intent（严格限制在白名单内，拒绝 wear/chat/refuse 等幻觉）
    m_int = re.search(r"[\[【(（]\s*intent\s*[:：]\s*([^\]】)）]+)[\]】)）]", full, re.I)
    if m_int:
        parts = [x.strip() for x in re.split(r"[|｜]", m_int.group(1))]
        if parts and parts[0]:
            _kind = parts[0].lower()
            if _kind in ("idle", "move", "take", "give", "put_on", "take_off", "chat", "find", "follow", "trade", "guard", "flee"):
                res["intent"] = {
                    "kind": _kind,
                    "target": parts[1] if len(parts) > 1 else "",
                    "detail": parts[2] if len(parts) > 2 else "",
                }
    
    # 意图兜底：如果模型没输出合法的意图，强制归为 idle
    if not res["intent"]:
        res["intent"] = {"kind": "idle", "target": "", "detail": ""}

    # options（快捷选项）已废弃：恒为空，前端不再显示三选一
    res["options"] = []

    # 清洗正文（只清洗主响应段，保留余波的纯净）
    content = re.sub(r"[\[【]\s*aff\s*[:：]\s*[+-]?\d+\s*[\]】]?", "", main_raw, flags=re.I)
    content = re.sub(r"[\[【]\s*do\s*[:：]\s*[yn]\s*[\]】]?", "", content, flags=re.I)
    content = re.sub(r"[\[【]\s*do_items\s*[:：][^\]】\n]*[\]】]?", "", content, flags=re.I)
    content = re.sub(r"[\[【]\s*weakness\s*[:：]\s*(?:hit|miss)\s*[\]】]", "", content, flags=re.I)
    content = re.sub(r"[\[【]\s*surrender\s*[:：][^\]】]*[\]】]", "", content, flags=re.I)
    content = re.sub(r"[\[【]\s*emotion\s*[:：][^\]】]*[\]】]", "", content, flags=re.I)
    content = re.sub(r"[\[【]\s*intent\s*[:：][^\n\]】]*[\]】]?", "", content, flags=re.I)
    content = re.sub(r"[\[【(（]\s*options?\s*[:：][^\]】)）\n]*(?:[\]】)）]|$)", "", content, flags=re.I | re.M)
    content = re.sub(r"\|\|\|\s*RIPPLE\s*\|\|\|?", "", content, flags=re.I) # 清洗漏网的残缺分隔符
    content = re.sub(r"[\[【][^\]】]*$", "", content).strip()
    res["content"] = content if content else "（没有说话。）"
    
    return res