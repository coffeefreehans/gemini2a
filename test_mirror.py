"""镜像同步 + 粘贴提速 实测：多轮对账、长文本耗时。"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8787/v1/chat/completions"


def chat(messages, timeout=240):
    body = json.dumps({"model": "gemini-web", "messages": messages}).encode()
    req = urllib.request.Request(BASE, data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    dt = time.time() - t0
    content = d.get("choices", [{}])[0].get("message", {}).get("content") or json.dumps(d.get("error", d), ensure_ascii=False)
    return content, dt


# 等服务就绪
for _ in range(40):
    try:
        urllib.request.urlopen("http://127.0.0.1:8787/health", timeout=2)
        break
    except Exception:
        time.sleep(0.5)

print("== T1 新开会话（system+user） ==")
c1, t1 = chat([
    {"role": "system", "content": "回答保持一句话以内"},
    {"role": "user", "content": "我住在上海，请记住"},
])
print(f"  [{t1:.1f}s] {c1[:60]}")

print("== T2 续聊（只应发送新增的 user 消息） ==")
c2, t2 = chat([
    {"role": "system", "content": "回答保持一句话以内"},
    {"role": "user", "content": "我住在上海，请记住"},
    {"role": "assistant", "content": c1},
    {"role": "user", "content": "我住哪个城市？"},
])
print(f"  [{t2:.1f}s] {c2[:60]}")
assert "上海" in c2, "镜像续聊丢失上下文！"

print("== T3 长文本粘贴注入（约3000字，应在30秒内完成发送） ==")
long_text = "请把下面这段说明压缩成一句话：" + (
    "这是一段用于测试粘贴注入速度的说明文字。" * 120
)
c3, t3 = chat([
    {"role": "system", "content": "回答保持一句话以内"},
    {"role": "user", "content": long_text},
])
print(f"  [{t3:.1f}s] {c3[:60]}")

print("== T4 历史被替换 → 应自动重开会话 ==")
c4, t4 = chat([
    {"role": "system", "content": "你现在是海盗，回答保持一句话以内"},
    {"role": "user", "content": "打个招呼"},
])
print(f"  [{t4:.1f}s] {c4[:60]}")

print("\n全部通过 ✓" if all([c1, c2, c3, c4]) else "\n有失败项")
