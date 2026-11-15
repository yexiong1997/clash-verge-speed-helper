# 官方 Clash Verge 的无感注入研究与启动修复

研究日期：2026-11-15。目标是继续使用已安装的官方原版，不修改或重签官方 `.app`，不重装官方应用，不关闭 SIP。下述结论区分本机实测、二进制证据与尚未实现的设计。

后续实施：官方启动脚本、无窗口常驻桥接和助手客户端现已接入，详见[当前实现与安装](STARTUP_SCRIPT_FEASIBILITY.md#当前实现与安装)。本报告后文中的“尚未实现”描述原研究阶段；首次完全后台挂接官方页面的限制仍然存在。

## 结果与交付范围

已经修复助手源码中的启动干扰：启动助手或将焦点切回助手，只被动检查、复用已有桥接；没有桥接时安静返回，不启动 Clash、不显示 Clash 主窗口、不打开权限设置或 Web Inspector、不输入加载器。单订阅和多订阅入口均已修改。已有桥接的状态会重新验证，关闭助手后的迟到探测不能恢复连接状态。

**这不是首次注入的后台化实现。** 实际执行跨订阅切换时，仍可能按需走原有的 Console 初始化路径；官方 App 或 WebView 重启后也不能依靠这次启动修复恢复已消失的脚本。本轮没有构建、安装或替换已安装的助手，以上改动处于源码中。

在已验证的通道内，没有找到同时满足“原版官方 App、官方签名、现有系统安全设置”且能冷启动后台执行 JavaScript 的入口。下面给出实际阻碍；这不等同于证明所有可能的入口均不存在。

## 为什么之前每次都弹出窗口

原来的调用链是：

```text
助手 applicationDidFinishLaunching / applicationDidBecomeActive
  → runtimeInjector.start
  → ensureOfficialAppRunning
  → /commands/visible
  → injectLoader
  → 激活 Clash、打开开发者工具、切到 Console、输入脚本
```

这些是助手的主动操作，不是 Clash 必须执行的订阅切换步骤。`start` 在尝试复用外部桥接前就会启动官方 App；默认的 `NSWorkspace.OpenConfiguration` 允许激活应用。原来的焦点回调只要发现桥接断开且有辅助功能权限，就会再次进入同一路径。

助手退出还会停止自己的 HTTP 桥接。内置桥接每次随机选择端口和 token，页面脚本固定使用当次地址，连续失败八次后删除自身。因此关闭助手后，Console 会出现旧地址的请求失败；下一次启动又需要重新初始化。

现有 `/private/tmp/clash-verge-runtime-state.json` 只描述早期独立桥接。独立桥接用 `POST /command` 和 `target`，内置桥接使用内存队列和 `uid`；不能只保存内置端口到该文件就宣称可以跨进程恢复。

## 目标与工具身份

| 项目 | 本次证据 |
| --- | --- |
| 官方应用 | `/Applications/Clash Verge.app` |
| 可执行文件 | `Contents/MacOS/clash-verge` |
| 版本与架构 | 2.5.10，arm64，50,646,032 bytes |
| 可执行文件 SHA-256 | `88a8d735dc2426e22b0040e91bc2baef5fbe30438c22ca78108795fe1ed89f85` |
| Team ID | `JPH3Z7PPBB` |
| 运行保护 | Hardened Runtime；未发现 `get-task-allow` 或关闭 library validation 的 entitlement |
| 研究主机 | macOS 27.2；系统框架结论仅对本机发行版实测 |
| 工具 | REA CLI 6.7.0；Xcode LLVM objdump、LLDB、Clang、Swift |

REA 已完成 Mach-O 与签名检查。当前没有可用的 Hopper、Ghidra、IDA 或 Kuna 深度提供程序，未安装或重配置这些工具；函数级证据来自实际 Mach-O 符号、LLVM 反汇编和系统框架的运行时元数据，不是伪称已经取得的完整反编译源码。

REA retained Evidence IDs：

- Mach-O：`ev_53b221be4cae5176b7f3f42e7f5efaa298680b51806fa271c593e34975c64a3d`
- 签名：`ev_dc1a7691cae8501618820bc8ff0a8482939e20ba746b389f3a9a3868bb8ce22a`

## 已分析的入口

### 1. 官方应用的 Tauri / Wry 执行链

官方 Mach-O 中恢复到 Wry `InnerWebView::eval`，本次文件中的起始地址为 `0x100043acc`；存在 `evaluateJavaScript:completionHandler:` 和 `setInspectable:`。这说明应用内部能够在自己的 WKWebView 执行 JavaScript，不代表任意外部进程可以调用该方法。

恢复到官方 `start_embedded_server`，起始地址为 `0x1008d7f94`，以及内部 `patch_profiles_config` 命令。结合官方基线源码和实际函数反汇编，未确认该 HTTP 服务公开任意 Tauri invoke 或 JS eval；基线路由为窗口显示、scheme 和 PAC。scheme 的订阅导入能力也不能等同于“切到已有 UID 并同步官方 UI”。

历史参考源码目录曾被之前的实验修改过，工作树中的 `/commands/profile` 是新增实验接口。此次使用 `git show HEAD:src-tauri/src/utils/server.rs` 核对基线，不能将这个实验接口说成官方原版已有能力。以上地址是本次二进制的静态地址，运行时地址还受 ASLR 影响。

### 2. 原生 Web Inspector 后台通道

在自建研究工具中加载本机 `WebInspector.framework`，恢复到这些类和 selector：

```text
RWIManager.sharedManager
RWIManager.currentMachine
RWIManager.openChannelForDebuggable:error:
RWITarget.allApplications
RWIApplication.allDebuggables
RWIChannel.start / close / sendMessageToBackend: / setDelegate:
接收消息：channel:receivedMessage:
```

这条通道有实际访问门槛。在 LLDB 中调试的是自建研究工具；没有附加到官方 Clash 进程。对本机 `-[RWIRelayDelegateMac _handleIncomingConnectionEvent:]` 的反汇编确认：

| 函数内偏移 | 检查内容 |
| --- | --- |
| `+168` | `com.apple.private.webinspector.remote-inspection-debugger` |
| `+192` | `com.apple.private.webinspector.driver` |
| `+288` 附近 | `Debugger Connection from PID (%d) did not have entitlement.` |

普通客户端的结果是 `machine_ready=0`、`apps=0`，直接 XPC 连接被中断或失效。仅对自建临时研究工具的副本分别添加上述 entitlement 并 ad-hoc 签名后，两份都被系统终止，子进程返回 `-9`。没有修改官方 App、系统框架或安全设置。不能把“在 plist 中写入私有 entitlement”当作取得权限。

还恢复了协议的区别：`msgData → {messageName,userInfo}` 加 `syn` 是应用侧协议；调试前端使用 `__selector` / `__argument` 以及 `_rpc_reportIdentifier:`、`_rpc_getConnectedApplications:`、`_rpc_forwardGetListing:`、`_rpc_forwardSocketSetup:`、`_rpc_forwardSocketData:`。早期 raw probe 使用了应用侧格式，不能拿它证明前端协议正确；实际 gate 位于消息处理之前，原生 RWIManager 客户端的失败提供了独立佐证。

### 3. WebKit socket inspector / 环境变量

WebKit 将 Cocoa 与 socket inspector 分开构建。本机 JavaScriptCore 导出清单包含 Cocoa inspector API，没有发现 socket server 的对应导出；Clash 可执行文件也没有 `WEBKIT_INSPECTOR_SERVER` 字符串。因此没有证实把 Linux 或 Windows 示例中的环境变量带到 macOS WKWebView 就能启用远程端口。

仅凭字符串或导出缺失不能证明所有私有路径都不存在，这里的结论只限于已检查的发行版、构建文件和导出表。

### 4. SafariDriver 与 Tauri 自动化

只对本机 `safaridriver --mcp` 执行了初始化及 `tools/list`，随后停止；没有读取标签页、导航网页或驱动真实 Safari 会话。工具目录存在 JavaScript 执行能力，但没有任意 App PID / bundle 的 attach 参数。官方 man page 的 `browserName` 限定为 Safari；不能据此宣称能连接 Clash 的 WKWebView。

Tauri 的 macOS WebDriver 路线要求把 WDIO / automation 插件编进目标 App，不适用于当前未修改的官方二进制。已查到的修改系统 daemon、关闭 SIP 或越狱式方案也不满足本任务约束。

## 本轮源码修复

- 新增独立的 `reconnectSilently` API。启动与获得焦点只调用它，避免与显式 `start` 的请求合并导致误触可见初始化。
- 被动探测会验证缓存的外部连接；内置连接只在仍有有效心跳时复用。没有连接是正常的 `success(false)`。
- 探测结果提交前检查生命周期 generation 和连接身份。与显式切换并发时不覆盖其状态；关闭后迟到的结果以取消结束。
- 显式初始化先尝试复用外部连接，再考虑启动官方 App。
- 为显式初始化补上 generation 检查，关闭时立即结清等待回调，避免旧任务恢复 attached 状态。
- `RuntimeBridge.stop()` 改为幂等，避免重复关闭同一个文件描述符。
- 移除旧测试中“真实 singleton 发起切换后立即取消”的竞争；现在用同步无效 UID 校验，避免测试越过取消检查后触及真实 App。

这次没有将内置桥接改成常驻服务，也没有实现冷启动静默注入；不能把上面的改动称作完整的无感注入方案。

## 补充：一次注入能否跨官方 App 重启保留

后续按官方 v2.5.10 的 Git HEAD `c83f7ad529c7c9cb3d2d54a0955ef8c1b22a411b` 核对持久化入口，没有读取或修改真实配置。

**新确认的正常入口是 `startup_script`。** 基线 `src-tauri/src/utils/resolve/mod.rs` 的 `resolve_setup()` 在 `init_window()` 之前等待 `init_startup_script()`；后者调用 `src-tauri/src/utils/init.rs` 的 `startup_script()`，读取配置的脚本路径，按 `.sh → bash`、`.ps1/.bat → powershell` 执行。官方二进制符号清单也包含该初始化路径的 `startup_script` 标识。官方设置界面提供了这个配置项。

它可以承载“一次配置、以后每次官方 App 启动自动拉起桥接或版本检查”。它是外部 shell 入口，不直接提供 WKWebView 的 DOM 或 JS eval；启动脚本被等待执行完毕，若用于启动长期后台进程必须及时返回，否则会阻塞后续建窗。后续已完成[启动脚本可行性核查与 6 项隔离实验](STARTUP_SCRIPT_FEASIBILITY.md)，确认必须完整重定向标准流，不能只加 `&` 或 `nohup`。只创建了临时测试脚本，没有安装实际启动配置。

其他候选的检查结果：

- `start_page` 会持久保存并直接交给 `WebviewUrl::App`；匹配版本的 Tauri 2.12.4 源码随后使用 `app_url.join(path)`。绝对 URL 是一个真实候选，但跳到外部页面后仍要解决 IPC 来源权限与保留原 UI；尚未在官方目标中证实可作为增强引导页。不能将可设置启动 URL 等同于可持久注入。
- 自定义 CSS 放入 `style#verge-theme`，没有确认正常的 JS 加载能力。主题 hook 中确有对 `#Gradient2.innerHTML` 的颜色插值，但全仓只找到查询，没有实际 `Gradient2` DOM 定义，不能判定为可用执行链。
- 官方前端没有发现读取 localStorage / IndexedDB 后自动执行存储脚本的启动链。保存脚本文本本身不会让它重新运行。
- 没有发现 Service Worker 注册与 `tauri:` scheme 支持链；标准注册也不能用 `blob:` 或 `data:` 脚本代替可持久的同源 worker。本次未对真实 WKWebView 做 Service Worker 运行实验，不能宣称这条路已经排除所有私有实现。
- 订阅增强脚本运行在新建的 Boa Context，用于生成代理配置，不在官方页面的 JS realm。

因此，目前可以有证据地承诺的是“设计一次安装、每次启动自动维护的机制”；尚不能承诺“一次运行时注入，跨 Clash 退出重启仍永久有效，直到更新才重做”。更新版本与进程/页面重建是不同事件；即使加载器文件不需要随每次启动更新，新页面仍需要重新加载它。

## 下一步可实现的持续复用方案

要解决“已注入一次之后，每次打开助手都能继续用”，可以让一个本机 broker 持有 HTTP 桥接，独立于助手窗口进程存活。助手关闭时只断开客户端，不销毁页面依赖的服务；重开时连接同一 broker。其协议需统一状态查询、精确 UID 切换、取消和结果核验，并有单实例、私有 manifest、会话身份与退出策略。

另一种方案是持久化内置桥接端口和会话 token，让页面脚本软离线、退避后重新握手；这样助手关闭期间仍可能出现网络失败记录。单纯删除 `console.error` 不能阻止 Web Inspector 记录失败的 fetch。

两种设计都只能复用仍存活的官方页面脚本。官方 App 退出或 WebView reload 后，JS realm 消失，仍需另一个经过验证的首次初始化通道；目前没有这样的后台通道。因此常驻 broker 是可实施的后续工程设计，尚不是本轮已交付或已实测完成的功能。

## 验证与证据位置

本轮运行结果：

- `python3 Tests/test_runtime_passive.py`：7 项通过，包含缓存失效、stop 迟到结果、显式初始化优先复用、stop 后重新 start 的 generation 隔离。测试使用复制的源码、合成 manifest 和 loopback 假服务；真实 App/权限/Inspector 入口被 `fatalError` 拦截，显式初始化测试仅跳过官方签名读取。
- `python3 Tests/test_runtime_bridge.py`：2 项通过，覆盖 CORS、鉴权、桥接往返、超时、取消和存活。
- `python3 Tests/test_runtime_injector_source.py`：通过。
- 多订阅生产入口与单订阅生产入口的 Swift typecheck：均通过；单订阅原有 `allowedFileTypes` 弃用警告仍在。
- 收尾重新核对官方可执行文件 SHA-256，与研究开始时一致；`codesign --verify --deep --strict` 通过。
- 未操作真实订阅、未测速真实节点；这些测试验证启动与连接生命周期，不冒充已完成首次后台注入验收。

原始记录位于仓库内被 git 忽略的 `build/runtime-injection-research/`，包括 REA JSON、符号清单、`wry-eval.asm`、`singleton-routes.asm`、`relay-client-gate.asm`、原生探针源码和框架元数据。`EVIDENCE_SHA256.txt` 记录研究文件摘要；研究工具不作为生产组件或安装产物。

## 公开来源

- [WebKit Cocoa inspector 构建源](https://github.com/WebKit/WebKit/blob/main/Source/JavaScriptCore/inspector/remote/SourcesCocoa.txt)
- [WebKit socket inspector 构建源](https://github.com/WebKit/WebKit/blob/main/Source/JavaScriptCore/inspector/remote/SourcesSocket.txt)
- [JavaScriptCore 平台选择](https://github.com/WebKit/WebKit/blob/main/Source/JavaScriptCore/PlatformJSCOnly.cmake)
- [Safari WebDriver 的官方说明](https://webkit.org/blog/6900/webdriver-support-in-safari-10/)
- [Tauri WebDriver 文档](https://v2.tauri.app/develop/tests/webdriver/)
- [CrabNebula Tauri 自动化安装前提](https://webdriver.io/docs/desktop-testing/tauri/crabnebula-setup)
- [inspect-webkit 固定版本对 Desktop Safari 限制的说明](https://github.com/EvanBacon/inspect-webkit/blob/85feb06f1afb8890d726b5a0f47065e97be96284/README.md#desktop-safari-not-supported)
