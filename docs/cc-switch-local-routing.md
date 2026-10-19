# cc-switch 本地路由代码分析：引擎、Provider 数据模型与自定义供应商支持

> 分析对象：`cc-switch/src-tauri/src/`（工作区 fork，含 `linux_switch/patches/cc-switch-headless-proxy.patch` 的 headless 改造）。
> 行号是定位锚点，随版本会漂移；给出的是当前 HEAD 的位置。完成日期：2026-10-19。

---

## 1. 组件地图

| 位置 | 职责 |
|---|---|
| `proxy/server.rs` | axum HTTP 网关：44 条路由，`build_router()`（L299 起）暴露全部入站端点；`start()` 绑定 `proxy_config.listen_address/listen_port`（DB 表默认 **15721**，L120 TcpListener::bind） |
| `proxy/handlers.rs` | 各端点入口，统一走 forwarder |
| `proxy/provider_router.rs` | 供应商选择：「正在用」指针 + 故障转移队列 + 熔断器（key = `app_type:provider_id`） |
| `proxy/forwarder.rs` | 转发管线核心（最大的一份代码）：提取 base_url/key → 模型映射 → 协议转换判定 → 上游请求 → 响应/SSE 处理 |
| `proxy/providers/adapter.rs` | `ProviderAdapter` trait：`extract_base_url / extract_key / get_auth_headers / needs_transform / transform_request/response` |
| `proxy/providers/{claude,codex,gemini}.rs` | 三大协议适配器；`mod.rs L266 get_adapter()` 按 AppType 分发 |
| `proxy/model_mapper.rs` | 用 env 键把请求里的模型名重写成上游真实模型（档位映射） |
| `proxy/http_client.rs` | 全局出站代理 HTTP 客户端；未配置时跟随 `HTTP_PROXY / HTTPS_PROXY / ALL_PROXY` 环境变量（L268-272），loopback 反环 |
| `proxy/` 其余 | failover_switch、circuit_breaker、error_mapper、usage/（记账）、thinking/tool 系列清洗器、transform_*（协议转换） |
| `proxy/providers/mod.rs` | also：转换判定函数 `should_convert_codex_responses_to_chat/anthropic` |
| `mode/current.rs` | “正在用哪一家”的指针语义（见 §4.1） |
| `database/dao/providers.rs` | Provider CRUD + current 指针 + custom_endpoints 管理 |
| `database/dao/settings.rs` L141 | `global_proxy_url` 设置键（全局出站代理） |
| `bin/cc-switch-proxy.rs`（来自 patch） | headless 守护进程入口：`--db/--host/--port` + `provider` / `proxy` 子命令 |

数据面（SQLite `~/.cc-switch/cc-switch.db`）：`providers`、`proxy_config`（每个 app 的监听与故障转移配置）、`settings.global_proxy_url`、`usage_daily_rollups`、`session_usage_dedup` 摘要。

## 2. 入站端点（协议入口）

`server.rs build_router()`：同一能力支持带前缀、双前缀多种写法（客户端侧零配置）。

| 路径 | 协议面 | handler |
|---|---|---|
| `POST /health` 之外的 `GET /health`、`GET /status` | — | 健康检查 / 状态 |
| `POST /v1/messages`、`/claude/v1/messages` | Anthropic Messages（Claude Code） | `handle_messages` |
| `GET/POST /claude-desktop/v1/{models,messages}` | Claude Desktop 3P 专用命名空间（模型路由表见 meta） | `handle_claude_desktop_*` |
| `POST /chat/completions`、`/v1/chat/completions`、`/v1/v1/…`、`/codex/v1/…` | OpenAI Chat Completions（Codex CLI 等） | `handle_chat_completions` |
| `GET/POST /responses`、`/v1/responses`、`/v1/v1/…`、`/codex/v1/…` | OpenAI Responses（POST + WebSocket 握手 GET） | `handle_responses*` |
| `GET /models`、`/v1/models` | OpenAI 模型列表（可达性探测） | `handle_models` |
| `ANY /v1beta/*path`、`/gemini/v1beta/*path`、`/gemini/v1/*path` | Gemini 原生 API | `handle_gemini` |
| `POST /alpha/search`（及双前缀） | 搜索类专用路 | `handle_alpha_search` |

## 3. Provider 数据模型：怎么“书写”一个供应商

### 3.1 存储：一张通用宽表

`providers` 表（`database/schema.rs` L27 起）：

```
id TEXT, app_type TEXT, name TEXT, settings_config TEXT(JSON),
website_url, category, created_at, sort_index, notes,
icon, icon_color, meta TEXT(JSON, DEFAULT '{}'),
is_current BOOLEAN DEFAULT 0, in_failover_queue BOOLEAN, …
```

`Provider` struct（`provider.rs` L11）：`id / name / settings_config / website_url / category / created_at / sort_index / notes / meta(ProviderMeta) / icon / icon_color / in_failover_queue`。

- **没有任何 schema 级的“类型”限制** —— 引擎拿到的就是一个 id + 一坨 JSON。所谓“自定义供应商”和内置预设/share 卡片在存储上完全同构。
- `category`（`official` / `aggregator`，缺省补 `official`，DAO L75-78）只服务于前端分组展示，转发路径不读它。
- `meta`（`provider.rs L472 ProviderMeta`）承载高级能力：`custom_endpoints`（多备选地址，DAO `add/remove_custom_endpoint`）、`api_format`（上游协议声明，见 §5）、`endpointAutoSelect`（测速自动选端点）、`usage_script`（余额查询脚本）、`claudeDesktopMode` / `claudeDesktopModelRoutes`（Desktop 桥模型路由）、`prompt_cache_routing`、`managed_account_id` 等。

### 3.2「正在用」这一层指针

选谁不只看 `is_current`：

- **DB `is_current`**：直连指针（settings 表也镜像一份，`mode/current.rs local_direct_pointer` 只留作回滚）。
- **`live-state.json`**（设备本地模式状态，`mode/controller.rs`）：每个可代理 app 记一份「模式 + 代理路由」。代理模式下 `mode.proxy_route` 指向的 provider 行**优先于**直连指针（`mode/current.rs L67 provider_in_use` → `proxy_route_row`）。
- decoder：路由不存在时按直连指针兜底并告警（`proxy_route_row` 尾部）。

这就是切供应商能“热生效”的原因：**每个请求都会重读指针 + 对应行**（`provider_router.rs select_providers_with_current` 注释）。

### 3.3 各协议的 `settings_config` 形态（=书写模板）

适配器是唯一“读懂”这份 JSON 的地方，键名约定如下。

**Claude / Claude Desktop（`ClaudeAdapter`，`proxy/providers/claude.rs`）**

```jsonc
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://relay.example.com",   // 上游地址
    "ANTHROPIC_AUTH_TOKEN": "sk-xxx",                    // → Authorization: Bearer
    // 或 ANTHROPIC_API_KEY: "sk-ant-xxx"                // → x-api-key（infer_anthropic_auth_strategy）
    // 档位映射（可选，引擎用它做模型重写）：
    "ANTHROPIC_MODEL": "…",                              // 默认
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "…",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "…",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "…",
    "ANTHROPIC_DEFAULT_FABLE_MODEL": "…",
    "CLAUDE_CODE_SUBAGENT_MODEL": "…"
  }
}
```

key 提取兜底链（`extract_key`）：`ANTHROPIC_AUTH_TOKEN → ANTHROPIC_API_KEY → OPENROUTER_API_KEY → OPENAI_API_KEY → GEMINI_API_KEY → AWS_BEARER_TOKEN_BEDROCK → 顶层 apiKey/api_key`。base_url 提取不认这些兜底键时按官方地址处理；DeepSeek 官方 Anthropic 端点有 thinking 历史专属修正。

**Codex / GrokBuild / OpenCode / OpenClaw / Hermes（`CodexAdapter`，`proxy/providers/codex.rs`）**

```jsonc
{
  "auth":   { "OPENAI_API_KEY": "sk-xxx" },                       // credentials/auth.json 等价物
  "config": "model = \"gpt-5\"\n[model_providers.relay]\nbase_url = \"https://relay.example.com/v1\"\nwire_api = \"chat\"",   // config.toml 等价物（TOML 字符串）
  "base_url": "https://relay.example.com/v1",                     // 可选顶层键（TOML 缺省时读它）
  "api_format": "chat"                                            // 可选：显式声明上游协议
}
```

- `wire_api` / `api_format` / `meta.api_format` 决定上游协议：`responses`（原生，官方直通）/ `chat`（Chat Completions，引擎做 Responses→Chat 转换）/ `anthropic`（引擎做 Responses→Anthropic Messages 转换）。判定函数：`codex_provider_uses_anthropic` / `……_chat_completions`（codex.rs L87/L172/L202）。
- OAuth 官方卡（ChatGPT 账号、xAI OAuth）有专属识别（`is_codex_official_provider` 等），不参与 failover、走托管凭据。

**Gemini（`GeminiAdapter`，`proxy/providers/gemini.rs`）**

```jsonc
{
  "env": {
    "GEMINI_API_KEY": "AIza…",
    "GOOGLE_GEMINI_BASE_URL": "https://generativelanguage.googleapis.com/v1beta"  // 可省
  }
}
// 或顶层 "base_url": "…"（L176 兜底；两者都缺 → 上游报“缺少 base_url 配置”）
```

**单独说明**：`OpenCode / OpenClaw / Hermes` 复用 `CodexAdapter`（mod.rs L266-275），`Pi / Mcode` 返回 None（代理不覆盖这两类）。

### 3.4 认证头规则（Claude 面）

`infer_anthropic_auth_strategy` 与 Anthropic SDK 语义对齐：写 `ANTHROPIC_AUTH_TOKEN` 就发 `Authorization: Bearer`，写 `ANTHROPIC_API_KEY` 就发 `x-api-key`。引擎还会做 header 值合法性校验（含 CR/LF 的凭据 → 401 而不是 panic）。

## 4. 请求转发管线（一次 /v1/messages 的旅程）

```
客户端 → axum handler
  → ProviderRouter.select_providers(app)          provider_router.rs
      · provider_in_use(): live-state.json 代理路由指针 > DB is_current
      · failover: 只走故障转移队列 P1→P2→…（in_failover_queue=true 的行）
      · 熔断: 每 app:provider 一把锁，失败到阈值直接跳过
      · Codex 官方 OAuth 卡不参与 failover（账号边界）
  → get_adapter(app).extract_base_url / extract_key / get_auth_headers
  → ModelMapping::from_provider：按 §3.3 档位键重写请求模型名（tier aliases）
  → 协议判定（见 §5）
  → http_client（全局出站代理或环境变量兜底）调 上游
  → 响应/SSE 清洗与记账（usage_daily_rollups、session_usage_dedup；chat history/gemini shadow 等桥接存储）
```

## 5. 协议转换（“转接口”能力到底在哪）

Claude 入站**缺省**不转换（上游须 Anthropic 兼容）；但 provider 声明 `api_format` 后引擎会做转换。判定链：`forwarder.rs`（`resolve_claude_api_format` → 兜底 `get_claude_api_format`）→ `needs_transform` → `transform_claude_request_for_api_format`（`proxy/providers/claude.rs L339`）。矩阵如下：

| 入站客户端协议 | 上游协议 | 是否转换 | 说明 |
|---|---|---|---|
| Anthropic Messages（Claude Code） | 按 provider 的 `api_format`（`meta.apiFormat` 为 SSOT，legacy 读 `settings_config.api_format`）：缺省 `anthropic` | 缺省否（原样转发+模型重写）；声明后是 | `openai_chat` → `anthropic_to_openai_with_reasoning_content`；`openai_responses` → `anthropic_to_responses`；`gemini_native` → `anthropic_to_gemini_with_shadow`；非法值/缺省 → 原样返回 |
| OpenAI Responses（Codex 系） | Responses 原生 | 否 | 官方直通 + namespace/xAI 特殊清洗 |
| OpenAI Responses | Chat Completions | **是**（`transform_codex_chat` 等） | `wire_api="chat"` 触发 |
| OpenAI Responses | Anthropic Messages | **是**（`transform_codex_anthropic`、流式 `streaming_codex_anthropic`） | `api_format/wire_api="anthropic"` 触发；`/responses/compact` 端点同样命中 |
| Gemini 原生 | Gemini 兼容 | 原样 | `gemini_url.rs` 处理 URL 形态 |

另一条“转接口”支路：**Claude Desktop 桥**（`/claude-desktop/v1/*`）用 `meta.claudeDesktopModelRoutes` 把 Claude-safe 模型名映射到上游真实模型。

## 6. 自定义供应商：支持吗？怎么支持？

**结论：完全支持，而且是第一公民。** 引擎不区分“预设/商店卡”与“自定义”——一个 provider 行能否工作，只取决于它的 `settings_config` 能否被适配器读懂（§3.3 的键）。

三条书写途径（同一份数据）：

1. **桌面 GUI**（cc-switch 原生）：`AddProviderDialog` / Provider 表单/ JSON 编辑器创建；`meta.custom_endpoints` 支持一个卡多地址切换。
2. **headless CLI**（本 fork patch，`patches/README.md`）：
   ```bash
   cc-switch-proxy provider add    --name X --base-url https://r.example --token sk-… \
                   [--model m] [--haiku h] [--sonnet s] [--opus o] [--set-current] --db …
   cc-switch-proxy provider update|delete|list|set-current …
   ```
   Claude 面的 add 会生成 §3.3 的 `env` 结构（含三个档位键），Codex 面生成 `auth`+`config`。
3. **dd-switch Web UI**：路由页「供应商」表单，封装的就是上面同一个 CLI（`app.py _proxy_cli`）；另有全局出站代理 `GET/POST /api/route/proxy`。

最小可用模板（Claude 中转，直接 DB 书写时注意 `id` 需唯一、`app_type='claude'`、`settings_config` 为合法 JSON 字符串）：

```jsonc
// ~/.cc-switch/cc-switch.db → providers 行
{
  "name": "我的中转",
  "app_type": "claude",
  "settings_config": {
    "env": {
      "ANTHROPIC_BASE_URL": "https://relay.example.com",
      "ANTHROPIC_AUTH_TOKEN": "sk-xxx",
      "ANTHROPIC_DEFAULT_SONNET_MODEL": "provider-real-sonnet-id",
      "ANTHROPIC_DEFAULT_OPUS_MODEL": "provider-real-opus-id",
      "ANTHROPIC_DEFAULT_HAIKU_MODEL": "provider-real-haiku-id"
    }
  },
  "meta": "{}",
  "category": null          // 引擎不读；DAO 落库补 "official"
}
```

**边界与限制（写卡前自查）**：

- Claude 入站**缺省**要求上游 Anthropic 兼容；声明 `meta.apiFormat`（或 legacy `settings_config.api_format`）后引擎按 §5 第一行做转换。想让 Codex/OpenCode 吃 OpenAI 或 Anthropic 兼容上游，用 Codex 面的 `wire_api`/`api_format`。
- OAuth 托管卡（Codex 官方账号、xAI OAuth、GitHub Copilot）与自己手写的 key 卡不能混写凭据字段，`is_*_oauth()` 有守卫；复制这些结构做自定义卡没有意义。
- `Pi / Mcode` 无适配器，代理不管这两类 app。
- 本地路由整体只在 **①转接口（上游非客户端原生协议，用 Codex 面的 wire_api 声明）②加代理（`proxy set`/HTTP(S)_PROXY 环境兜底）** 两种场景有价值；直连通畅不必开。

## 7. 全局出站代理（引擎侧）

- 存储：`settings` 表键 `global_proxy_url`（`database/dao/settings.rs L141`），与桌面「全局代理」同源。
- 生效：app 启动时 `http_client::init(url)` 读一次（`lib.rs L1240`），无效配置自动清除并回退直连；运行中改库**不会**热生效——headless 守护进程同理，所以 `proxy set/clear` 后必须重启路由。未配置时跟随 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY`（`http_client.rs L268-272`，loopback 反环保护）。

## 8. 与 linux_switch 的对接速查

```bash
# 守护进程（dd-switch 以 127.0.0.1:9301 spawn）
cc-switch-proxy --db ~/.cc-switch/cc-switch.db --host 127.0.0.1 --port 9301

# 供应商
cc-switch-proxy provider list|add|update|delete|set-current --db … [--app claude]

# 全局出站代理
cc-switch-proxy proxy get|set --url http(s)://|socks5(h)://…|clear --db …

# 客户端指向
~/.claude/settings.json → env.ANTHROPIC_BASE_URL = http://127.0.0.1:9301   # route_apply 由 dd-switch 写入
```

---

*生成方式：静态代码走读（server/provider_router/forwarder/providers/mode/dao/schema 即核心证据链），未包含逐行引用的完整调用栈；如需更深的某一环（transform 细节、记账、熔断参数），在 `proxy/` 对应文件内继续下钻即可。*
