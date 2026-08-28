"""
浏览器驱动：启动一个独立配置目录的真实浏览器窗口，注入现有 Google 登录 cookie，
然后在 Gemini 网页聊天框里用拟真键入完成问答。

要点：
- 用本机 Chrome 的真实可执行文件启动（指纹与日常一致），headed 模式；
- 登录态来自你现有浏览器的 cookie 提取注入，成功后持久化在 browser-profile 里，
  之后重启无需重新提取；如注入失败则需在窗口里手动登录一次（也会被记住）；
- 输入用真实键盘事件逐词敲入（Shift+Enter 换行），短文本按人类节奏、长文本加速；
- 每次请求回到 Gemini 根路径开新会话，历史上下文由服务端整体重发。

所有界面选择器集中在文件顶部，Google 改版时只改这里。
"""
import asyncio
import json
import random
import re
import time
from pathlib import Path

from playwright.async_api import Locator, Page

import config
from cookie_store import collect_seed_cookies
from flattener import flatten_messages, message_signature, delta_suffix


class BridgeError(Exception):
    pass


class NotLoggedInError(BridgeError):
    pass


class ChromeLaunchError(BridgeError):
    pass


class SubmitError(BridgeError):
    pass


class ResponseTimeoutError(BridgeError):
    pass


_EDITOR_SELECTORS = [
    'rich-textarea .ql-editor[contenteditable="true"]',
    'div.ql-editor[contenteditable="true"]',
    '[contenteditable="true"][role="textbox"]',
    "textarea[data-testid], textarea",
]
_RESPONSE_SELECTORS = ["model-response", "message-content", ".response-container-content"]

_STABLE_POLLS_REQUIRED = 4     # 连续 4 次轮询无变化视为生成完毕
_POLL_INTERVAL_MS = 700
_HUMAN_THRESHOLD_CHARS = 200   # auto 模式下超过该长度改用粘贴注入


# ---------------------------------------------------------------------------
# 浏览器生命周期
# ---------------------------------------------------------------------------

_pw = None
_context = None
_seeded_this_run = False


def find_chrome() -> str | None:
    import os

    candidates = []
    if config.CHROME_PATH:
        candidates.append(config.CHROME_PATH)
    local_app = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pfx86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    for base in (pf, pfx86, local_app):
        if base:
            candidates.append(rf"{base}\Google\Chrome\Application\chrome.exe")
            candidates.append(rf"{base}\Microsoft\Edge\Application\msedge.exe")
    for path in candidates:
        if Path(path).exists():
            return path
    return None


async def get_context():
    global _pw, _context, _seeded_this_run
    if _context is not None:
        try:
            await _context.pages[0].title()
            return _context
        except Exception:
            _context = None

    if _pw is None:
        from playwright.async_api import async_playwright

        _pw = await async_playwright().start()

    exe = find_chrome()
    try:
        _context = await _pw.chromium.launch_persistent_context(
            user_data_dir=config.PROFILE_DIR,
            executable_path=exe,
            headless=False,
            locale="zh-CN",
            viewport={"width": 1340, "height": 880},
            args=[
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
            ],
            ignore_default_args=["--enable-automation"],
        )
    except Exception as e:
        raise ChromeLaunchError(f"浏览器启动失败（{e}）。可尝试在 .env 设置 GEMINI2A_CHROME_PATH 指向 chrome.exe") from e

    print(f"[gemini2a] 浏览器已启动：{'系统Chrome' if exe else 'Playwright Chromium'}")
    if config.COOKIE_AUTO_EXTRACT and not _seeded_this_run:
        await _seed_login_cookies(_context)
        _seeded_this_run = True
    return _context


async def _seed_login_cookies(context) -> None:
    """保证自动化窗口里存在 Gemini 登录态：优先用登录备份恢复，其次现提取注入。"""
    existing = await context.cookies()
    have_login = any(c.get("name") == "__Secure-1PSID" for c in existing)
    if not have_login:
        # 配置库丢了登录态？先尝试从上次成功的备份文件恢复
        backup_path = config.ROOT / "login-backup.json"
        if backup_path.exists():
            try:
                saved = json.loads(backup_path.read_text(encoding="utf-8"))
                if isinstance(saved, list) and any(
                    c.get("name") == "__Secure-1PSID" for c in saved
                ):
                    await context.add_cookies(saved)
                    print("[gemini2a] 已从 login-backup.json 自动恢复登录态")
                    return
            except Exception as e:
                print(f"[gemini2a] 备份恢复失败({e})，改走提取/手动登录流程")
    else:
        print("[gemini2a] 浏览器内已有登录态")
        _backup_google_cookies(existing)
        return

    seeds: dict[str, str] = {}
    source = ""
    if config.COOKIE_1PSID:
        seeds["__Secure-1PSID"] = config.COOKIE_1PSID
        if config.COOKIE_1PSIDTS:
            seeds["__Secure-1PSIDTS"] = config.COOKIE_1PSIDTS
        source = ".env"
        print("[gemini2a] 使用 .env 中手动填写的 cookie")
    else:
        extracted = collect_seed_cookies()
        source = extracted.pop("_source", "")
        seeds = extracted
        if not seeds.get("__Secure-1PSID"):
            print(
                "[gemini2a] 本机未能提取到可用的 Gemini 登录 cookie"
                "（新版 Chrome 对 Google 域启用 App-Bound 加密时会失败，属正常现象），"
                "请在弹出的窗口里手动登录一次——登录成功会自动备份，之后永远不再要求。"
            )
            return
        print(f"[gemini2a] 已从 {source} 提取到登录 cookie")

    payload = []
    for name, value in seeds.items():
        payload.append(
            {
                "name": name,
                "value": value,
                "domain": ".google.com",
                "path": "/",
                "secure": True,
                "httpOnly": name.startswith(("__Secure",)),
                "sameSite": "Lax",
            }
        )
    try:
        await context.add_cookies(payload)
        print(f"[gemini2a] 已注入 {len(payload)} 个登录 cookie（来源：{source}），本次免登录")
        _backup_google_cookies(await context.cookies())
    except Exception as e:
        print(f"[gemini2a] cookie 注入失败({e})，请在本窗口手动登录")


def _backup_google_cookies(all_cookies: list[dict]) -> None:
    """把 google.com 会话 cookie 明文备份到本地，供启动失败时自动恢复。"""
    try:
        google = [
            c for c in all_cookies
            if c.get("domain", "").endswith("google.com")
            and c.get("name", "").startswith((
                "__Secure-1P", "__Secure-3P", "SID", "HSID", "SSID",
                "APISID", "SAPISID", "NID",
            ))
        ]
        if not any(c.get("name") == "__Secure-1PSID" for c in google):
            return
        path = config.ROOT / "login-backup.json"
        path.write_text(json.dumps(google, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        print("[gemini2a] 登录态已备份到 login-backup.json")
    except Exception as e:
        print(f"[gemini2a] 登录态备份失败：{e}")


async def save_login_backup() -> None:
    """对外入口：随时把当前窗口里的登录态快照存盘。"""
    global _context
    if _context is None:
        return
    try:
        _backup_google_cookies(await _context.cookies())
    except Exception:
        pass


def adopt_last_user(text: str) -> None:
    """采用网页端当前会话的最后一条用户消息作为同步指针（切换会话时接上历史）。"""
    global _last_user_sig
    _last_user_sig = message_signature({"role": "user", "content": text})
    print(f"[gemini2a] 已采用网页端会话指针（最后一条用户消息 {len(text)} 字符）")


def reset_mirror() -> None:
    """清空镜像同步指针：下一条消息将重开会话并全量发送。"""
    global _last_user_sig, _sys_sig
    _last_user_sig = None
    _sys_sig = None
    print("[gemini2a] 镜像指针已重置（下一条消息将新开会话）")


async def open_new_chat_page() -> Page:
    """强制回到全新会话页面（供“清空会话”使用）。"""
    context = await get_context()
    page = await _page(context)
    await goto_gemini(page, force=True)
    return page


async def shutdown() -> None:
    global _pw, _context
    if _context is not None:
        try:
            await _context.close()
        except Exception:
            pass
        _context = None
    if _pw is not None:
        try:
            await _pw.stop()
        except Exception:
            pass
        _pw = None


# ---------------------------------------------------------------------------
# 页面操作
# ---------------------------------------------------------------------------

async def _page(context) -> Page:
    """只复用 Gemini 标签页；绝不动窗口里其他标签（可能是用户手动打开的页面）。"""
    for page in context.pages:
        try:
            if "gemini.google.com" in page.url:
                return page
        except Exception:
            continue
    return await context.new_page()


async def goto_gemini(page: Page, force: bool = False) -> None:
    """
    打开 Gemini 根路径。
    已在 gemini/app 时跳过重复导航：goto 会整页重载，登录后的界面水合要十几秒，
    反复刷新只会让输入框一直“不存在”。force=True 强制重载（新开会话用）。
    """
    try:
        current = page.url
    except Exception:
        current = ""
    if not force and current.startswith("https://gemini.google.com/app"):
        return

    last_exc: Exception | None = None
    for _ in range(3):
        try:
            await page.goto(config.GEMINI_URL, wait_until="domcontentloaded", timeout=60_000)
            return
        except Exception as e:  # noqa: BLE001
            last_exc = e
            await page.wait_for_timeout(1200)
    url = ""
    try:
        url = page.url
    except Exception:
        pass
    if "gemini.google.com" in url:
        return
    raise BridgeError(f"无法打开 Gemini 页面（最后错误：{last_exc}）。请检查弹出的浏览器窗口。")


async def _find_editor(page: Page, timeout_s: float = 45.0) -> Locator | None:
    """找到可见的输入框；命中后再快速复查一次，确认不是转瞬即逝的空壳。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for sel in _EDITOR_SELECTORS:
            loc = page.locator(sel).last
            try:
                if await loc.count() and await loc.is_visible():
                    await page.wait_for_timeout(250)
                    if await loc.count() and await loc.is_visible():
                        return loc
            except Exception:
                continue
        await page.wait_for_timeout(300)
    return None


async def _check_login(page: Page) -> None:
    url = page.url
    if "accounts.google.com" in url or "ServiceLogin" in url:
        raise NotLoggedInError(
            "Gemini 未登录或需要验证：请在自动打开的浏览器窗口里登录 Google 账号"
            "（登录与验证信息都会记住，之后不会再要求）；完成后无需重启本服务，直接重试请求即可。"
        )
    editor = await _find_editor(page, timeout_s=10)
    if editor is None:
        raise NotLoggedInError(
            "检测不到 Gemini 输入框。请在浏览器窗口确认：① 已登录且账号可用 Gemini；"
            "② 若出现“你是谁/不是机器人”等验证请手动完成。若都已正常仍报错，"
            "说明 Gemini 界面可能改版，需要更新 browser.py 里的选择器。"
        )


_RESPONSE_LABEL_LINES = {"", "gemini", "gemini 说", "gemini說", "gemini said", "model"}


def _clean_response_text(text: str) -> str:
    """去掉界面自带的标题/标签行（如首行“Gemini 说”）和多余空行。"""
    lines = text.splitlines()
    start = 0
    while start < len(lines) and lines[start].strip().lower() in _RESPONSE_LABEL_LINES:
        start += 1
    end = len(lines)
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end]).strip()


async def _response_at(page: Page, index: int) -> str:
    """
    取第 index 个回复（本轮对应的那条）。
    页面上同时存在多轮对话时，用“最新一条”会取到别的轮次——必须按序号精确取。
    """
    for sel in _RESPONSE_SELECTORS:
        try:
            loc = page.locator(sel)
            if sel == "model-response":
                if await loc.count() <= index:
                    continue
                el = loc.nth(index)
            else:
                el = loc.last  # 其他候选选择器没有稳定的序号语义，仅兜底
            if not await el.is_visible():
                continue
            text = (await el.inner_text()).strip()
            if text:
                return _clean_response_text(text)
        except Exception:
            continue
    return ""


async def latest_response_text(page: Page) -> str:
    """
    从最新（最后）的候选节点向前找第一个“可见且非空”的回复。
    页面流式渲染时会出现空壳节点，只看 .last 容易拿到空字符串。
    """
    for sel in _RESPONSE_SELECTORS:
        try:
            all_loc = page.locator(sel)
            count = await all_loc.count()
        except Exception:
            continue
        for i in range(count - 1, max(-1, count - 5), -1):
            el = all_loc.nth(i)
            try:
                if not await el.is_visible():
                    continue
                text = (await el.inner_text()).strip()
            except Exception:
                continue
            if text:
                return _clean_response_text(text)
    return ""


# ---------------------------------------------------------------------------
# 拟真输入
# ---------------------------------------------------------------------------

async def _approach_and_click(page: Page, editor: Locator) -> None:
    """鼠标沿小幅抖动路径接近输入框后点击，避免瞬移光标。"""
    box = await editor.bounding_box()
    if not box:
        return
    tx = box["x"] + box["width"] * 0.5 + random.uniform(-60, 60)
    ty = box["y"] + box["height"] * 0.5 + random.uniform(-8, 8)
    cur_x = tx + random.uniform(-260, 260)
    cur_y = ty + random.uniform(90, 190)
    await page.mouse.move(cur_x, cur_y, steps=random.randint(6, 12))
    await page.mouse.move((cur_x + tx) / 2 + random.uniform(-24, 24),
                          (cur_y + ty) / 2 + random.uniform(-14, 14),
                          steps=random.randint(3, 6))
    await page.mouse.move(tx, ty, steps=random.randint(3, 5))
    try:
        await editor.click(timeout=4000)
    except Exception:
        await editor.click(timeout=8000, force=True)


def _typing_style(text_len: int) -> str:
    if config.TYPING_MODE == "human":
        return "human"
    if config.TYPING_MODE == "fast":
        return "paste"
    return "human" if text_len < _HUMAN_THRESHOLD_CHARS else "paste"


async def _type_text(page: Page, editor: Locator, text: str, style: str) -> None:
    """
    两种输入方式：
    - human : 逐词真实键盘输入（短文本，最拟真）
    - paste : 模拟人工粘贴（长文本）——整段/整行一次性注入并触发与打字相同的
              input 事件，行间用 Shift+Enter 换行。agent 的长系统提示若逐词敲
              要几分钟，人工操作本来也是粘贴，几秒完成。
    """
    kbd = page.keyboard
    if style == "human":
        pieces = re.split(r"\n", text)
        for idx, seg in enumerate(pieces):
            for token in re.findall(r"\S+\s*", seg):
                await kbd.type(token, delay=random.uniform(26, 82))
                if random.random() < 0.05:
                    await page.wait_for_timeout(random.randint(200, 700))
            if idx < len(pieces) - 1:
                await kbd.press("Shift+Enter")
        await page.wait_for_timeout(random.randint(120, 380))
        return

    # paste：按行注入（execCommand 与人工粘贴触发相同的事件链）
    lines = text.split("\n")
    for idx, line in enumerate(lines):
        if line:
            try:
                ok = await editor.evaluate(
                    "(el, t) => { el.focus(); return document.execCommand('insertText', false, t); }",
                    line,
                )
                if ok is False:
                    await page.keyboard.insert_text(line)
            except Exception:
                await page.keyboard.insert_text(line)
        if idx < len(lines) - 1:
            await kbd.press("Shift+Enter")


# ---------------------------------------------------------------------------
# 问答主流程
# ---------------------------------------------------------------------------

_request_lock = asyncio.Lock()

# 镜像同步状态 v3：只跟踪“最后一条用户消息”；续聊只发增量后缀，
# 避免 agent 注入头（AGENTS.md/任务记录等，每轮几乎相同）导致网页端
# 反复出现“第一句话的内容”。
_last_user_sig: str | None = None
_last_user_text: str = ""           # 已同步的最后一条用户消息原文（算后缀用）
_sys_sig: str | None = None         # 主会话 system 指纹（不同 = 旁路调用）


def _text_of(m: dict) -> str:
    from flattener import _content_to_text

    return _content_to_text(m.get("content"), [])


def _plan_delta(messages: list[dict]) -> tuple[list[dict], bool, bool, str]:
    """
    对账：返回 (要发送的消息, 是否新开会话, 是否旁路调用, 建议发送文本)。

    - 首条 system 与主会话不同（典型：agent 后台“生成标题”类请求）
      → 旁路调用：在独立临时标签页全新会话，不污染主会话
    - 主会话：
        · 尚未同步过 → 新会话 + 全量
        · 最后一条用户消息没变 → 无需发送
        · 有新增用户消息 → 续聊，只发“相对上一条已同步消息的增量后缀”
        · 上次那条已不在历史里（历史被替换）→ 新会话 + 全量
    """
    users = [(i, m) for i, m in enumerate(messages) if m.get("role") == "user"]

    first_sig = message_signature(messages[0])
    isolated = _sys_sig is not None and first_sig != _sys_sig
    if isolated:
        return messages, True, True, ""

    if not users:
        return [], False, False, ""
    _, last_msg = users[-1]
    last_sig = message_signature(last_msg)

    if _last_user_sig is None:
        return messages, True, False, ""

    if last_sig == _last_user_sig:
        return [], False, False, ""

    j = -1
    for i, m in enumerate(messages):
        if m.get("role") == "user" and message_signature(m) == _last_user_sig:
            j = i
    if j < 0:  # 上次同步的那条用户消息已不在历史里：历史被替换，重开
        return messages, True, False, ""

    delta = [m for m in messages[j + 1:] if m.get("role") == "user"]
    if not delta:
        return [], False, False, ""

    new_text = _text_of(delta[-1])
    suffix = delta_suffix(_last_user_text, new_text)
    return delta, False, False, suffix


def _commit_sent(messages: list[dict], reply_text: str) -> None:
    """主会话提交成功后更新同步指针。"""
    global _last_user_sig, _last_user_text, _sys_sig
    if not config.MIRROR_SYNC:
        return
    if _sys_sig is None and messages:
        _sys_sig = message_signature(messages[0])
    users = [m for m in messages if m.get("role") == "user"]
    if users:
        _last_user_sig = message_signature(users[-1])
        _last_user_text = _text_of(users[-1])


async def _open_and_submit(page: Page, prompt: str, force_new_chat: bool) -> None:
    """提交一次 prompt；失败（含点击/超时类瞬时问题）自动整段重试一次。"""
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            await _open_and_submit_once(page, prompt, force_new_chat)
            return
        except NotLoggedInError:
            raise  # 登录指引类的错误重试没有意义，直接交给上层给出提示
        except Exception as e:  # noqa: BLE001
            last_exc = e
            print(f"[gemini2a] 提交第{attempt + 1}次失败：{e}")
            await page.wait_for_timeout(1500)
    raise last_exc  # type: ignore[misc]


async def _open_and_submit_once(page: Page, prompt: str, force_new_chat: bool) -> None:
    if force_new_chat:
        await goto_gemini(page, force=True)   # 回到根路径=全新会话
        # 新界面是 SPA：goto 后编辑器会先出现、随后水合重建。不等稳定就输入，
        # 敲进去的字会被界面重置吞掉（Gemini 收到残缺消息）。
        try:
            await page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception:
            pass
        await page.wait_for_timeout(600)
    # 续聊模式不导航：沿用网页端当前会话（镜像同步的核心）
    editor = await _find_editor(page)
    if editor is None:
        await _check_login(page)  # 给出可操作的错误指引
        raise SubmitError("找不到输入框且页面状态未知")
    await _check_login(page)

    await _approach_and_click(page, editor)
    await editor.fill("")
    await page.wait_for_timeout(random.randint(120, 300))

    await _type_text(page, editor, prompt, _typing_style(len(prompt)))
    await page.wait_for_timeout(random.randint(150, 420))

    # 完整性校验：输入若被水合重置吞掉，自动改用粘贴方式补写一次
    expected = "".join(prompt.split())
    for attempt in range(2):
        try:
            actual = "".join((await editor.inner_text()).split())
        except Exception:
            actual = ""
        if len(actual) >= len(expected) * 0.9:
            break
        print(f"[gemini2a] 检测到输入被界面重置（{len(actual)}/{len(expected)}），改用粘贴补写")
        await editor.fill("")
        await _type_text(page, editor, prompt, "paste")
        await page.wait_for_timeout(300)

    baseline_queries = await page.locator("user-query").count()

    async def submitted() -> bool:
        try:
            count = await page.locator("user-query").count()
            return count > baseline_queries
        except Exception:
            return False

    await page.keyboard.press("Enter")

    ok = False
    for _ in range(12):  # 最多等 3 秒
        if await submitted():
            ok = True
            break
        await page.wait_for_timeout(250)

    if not ok:  # 回车没生效时兜底点发送按钮
        for sel in ("button.send-button", 'button[aria-label*="Send" i]', 'button[aria-label*="发送"]'):
            try:
                btn = page.locator(sel).last
                if await btn.count() and await btn.is_enabled():
                    await btn.click(timeout=3000)
                    break
            except Exception:
                continue
        for _ in range(8):
            if await submitted():
                ok = True
                break
            await page.wait_for_timeout(250)

    if not ok:
        raise SubmitError("消息未能发出：可能被限流、触发验证码，或界面改版。请查看浏览器窗口当前状态。")


async def ask(messages: list[dict]) -> str:
    """
    阻塞式问答（镜像同步）：messages 为 OpenAI 格式的完整历史。
    与网页端对账后只把新增的用户消息发进同一个会话（持续同步）；
    旁路调用（如标题生成）在独立临时标签页执行，主会话完全不受影响。
    """
    async with _request_lock:
        started = time.time()

        delta, new_chat, isolated, suffix = _plan_delta(messages)

        if not delta:
            context = await get_context()
            page = await _page(context)
            return await latest_response_text(page)

        prompt = suffix if (suffix and not new_chat and not isolated) else flatten_messages(delta)
        if len(prompt) > config.MAX_PROMPT_CHARS:
            raise BridgeError(
                f"本轮需发送 {len(prompt)} 字符，超过上限 {config.MAX_PROMPT_CHARS}，请精简历史。"
            )
        tag = "旁路调用(独立标签页)" if isolated else ("新开会话" if new_chat else ("续聊(增量)" if suffix else "续聊"))
        print(f"[gemini2a] 镜像同步：本轮发送 {len(delta)} 条消息（{tag}）")

        context = await get_context()
        if isolated:
            page = await context.new_page()
        else:
            page = await _page(context)
            if not new_chat and await page.locator("user-query").count() == 0:
                # 页面被手动新开过：全量重发对齐
                delta, new_chat = messages, True
                prompt = flatten_messages(delta)

        final_text = ""
        try:
            baseline_resp = await page.locator("model-response").count()
            await _open_and_submit(page, prompt, force_new_chat=new_chat)

            stable_polls = 0
            saw_any_change = False
            deadline = time.time() + config.REQUEST_TIMEOUT

            while True:
                if time.time() > deadline:
                    raise ResponseTimeoutError(
                        f"{config.REQUEST_TIMEOUT}s 内回复未生成完（已收到 {len(final_text)} 字符），"
                        "可在 .env 调大 GEMINI2A_REQUEST_TIMEOUT。"
                    )
                if "accounts.google.com" in page.url or "ServiceLogin" in page.url:
                    raise NotLoggedInError(
                        "发送后被重定向到了登录页：请在浏览器窗口登录 Google 账号（只需一次，会记住），然后重试。"
                    )
                text = await _response_at(page, baseline_resp)
                changed = text != final_text
                final_text = text
                if changed:
                    saw_any_change = True
                    stable_polls = 0
                else:
                    stable_polls += 1

                if (
                    stable_polls >= _STABLE_POLLS_REQUIRED
                    and saw_any_change
                    and time.time() - started > 4
                ):
                    break
                await page.wait_for_timeout(_POLL_INTERVAL_MS)

            if not final_text and saw_any_change:
                url_hint = page.url
                try:
                    body_excerpt = (await page.inner_text("body"))[:400].replace("\n", " ")
                except Exception:
                    body_excerpt = "(无法读取页面文本)"
                raise BridgeError(
                    f"Gemini 界面出现过内容但最终为空（url={url_hint}）。"
                    f"页面当前显示：{body_excerpt} —— 常见原因：需要登录/人机验证、被限流、或界面改版。"
                )
        finally:
            if isolated:
                try:
                    await page.close()
                    print("[gemini2a] 旁路标签页已关闭，主会话未受影响")
                except Exception:
                    pass

        print(f"[gemini2a] 回复完成：{len(final_text)} 字符，耗时 {time.time() - started:.1f}s")
        if not isolated:
            _commit_sent(messages, final_text)
        await save_login_backup()
        return final_text

async def ask_stream(messages: list[dict]):
    """
    流式问答（镜像同步）：yield 增量文本。旁路调用在独立临时标签页执行。
    """

    async def generator():
        async with _request_lock:
            started = time.time()

            delta, new_chat, isolated, suffix = _plan_delta(messages)

            if not delta:
                context = await get_context()
                page = await _page(context)
                text = await latest_response_text(page)
                if text:
                    yield text
                return

            prompt = suffix if (suffix and not new_chat and not isolated) else flatten_messages(delta)
            if len(prompt) > config.MAX_PROMPT_CHARS:
                raise BridgeError(
                    f"本轮需发送 {len(prompt)} 字符，超过上限 {config.MAX_PROMPT_CHARS}，请精简历史。"
                )
            tag = "旁路调用(独立标签页)" if isolated else ("新开会话" if new_chat else ("续聊(增量)" if suffix else "续聊"))
            print(f"[gemini2a] 镜像同步：本轮发送 {len(delta)} 条消息（{tag}）")

            context = await get_context()
            if isolated:
                page = await context.new_page()
            else:
                page = await _page(context)
                if not new_chat and await page.locator("user-query").count() == 0:
                    delta, new_chat = messages, True
                    prompt = flatten_messages(delta)

            prev_snapshot = ""
            stable = 0
            emitted = ""
            rewritten = False
            final_full = ""
            deadline = time.time() + config.REQUEST_TIMEOUT
            try:
                baseline_resp = await page.locator("model-response").count()
                await _open_and_submit(page, prompt, force_new_chat=new_chat)

                while True:
                    if time.time() > deadline:
                        raise ResponseTimeoutError("生成超时")
                    if "accounts.google.com" in page.url:
                        raise NotLoggedInError("发送后被重定向到登录页：请在浏览器窗口登录后重试。")
                    now = await _response_at(page, baseline_resp)
                    if now == prev_snapshot:
                        stable += 1
                    else:
                        stable = 0
                        prev_snapshot = now

                    if now.startswith(emitted):
                        if len(now) > len(emitted):
                            piece = now[len(emitted):]
                            emitted = now
                            if piece:
                                yield piece
                    elif emitted and not now.startswith(emitted):
                        rewritten = True

                    if now and stable >= _STABLE_POLLS_REQUIRED + 2:
                        break
                    await page.wait_for_timeout(_POLL_INTERVAL_MS)

                final_full = prev_snapshot
                await save_login_backup()
            finally:
                if isolated:
                    try:
                        await page.close()
                        print("[gemini2a] 旁路标签页已关闭，主会话未受影响")
                    except Exception:
                        pass

            print(f"[gemini2a] 流式回复完成：{len(final_full)} 字符，耗时 {time.time() - started:.1f}s")
            if not isolated:
                _commit_sent(messages, final_full)
            if not emitted and final_full:
                yield final_full
            elif rewritten and final_full != emitted:
                yield "\n\n[[gemini2a: 前文有改写，以下为最终完整回复]]\n\n" + final_full

    return generator

