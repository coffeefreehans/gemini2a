# gemini2a — 用 Gemini 网页聊天框给你的 Agent 提供模型

把 Gemini 网页版（gemini.google.com）包装成 **OpenAI 兼容 API**：
程序内嵌一个真实浏览器窗口，用**真实键盘事件、拟人节奏**把消息敲进网页聊天框，再把回复按 `/v1/chat/completions` 格式还给你的 agent。

任何支持「自定义 OpenAI 接口」的 agent / 工具都能直接接入。

---

## 快速开始（双击即用）

```
双击 dist\gemini2a.exe
```

弹出桌面控制台即运行成功：

- **左右双栏**：左栏是聊天测试台（流式输出/系统提示/多轮会话/停止按钮），右栏是**网页端实况**——Gemini 页面上的对话每 3 秒自动同步显示，两边说的每句话都看得见；
- **服务控制**按钮：服务的启动/停止在界面里完成，不依赖命令行；
- **AGENT 接入参数可直接修改**：端口、API Key、模型名填完点“应用设置”立即生效（Key/模型名输入即实时保存），自动写入 exe 旁边的 `.env`；
- 程序后台自带本地服务，agent 随时可接；
- 首次会自动弹出一个独立的 Chrome 窗口用于驱动 Gemini：若提示登录请在里面登录一次（登录态自动备份到 `login-backup.json`，丢失时启动自动恢复；该窗口可最小化，请勿关闭）。

### 镜像同步与旁路隔离

- agent 的完整历史与 Gemini 网页端**保持同一个会话**：每轮只发送新增消息，网页端自己生成的回复不会重复注入；agent 清空/替换历史时自动重开网页会话对齐；
- agent 平台后台的**旁路请求**（如“给会话生成标题”，首条 system 与主会话不同）会被识别并分配到**独立的临时会话**，不会污染主对话，返回值也精确对应各自轮次；
- 长文本输入用模拟粘贴注入（秒级），短文本（<200字）保持逐词拟人；切换用 `GEMINI2A_TYPING_MODE=human|paste`。

重新打包：双击 `build_exe.bat`，产物为 `dist\gemini2a.exe`（约 78MB，首次双击启动稍慢属正常）。
需要重新构建环境时：

```bash
pip install -r requirements.txt pyinstaller
python gemini2a_gui.py     # 源码方式运行桌面界面
python main.py             # 源码方式运行纯后台服务（自动打开网页控制台）
```

以源码/服务方式启动时同样会弹出一个**独立的浏览器窗口**（驱动 Gemini 用的，不影响你平时的浏览器）：

- 启动时会尝试从本机 Chrome/Edge 提取现有 Google 登录 cookie 直接注入（免登录）；
- 新版 Chrome 对 Google 域启用 App-Bound 加密时无法离线提取（日志会说明），此时在弹出的窗口里**手动登录一次即可，之后永久记住**；
- 若窗口出现“不是机器人”验证，手动完成一次；窗口可最小化，别关掉（关掉=停服务）。

## Agent 这边怎么接

只需要三个参数，和配任何 OpenAI 中转一样：

| 参数 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | 任意字符串（除非你在 `.env` 设置了 `GEMINI2A_API_KEY`，则填它） |
| Model | `gemini-web`（可用 `.env` 改名，支持多别名场景） |

### OpenAI SDK（Python）

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8787/v1",
    api_key="anything",              # 未设置 GEMINI2A_API_KEY 时随意
)

resp = client.chat.completions.create(
    model="gemini-web",
    messages=[
        {"role": "system", "content": "你是一个简洁的助手"},
        {"role": "user", "content": "你好"},
    ],
    # stream=True 也支持
)
print(resp.choices[0].message.content)
```

### curl

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-web","messages":[{"role":"user","content":"你好"}]}'
```

流式：请求体加 `"stream": true`，返回标准 SSE（`chat.completion.chunk` … `data: [DONE]`）。

### 常见工具/框架填法

| 工具 | 配置位置 | 填法 |
|---|---|---|
| Cherry Studio / NextChat / LobeChat | 设置→模型提供方→OpenAI 兼容 | API Host `http://127.0.0.1:8787`，Key 随意，模型名 `gemini-web` |
| Dify / FastGPT | 模型供应商→OpenAI-API-compatible | 同上 |
| n8n / Make | OpenAI 节点→Custom Base URL | `http://host.docker.internal:8787/v1`（容器内访问宿主机） |
| LangChain | `ChatOpenAI(base_url=...)` | `base_url="http://127.0.0.1:8787/v1", api_key="x", model="gemini-web"` |
| OpenAI Agents SDK / Cline / 各类 CLI agent | provider 配置 | 同 OpenAI SDK |

> agent 侧发来的 `temperature/max_tokens/tool_calls` 等参数会被忽略；多轮 messages 会由桥拼成一条完整 prompt 发给网页版，因此 system prompt 与历史上下文都有效（已实测）。

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/models` | 返回 `gemini-web` 模型列表 |
| POST | `/v1/chat/completions` | 对话补全，支持 `stream` |
| GET | `/` | 可视化控制台 |
| GET | `/health` | 服务与浏览器状态 |
| GET | `/debug/editor` | 诊断输入框识别情况（排障用，可加 `?goto=1` 复现完整导航） |

## 配置（`.env`，参考 `.env.example`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `GEMINI2A_PORT` | 8787 | 监听端口 |
| `GEMINI2A_API_KEY` | 空 | 设置后强制 Bearer 校验 |
| `GEMINI2A_MODEL_NAME` | gemini-web | 对外模型名 |
| `GEMINI2A_TYPING_MODE` | auto | `auto`/`human`/`fast`，控制键入节奏 |
| `GEMINI2A_REQUEST_TIMEOUT` | 240 | 单次回复最长等待秒数 |
| `GEMINI2A_CHROME_PATH` | 自动探测 | 浏览器路径 |
| `GEMINI2A_COOKIE_AUTO_EXTRACT` | true | 启动时注入现有登录态 |

## 已知限制

- **并发 1**：同一时间只处理一个浏览器请求，其余排队。
- **速度**：走真实界面 + 拟人键入，单次往返通常 10~40s；长 prompt（human 模式逐词）更慢，超长文本建议 `fast` 或精简历史（上限 12 万字符）。
- **纯文本**：不支持图片/文件上传；工具调用(tool calling)不可用。
- **用量限制**：受你的账号权益约束；未登录的匿名会话额度非常有限，**建议登录后使用**。
- ⚠️ **风险提示**：自动化使用网页版可能违反 Google 服务条款，存在限流/封号风险。仅供个人学习研究，自担风险，勿用于生产或大规模调用。

## 故障排查

| 现象 | 处理 |
|---|---|
| 提示提取不到 cookie | 新版 Chrome 的 App-Bound 加密所致（正常）。在弹出的窗口里登录一次即可 |
| 提示检测不到输入框 | 先看浏览器窗口是否停在异常页（验证码/降级页）；正常仍报错则是 Google 改版，更新 `browser.py` 顶部 `_EDITOR_SELECTORS` / `_RESPONSE_SELECTORS`，并用 `/debug/editor?goto=1` 观察命中情况 |
| 回复为空/504 | 在 `.env` 调大 `GEMINI2A_REQUEST_TIMEOUT`；查看服务端日志的具体原因 |
| 发送后被重定向到登录页 | 在窗口里完成登录，无需重启服务，直接重试 |

## 文件结构

```
gemini2a.exe      双击即用的成品（dist 目录内）
main.py           入口：纯后台服务 + 自动打开网页控制台
gemini2a_gui.py   入口：桌面 GUI（exe 用这个）
build_exe.bat     一键重新打包 exe
server.py         FastAPI：/v1/* 兼容层 + 网页控制台路由
browser.py        浏览器驱动：启动、cookie 注入、拟真键入、回复轮询
cookie_store.py   从本机 Chrome/Edge 提取解密 Google cookie（DPAPI+AES-GCM）
flattener.py      OpenAI messages -> 单条 prompt（多轮/系统提示）
config.py         .env / 环境变量配置
test_ui.py        无头浏览器 UI 自测脚本
web/index.html    网页版控制台页面
```
