# 一次配置，让官方 Clash Verge 每次启动辅助程序

研究日期：2026-11-15。结论：官方 `startup_script` 足以作为持久的辅助程序启动入口，不需要修改、重签或重装 `/Applications/Clash Verge.app`。源码调用链已确认，后台进程的等待和脱离行为已在本机隔离验证。后续实现已接入项目，安装方式见下一节；后文保留研究阶段的证据与限制。

## 当前实现与安装

`Scripts/install-multi.sh` 的默认用户安装会构建并部署独立的 `ClashRuntimeBroker`，将稳定启动器写入 `~/Library/Application Support/ClashVergeSpeedHelper/Runtime/`，再启用官方 `startup_script`。首次配置若遇到官方进程正在运行，会先保存重启恢复记录、有序退出官方进程、只更新该字段，再重新打开原版 Clash。已有启动脚本会被保留，继续按原解释器、工作目录和退出码运行。

后台程序没有窗口或 Dock 图标，通过文件锁保持单实例。它持有 loopback HTTP 桥接，助手退出只断开客户端。助手下次打开时复用现有连接；如果后台未运行，会尝试静默启动已安装的后台程序。后台本身不调用 AX、不启动 Clash、不打开 Inspector。

显式跨订阅切换时，如果官方页面尚未挂接，助手仍通过现有的权限检查和 Console 初始化将页面连接到后台桥接。这解决助手生命周期导致的重复初始化，不代表官方 App 重启后已经可以全程无感重新注入。

安装器记录原启动脚本，重复安装不会把自身当作原脚本；已经激活过的安装若发现用户另改设置，会保留用户选择。升级或卸载先等后台释放单实例锁，状态接口失联但锁仍占用时停止操作。Clash 的待恢复重启意图保存在用户私有目录，安装中断后重试会恢复对应事务。

安装命令：

```sh
Scripts/install-multi.sh
```

只卸载后台自动启动配置、恢复原设置并移除后台程序（保留测速助手 App）：

```sh
python3 Scripts/install-runtime-broker.py --uninstall --restart-clash
```

卸载时如果用户已经改成别的启动脚本，则保留新设置。自定义 `CLASH_MULTI_INSTALL_ROOT` 的测试安装默认不接触真实 Clash 配置；后台安装集成测试使用显式临时 `--root` 和 `--config`。

相关文件：`Sources/RuntimeBrokerMain.swift`、`Sources/ClashRuntimeInjector.swift`、`Scripts/install-runtime-broker.py`。新增 `Tests/test_runtime_broker.py` 的 16 项测试覆盖单实例、真实 Swift 客户端退出重连、请求去重、取消结果、停止竞争、配置保留、升级中断、恢复事务和不可访问的旧服务。

本机安装验收：默认用户目录中的正式助手已更新，官方 `startup_script` 已指向稳定启动器；原设置为空。官方进程有序重启后启动器日志被创建，后台服务就绪。桌面打开助手、关闭窗口退出、再重新打开后，后台 PID 与会话保持不变，后台没有窗口，Clash 没有新增 Inspector 窗口。官方可执行文件 SHA-256 与研究开始时一致，严格签名验证通过。此验收没有切换真实订阅；当前页面尚未注入，后台状态如实为 `attached=false`。

## 证据范围

- 官方目标：Clash Verge Rev 2.5.10，arm64。已有 Mach-O 分析中包含 `startup_script` 符号；本轮签名 entitlement 检查再次确认 `com.apple.security.app-sandbox=false`。
- 源码基线：`c83f7ad529c7c9cb3d2d54a0955ef8c1b22a411b`（`Release: v2.5.10`）。所有源码依据来自 `git show HEAD` / `git grep HEAD`，未使用历史目录里已改动的工作树版本。
- shell 依赖：Cargo.lock 指定 `tauri-plugin-shell 2.5.0`；缓存 crate 的 SHA-256 为 `8567d42b2c7264355daa1df415ec77f52aa42e885924c86a969be2e920e05521`，与锁文件一致。已核对解包源码和缓存文件一致。
- 运行实验：只创建临时脚本及有限时长的无害子进程，没有启动、退出、操作官方 App，没有读取真实订阅、控制器凭据或当前 `verge.yaml`。

## 官方调用链与触发时机

```text
新主应用进程的 setup
  → resolve_setup_async
  → resolve_setup
  → init_startup_script
  → init::startup_script
  → shell().command("bash").current_dir(script_parent)
       .args([absolute_script_path]).output().await
  → service manager / window / core 初始化
```

基线位置：

| 文件与起始行 | 确认内容 |
| --- | --- |
| `src-tauri/src/lib.rs:231` | setup 发起完整初始化 |
| `src-tauri/src/utils/resolve/mod.rs:50` | 初始化是异步任务 |
| `src-tauri/src/utils/resolve/mod.rs:63` | 等待脚本后才继续服务、窗口和核心初始化 |
| `src-tauri/src/utils/resolve/mod.rs:148` | 脚本调用的错误只记录，随后继续 |
| `src-tauri/src/utils/init.rs:599` | 从持久配置读取路径并执行 |
| `src-tauri/src/config/verge.rs:429` | 配置保存到 `verge.yaml` |
| `src-tauri/src/config/verge.rs:452` | `startup_script` 支持字段级更新 |
| `src/components/setting/setting-verge-basic.tsx:213` | 官方界面已有启动脚本选择和清空功能 |

这里的“每次启动”是每次主 Clash 进程重新启动，包括用户启动和已配置的登录启动；不是每次打开窗口。Dock Reopen 只显示窗口或退出轻量模式，不走启动脚本。已经有主进程时，再次运行被 singleton 判为 Secondary 的进程会提前退出，也不会再次执行该初始化链。

该入口不负责启动 Clash 自身。系统重启后，只有 Clash 被用户或它自己的登录启动机制拉起，脚本才会执行。

## 权限与路径

这是官方 Rust 层直接调用 shell API。`tauri-plugin-shell/src/lib.rs:63` 直接返回 `Command::new(program)`；前端 JS/IPC 的 capability scope 检查位于 `src/commands.rs`，不是本调用链的前置条件。因此不需要为这个启动动作增加前端 shell capability，也不需要辅助功能或屏幕录制权限。

辅助程序以当前用户身份执行，不会因此取得管理员权限。以后若访问受 TCC 保护的文件、进行 AX 操作或控制官方页面，相应限制仍单独适用。

具体行为：

- `.sh` 使用 PATH 中的 `bash`；`.ps1` / `.bat` 使用 `powershell`。macOS 使用 `.sh`，不能靠 shebang 将它变成 zsh 脚本。
- 路径作为一个参数传递，中文和空格可正常处理；需要保存真实绝对路径，字符串中的 `~` 和 `$HOME` 不由这一层展开。
- 工作目录是脚本父目录。执行不会自动读取交互式 shell 的初始化文件；辅助程序和工具宜使用绝对路径。
- 脚本被 bash 读取，本次实验中的 `0600` 脚本也可执行；正式安装仍建议目录和脚本仅对本用户可写。

建议将稳定入口放在 `~/Library/Application Support/ClashVergeSpeedHelper/start-clash-bridge.sh`，避免放在构建临时目录、随安装替换的 `.app` 内部，或需要额外文件授权的文稿目录。`startup_script` 保存展开后的绝对路径。后台二进制可以由助手安装器部署到该用户私有目录，升级时原子替换。

## 必须避免的启动阻塞

Clash 使用 `.output().await`，而且没有超时。该调用既等待 bash 退出，也等待 stdout / stderr 管道读完；同版本依赖 `src/process/mod.rs:397` 先 wait 子进程，`:400` 再 join 两个读线程，然后发送 `Terminated`。

因此以下两种写法都不充分：

```sh
helper &
nohup helper &
```

在这里 stdout/stderr 是管道，不是终端；仅 `nohup` 不会可靠地替换它们。只要后台进程仍持有管道，启动脚本已经退出也无法让 `.output()` 完成。长时间输出还会积累在父进程内存中，且 Clash 丢弃返回的 `Output`，不会自动将这些内容写入日志。

启动器应做三件事：检查辅助程序存在；将 stdin、stdout、stderr 完整重定向；后台启动并立即返回。概念结构如下，尚未安装，也不是当前项目已存在的可执行命令：

```sh
#!/bin/bash
broker='/绝对路径/ClashRuntimeBroker'
log_file='/绝对路径/bridge.log'
[ -x "$broker" ] || exit 0
/usr/bin/nohup "$broker" </dev/null >>"$log_file" 2>&1 &
exit 0
```

生产版本还需要日志限额和启动结果自检。启动器不能同步等待 Clash 的窗口、核心或桥接 ready，因为它们位于同一启动链的后面；应由后台进程在启动器返回后有界等待。

## 隔离实验

最初尝试直接编译完整 `tauri-plugin-shell` 依赖，但离线缓存缺少 Tauri GUI 依赖 `muda`。没有为此安装或下载新依赖。改为编译原版 `process/mod.rs`，使用同版本 `shared_child`、`os_pipe`、Tokio 等缓存依赖，以及一个最小 Tokio runtime 适配层。命令创建、管道读写、等待子进程、收集 `.output()` 的实现直接来自未修改的原版模块。

这验证进程等待语义，不是完整 Tauri App、系统 TCC 或已安装官方 App 的端到端运行测试。

| 场景 | 实测 `.output()` 返回耗时 | 结果 |
| --- | ---: | --- |
| 中文空格绝对路径、脚本 cwd、非可执行 `.sh` | 2 ms | 正确执行 |
| 后台子进程继承输出管道 | 1514 ms | 必须等 1.5 秒子进程结束 |
| 只加 `nohup` | 1527 ms | 同样等待子进程结束 |
| `nohup` 加三路重定向 | 5 ms | 父启动器立即返回，子进程继续运行并写入完成标记 |
| 脚本 `exit 7` | 5 ms | 返回 `Ok(Output)`，其中状态为失败 |
| 辅助程序缺失时直接返回 | 4 ms | 正常退出，不等待 |

耗时仅是本次本机结果，不是性能承诺。6 项全部通过。探针、测试源码及结果保存在 git 忽略的 `build/startup-script-probe/`，没有创建常驻服务或登录项。

正式接入还应注意：Clash 未检查 `.output()` 的退出码，所以脚本非零退出不会由这层自动报告为启动失败。缺文件、扩展名不支持、spawn 失败会被记录后继续，但挂起不会超时。后台程序必须自行记录错误并向助手提供状态。

## 推荐接入方式

1. 新增专门的无窗口后台程序。当前测速助手主程序会创建 UI，直接启动它不符合无感要求。后台程序只维护桥接、识别官方 App 会话、等待就绪，不自动打开 Inspector。
2. 首次安装时部署稳定的 `.sh` 启动器和后台程序，然后设置官方 `startup_script`。使用已有官方配置事务或在确认官方进程退出后原子更新配置，避免与内存中的旧配置互相覆盖。本轮未执行安装步骤。
3. 如用户已有启动脚本，保存原路径，包装时按原解释器、原 cwd 单独执行；不能覆盖丢失，也不应简单 `source` 原脚本改变其原有运行语义。卸载时只在当前字段仍指向本启动器的条件下恢复原值。
4. 后台程序用操作系统文件锁或独占本机 socket 保证单实例；重复启动通知或助手重开时复用存活实例。不要仅凭 PID 文件判断，以免 PID 复用或崩溃后残留。
5. 后台程序与助手 UI 分开管理生命周期，检测 Clash 退出后按策略退出或等待下一会话。官方 Rust shell 路径不保证替我们回收后台进程；它也不是崩溃后自动重启的服务管理器。当前需求先不额外安装 LaunchAgent。
6. Clash 更新后先检查版本、签名和协议兼容性，再连接。启动器路径和配置位于用户数据目录，正常替换 `.app` 不需要重新填写；删除用户配置、迁移目录、字段改名或未来版本改动仍可能要求修复。

## 可以和不可以承诺什么

可以据现有证据设计并实施“一次配置，官方 Clash 每次真正启动时自动启动无窗口辅助程序”。正常启动不需要控制窗口或显示终端，也不需要每次重新授权。

它只解决启动和维护辅助程序。脚本在独立进程中运行，不自动取得官方 WKWebView 的 JavaScript 执行权；Clash 重启后重新挂载页面补丁的无感通道仍未验证成功。参见[运行时注入研究](RUNTIME_INJECTION_RESEARCH.md)。
