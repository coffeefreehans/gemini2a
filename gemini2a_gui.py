"""
gemini2a 桌面版入口：WebView2 原生窗口 + 内嵌本地服务。

- 窗口里就是完整的图形控制台（聊天/设置/网页端实况），无命令行
- 本地服务随程序自动启动，agent 接 http://127.0.0.1:{port}/v1
- 服务启停/端口切换通过 window.pywebview.api 由页面按钮调用本地内核完成
- WebView2 不可用时自动退回默认浏览器打开控制台（服务照常）

打包：
    pyinstaller --noconsole --onefile --name gemini2a ^
        --collect-all uvicorn --collect-all playwright ^
        --collect-all webview --collect-all pythonnet ^
        --hidden-import Crypto.Cipher.AES --hidden-import clr ^
        --add-data "web;web" gemini2a_gui.py
"""
import json
import os
import sys
import threading
import time
import urllib.request

import config

if getattr(sys, "frozen", False):
    # 无控制台模式下所有输出落盘，排障用
    try:
        _logf = open(str(config.ROOT / "gemini2a.log"), "a",
                     buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = _logf
    except Exception:
        pass

_server_ref = {"srv": None, "alive": False, "external": False}


def _log(msg: str) -> None:
    print(f"[gui] {msg}")


def _existing_service_alive() -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{config.PORT}/health", timeout=1.5
        ) as r:
            d = json.loads(r.read())
            return isinstance(d, dict) and d.get("status") == "ok"
    except Exception:
        return False


def server_thread():
    import uvicorn

    import server

    if _existing_service_alive():
        _server_ref["external"] = True
        _log("检测到已有实例在服务，本窗口直接复用")
        return
    _server_ref["external"] = False

    srv = uvicorn.Server(uvicorn.Config(
        server.app, host=config.HOST, port=config.PORT,
        log_level="warning", access_log=False,
    ))
    _server_ref["srv"] = srv
    _server_ref["alive"] = True

    def runner():
        try:
            srv.run()
        finally:
            if _server_ref.get("srv") is srv:
                _server_ref["alive"] = False
            time.sleep(0.6)
            if not _existing_service_alive():
                _log(f"后台服务未启动：端口 {config.PORT} 可能被其他程序占用"
                     "（可在控制台设置里换端口）")

    threading.Thread(target=runner, daemon=True).start()


def _stop_server_blocking(timeout: float = 10.0) -> None:
    srv = _server_ref.get("srv")
    if srv is None:
        return
    srv.should_exit = True
    deadline = time.time() + timeout
    while _server_ref.get("alive") and time.time() < deadline:
        time.sleep(0.15)


class NativeApi:
    """暴露给页面 JS（window.pywebview.api）的本地能力。"""

    def is_running(self) -> str:
        running = bool(_server_ref.get("alive")) or bool(_server_ref.get("external"))
        return "1" if running else "0"

    def start_server(self) -> str:
        if self.is_running() == "1":
            return "already"
        threading.Thread(target=server_thread, daemon=True).start()
        for _ in range(40):
            if _server_ref.get("alive"):
                break
            time.sleep(0.25)
        return "ok"

    def stop_server(self) -> str:
        _stop_server_blocking()
        return "ok"

    def apply_port(self, port: str) -> str:
        try:
            p = int(str(port).strip())
            assert 1 <= p <= 65535
        except Exception:
            return "bad_port"
        _stop_server_blocking()
        config.PORT = p
        config.persist_env({"GEMINI2A_PORT": p})
        server_thread()
        for _ in range(40):
            if _server_ref.get("alive"):
                break
            time.sleep(0.25)
        _log(f"端口已切换为 {p}")
        return "ok"

    def open_log(self) -> str:
        try:
            os.startfile(str(config.ROOT / "gemini2a.log"))  # noqa: S606
        except Exception:
            pass
        return "ok"


def main() -> None:
    print(f"===== gemini2a 启动 port={config.PORT} =====")
    threading.Thread(target=server_thread, daemon=True).start()

    # 给服务最多 25 秒就绪（首启解压慢），期间窗口先开也不影响
    url = f"http://127.0.0.1:{config.PORT}/"
    try:
        import webview

        window = webview.create_window(
            "gemini2a — Gemini 网页桥接",
            url,
            js_api=NativeApi(),
            width=1380, height=880,
            min_size=(1100, 660),
            background_color="#0d1117",
        )

        def on_closed():
            _stop_server_blocking(timeout=6)

        window.events.closed += on_closed

        try:
            webview.start(gui="edgechromium")
        except Exception as e:
            _log(f"WebView2 窗口启动失败（{e}），回退到默认浏览器")
            import webbrowser

            webbrowser.open(url)
            while True:
                time.sleep(3600)  # 保持服务运行
    except Exception as e:
        _log(f"创建窗口失败（{e}），回退到默认浏览器")
        import webbrowser

        webbrowser.open(url)
        while True:
            time.sleep(3600)

    _stop_server_blocking(timeout=6)


if __name__ == "__main__":
    main()
