# solver

一个自包含的 Cloudflare Turnstile 求解服务：**只通过 HTTP API 使用**，调用方不需要
import 这个 Python 包、不需要 `sys.path` / `PYTHONPATH`，只要有 HTTP 客户端就行。

内部用真实 Chrome（SeleniumBase `uc=True` + CDP 点击）在目标站点页面里渲染 widget
并取回 token，Token 因此绑定目标站点的 hostname。

> ⚠️ **同一出口 IP**：Turnstile 的验证要求求解与业务提交来自同一网络出口。把 solver
> 跑在与调用方同一台机器 / 同一内网，别把 token 从一个出口 IP 拿到另一个 IP 去提交。

---

## 镜像

| 位置 | 拉取命令 |
|---|---|
| GHCR（本仓库 Actions 构建） | `docker pull ghcr.io/<owner>/solver:latest` |
| 本地构建 | `docker build -t solver .` |

GHCR 包默认私有，部署主机拉取前需登录：

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-user> --password-stdin
```

`.github/workflows/build.yml` 在 push 到 `main`/`master`、打 `v*` tag、手动触发时
先跑单元测试（不需要 Chrome），通过后才构建并推送 `ghcr.io/<owner>/solver`。
tag 包括 `latest`、分支名、`v*` 语义版本和 `sha-xxxxx`；PR 只跑测试和构建、不推送。

> ⚠️ Google Chrome 的 apt 源只有 amd64，镜像因此只构建 `linux/amd64`。arm64 主机上
> 用 `docker build --platform linux/amd64 -t solver .` 或直接拉 GHCR 的 amd64 镜像。

---

## 快速开始

### 容器（推荐）

```bash
cd solver-api                     # 含本 README 的目录
cp .env.example .env              # 修改 SOLVER_API_KEY 为自己的随机串
docker compose up -d --build

curl http://127.0.0.1:8000/healthz
```

容器内默认跑 Xvfb + headful Chrome（`HEADLESS=false` 更稳），**端口只绑
`127.0.0.1`**，不对公网暴露。

### 直接用 GHCR 镜像

```bash
docker run -d --name solver \
  --shm-size=1g \
  -e SOLVER_API_KEY=your-key \
  -e HEADLESS=false \
  -p 127.0.0.1:8000:8000 \
  ghcr.io/<owner>/solver:latest
```

### 本机 Python

```bash
cd solver-api
python -m pip install -r requirements.txt
python -m solver                  # = python -m solver，等价于 uvicorn solver.app:app
```

`SOLVER_HOST`（默认 `127.0.0.1`）、`SOLVER_PORT`（默认 `8000`）可覆盖。
安装成包则可用 `pip install . && solver-api`。

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SOLVER_API_KEY` | 空 | 设置后 `/solve` 强制校验 `X-Solver-Key`（或 `Authorization: Bearer`），比对走 `hmac.compare_digest`。**留空 = 不校验**，仅适合绑 127.0.0.1 |
| `SOLVER_HOST` | `127.0.0.1` | `python -m solver` / 容器内监听地址 |
| `SOLVER_PORT` | `8000` | 端口 |
| `SOLVER_QUEUE_TIMEOUT` | `120` | 排队等待浏览器的最长秒数，超过返回 `429 busy` |
| `HEADLESS` | `false` | Chrome 是否 headless（本仓库验证过的方式是有窗口） |
| `LOG_LEVEL` | `INFO` | 服务日志级别 |
| `CHROME_ARGS` | 空 | 逗号分隔的 Chromium 启动参数，如 `--disable-dev-shm-usage,--window-size=1280,900` |

---

## HTTP API

### `GET /`

自述：服务名、版本、可用端点。

### `GET /healthz`

```json
{"status": "ok"}
```

### `GET /version`

```json
{"name": "solver", "version": "1.0.0"}
```

`/healthz` 与 `/version` **不需要鉴权**。

### `POST /solve`

请求体（全部 `Content-Type: application/json`）：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `url` | string | ✅ | — | 目标站点的绝对 http(s) 地址，widget 就在这个页面里渲染 |
| `sitekey` | string | ✅ | — | 该页面使用的 Turnstile sitekey |
| `action` | string | ✖ | `null` | Turnstile `action`，≤ 32 字符 |
| `cData` | string | ✖ | `null` | Turnstile `cData`，≤ 255 字符 |
| `theme` | enum | ✖ | `dark` | `auto` / `dark` / `light` |
| `size` | enum | ✖ | `normal` | `normal` / `compact` / `flexible` |
| `appearance` | enum | ✖ | `always` | `always` / `execute` / `interaction-only` |
| `execute` | bool | ✖ | `false` | `true` = 渲染后主动 `turnstile.execute()`（invisible 站点） |
| `timeout` | number | ✖ | `60` | 求解预算，秒，`1..300` |

成功响应 `200`：

```json
{"token": "0x....", "userAgent": "Mozilla/5.0 ...", "elapsed_ms": 21456}
```

`token` 只可**一次性**使用，用完即失效；每个目标接口都要单独取一枚。

#### 错误响应

统一信封 `{"detail": {"stage": ..., "error": ...}}`：

| HTTP | `stage` | 含义 |
|---|---|---|
| `400` | `invalid_request` | 参数缺/错（如非绝对 URL、theme 不在枚举、timeout 越界、action 超长） |
| `401` | `unauthorized` | 设置了 `SOLVER_API_KEY` 但请求头缺失或不匹配 |
| `429` | `busy` | 等其他请求用完浏览器超时（`SOLVER_QUEUE_TIMEOUT`）;稍后重试或调大 |
| `502` | `no_token` | widget 没产出 token，`error` 里带 `diagnostics={...}` |
| `502` | `browser_error` | 浏览器/页面级异常 |
| `502` | `unexpected` | 兜底 |

---

## 调用示例

### curl

```bash
curl -sS http://127.0.0.1:8000/solve \
  -H 'Content-Type: application/json' \
  -H 'X-Solver-Key: your-key' \
  -d '{"url":"https://example.com/login","sitekey":"0x4AAAA...","timeout":60}' \
  | jq -r '.token'
```

### Python（任意项目，无需 import 本包）

```python
import requests

resp = requests.post(
    "http://127.0.0.1:8000/solve",
    json={"url": "https://example.com/login", "sitekey": "0x4AAAA...", "timeout": 60},
    headers={"X-Solver-Key": "your-key"},
    timeout=90,
)
detail = resp.json()
if resp.status_code != 200:
    raise RuntimeError(f"{detail['detail']['stage']}: {detail['detail']['error']}")
token = detail["token"]
```

项目里也带了一份可直接跑的客户端：`examples/solve_once.py`。

```bash
python examples/solve_once.py \
  --url "https://example.com/login" --sitekey "0x4AAAA..."
```

---

## `no_token` 怎么读

`502` 的 `error` 字段会带 `diagnostics={...}`，按这些字段判断归属：

| 字段 | 说明 |
|---|---|
| `api` | `typeof window.turnstile`；不是 `object` = api.js 没加载 |
| `stage` | `waiting-api` → api.js 未就绪；`script-added` → 脚本仍在加载；`rendered` → widget 已渲染但需交互（会自动点击）；`solved` → 已出 token；`error:<code>` → Cloudflare 错误码（如 `300030`） |
| `scriptState` | `loading`/`loaded`/`error` |
| `csp` | CSP 违规监听结果，有值 = 目标站 CSP 禁止了 `challenges.cloudflare.com`，**该站点无法通用求解** |
| `widgetId` / `widgetRect` | 成功渲染才有值 |
| `pageText` | 目标页前 120 字符，用来确认是否重定向到别页 |
| `url` / `readyState` | 实际打开地址与文档状态 |

注意：Turnstile 把挑战 iframe 放在 **closed shadow root** 内，所以
`document.querySelectorAll("iframe")` 恒为 0，这是正常的；判定渲染成功应看
隐藏 input `cf-turnstile-response`（诊断里的 `inputs` / `inputLen`）。

---

## 局限

- **sitekey 域名白名单**：token 绑 sitekey + 渲染页 hostname。`url` 必须是目标站点
  自己的页面，且 sitekey 配置里允许这个域名；跨域渲染拿到的 token 提交时会被拒。
- **CSP**：目标站 CSP 若禁 `challenges.cloudflare.com`（诊断 `csp` 字段可见），无解。
- **出口 IP**：数据中心 IP 可能被升级为图片拼图（`stage=rendered` + 无 token），
  这类挑战无代码解法，需要换出口 IP。
- **并发**：一个浏览器全局串行，同时发多个 `/solve` 会排队；排队超过
  `SOLVER_QUEUE_TIMEOUT` 得到 `429 busy`。

---

## 测试

```bash
python -m pip install -r requirements.txt pytest httpx
python -m pytest -q            # 不碰 Chrome，全部 mock
python -m pytest -q -m smoke   # 可选：真浏览器 + 网络，需先设 SOLVER_SMOKE_URL / SOLVER_SMOKE_SITEKEY
```

---

## 目录结构

```
solver/                      # 本仓库根目录
├── pyproject.toml          # name = "solver"，可 pip install
├── requirements.txt        # fastapi / uvicorn / seleniumbase（无 turnstile-solver）
├── docker-compose.yml      # 单服务部署，绑 127.0.0.1:8000
├── Dockerfile              # Chrome + Xvfb + 本包
├── README.md               # 本文件
├── .env.example            # 配置样例
├── .github/workflows/
│   └── build.yml            # 先跑单测，再构建并推送 ghcr.io/<owner>/solver
├── examples/
│   └── solve_once.py       # 独立客户端示例
├── tests/                  # 独立 pytest 配置（pythonpath=["."] testpaths=["tests"]）
└── solver/                 # Python 包
    ├── __init__.py
    ├── __main__.py         # python -m solver
    ├── app.py              # FastAPI：鉴权 / 校验 / 429 / 错误信封 / /version
    └── browser.py          # 求解核心（顶层 return JS、light-DOM 点击、诊断）
```