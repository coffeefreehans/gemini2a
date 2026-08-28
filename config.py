"""Gemini2A 配置：优先读 .env，其次环境变量，最后用默认值。"""
import os
import sys
from pathlib import Path

# 打包成 exe 后：根目录=exe 所在目录（配置/登录态跟着程序走）
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).parent
else:
    ROOT = Path(__file__).parent


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


HOST = os.environ.get("GEMINI2A_HOST", "127.0.0.1")
PORT = _int("GEMINI2A_PORT", 8787)
MODEL_NAME = os.environ.get("GEMINI2A_MODEL_NAME", "gemini-web")

# 设置后要求请求带 Authorization: Bearer <key>；留空则不校验
API_KEY = os.environ.get("GEMINI2A_API_KEY", "")

REQUEST_TIMEOUT = _int("GEMINI2A_REQUEST_TIMEOUT", 240)
MAX_PROMPT_CHARS = _int("GEMINI2A_MAX_PROMPT_CHARS", 120_000)

GEMINI_URL = "https://gemini.google.com/app"

# ---- 浏览器 ----
# 优先用本机真实 Chrome；找不到则回退 Playwright 自带的 Chromium
CHROME_PATH = os.environ.get("GEMINI2A_CHROME_PATH", "")
PROFILE_DIR = str((ROOT / os.environ.get("GEMINI2A_PROFILE_DIR", "browser-profile")).resolve())

# ---- 输入模式 ----
# auto  : 短文本(<200字)逐词拟人输入，长文本模拟粘贴注入（人工本来就是这么干的）
# human : 全部逐词拟人输入（超长文本会非常慢）
# paste : 全部粘贴注入
TYPING_MODE = os.environ.get("GEMINI2A_TYPING_MODE", "auto").lower()

# 镜像同步：与网页端保持同一个会话，每轮只把新增消息发过去（历史被改动时自动重开对齐）
MIRROR_SYNC = os.environ.get("GEMINI2A_MIRROR_SYNC", "true").lower() != "false"

# 启动后自动用默认浏览器打开可视化控制台
OPEN_UI = os.environ.get("GEMINI2A_OPEN_UI", "true").lower() != "false"


def persist_env(updates: dict) -> None:
    """把配置写入程序目录的 .env（保留文件里的其他行与注释）。"""
    updates = {k: str(v) for k, v in updates.items()}
    path = ROOT / ".env"
    old_lines = []
    if path.exists():
        old_lines = path.read_text(encoding="utf-8").splitlines()

    def formatted(key: str) -> str:
        val = updates[key]
        return f"{key}={val}" if val != "" else f"{key}="

    seen: set[str] = set()
    out_lines: list[str] = []
    for line in old_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out_lines.append(formatted(key))
                seen.add(key)
                continue
        out_lines.append(line)
    for key in updates:
        if key not in seen:
            out_lines.append(formatted(key))
    path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

# ---- cookie 注入 ----
# 优先级：.env 手动指定 > 从本机浏览器提取 > 窗口内手动登录（持久保留）
COOKIE_1PSID = os.environ.get("GEMINI2A_COOKIE_1PSID", "")
COOKIE_1PSIDTS = os.environ.get("GEMINI2A_COOKIE_1PSIDTS", "")
# 自动提取：新版 Chrome 对 Google 域 cookie 使用 App-Bound(v20) 加密时无法离线解密，
# 会提示改为窗口内手动登录一次或手填 cookie。
COOKIE_AUTO_EXTRACT = os.environ.get("GEMINI2A_COOKIE_AUTO_EXTRACT", "true").lower() != "false"
