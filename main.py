"""gemini2a 入口：python main.py [--port 8787]"""
import argparse

import uvicorn

import config
import server


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini 网页版 -> OpenAI 兼容 API 桥")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    args = parser.parse_args()

    print("=" * 62)
    print(" gemini2a — 用 Gemini 网页聊天框给 agent 提供模型")
    print("-" * 62)
    print(f" API 地址   : http://{args.host}:{args.port}/v1")
    print(f" 模型名     : {config.MODEL_NAME}")
    print(f" 输入模式   : {config.TYPING_MODE}（真实键盘事件 + 随机节奏）")
    print(f" cookie注入 : {'开启' if config.COOKIE_AUTO_EXTRACT else '关闭'}")
    print(f" API key    : {'已启用' if config.API_KEY else '未设置(不校验)'}")
    print("-" * 62)
    print(" 工作方式：会打开一个独立的浏览器窗口（不是你平时的那个浏览器），")
    print("   自动把现有 Chrome/Edge 的 Google 登录态注入进去，免登录直接用；")
    print("   若窗口里出现登录/人机验证，手动完成一次即可，之后会记住。")
    print("   窗口保持开着（可最小化），桥就能一直工作；关掉窗口=停止服务。")
    print("=" * 62)

    uvicorn.run(server.app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
