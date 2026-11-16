# 更新 cc-switch 子模块 v4.0.10 → v4.0.33（补丁随 pin 一起动）

> **状态：已执行**（2026-11-16 完成，10 步全过验证门，测试全绿：
> Rust 4878 passed / Python 110 OK）。
> 目标 commit：`86e23b6331b9ce2cee314d1ebb5b186da0de645a`（v4.0.33 的实际
> commit；`ee5b0d39` 是 annotated tag 对象，peel 到该 commit——gitlink 与文档
> 一律用 `86e23b63`）。
> 与原计划的两处偏差：① Cargo.lock **无变更**（上游 lock 已含 tokio 的
> `signal-hook-registry` 边），再生补丁为 **12 文件**而非 13、无 lock hunk；
> ② 步骤 1 门中"11G target 缓存保留"仅指开发阶段（步骤 2–5），步骤 7 的
> canonical 冷构建按计划 `worktree remove` 后从零编译（release 7m53s）。

## Context

上游领先 773 commit，补丁触碰文件改动巨大（forwarder.rs +2301 行、handlers.rs +1678、
lib.rs +531、claude.rs +380）。仓库规矩：pin 只能和补丁一起更新；补丁开发在 detached
worktree（`~/.cache/cc-switch-proxy-build`），子模块保持 pristine。
产物：rebase 后的补丁 + 新 pin + 重建二进制 + 测试绿 + 文档同步。commit 后 **push/部署先问用户**。

核实结论：

- **文本冲突仅 3 处**，其余 hunk 原样适用（git apply 自动找偏移）；5 个新文件干净应用；
  新文件依赖的 API 全部未变；MSRV 1.85 < 工具链 1.95；空 dist/ 占位仍满足 tauri-build。
- **语义判定：保留全部 hunk。** 上游改为按每请求 resolved api_format 决定转换
  （forwarder.rs:1919 / handlers.rs:426+464），Claude 适配器 resolved 恒 Some →
  claude.rs 的 `has_routes||` 门控在主路径是死代码、handlers anthropic 直通臂不可达，
  **冗余但无害**（流式路径同样走 resolved，无独立决策；handlers.rs:4275 是测试代码非生产重试）。
  上游新逻辑反而修掉了旧门控的一个潜在流式隐患。在 patches/README 注明门控已惰性。
- ClaudeDesktop 新按模型映射（`meta.claudeDesktopModelRoutes`）仅 `AppType::ClaudeDesktop`
  分支生效，dd-switch app=claude 不受影响，存储键也不冲突。
- init_at 继承的上游新启动步骤（db-change hook / 迁移 / auto-vacuum / pricing-seed）
  **无 Tauri 依赖**：hook 在没有 auto_sync worker 时 no-op，迁移纯 SQL，其余 warn-only。
  注意 pricing/backup 副作用落 `get_app_config_dir()`（认 `CC_SWITCH_TEST_HOME`）→
  **Rust 与 Python 测试都要设 CC_SWITCH_TEST_HOME**，避免碰真实 ~/.cc-switch。

## 锚点迁移表

| 文件 | 处理 |
|---|---|
| `Cargo.toml` | **改写**：tokio 行（49）现为 `…"net", "process"]`，追加 `"signal"` 保留 process |
| `lib.rs` hunk2 | **改写**：`mod provider_kind;`(48) 插在 provider(47)/proxy(49) 之间，重定位两条插入 |
| `proxy/mod.rs` | **改写**：`loopback_probe`(26) 打断上下文；插入点 model_mapper→opaque_state_rectifier 不变 |
| `database/mod.rs` | 偏移应用（98→143），init/init_at 拆分点逐字节未变 |
| `lib.rs` hunk1 | 偏移应用（16→24） |
| `forwarder.rs` ×3 | 偏移应用（1698→1889 / 2243→2517 / 3146→3438） |
| `handlers.rs` | 偏移应用（763→863），match 上下文一致 |
| `claude.rs` | 偏移应用（962→983），`claude_api_format_needs_transform` 本模块已有 |
| 5 个新文件 | 干净应用（headless.rs / provider_cli.rs / proxy_cli.rs / bin/cc-switch-proxy.rs / proxy/model_route.rs） |
| `Cargo.lock` | **丢弃旧 hunk**，cargo 重新生成（signal-hook-registry 上游 lock 已有，diff 仅 tokio 边）；再生补丁必须含新 lock hunk |

## 执行步骤（每步带验证门）

1. **dev worktree 原地重置**：`git -C ~/.cache/cc-switch-proxy-build reset --hard ee5b0d39`
   （旧补丁应用态可弃——以已提交的 patch 文件为准；staged 新文件随 reset 清除；
   dist/ gitignored 存活；11G target/ 缓存保留）。
   门：status 干净、HEAD=ee5b0d39、dist/ 在。
2. **迁移补丁**：先 `git apply --reject --exclude='src-tauri/Cargo.lock' <旧补丁>`
   （干净 hunk 自动偏移应用，3 处破 hunk 落 .rej）→ 按上表手工修 3 处 → 删 .rej。
   门：`git diff --stat` = 8 改 + 5 新，无 .rej 残留。
3. **构建环境 + check**：写 `temp/build-env.sh`（sysroot PKG_CONFIG_*/CPATH/LIBRARY_PATH/
   LD_LIBRARY_PATH → `~/.cache/cc-switch-proxy-sysroot`，**与缓存构建逐字一致**；
   `RUSTFLAGS="-C linker-features=-lld"`；`CARGO_BUILD_JOBS=4`）。source 后 `cargo check`。
   crate 走 rsproxy.cn 镜像**直连不加代理**。门：check 干净；lock diff 仅 tokio 边。
4. **Rust 测试**：`CC_SWITCH_TEST_HOME=<repo>/temp/fakehome-v4033 cargo test --lib`
   （全量，对齐 patches/README 验证标准；已知 codex 计时测试负载敏感，flake 则单跑复验）；
   `cargo fmt --check` + `cargo clippy -D warnings`。门：全绿。
5. **再生补丁**：worktree 根 `git add -N . && git diff > <repo>/patches/cc-switch-headless-proxy.patch`。
   门：13 文件 + 含新 Cargo.lock hunk。
6. **更新 pin**：子模块 `git checkout ee5b0d39`（detached，pristine）→ 父仓库 `git add cc-switch`。
   门：`make check` 输出 "patch applies cleanly"。
7. **出二进制（canonical 冷构建）**：先 `git -C cc-switch worktree remove --force
   ~/.cache/cc-switch-proxy-build`（**关键**：rm -rf 留残留注册会让 Makefile 的
   worktree add 失败）→ source build-env → `make binary`（全新 worktree 冷构建 ~600
   crates，-j4，后台跑 + 监控；strip + 安装 patches/bin/）。
   门：新二进制 `--help` 可运行（LD_LIBRARY_PATH=~/deploy/runtime-libs/usr/lib/x86_64-linux-gnu）。
8. **Python 全套**：`cd dd-switch && CC_SWITCH_TEST_HOME=<repo>/temp/e2e-home
   LD_LIBRARY_PATH=~/deploy/runtime-libs/usr/lib/x86_64-linux-gnu
   .venv/bin/python -m unittest discover -s test_suite`。
   e2e 前置齐全（.testdata ✓ runtime-libs ✓）→ **真跑非 skip**，重点看 opencode e2e
   按模型路由矩阵 + anthropic 路由流式。门：全套绿（109+）。
9. **文档**：patches/README pin ×2（14/31 行）+ Verified 数字按实跑更新 + claude.rs
   门控惰性注明（231–236 行描述）+ 上游 ClaudeDesktop 映射不冲突一句；CLAUDE.md pin
   （128 行，gitignored 本地改）；根 README 无字面 hash 不动；TEST-REPORT 历史记录保留。
   门：grep 无残留 479964f2/v4.0.10（TEST-REPORT 历史除外）。
10. **commit**（pin + 补丁 + 二进制 + 文档；子模块本身不提交）。**push 前问用户**；
    部署（deploy 目录 merge + `git submodule update --init` + restart）也先问。
    部署提示：新二进制首启会对真实 cc-switch.db 跑上游 schema 迁移（自动预备份，
    与桌面端同路径，属预期）。

## 风险与对策

- 3 处破 hunk 手工迁移出错 → 步骤 3/4 编译+测试兜底；语义已验证无需改行为。
- 冷构建耗时长 → 后台跑，中途绝不动 RUSTFLAGS/sysroot（动则 11G 缓存全废）。
- 全量 Rust 测试负载敏感 flake → 单跑复验，如实记录。
- 测试碰真实 ~/.cc-switch → CC_SWITCH_TEST_HOME 全程隔离（Rust + Python）。
- 若 cargo check 暴露上游 API 意外变化（已核全，概率低）→ 停下报告，不硬改语义。