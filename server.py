"""OpenAI 兼容 API：/v1/models、/v1/chat/completions（流式与非流式）。"""
import asyncio
import json
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Union

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import browser
import config

WEB_DIR = (Path(getattr(sys, "_MEIPASS", "")) / "web") if getattr(sys, "frozen", False) else Path(__file__).parent / "web"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    import webbrowser

    async def warm():
        try:
            context = await browser.get_context()
            page = await browser._page(context)
            await browser.goto_gemini(page)
            await browser._check_login(page)
            print("[gemini2a] 预热完成，Gemini 页面就绪")
        except Exception as e:
            print(f"[gemini2a] 预热提示（不影响服务可用）：{e}")

    async def open_ui():
        # 等 uvicorn 完成端口监听后再打开页面
        for _ in range(30):
            await asyncio.sleep(0.5)
            try:
                import urllib.request

                with urllib.request.urlopen(f"http://127.0.0.1:{config.PORT}/health", timeout=1):
                    break
            except Exception:
                continue
        else:
            return
        url = f"http://127.0.0.1:{config.PORT}/"
        try:
            await asyncio.to_thread(webbrowser.open, url)
            print(f"[gemini2a] 控制台已自动打开：{url}")
        except Exception as e:
            print(f"[gemini2a] 自动打开控制台失败({e})，请手动访问 {url}")

    if config.OPEN_UI:
        asyncio.create_task(open_ui())
    asyncio.create_task(warm())
    yield
    await browser.shutdown()


app = FastAPI(title="gemini2a", docs_url="/docs", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class Message(BaseModel):
    role: str = "user"
    content: Union[str, list, dict, None] = None

    model_config = {"extra": "ignore"}  # 忽略 name/tool_calls 等额外字段


class ChatCompletionRequest(BaseModel):
    model: str = ""
    messages: list[Message]
    stream: bool = False
    # temperature / max_tokens 等参数网页版不支持，直接忽略
    model_config = {"extra": "ignore"}


def _auth(authorization: Optional[str]) -> None:
    if not config.API_KEY:
        return
    expected = f"Bearer {config.API_KEY}"
    if authorization != expected:
        raise HTTPException(
            status_code=401,
            detail="API key 无效。请在请求头携带 Authorization: Bearer <GEMINI2A_API_KEY>",
        )


def _error_response(exc: Exception) -> JSONResponse:
    status_map = {
        browser.NotLoggedInError: 401,
        browser.ChromeLaunchError: 503,
        browser.SubmitError: 502,
        browser.ResponseTimeoutError: 504,
    }
    status = next((code for cls, code in status_map.items() if isinstance(exc, cls)), 500)
    payload = {"error": {"message": str(exc), "type": exc.__class__.__name__, "code": status}}
    return JSONResponse(status_code=status, content=payload)


@app.get("/")
async def home():
    """可视化控制台：聊天测试台 + 接入参数 + 状态监控。"""
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "browser_started": browser._context is not None,
        "typing_mode": config.TYPING_MODE,
        "model": config.MODEL_NAME,
    }


@app.get("/debug/editor")
async def debug_editor(goto: int = 0):
    """诊断用：列出各候选输入框选择器在当前页面的真实命中情况；goto=1 复现完整导航链路。"""
    import time as _t

    try:
        context = await browser.get_context()
        page = await browser._page(context)
    except Exception as e:
        return {"error": str(e)}
    result = {"url": page.url, "steps": [], "candidates": []}
    if goto:
        t0 = _t.time()
        try:
            await browser.goto_gemini(page)
            result["steps"].append(f"goto ok in {_t.time()-t0:.1f}s, url={page.url}")
        except Exception as e:
            result["steps"].append(f"goto FAILED: {e!r}")
            return result
        t1 = _t.time()
        editor = await browser._find_editor(page, timeout_s=12)
        result["steps"].append(f"find_editor({'FOUND' if editor else 'MISS'} in {_t.time()-t1:.1f}s)")
        if editor is None:
            result["body_head"] = (await page.inner_text("body"))[:300]
            for sel in browser._EDITOR_SELECTORS:
                try:
                    loc = page.locator(sel)
                    count = await loc.count()
                    info = {"selector": sel, "count": count}
                    if count:
                        last = loc.last
                        info["visible"] = await last.is_visible()
                        info["box"] = await last.bounding_box()
                        info["tag"] = await last.evaluate("el => el.tagName + '|' + el.className")
                    result["candidates"].append(info)
                except Exception as e:
                    result["candidates"].append({"selector": sel, "error": repr(e)})
            result["probe"] = await page.evaluate(
                """() => {
                    const out = {};
                    const sels = ['rich-textarea','textarea','[contenteditable=\"true\"]','.ql-editor',
                                 'text-area','md-outlined-text-field','[role=\"textbox\"]','input[type=\"text\"]'];
                    for (const sel of sels) {
                        const els = document.querySelectorAll(sel);
                        out[sel] = els.length ? Array.from(els).slice(0,3).map(e => ({
                            tag: e.tagName,
                            cls: String(e.className||'').slice(0,50),
                            ce: e.getAttribute ? e.getAttribute('contenteditable') : null,
                            shadow: !!e.shadowRoot,
                            visible: !!(e.offsetWidth || e.offsetHeight)
                        })) : 0;
                    }
                    return out;
                }"""
            )
            return result
    for sel in browser._EDITOR_SELECTORS:
        try:
            loc = page.locator(sel)
            count = await loc.count()
            info = {"selector": sel, "count": count}
            if count:
                last = loc.last
                info["visible"] = await last.is_visible()
                tag = await last.evaluate("el => el.tagName + '|' + el.className + '|' + (el.getAttribute('contenteditable')||'')")
                info["ident"] = tag
                info["html"] = (await last.evaluate("el => el.outerHTML"))[:300]
            result["candidates"].append(info)
        except Exception as e:
            result["candidates"].append({"selector": sel, "error": repr(e)})
    return result


@app.get("/web/history")
async def web_history():
    """网页端当前会话的完整对话（按页面 DOM 顺序），供 GUI 实时同步显示。

    注意：服务停止后浏览器已关闭，此接口绝不重新拉起浏览器（否则会被
    前端轮询反复重启 Chrome）。
    """
    if browser._context is None:
        return {"url": "", "items": [], "idle": True}
    try:
        context = await browser.get_context()
        page = await browser._page(context)
        items = await page.evaluate(
            """() => {
                const nodes = document.querySelectorAll('user-query, model-response');
                const out = [];
                nodes.forEach(n => {
                    const role = n.tagName === 'USER-QUERY' ? 'user' : 'ai';
                    let t = '';
                    if (role === 'user') t = n.innerText || '';
                    else {
                        const mc = n.querySelector('message-content');
                        t = (mc || n).innerText || '';
                    }
                    t = t.trim();
                    if (t) out.push({role, text: t.slice(0, 4000)});
                });
                return out;
            }"""
        )
        return {"url": page.url, "items": items}
    except Exception as e:
        return {"error": repr(e)}


class SettingsIn(BaseModel):
    api_key: Optional[str] = None
    model: Optional[str] = None


@app.get("/settings")
async def get_settings():
    return {
        "api_key": config.API_KEY,
        "model": config.MODEL_NAME,
        "port": config.PORT,
        "mirror_sync": config.MIRROR_SYNC,
        "typing_mode": config.TYPING_MODE,
    }


@app.post("/settings")
async def set_settings(s: SettingsIn):
    if s.api_key is not None:
        config.API_KEY = s.api_key.strip()
    if s.model is not None:
        m = s.model.strip() or "gemini-web"
        config.MODEL_NAME = m
    config.persist_env({
        "GEMINI2A_API_KEY": config.API_KEY,
        "GEMINI2A_MODEL_NAME": config.MODEL_NAME,
    })
    print(f"[gemini2a] 设置已保存：model={config.MODEL_NAME}，key={'已设置' if config.API_KEY else '(未启用鉴权)'}")
    return {"ok": True}


@app.post("/web/new_chat")
async def web_new_chat():
    """清空会话：网页端强制回到全新会话，并重置镜像同步指针。"""
    if browser._context is None:
        return {"ok": False, "error": "服务未运行"}
    try:
        await browser.open_new_chat_page()
        browser.reset_mirror()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


@app.get("/web/sessions")
async def web_sessions():
    """Gemini 侧栏的会话列表 + 当前打开的会话 id。"""
    if browser._context is None:
        return {"sessions": [], "current": "", "idle": True}
    try:
        context = await browser.get_context()
        page = await browser._page(context)
        items = await page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll('a[href*="/app/"]').forEach(a => {
                    const m = (a.getAttribute('href') || '').match(/\\/app\\/([a-f0-9]{8,})/);
                    if (!m) return;
                    const t = (a.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
                    if (t && !out.some(x => x.id === m[1])) out.push({id: m[1], title: t});
                });
                return out;
            }"""
        )
        cur = page.url.split("/app/")[-1].split("?")[0]
        return {"sessions": items, "current": cur}
    except Exception as e:
        return {"sessions": [], "current": "", "error": repr(e)}


@app.post("/web/open")
async def web_open(body: dict):
    """在网页端打开指定会话，并采用其历史作为同步指针（agent 可无缝续聊该会话）。"""
    import re as _re

    sid = str(body.get("id", ""))
    if not _re.fullmatch(r"[a-f0-9]{8,}", sid):
        raise HTTPException(status_code=400, detail="无效的会话 id")
    if browser._context is None:
        raise HTTPException(status_code=503, detail="服务未运行")
    try:
        context = await browser.get_context()
        page = await browser._page(context)
        url = f"https://gemini.google.com/app/{sid}"
        for _ in range(3):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                break
            except Exception:
                await page.wait_for_timeout(1200)
        await page.wait_for_timeout(1500)
        # 采用该会话的最后一条用户消息作为同步指针 → agent 下一条消息直接续聊
        try:
            items = await page.evaluate(
                """() => {
                    const out = [];
                    document.querySelectorAll('user-query').forEach(n => {
                        const t = (n.innerText || '').trim();
                        if (t) out.push(t);
                    });
                    return out;
                }"""
            )
            if items:
                browser.adopt_last_user(items[-1])
        except Exception:
            pass
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=repr(e))


@app.get("/v1/models")
async def models(authorization: Optional[str] = Header(None)):
    _auth(authorization)
    return {
        "object": "list",
        "data": [{"id": config.MODEL_NAME, "object": "model", "created": 0, "owned_by": "gemini2a"}],
    }


def _fake_usage(prompt_len: int, completion: str) -> dict:
    # 网页版拿不到真实 token 数，按字符数粗略估算
    return {
        "prompt_tokens": max(prompt_len // 4, 1),
        "completion_tokens": len(completion) // 4 or 1,
        "total_tokens": (prompt_len + len(completion)) // 4 or 2,
    }


def _sse_chunk(chunk_id: str, created: int, model: str, delta: dict, finish: Optional[str]) -> str:
    body = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, authorization: Optional[str] = Header(None)):
    _auth(authorization)

    raw_messages = [m.model_dump() for m in req.messages]
    prompt_len = sum(
        len(m.get("content") or "") if isinstance(m.get("content"), str) else 50
        for m in raw_messages
    )

    model_name = req.model or config.MODEL_NAME
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if not req.stream:
        try:
            text = await browser.ask(raw_messages)
        except browser.BridgeError as e:
            print(f"[gemini2a] 请求失败：{e}")
            return _error_response(e)
        except Exception as e:  # noqa: BLE001
            print(f"[gemini2a] 未预期错误：{e!r}")
            return _error_response(e)
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model_name,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
            ],
            "usage": _fake_usage(max(prompt_len, 1), text),
        }

    async def event_stream():
        try:
            generator = await browser.ask_stream(raw_messages)
            yield _sse_chunk(completion_id, created, model_name, {"role": "assistant"}, None)
            acc = []
            async for piece in generator():
                acc.append(piece)
                yield _sse_chunk(completion_id, created, model_name, {"content": piece}, None)
            yield _sse_chunk(completion_id, created, model_name, {}, "stop")
            yield "data: [DONE]\n\n"
            print(f"[gemini2a] 流式请求完成：{sum(len(a) for a in acc)} 字符")
        except browser.BridgeError as e:
            print(f"[gemini2a] 流式请求失败：{e}")
            error_body = {"error": {"message": str(e), "type": e.__class__.__name__}}
            yield f"data: {json.dumps(error_body, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
