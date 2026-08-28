"""把 OpenAI 格式的 messages 数组压平成一条可粘贴进 Gemini 网页输入框的 prompt。"""

_ROLE_HEADERS = {
    "system": "System",
    "developer": "System",
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool",
}


def _content_to_text(content, dropped_images: list) -> str:
    """content 可能是字符串，也可能是 OpenAI 多模态数组，只保留文本部分。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type", "").startswith("image"):
                    dropped_images.append(item.get("type"))
        return "\n".join(p.strip() for p in parts if p).strip()
    if isinstance(content, dict):  # 少数客户端直接给 {text: ...}
        return _content_to_text(content.get("text"), dropped_images)
    return str(content)


def message_signature(message: dict) -> str:
    """消息指纹：全量 sha1，杜绝“前200字相同”导致的定位错乱。"""
    import hashlib

    role = message.get("role", "user")
    body = _content_to_text(message.get("content"), [])
    return f"{role}|{len(body)}|{hashlib.sha1(body.encode('utf-8')).hexdigest()[:16]}"


def common_prefix_len(a: str, b: str, limit: int = 200_000) -> int:
    """两段文本的最长公共前缀长度（截断保护）。"""
    n = min(len(a), len(b), limit)
    if a[:n] == b[:n]:
        return n
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if a[:mid] == b[:mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


def delta_suffix(prev_text: str, new_text: str) -> str:
    """
    agent 消息通常是“注入头 + 新指令”，且各轮注入头基本相同。
    返回 new_text 相对 prev_text 的真实增量后缀；无增量时返回原文
    （防重复提交由同步层保证，这里不做二次判断）。
    """
    if prev_text and new_text.startswith(prev_text):
        suffix = new_text[len(prev_text):]
        return suffix if suffix.strip() else new_text
    # 前缀不同：尝试“旧消息尾部 == 新消息开头”的重叠（agent 重排上下文时）
    if prev_text:
        overlap = min(len(prev_text), len(new_text), 50_000)
        best = 0
        for k in range(overlap, 0, -1):
            if prev_text.endswith(new_text[:k]):
                best = k
                break
        if best and new_text[best:].strip():
            return new_text[best:]
        if best:
            return ""
    return new_text


def flatten_messages(messages: list) -> str:
    """
    规则：
    - 只有一条 user 消息时原样发送（不添加任何包装）；
    - 有 system / 多轮历史时按 “System: ... User: ... Assistant: ...” 结构拼接，
      结尾追加 “Assistant:” 引导模型接着作答。
    """
    dropped: list = []
    parts = [
        (m.get("role", "user"), _content_to_text(m.get("content"), dropped))
        for m in messages
    ]
    parts = [(r, c) for r, c in parts if c]

    if len(dropped) > 1 or (dropped and len(parts) > 0):
        # 不中断请求，只在服务端日志可见
        print(f"[gemini2a] 丢弃了 {len(dropped)} 个非文本内容块(图片/音频等网页版桥暂不支持)")

    if not parts:
        raise ValueError("messages 里没有任何文本内容")

    if len(parts) == 1 and parts[0][0] == "user":
        return parts[0][1]

    lines = [f"{_ROLE_HEADERS.get(role, role.title())}:\n{content}" for role, content in parts]
    lines.append("Assistant:")
    return "\n\n".join(lines)
