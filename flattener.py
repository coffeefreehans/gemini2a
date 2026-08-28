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
    """消息指纹：用于网页端镜像同步时判断哪些消息是新增的。"""
    role = message.get("role", "user")
    return f"{role}|{len(_content_to_text(message.get('content'), []))}|{_content_to_text(message.get('content'), [])[:200]}"


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
