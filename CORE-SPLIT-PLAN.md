# 拆分 tauri-free core 库：cc-switch-proxy 无 WebKit 化（CORE-SPLIT-PLAN）

> **状态：已执行（2026-11-17）**。目标 pin：`86e23b63`（v4.0.33）。
> 结果：core 无 tauri 依赖；daemon `ldd` 仅 5 个基础库（无 WebKitGTK/GTK/JSC），
> 不设 `LD_LIBRARY_PATH` 即可运行；GUI 构建与 commands/tray 行为不变；
> Rust 测试 4879 passed / 14 ignored（合计 4893），Python e2e 110/110。
> 与计划的差异见 §11。
> 本文档沿用 `UPDATE-PLAN-v4.0.33.md` 的体例：每步带验证门；构建纪律
> （`CARGO_BUILD_JOBS=4`、`RUSTFLAGS="-C linker-features=-lld"`、sysroot
> `~/.cache/cc-switch-proxy-sysroot`、`CC_SWITCH_TEST_HOME` 隔离、先 check 后
> build、中途绝不动 flags/sysroot）全程适用。开发在 detached worktree
> `~/.cache/cc-switch-proxy-build`，子模块保持 pristine。
> 依据：两轮 Explore 侦察 + 一轮 Plan 设计，均在 `86e23b63` pristine 子模块上逐点核实。

## 1. Goal / 目标与非目标

**目标**：把 cc-switch 的 Rust 引擎拆成一个不依赖 tauri/webkit 的 `cc-switch-core`
rlib crate，headless daemon `cc-switch-proxy` 只链接 core：

- `cargo tree -p cc-switch-core -i tauri` 为空（编译期硬保证，不是 cfg 纪律）；
- daemon 二进制 `ldd` 无 `libwebkit2gtk-4.1` / `libjavascriptcoregtk-4.1` / `libgtk-3`；
- daemon 构建不再需要 WebKitGTK/GTK **dev** 包，运行不再需要 `runtime-libs/`
  （部署主机 `LD_LIBRARY_PATH` 拐棍可退役）；
- GUI app crate 行为不变（`tauri::Builder`、~388 commands、托盘、插件全保留）；
- 全量 Rust 测试（v4.0.33 基线 4878 passed / 14 ignored）与 Python e2e（110）全绿；
- 现有补丁特性（model_route 按模型分发、provider/proxy CLI、`init_at`）原样保留；
- **顺带修复 C2**：headless 下 failover 路由记录（`record_failover_route`）从
  "静默丢失" 变为真正生效。

**非目标（明确不过度承诺）**：

- **OAuth 供应商 headless 仍不可用**。拆分后三个 OAuth manager 以 `Option<Arc<…>>`
  注入，headless 传 `None`，forwarder 的 Copilot/Codex/xAI 分支照旧走
  `AuthError` else 臂（与今天 `app_handle: None` 的行为逐字一致）；GeminiCliOAuth
  仍需活 `access_token`、无刷新路径。拆分让 **API-key 供应商**无 webkit 化，
  不启用 OAuth 登录/刷新。patches/README 的 OAuth caveat 原文保留。
- 不改路由语义、不改 DB schema、不改 CLI 接口。纯结构手术 + C2 一处行为修复。
- 不在本计划内决定最终归属（上游 PR / fork / 本地补丁）——见 §8 分阶段策略。

## 2. 本次核实新增事实（V-findings，补两轮侦察之缺）

在 `86e23b63` pristine 子模块上逐点核实，以下为保证计划可执行所必需：

- **V1 — 事件面比侦察更宽**：`ProxyService::emit(event, payload)`
  （`services/proxy.rs:214`）已是通用事件漏斗，`mode/controller.rs:875,890`
  经它发 `proxy-loopback-hijacked` / `proxy-official-warning`。所以 sink 必须是
  通用 `emit(&str, Value)`，不是三个硬编码事件方法。另有
  `notify_tray()`（`services/proxy.rs:98-102`）→ `tray::schedule_tray_status_check`。
- **V2 — 额外的 core→commands 生产耦合（C1 之外）**：
  `services/provider/codex_client_catalog.rs:249,619,621` →
  `commands::misc::{run_detected_tool_command_with_timeout, decode_command_output}`；
  `services/provider/codex_official_models.rs:129` → `commands::misc::local_tool_version`；
  `services/claude_account/mod.rs:733`、`login.rs:453`、`usage.rs:92,121`、
  `snapshot/mod.rs:103` → `commands::misc::{detected_tool_command,
  terminate_child_tree, tool_runs_in_wsl}`；`services/mcp_connectivity.rs:48,247`
  → `commands::misc::{login_shell_path, CommandDeadline, merge_path_segments}`。
  这些 helper 本身 **tauri-free**（纯 `std::process` + WSL 探测），但散在
  **12,780 行**的 GUI 模块 `commands/misc.rs` 里（`VALID_TOOLS:170`、
  `tool_env_type_and_wsl_distro:186-205`、`decode_command_output:551`、
  `probe_local_version:1331`、`ShellProbe:1860`、`merge_path_segments:3296`、
  `login_shell_path:3333`、`CommandDeadline:4536`、`terminate_child_tree:4562/4576`、
  `wait_child_output:4603`、`run_detected_tool_command_with_timeout:4709`、
  `detected_tool_command:4792`、`tool_runs_in_wsl:5295`）。
  **必须整簇抽出**（含私有闭包），这是侦察 "1-2 天 API 工作" 之外最大的新增项。
  `services/provider/claude_official.rs:22` / `claude_editor.rs:453` 把
  `claude_account` 拉进 core 闭包，故该簇不可回避。
- **V3 — 测试态 core→commands 耦合**（测试随文件搬进 core 时必须先断）：
  `services/provider/mod.rs` 测试（`mod tests` 起于 **223 行**，全文件 9020 行）
  调 `commands::auth::{logout_codex_oauth_with_switch_lock:797,
  remove_codex_oauth_account_with_switch_lock:672}`——两者均 tauri-free
  （`&AppState`），可搬 core；`mode/controller.rs` 测试（起于 2007 行，全文件
  9183 行）调 `commands::proxy::save_proxy_client_host:296`——亦 tauri-free
  （`&AppState` + settings + `mode::controller::resync_routes`），可搬 core。
- **V4 — usage_events 第二调用点**：`database/dao/usage_rollup.rs:104` 也调
  `notify_log_recorded`（侦察只列了 `proxy/usage/logger.rs:233`）。
  `usage_events.rs` 在 **crate 根**（`src/usage_events.rs`），不在 proxy/ 下。
- **V5 — tray 纯 helper 的私有依赖**：`tray_language()`（tray.rs:670）依赖
  `detect_system_tray_language`（tray.rs:196，`sys_locale`，tauri-free）+
  `map_locale_to_tray_language`；`provider_needs_routing`（tray.rs:1273）依赖
  `is_official_account`（tray.rs:1212，纯逻辑，用
  `proxy::providers::is_codex_official_provider` + `ProviderKind`）。四个函数一起搬。
- **V6 — db-change hook 是泛型双后端**：`database/mod.rs:129-141`
  `register_db_change_hook` 在 rusqlite `update_hook` 里调
  `auto_sync::notify_db_changed::<WebDavAutoSync>` 与 `::<S3AutoSync>`
  （`services/auto_sync.rs:170`，mpsc channel 信号）。两个后端都在 app 侧。
  需要 core 侧 hook 注册表 + app 侧注册（§4.5）。
- **V7 — runtime 替换必须保语义**：D 清单里有三处是
  `tauri::async_runtime::block_on`（`services/provider/live.rs:476,521`、
  `codex_official_models.rs:309`），不是 spawn。tauri 的 async_runtime 是
  **自带全局 tokio Runtime** 的包装：`spawn`/`block_on` 在任意线程（含别的
  runtime 的工作线程、同步 DAO 写路径）都合法。裸换 `tokio::spawn` /
  `Handle::block_on` 会在无当前 runtime 或嵌套 runtime 处 panic。
  **解法：core 自带一个薄 `runtime` 模块**（懒建全局 multi-thread Runtime，
  暴露 `spawn/spawn_blocking/block_on`），逐字镜像 tauri 语义（§4.6）。
  `codex_direct.rs:244-250` 的 `off_runtime()` 是 `spawn_blocking` choke point，同换。
- **V8 — 无 tauri::test 障碍**：core 候选树里唯一 grep 命中 `proxy/server.rs`
  的 "mock_app" 是 **axum Router**（server.rs:713 等），不是 tauri mock；
  候选树零 `tauri::test` 使用。`session_manager/` 全树 **零 tauri**
  （被 `session_usage_*` 拉入闭包时可整体进 core）。`services/subscription.rs`
  tauri-free（`usage_cache` 依赖它）。`browser_guard.rs:149` 的
  `"tauri://localhost"` 是字符串字面量，非依赖。
- **V9 — 构建布局**：`src-tauri/Cargo.toml` 目前是**单 crate、无 workspace**；
  `[lib] name = "cc_switch_lib"`、rlib-only；`tauri-build` 在 build-deps；
  dev-dep `tauri/test`；release profile（codegen-units=16 / thin-lto / opt-level=s /
  unwind / strip）在 src-tauri 根。`patches/Makefile` 用
  `--manifest-path $(BUILD_DIR)/src-tauri/Cargo.toml --bin cc-switch-proxy`，
  产物路径 `src-tauri/target/release/cc-switch-proxy`。workspace 化后
  target/ 仍在 src-tauri 根，Makefile 产物路径不变。

## 3. 架构决策：crate-split（workspace member），否决 feature-gate

**推荐：物理拆 crate。** `src-tauri/core/` 作为 workspace member
`cc-switch-core`（lib name `cc_switch_core`），app crate `cc-switch` 依赖它；
headless bin 搬进 core。理由：

1. **C1/V2 是结构问题，cfg 治不了**。forwarder（10,234 行）与
   services/provider（9,020 行）import GUI commands 模块；feature-gate 意味着在
   最热的文件里给 import、8 处 `.state::<>()`、failover、runtime 调用全部加
   `#[cfg(feature = "gui")]` 双路径。上游 773 commit 级别的 churn 下，每次 bump
   都要在 cfg 迷宫里对冲突——比现在的 12 文件补丁恶劣一个量级。
2. **"无 tauri" 必须是编译期硬保证**。单 crate + `--no-default-features` 下，
   tauri 仍可经 dev-deps、build.rs、漏网 cfg 渗入依赖树；ldd 干净只是运气好。
   独立 crate 的 Cargo.toml 里**没有 tauri 这一行**，`cargo tree -i tauri` 为空
   是可重复执行的门（§7 步骤 9）。
3. **构建收益只有 crate-split 给得足**：daemon 构建 `cargo build -p cc-switch-core`
   **完全不编译 app crate**——tauri-build、generate_context!、9 插件、webkit/gtk
   原生库全部退出 daemon 的编译图（~600 crates → 预计 ~450），dev 闭包/sysroot
   对 daemon 构建不再必要。feature-gate 做不到这点（build.rs 与 context 宏仍在
   同一 crate）。
4. **可上游性**：一个边界清晰、测试全绿、零行为变更（除 C2 修复）的 core crate
   是可 review 的架构 PR；cfg 迷宫不是。
5. **代价可控**：文件移动用 `git mv`（保历史）；**模块重导出技巧**把 app 侧
   改动压到最小——app `lib.rs` 用 `pub use cc_switch_core::{database, proxy, mode,
   live, …};` 替代 `mod` 声明，app 内成百上千处 `crate::database::…` 路径
   **一字不改**继续解析（重导出在 crate 根重建同名命名空间）。`services` 两边
   各有一部分：app 的 `services/mod.rs` 保留 app-only 子模块 + `pub use
   cc_switch_core::services::{provider, proxy, usage_cache, …};`。core 内部
   `crate::` 路径 likewise 原样解析到 core 根。真正要改的只有：跨 crate 可见性
   （`pub(crate)`→`pub`，编译器驱动、有限集合）+ Phase A 的断耦合编辑。

**否决 feature-gate 的补充理由**：`tauri::generate_context!`（lib.rs:2056）与
`tauri_build::build()`（build.rs）无法被 feature 干净移除（宏展开期依赖
tauri.conf.json/capabilities），最终还是要 cfg 掉整个 lib.rs 的 GUI 半边——
那等于用 cfg 手画一条 crate 边界，却享受不到 cargo 的依赖隔离。

### 3.1 Workspace 布局

```
cc-switch/src-tauri/
├── Cargo.toml            # [package] cc-switch (app) + [workspace] members=["core"]
│                         #   保留: tauri/tauri-build/9插件/webkit2gtk/gtk/arboard/
│                         #         auto-launch/dev-dep tauri(test)/release profile
│                         #   新增: cc-switch-core = { path = "core" }
├── Cargo.lock            # workspace 根 lock（两 crate 共享；core deps 是子集）
├── build.rs              # tauri_build::build() —— 留在 app
├── tauri.conf.json / capabilities/   # 只喂 app 的 generate_context!
├── src/                  # app crate: lib.rs(run/Builder/setup/~388 commands)、
│   │                     #   main.rs、commands/、tray*.rs、deeplink、linux_fix、
│   │                     #   app_update、app_store(plugin I/O)、auto_launch、
│   │                     #   services/{auto_sync,s3*,webdav*,mcp,…GUI 部分}
│   └── gui_sink.rs       # 新: TauriSink 适配器（impl core::events::EventSink）
└── core/
    ├── Cargo.toml        # [package] cc-switch-core；[lib] name="cc_switch_core"
    │                     #   无 tauri/tauri-build/插件/webkit/gtk；无 build.rs
    │                     #   tokio + "signal"（daemon 用）；dev-deps: serial_test/
    │                     #   tempfile/tokio(test-util) —— 无 tauri(test)
    └── src/
        ├── lib.rs        # pub mod 声明（全部 pub）
        ├── events.rs     # 新: EventSink trait + NoopSink
        ├── runtime.rs    # 新: 全局 tokio Runtime 薄包装（镜像 tauri::async_runtime）
        ├── tool_cmd.rs   # 新: 从 commands/misc.rs 抽出的进程/工具执行簇（V2）
        ├── provider_rules.rs  # 新: tray 纯 helper 四函数（V5）
        ├── context.rs    # 新: ProxyContext + OAuthManagers（或并入 proxy/types.rs）
        ├── database/  proxy/(含 model_route.rs)  mode/  live/  session_manager/
        ├── services/{provider,proxy,usage_cache,usage_stats,subscription,
        │             model_pricing,sql_helpers,claude_account,kimi_oauth,
        │             mcp_connectivity,dsh_account,session_usage*,…闭包内模块}
        ├── store.rs  config.rs  app_config.rs  provider.rs  provider_kind.rs
        ├── settings.rs  error.rs  panic_hook.rs  usage_events.rs  …*_config 闭包
        ├── headless.rs  provider_cli.rs  proxy_cli.rs   # 现补丁三模块搬入
        └── bin/cc-switch-proxy.rs                        # daemon 入口搬入
```

依赖方向：**app → core，单向**。core → app 的边在 Phase A 全部断掉，
`cargo check -p cc-switch-core` 本身就是无环证明。tauri-build /
generate_context! / 插件注册 / `app.manage` 全部留在 app crate。

## 4. 注入设计（替代 Option<AppHandle>）

### 4.1 EventSink（core/src/events.rs，新）

```rust
pub trait EventSink: Send + Sync + 'static {
    /// 通用事件发射（V1：mode/controller 也经 ProxyService::emit 发任意事件）
    fn emit(&self, event: &str, payload: serde_json::Value);
    /// 供应商切换后重建托盘菜单（failover 路径）
    fn refresh_tray(&self) {}
    /// 代理 start/stop 后让托盘后台比对状态
    fn schedule_tray_status_check(&self) {}
}

pub struct NoopSink;
impl EventSink for NoopSink {
    fn emit(&self, _event: &str, _payload: serde_json::Value) {}
}
```

app 侧适配器（`src-tauri/src/gui_sink.rs`，新；core 永不认识 tauri）：

```rust
pub struct TauriSink(pub tauri::AppHandle);
impl cc_switch_core::events::EventSink for TauriSink {
    fn emit(&self, event: &str, payload: serde_json::Value) {
        use tauri::Emitter;
        if let Err(e) = self.0.emit(event, payload) {
            log::warn!("发送事件 {event} 失败: {e}");
        }
    }
    fn refresh_tray(&self) { crate::tray::refresh_tray_menu(&self.0); }
    fn schedule_tray_status_check(&self) {
        crate::tray::schedule_tray_status_check(&self.0);
    }
}
```

### 4.2 OAuthManagers + ProxyContext（core，新；放 proxy/types.rs 或 context.rs）

```rust
#[derive(Clone, Default)]
pub struct OAuthManagers {
    pub codex:   Option<Arc<CodexOAuthManager>>,                  // proxy/providers/codex_oauth_auth
    pub copilot: Option<Arc<tokio::sync::RwLock<CopilotAuthManager>>>,
    pub xai:     Option<Arc<tokio::sync::RwLock<XaiOAuthManager>>>,
}   // RwLock 具体类型以 commands/copilot.rs:14、xai_oauth.rs:13 现用为准

#[derive(Clone)]
pub struct ProxyContext {
    pub sink: Arc<dyn EventSink>,          // headless: NoopSink
    pub oauth: OAuthManagers,              // headless: 全 None → AuthError 臂不变
    pub app_state: Option<Arc<AppState>>,  // failover 路由记录；测试可 None
}
impl Default for ProxyContext { /* NoopSink / 空 oauth / None */ }
```

- `ProxyState.app_handle`（server.rs:50）→ 上述三字段；
  `ProxyServer::new(config, db, app_handle)`（server.rs:73）→
  `ProxyServer::new(config, db, ctx: ProxyContext)`——**唯一 tauri 型签名消灭**。
- `RequestForwarder.app_handle`（forwarder.rs:367,509,530）→ 同三字段
  （从 ProxyState clone）。8 处 `.state::<XxxOAuthState>()`
  （forwarder.rs:1605,1867,2367,2418,2487,3479,3529,3557）改为
  `self.oauth.codex.as_ref().ok_or_else(|| AuthError…)` 等——
  **None 时的错误臂与今天 app_handle=None 逐字一致**（headless OAuth 行为不变）。
  forwarder.rs:32 `use crate::commands::{…}` 删除（C1）。
- `ProxyService.app_handle`（services/proxy.rs:24）→
  `sink: Arc<dyn EventSink>` + `oauth: OAuthManagers` +
  `app_state: RwLock<Option<Weak<AppState>>>`。
  `new(db)` 签名不变（默认 NoopSink），加 builder 式 setter：
  `set_sink(Arc<dyn EventSink>)`、`set_oauth(OAuthManagers)`、
  `bind_app_state(&Arc<AppState>)`（存 Weak）。
  `set_app_handle`（:70）删除，GUI 改调 `set_sink(Arc::new(TauriSink(h)))`。
  `emit()`（:214）→ `self.sink.emit(event, payload)`；
  `notify_tray()`（:98）→ `self.sink.schedule_tray_status_check()`。
  `start()`（:139-140,575-576）构造 ProxyServer 时 upgrade Weak →
  `ProxyContext { sink, oauth, app_state: upgraded }`。

**AppState 强环问题**：`AppState → ProxyService → ProxyServer → ProxyState →
Arc<AppState>` 是强环。用 **Weak 绑定 + start 时 upgrade** 化解：强环只在
server 运行期间存在（进程/应用生命周期内本就该活），`stop_server()` 把 server
从 RwLock take 出 drop 后环即断。GUI 与 headless 都在 `Arc::new(AppState::new(db))`
之后立刻 `proxy_service.bind_app_state(&arc)`。测试 `for_test(db)`
（server.rs:67）可自绑或留 None（failover 跳过，与今天一致）。

**GUI 接线（lib.rs setup，818-821 / 1343-1403 区域）**：manager Arc 只构造一次，
`app.manage(CodexOAuthState(arc.clone()))` 供 commands 用，同一 Arc 进
`OAuthManagers` 供 core 用——三个 newtype（commands/codex_oauth.rs:19、
copilot.rs:14、xai_oauth.rs:13）**留在 app**，core 只见裸 Arc。
⚠ 接线时核对 lib.rs:1343-1403 现管理的 CodexOAuthState 与
`AppState.codex_oauth_manager`（store.rs:20-21）是否同一实例：
**保持现有实例拓扑，不在拆分中顺手合并**（行为保持原则）。

### 4.3 C2 failover 重 plumbing（proxy/failover_switch.rs）

`try_switch` / `do_switch`（:38-44,71-77）的 `Option<&tauri::AppHandle>` →
`&ProxyContext`。forwarder.rs:805-810 的 spawn 改传 ctx clone。
`do_switch` 核心（替换 :91-119）：

```rust
if let Some(state) = &ctx.app_state {
    switched = crate::mode::controller::record_failover_route(
        state, &app_enum, provider_id,
    ).await.map_err(AppError::Message)?;
    if !switched { return Ok(false); }
    ctx.sink.refresh_tray();          // headless: no-op
}
ctx.sink.emit("provider-switched", serde_json::json!({
    "appType": app_type, "providerId": provider_id, "source": "failover"
}));
```

`record_failover_route`（mode/controller.rs:1702）签名本就是
`(&AppState, &AppType, &str)`——**零改动**，只是调用方从
`app.try_state::<AppState>()` 换成直接注入。headless.rs 构造
`Arc<AppState>`（store.rs 进 core 后 `AppState::new` tauri-free）并 bind →
**headless failover 路由记录真正生效**。新增回归测试：
`ProxyContext { sink: NoopSink, app_state: Some(..) }` 下触发 failover，
断言 `mode::current::provider_in_use` 已换（今天 None handle 下此测试必红，
修复后绿——C2 的验收测试）。

### 4.4 usage_events（C3；core 根模块，sink 化）

`OnceLock<AppHandle>`（usage_events.rs:25）→ `OnceLock<Arc<dyn EventSink>>`；
`init(handle)` → `init(sink: Arc<dyn EventSink>)`；`notify_log_recorded`（:46）
防抖逻辑不动，`tauri::async_runtime::spawn`（:61）→ `crate::runtime::spawn`，
`handle.emit(EVENT_USAGE_LOG_RECORDED, ())` →
`sink.emit(EVENT_USAGE_LOG_RECORDED, serde_json::Value::Null)`
（tauri 把 `()` 序列化为 null，载荷等价）。两个调用点
（logger.rs:233、dao/usage_rollup.rs:104，V4）与 `TEST_NOTIFY_COUNT` 测试钩子
（:73-81）不动。GUI setup 调 `usage_events::init(Arc::new(TauriSink(h)))`；
headless 不注入 → no-op（与今天一致）。**归 core**（写日志路径在 core，
sink 抽象后无 tauri）。

### 4.5 db-change hook（C4；注册表在 core，后端在 app）

`database/mod.rs:129-141` 改为查 core 注册表：

```rust
// core/database/mod.rs
type DbChangeListener = Arc<dyn Fn(&str) + Send + Sync + 'static>;
static DB_CHANGE_LISTENERS: RwLock<Vec<DbChangeListener>> = RwLock::new(Vec::new());
static HAS_LISTENERS: AtomicBool = AtomicBool::new(false);
pub fn register_db_change_listener(f: impl Fn(&str) + Send + Sync + 'static) { … }
// register_db_change_hook: HAS_LISTENERS 为 false 时直接跳过迭代（headless 零开销）
```

GUI setup（lib.rs:1336 workers 启动处附近）注册两个闭包：
`database::register_db_change_listener(|t| auto_sync::notify_db_changed::<WebDavAutoSync>(t))`
与 `::<S3AutoSync>` 同理。`auto_sync`/`s3_auto_sync`/`webdav_auto_sync`
**整体留 app**（workers 只从 GUI 启动）。headless 不注册 → update_hook 空转，
与今天 no-op 等价。注册发生在 `Database::init` 之后也正确（hook 每次写时查表）。

### 4.6 runtime 模块（D + C6；core/src/runtime.rs，新）

```rust
static RT: OnceLock<tokio::runtime::Runtime> = OnceLock::new();
fn rt() -> &'static Runtime { RT.get_or_init(|| Builder::new_multi_thread().enable_all().build().expect(…)) }
pub fn spawn(f: impl Future<Output=()> + Send + 'static) -> JoinHandle<()> { rt().spawn(f) }
pub fn spawn_blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> JoinHandle<T> { rt().spawn_blocking(f) }
pub fn block_on<T>(f: impl Future<Output=T>) -> T { rt().block_on(f) }
```

逐字镜像 `tauri::async_runtime`（它本身就是全局 tokio Runtime 的包装）：
任意线程可调、同步 DAO 写路径安全、嵌套 runtime 不 panic（V7）。
替换清单（生产 9 处 + 测试 ~49 处）：
`services/provider/live.rs:476,521`（block_on）、
`codex_official_models.rs:309`（block_on）`,790,799`（spawn）、
`codex_direct.rs:247`（spawn_blocking，off_runtime choke point）、
`mcp_connectivity.rs:93`、`dsh_account.rs:679`、`mode/controller.rs:858`（C6）、
`usage_events.rs:61`、`services/speedtest.rs:153,160`（block_on，测试）、
`services/provider/mod.rs` 测试 ~47 处。
GUI 下 core 全局 runtime 与 tauri 命令 runtime 并存（今天 tauri 的
async_runtime 全局即其命令 runtime；拆后 core 任务跑 core runtime）——
两者都是 multi-thread tokio，语义等价；如上游 review 要求共享，可加
`runtime::set_handle(Handle)` 在 GUI setup 注入 tauri handle（可选精化，
默认懒建全局，headless/测试零接线）。

### 4.7 app_dir 缓存（C5）

`app_store.rs:12` 的 `APP_CONFIG_DIR_OVERRIDE: OnceLock<RwLock<Option<PathBuf>>>`
+ `get_app_config_dir_override()`（:25，tauri-free 读路径）搬入 core
（`config.rs` 内或新 `app_dir.rs`）；`app_store.rs` **留 app**，保留
plugin-store 的 `read_override_from_store`/`refresh_*`/`migrate_*`（:29,67,128），
写入 core 缓存。`config.rs:314` 改引 core 缓存。headless 用默认目录或 `--db`，
与今天一致。

## 5. 模块分区表

**划分算法（机械、编译器驱动）**：以最小 API 为种子，对候选树做
`crate::` import 传递闭包审计（`grep -rn "use crate::\|crate::[a-z_]*::"`），
闭包内的 tauri-free 模块进 core；断耦合（Phase A）先行，使闭包收敛。
下表是核实后的第一轮划分；"审计定" 桶由步骤 2 的闭包脚本 + 步骤 9 的编译器最终裁决。

| 模块树 | 归属 | 依据/条件 |
|---|---|---|
| `database/`（20 文件） | **core** | 零 tauri；hook 改注册表（§4.5）；`usage_stats::LogCountCache` 依赖随之进 core |
| `proxy/` 全树（含补丁 `model_route.rs`） | **core** | C1/C2/C3 断后零 tauri；browser_guard:149 是字符串 |
| `services/provider/` 全树（含 9k 行测试） | **core** | V2 helper 抽出 + V3 测试 helper 搬入 + runtime 替换后干净 |
| `services/{proxy,usage_cache,usage_stats,subscription,model_pricing,sql_helpers}` | **core** | ProxyService sink 化；usage_cache→subscription tauri-free（V8）；usage_stats 被 logger.rs:8 / database 依赖 |
| `services/{claude_account,kimi_oauth,mcp_connectivity,dsh_account}` | **core** | 被 provider/forwarder 闭包拉入；V2 簇 + tray_language + runtime 断耦合后干净 |
| `services/session_usage*`、`session_manager/` | **审计定（倾向 core）** | session_manager 零 tauri（V8）；被 usage_stats/session_usage 拉入则整体进 core |
| `mode/`、`live/` | **core** | mode 唯一 tauri 点 controller.rs:858（C6）；live 全干净 |
| `store.rs`（AppState） | **core** | ProxyService 修好后传递性 tauri 型消失；~107 处 `&AppState` 签名零改动 |
| `config.rs`、`app_config.rs`、`provider.rs`、`provider_kind.rs`、`settings.rs`、`error.rs`、`panic_hook.rs`、`usage_events.rs` | **core** | C3/C5 断后干净；usage_events sink 化归 core |
| 各 `*_config`（claude/codex/gemini/opencode/dsh/kimi/grok/mcode/omp/openclaw/workbuddy/agy/hermes/pi/copilot/claude_desktop）、`claude_mcp`/`gemini_mcp`/`claude_plugin`/`codex_rollout_file`/`codex_state_db`/`codex_history_migration`/`jsonc_document`/`model_capabilities`/`prompt*`/`usage_script`/`sync_utils`/`lightweight` | **审计定（多数倾向 core）** | live::engine / mode 写客户端配置拉入；均不在 tauri grep 命中名单 |
| 新 `events.rs`/`runtime.rs`/`tool_cmd.rs`/`provider_rules.rs`/`context.rs` | **core** | §4 |
| `headless.rs`/`provider_cli.rs`/`proxy_cli.rs`/`bin/cc-switch-proxy.rs` | **core** | daemon 只链 core 的关键；17 个 CLI 测试随迁 |
| `commands/` 全树 | **app** | ~388 tauri commands；misc.rs 抽出 tool_cmd 簇后其余不动；auth/proxy 的 tauri-free helper 搬 core 后留薄包装 |
| `tray.rs`/`tray_compat`/`tray_panel` | **app** | 四个纯 helper 搬 core（V5）后其余 GUI |
| `services/{auto_sync,s3*,webdav*,mcp,speedtest?}` | **app** | workers GUI-only；speedtest 若被 core 闭包拉入则 runtime 替换后进 core（审计定） |
| `deeplink`/`linux_fix`/`app_update`/`app_store`(plugin I/O)/`auto_launch`/`window_fit`/`init_status`/`frontend_parity_tests`/`agy_smoke_tests` | **app** | GUI/平台专属 |
| `lib.rs run()`/Builder/setup/generate_context!、`main.rs`、build.rs、tauri.conf.json、capabilities | **app** | headless 完全绕过 run() |

**环依赖风险**：现存唯一 core→app 边是 C1/V2/V3/V5 的 commands/tray import，
Phase A 全断；此后 app→core 单向。`cargo check -p cc-switch-core` 通过 = 无环证明。
`services` 命名空间两 crate 各持一半，靠 app 侧 `pub use` 重导出统一，无冲突。

## 6. 断耦合清单（C1–C6 + D + 新增，逐项销账）

| # | 位置 | 手术 | 步骤 |
|---|---|---|---|
| C1 | forwarder.rs:32 + 8 处 .state | 删 import；OAuthManagers 裸 Arc 注入（§4.2） | 5 |
| C2 | failover_switch.rs:40,73,91-119 | try_switch/do_switch 收 &ProxyContext；record_failover_route 直注 AppState；sink.refresh_tray/emit（§4.3） | 5 |
| C3 | usage_events.rs:25,34,61 + logger.rs:233 + usage_rollup.rs:104 | OnceLock<Arc<dyn EventSink>>；runtime::spawn（§4.4） | 6 |
| C4 | database/mod.rs:129-141 | core 注册表 + app 注册双后端（§4.5） | 6 |
| C5 | config.rs:313-314 / app_store.rs:12,25 | 缓存进 core，plugin I/O 留 app（§4.7） | 7 |
| C6 | mode/controller.rs:858 | runtime::spawn（§4.6） | 8 |
| D | live.rs:476,521；codex_official_models.rs:309,790,799；codex_direct.rs:247；mcp_connectivity.rs:93；dsh_account.rs:679；speedtest.rs:153,160；provider/mod.rs 测试 ~47 | 全部 → crate::runtime::{spawn,block_on,spawn_blocking} | 8 |
| V2 | commands/misc.rs 13+ 函数簇 → 新 tool_cmd.rs；6 个消费文件改引 | 整簇抽取（含私有闭包），misc.rs `use crate::tool_cmd::*` 保内部调用 | 2 |
| V3 | commands/auth.rs:672,797 → core services；commands/proxy.rs:296(+resync_proxy_clients) → core | tauri-free helper 搬 core，commands 留薄包装 | 4 |
| V5 | tray.rs:196,670-674,1212,1273(+map_locale_to_tray_language) → provider_rules.rs；forwarder.rs:575、kimi_oauth/mod.rs:272、codex_terminal.rs:62 改引 | 四函数+私有依赖一起搬 | 3 |
| 签名 | server.rs:50,73,76；forwarder.rs:367,509,530；services/proxy.rs:24,70,99,139,215,575 | ProxyContext 三字段替换（§4.2） | 5 |

## 7. 执行步骤（每步带验证门）

> 全程：source `temp/build-env.sh`（与缓存构建逐字一致：sysroot
> `~/.cache/cc-switch-proxy-sysroot`、`RUSTFLAGS="-C linker-features=-lld"`、
> `CARGO_BUILD_JOBS=4`）；Rust/Python 测试均设 `CC_SWITCH_TEST_HOME`；
> 先 `cargo check` 后 build/test；crate 走 rsproxy.cn 直连。
> **Phase A 在单 crate 内原地断耦合（每步可编译），Phase B 才搬 crate**——
> 这是 "每步树可编译" 的关键排序。

**0. 基线锚定**。dev worktree 重置到 `86e23b63` + 应用现行 12 文件补丁
（`git apply`），`mkdir -p dist`。记录基线：`cargo test --lib -- --list`
按模块计数（总数 4878+17 口径以实跑为准，含 14 ignored）；
`cargo check` 干净。
门：status 仅补丁改动；基线计数落盘 `temp/core-split-baseline.txt`。

**1. 闭包审计脚本**。对 §5 候选集跑 import 闭包（grep `crate::` 边 +
`tauri` token），产出 "core 候选 / 待断边" 清单；确认 §5 "审计定" 桶归属；
核对 lib.rs:1343-1403 OAuth 实例拓扑（§4.2 ⚠）。
门：清单覆盖 V2/V3 全部边；无未解释的 core→commands/tray/app_store 边。

**2. 抽 tool_cmd 簇（V2，最大单项）**。`git mv` 不适用（簇散在 misc.rs），
手工剪贴 13+ 函数及其私有依赖闭包到新 `src/tool_cmd.rs`；misc.rs 顶部
`use crate::tool_cmd::{…}`（pub(crate) 重导出保 commands 内部调用点）；
6 个消费文件（codex_client_catalog / codex_official_models / claude_account×4 /
mcp_connectivity）改引 `crate::tool_cmd`。编译器驱动补漏。
门：`cargo check` 干净；`cargo test --lib` 相关模块绿；
`grep -rn "crate::commands" services/claude_account services/provider/codex_client_catalog.rs services/provider/codex_official_models.rs services/mcp_connectivity.rs` = 0。

**3. 搬 tray 纯 helper（V5）**。四函数 + `map_locale_to_tray_language` →
新 `src/provider_rules.rs`；tray.rs 改引；三个调用点改引。
门：check 干净；`grep "crate::tray" proxy/ services/ mode/` 生产代码 = 0；
tray 相关测试绿。

**4. 搬 tauri-free command helper（V3）**。`save_proxy_client_host` +
`resync_proxy_clients` → core 候选位（services/proxy.rs 邻域）；
`logout_codex_oauth_with_switch_lock` / `remove_codex_oauth_account_with_switch_lock`
→ services（codex account ops）；commands/auth.rs、commands/proxy.rs 留
`#[tauri::command]` 薄包装调搬移后的函数。
门：check 干净；provider/controller 测试绿（它们现在调 crate 内路径）。

**5. 注入改造（C1+C2+签名）**。新增 events.rs / context.rs；改
ProxyState / ProxyServer::new / RequestForwarder / ProxyService /
failover_switch（§4.1-4.3）；GUI lib.rs setup 接 TauriSink + OAuthManagers +
bind_app_state（新 gui_sink.rs）；headless.rs 传 NoopSink + 自建 AppState；
**新增 C2 回归测试**（headless failover 记录生效）。
门：check 干净；`grep -rn "tauri" proxy/` = 0（字符串除外）；
`cargo test --lib proxy::` 全绿含新 C2 测试；GUI `cargo check` 干净。

**6. usage_events sink 化 + db hook 注册表（C3+C4）**。§4.4/§4.5；
GUI setup 注册双后端 + usage_events::init(TauriSink)。
门：check 干净；`cargo test --lib usage` 绿（TEST_NOTIFY_COUNT 断言不变）；
`grep tauri usage_events.rs database/` = 0。

**7. app_dir 缓存拆分（C5）**。§4.7。
门：check 干净；config/app_store 测试绿；headless `--db` 路径行为不变
（CLI 测试）。

**8. runtime 模块 + 全量替换（D+C6）**。§4.6，生产 9 + 测试 ~49 处。
门：check 干净；`grep -rn "tauri::async_runtime" <core候选集>` = 0；
**全量 `cargo test --lib` = 基线计数全绿**（flake 单跑复验，如实记录）。

**9. Phase A 总门 + workspace 化 + 搬 crate（B1/B2）**。
`cargo fmt --check` + `cargo clippy -D warnings` 干净后：src-tauri/Cargo.toml 加
`[workspace] members=["core"]`；建 core/Cargo.toml（deps 子集，**无 tauri 系**，
tokio+signal，dev-deps 无 tauri(test)）；按 §5 `git mv` 模块树入 core/src/；
core/lib.rs 全 `pub mod`；app lib.rs 改 `pub use cc_switch_core::{…}`；
app services/mod.rs 重导出 core 部分；headless/CLI/bin 搬 core；
app 加 `cc-switch-core = { path = "core" }`。
门：`cargo check -p cc-switch-core` 干净 **且**
`cargo tree -p cc-switch-core -i tauri` 报 "did not match any packages"；
`cargo check`（app）干净；lock diff 记录（预期仅 workspace 边，deps 子集不改包集合）。

**10. 可见性/测试收尾（B3）**。编译器驱动 `pub(crate)`→`pub`（仅跨 crate
被调项；能留 pub(crate) 的留）；残留引用 app-only 模块的测试改引或留 app；
`tauri::test` 使用者留 app（V8 已确认候选树为零，此门是保险）。
门：**两 crate `cargo test --lib` 合计 = 基线计数**（4878+17 口径，14 ignored
不漂移；按模块对账，防止搬移中静默丢测试）；fmt/clippy 双 crate 干净。

**11. daemon 无 webkit 门（B4，核心验收）**。
`cargo build --release -p cc-switch-core --bin cc-switch-proxy`（-j4，后台+监控）。
门：`ldd target/release/cc-switch-proxy` **无 libwebkit2gtk-4.1 /
libjavascriptcoregtk-4.1 / libgtk-3 / libjavascriptcore**；
**不设 LD_LIBRARY_PATH=runtime-libs** 直接 `--help` 可运行；
`--port 9301` 启动 → `/health` 200、`/status` running、SIGTERM 干净退出；
`provider list/add/set-current`、`proxy get/set/clear` 对 throwaway DB 冒烟。

**12. GUI 完整构建门**。sysroot 环境下 `cargo build -p cc-switch`（app crate，
tauri::Builder/generate_context! intact）。
门：构建成功；GUI 侧测试（commands/tray/startup 等）绿；
`cargo test --lib`（app）计数与步骤 10 对账一致。

**13. Makefile + 补丁再生**。Makefile：build 命令改
`cargo build --release -p cc-switch-core --bin cc-switch-proxy`；
`mkdir dist` 对 core 不再必要（可留作 app 构建兼容）；BUILT_BIN 路径不变
（workspace target 仍在 src-tauri/target）。worktree 根
`git add -N . && git diff > patches/cc-switch-headless-proxy.patch`。
门：`make check`（pristine 子模块）"patch applies cleanly"；
补丁文件数如实记录（预计 ~50-70，含大量 rename/delete+add）；
`make binary` canonical 冷构建过步骤 11 全部门。

**14. Python e2e 全套**。`cd dd-switch && CC_SWITCH_TEST_HOME=<repo>/temp/e2e-home
.venv/bin/python -m unittest discover -s test_suite`，
`DD_SWITCH_ROUTE_PROXY_BIN` 指新二进制，**故意不设 LD_LIBRARY_PATH**。
门：110 全绿（真跑非 skip）；重点 opencode e2e 按模型路由矩阵 + anthropic
流式（model_route 特性随 proxy/ 整体搬移后行为不变的端到端证明）。

**15. 文档同步**。patches/README：runtime-libs 段落改 "legacy（旧二进制）"、
编译前置删 WebKitGTK dev 要求、OAuth caveat 保留、Verified 数字按实跑更新；
CLAUDE.md 构建纪律段补 core-only 构建路径（gitignored 本地改）；
根 README / README.zh-CN **同步改**（house rule）；本计划状态头改 "已执行" +
偏差记录。门：grep 无残留 "still links Tauri/WebKitGTK" 类旧描述（历史文档除外）。

**16. commit**（子模块本身不提交；补丁+Makefile+二进制+文档）。
**push/部署先问用户**（house rule）。部署提示：新二进制不再需要
runtime-libs/EnvironmentFile 拐棍，但 `~/.cc-switch/cc-switch.db` 首启迁移
行为与桌面端一致（自动预备份），属预期。

## 8. 策略与归属（分阶段，关键决策）

本拆分触碰 ~50-70 文件（含搬移），远超 12 文件薄补丁；上游 v4.0.10→v4.0.33
领先 773 commit，churn 剧烈。**三个归宿的评估**：

- **(a) 上游 PR（farion1231/cc-switch，MIT，活跃）— 推荐目标**。
  headless core 对他们自身价值显著：`cargo test -p cc-switch-core` 无需
  webkit 即可跑引擎全量测试（CI 大减负）；他们已有 `app_handle: None` 的
  测试路径，说明方向契合；v4.0.33 新增 `claudeDesktopModelRoutes` 表明
  按模型路由是 convergent evolution，model_route 有对话空间。
  PR 切三片：**PR#1 纯架构拆分**（零行为变更 + C2 修复，测试全绿，最可 review）；
  **PR#2 model_route 特性**（对齐他们的 per-model 方向）；**PR#3 headless bin/CLI**
  （或留本地）。合入后我们的补丁**缩到 CLI 薄层甚至消失**，子模块继续跟上游。
- **(b) 自有 fork — 备选**。子模块改指 fork，split 落 fork commit，完全控制。
  代价：永久承担 773-commit 量级 churn 的 rebase；fork 与上游渐行渐远；
  部署/文档改 fork URL。仅当 (a) 被拒或停滞超过阈值（建议 4-6 周无实质 review）
  时启用。
- **(c) 本地 mega-patch — 反对**。50-70 文件**结构性**补丁（git diff 里搬移
  呈现 delete+add，`git apply` 的偏移容错对整树搬移完全失效）；每次上游 bump
  等于手工重做拆分——v4.0.33 迁移 12 文件补丁尚有 3 处破 hunk，structural
  mega-patch 对 773 commit churn 将是每次 bump 数天的冲突地狱，且无法
  `make check` 可靠验证。**只允许作为 Phase 1 的短暂存在**（步骤 13 产物），
  不是长期归宿。

**分阶段执行**：

- **Phase 1（本计划步骤 0-16）**：dev worktree 本地证明拆分——ldd 无 webkit +
  双 crate 测试全绿 + e2e 110 绿。不做归属承诺；产物是 mega-patch + 验证记录。
- **Phase 2（决策点）**：拿 Phase 1 证据开上游 issue/PR#1。
  **决策判据**：① 上游 2-3 周内是否实质回应；② 是否接受 core 边界与
  EventSink 设计（bikeshed 可让，边界不让）；③ CI 是否认可 core-only 测试收益。
  接受 → (a)，补丁逐片上游化、本地补丁递减；拒绝/停滞 → (b)，
  `git remote add fork`，子模块 repoint，mega-patch 转为 fork commit（从此消失）。
- **现有补丁/model_route 在各归宿下的去向**：(a) model_route 走 PR#2，
  init_at/CLI 走 PR#3 或留本地薄补丁（路径改 core/ 后 hunk 原样重落）；
  (b) 全部落 fork，补丁文件退役为历史；(c) 不推荐，若被迫短期维持，
  每次 bump 按本计划步骤 2-10 重放（成本已述）。

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| **闭包蠕变**（session_usage→session_manager、*_config 群、usage_stats 5k 行）| 步骤 1 机械审计 + 编译器裁决；session_manager 已确认零 tauri（V8）；预算见 §10 |
| **测试迁移是真正大头**（provider/mod.rs 9020 行、forwarder 10234、controller 9183，测试占大半且触 pub(crate) 内部）| 测试随文件进 core（保内部访问）；V3 helper 先搬（步骤 4）；步骤 10 按模块对账计数，丢测试即红 |
| **runtime 替换语义陷阱**（block_on 嵌套、同步路径 spawn，V7）| core::runtime 全局 Runtime 逐字镜像 tauri，不用裸 tokio::spawn；全量测试兜底 |
| **tool_cmd 簇抽取漏私有依赖**（misc.rs 12.8k 行）| 编译器驱动补漏；步骤 2 独立成步、独立门 |
| **OAuth 实例拓扑被顺手改变** | §4.2 ⚠：保持现状，拆分不合并实例 |
| **pub 可见性 churn / 跨 crate pub(crate)** | 编译器驱动、最小提升；能留则留 |
| **workspace 化使 11G 缓存指纹失效** | 预期一次全量重建（release -j4 实测 ~8min 量级）；RUSTFLAGS/sysroot 逐字冻结，中途绝不动 |
| **上游 review bikeshed / 停滞** | Phase 2 判据 + fork 备选；Phase 1 产物独立可用 |
| **测试碰真实 ~/.cc-switch** | CC_SWITCH_TEST_HOME 全程隔离（Rust+Python，house rule） |
| **codex 计时测试负载 flake** | 单跑复验，如实记录（v4.0.33 已知） |
| **过度承诺 OAuth** | §1 非目标 + patches/README caveat 保留；headless OAuth 行为逐字不变 |

## 10. 工作量估计

| 项 | 估时 |
|---|---|
| Phase A 断耦合 API 工作（C1-C6 + D，侦察口径） | 1-2 天 |
| tool_cmd 簇抽取（V2，新发现，12.8k 行文件手术） | 0.5-1 天 |
| crate 拆分/搬移/可见性/workspace（B1-B3） | 1 天 |
| 测试迁移与断耦合收尾 + 计数对账 | 1 天 |
| 构建门/ldd/e2e/Makefile/补丁再生/文档 | 0.5-1 天 |
| **合计（Phase 1）** | **4-6 个专注工作日** |
| Phase 2 上游 PR 打磨 + review 迭代 | 另计（不可控，判据见 §8） |

## 11. 执行偏差记录（2026-11-17）

所有计划目标达成（core 无 tauri、daemon 无 WebKit、GUI 不变、测试守恒、C2 修复）。
以下是与计划的**实际差异**，均为编译器闭包裁决或执行发现，不是设计变更：

### 11.1 模块归属（对齐 §5）

1. `services/mcp.rs` → **core**（§5 原列 app）：被 provider 闭包拉入，且只依赖 core 侧类型。
2. `lightweight.rs` 留在 **app**（§5 倾向 core）：桌面窗口相关，core 闭包不需要。
3. `services/speedtest.rs`、`services/config.rs`、`s3*/webdav*`、`backup_storage`、`balance`、`stream_check`、`sync_protocol` 等留在 **app**（未被 core 闭包拉入）。
4. `sync_mutex` 从 app `services/sync_protocol` 提取到 `core/src/sync_utils.rs`（app 侧保留薄委托）：core 的 `services/prompt.rs` 需要它，而 sync_protocol 依赖 app 的 webdav/s3。
5. `initialize_common_config_snippets` 与日志脱敏 helper（`url_for_log*`、`redact_*`、`error_for_log`、`RedactedUrl`）从 app `lib.rs` 移到 core `lib.rs`，app 侧 `pub use` 重导出。

### 11.2 跨 crate 测试可见性（执行发现）

`#[cfg(test)]` 在 core 作为依赖参与 app 测试构建时**不编译**。因此这些仅测试用的项去掉 cfg 并 `pub`：`test_support` 模块（pi/omp/copilot）、`TEST_AGENT_DIR` 覆盖、`take_test_notify_count`、`seed_test_account_with_token_endpoint`、tool_cmd 的 `windows|test` 项。行为不变，仅测试路径可达。

`webdav_and_s3_operations_share_one_sync_mutex` 的收尾断言改为“5s 内可重新获取”：tokio Mutex 的公平移交会在释放瞬间把锁交给排队者，瞬时 `try_lock` 在并行测试下不可靠。

### 11.3 补丁与构建

- 补丁远大于 §10 的 50-70 文件估计：331 个 diff 入口（303 rename + 16 新增 + 11 修改 + 1 删除），~545 KB；`git apply` 以 rename 形式重放 304 个搬移文件。
- 再生方法修正：`git add -N . && git diff` 会**丢掉搬移删除**（`git add -N` 顺带把删除入了 index），改为 `git add -A && git diff --cached && git reset`；`patches/README.md` / `CLAUDE.md` 已同步。
- `make binary` 改为 `cargo build --release -p cc-switch-core --bin cc-switch-proxy`；workspace target 目录不变；`mkdir dist` 仅为 GUI 兼容保留。
- 冷构建约 12 分钟（-j4），可重复（两次独立冷构建产物同为 14015520 字节）。

### 11.4 验证结果（实跑）

- Rust：core 4245 passed / 11 ignored；app 634 passed / 3 ignored → 4879 passed + 14 ignored = 4893（v4.0.33 基线 4892 + C2 回归测试 1；名称级对账零丢失）。fmt/clippy `--all-targets -D warnings` 两 crate 全清。
- daemon：`ldd` 仅 5 个库（libssl/libcrypto/libgcc_s/libm/libc），无 WebKitGTK/GTK/JSC；未设 `LD_LIBRARY_PATH` 时 `--help` 可用；`--port 9301` → `/health` 200、`/status` running、SIGTERM 干净退出（exit 0）；`provider`/`proxy` CLI 冒烟通过。
- GUI：`cargo build -p cc-switch` 成功（app 测试计数与步骤 10 一致）。
- 补丁：`make check` 对 pristine 子模块 “patch applies cleanly”；canonical 冷 `make binary` 通过步骤 11 全部门。
- Python e2e：110/110 OK（新二进制、刻意不设 `LD_LIBRARY_PATH`）。首轮 1 个 live 用例（opencode `mimo-v2.6-flash`）收到上游空回复失败，单跑与全量重跑均绿，判定为上游瞬时 flake。

---

### Critical Files for Implementation

- `cc-switch/src-tauri/src/proxy/forwarder.rs`（C1：8 处 OAuth 查找 + app_handle 字段；最大断耦合面）
- `cc-switch/src-tauri/src/proxy/server.rs`（ProxyState/ProxyServer::new —— 唯一 tauri 型签名；ProxyContext 注入点）
- `cc-switch/src-tauri/src/proxy/failover_switch.rs`（C2 重接线：record_failover_route 必须 headless 生效）
- `cc-switch/src-tauri/src/commands/misc.rs`（V2：待抽入 core::tool_cmd 的 tauri-free 工具/进程 helper 簇）
- `cc-switch/src-tauri/Cargo.toml`（workspace 根 + app/core 依赖分区；tauri-build/generate_context 留此）
