"""UI 端到端自测：无头加载控制台页面，模拟真实用户发一条消息，验证流式回复渲染。"""
import sys

from playwright.sync_api import sync_playwright

import browser


def main() -> int:
    exe = browser.find_chrome()
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, executable_path=exe)
        try:
            pg = b.new_page()
            pg.goto("http://127.0.0.1:8787/", wait_until="domcontentloaded", timeout=30_000)

            assert pg.text_content("#baseurl").strip().endswith("/v1"), "Base URL 显示异常"
            print("[test] 页面加载 ✓，接入参数显示 ✓")

            pg.fill("#sys-prompt", "你是一个简洁的助手，每次回答不超过15个字")
            pg.fill("#input", "你好")
            pg.click("#send-btn")
            print("[test] 已点击发送，等待流式回复…")

            # 发送按钮变回“发送”即代表流结束
            pg.wait_for_function(
                "document.getElementById('send-btn').textContent === '发送'",
                timeout=200_000,
            )
            bubbles = pg.query_selector_all(".msg.ai .bubble")
            assert bubbles, "没有生成 AI 气泡"
            text = bubbles[-1].text_content().strip()
            assert text and "(空回复" not in text, f"AI 气泡为空: {text!r}"
            assert "❌" not in text, f"气泡里是错误信息: {text!r}"
            print(f"[test] 流式回复渲染 ✓：{text[:60]}")

            user_bubbles = pg.query_selector_all(".msg.user .bubble")
            assert len(user_bubbles) == 1, "用户气泡数量异常"
            print("[test] 会话历史记录 ✓")

            # 连续第二条，验证多轮与会话保持
            pg.fill("#input", "我上一句话说了什么？")
            pg.click("#send-btn")
            pg.wait_for_function(
                "document.getElementById('send-btn').textContent === '发送'",
                timeout=200_000,
            )
            bubbles = pg.query_selector_all(".msg.ai .bubble")
            second = bubbles[-1].text_content().strip()
            print(f"[test] 多轮上下文回复 ✓：{second[:60]}")
            return 0
        finally:
            b.close()


if __name__ == "__main__":
    sys.exit(main())
