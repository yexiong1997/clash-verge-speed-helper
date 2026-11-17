# Windows V1 产品规格与验收标准（`feat/windows-v1`）

- **状态**：已对齐开发与测试口径，待实现与验收闭环
- **适用范围**：Windows x64 桌面应用（`Windows/`）、构建/安装脚本（`Scripts/build-verified-worker.ps1`、`Scripts/build-multi.ps1`、`Scripts/install-multi.ps1`）与 Windows 测试矩阵（`Tests/test_windows_*.py`、`Tests/test_packaged_smoke.py`）。
- **协同边界**：本文件只定义产品范围、桥接契约、交互规则与 `Given-When-Then` 验收门禁；macOS 版源码、`Sources/` 与 `Worker/verified-worker.patch` 不在本期改动范围内。

---

## 1. 产品目标与 Non-goals（明确不做）

### 1.1 V1 目标
将 Windows 多订阅测速助手收口为可独立构建、可离线冒烟、状态机自洽、误触发可控的 V1 桌面交付：
1. **无头与打包双通路**：`Windows/run.py` 解耦 `pywebview` 顶层导入，支持无 GUI 环境下导入 `PublicApi` 与执行 `--smoke-test`；提供可重复的本地构建/安装入口。
2. **TUN 环境非阻塞提醒**：安全读取本机 `clash-verge.yaml` 的 `tun.enable` 状态并暴露只读 `tun_enabled`，在开启 TUN 时提示用户手动关闭后再测速，不阻断正常流程。
3. **统一页内轻确认**：用统一页内确认组件替代原生 `window.confirm`，补齐「加压验证 · 20M」的确认门槛，并解决异步参数/状态变化后的误确认提交问题。
4. **门禁与并发状态机收口**：严格区分 `SwitchLatch.outcome_unknown`（全局写锁，但放行纯测速）与 `pending`（跨订阅受控续接），修补并发重入与取消定时锁边界。

### 1.2 Non-goals（V1 明确不做）
- **不做** Clash Verge 运行时注入或自动跨订阅热切换（跨订阅最优节点沿用 `pending` → 用户到 Clash Verge 手动切订阅 → 回助手「刷新」+「刷新策略组」→「继续应用最优」）。
- **不做** TCP `external-controller` 降级（仅认 `clash-verge.yaml` 中 `external-controller-pipe` 或 `external-controller-unix`，两者同非空或缺失时直接拒绝）。
- **不做** 后台常驻服务或开机自启定时任务（定时仅在应用运行期内存生效，关闭即停，选区/参数/策略组/授权变化即撤防）。
- **不做** 节点列表自动改写、`proxy-providers` / `rule-providers` 远程拉取、不可信订阅网络沙箱，也不以关闭系统防护方式绕过未签名安全提示。
- **不做** macOS 全套复杂动效复刻（保持 Clear Prism Glass 轻量交互，并兼容 `prefers-reduced-motion`）。

---

## 2. 后端桥接与状态机契约（`Windows/*.py`）

### 2.1 `Windows/run.py` 与 `PublicApi`
- `PublicApi` 仅暴露既有 14 个白名单方法：`get_state`、`refresh`、`set_selection`、`start`、`retest_node`、`start_funnel`、`stop`、`refresh_groups`、`switch_manual`、`undo_switch`、`continue_pending`、`set_schedule`、`export_tsv`、`copy_node_name`；严禁暴露 `window`、`registry`、`secret` 或底层管道/文件诊断信息。
- `import webview` 下移至 `main()` 创建窗口分支（`--smoke-test` 返回之后），确保测试与离线检查无需 `pywebview` 即可导入 `PublicApi` 并运行 `--smoke-test`。
- Worker 路径解析优先使用 `CLASH_SPEED_HELPER_WORKER`，其次使用打包同目录 `clash-speedtest.exe` 或源码构建产物/约定的安装回退路径，并保留 `--profile-root <隔离目录>` 参数支持。

### 2.2 `get_state()` 增量字段：`tun_enabled`
- **字段类型**：`bool | None`（JSON 序列化为 `true` / `false` / `null`）。
- **读取来源**：已钉住（pinned）的 `clash-verge.yaml` 嵌套键 `tun.enable`（对齐 macOS `Sources/main.swift`）。
- **异常分级（安全与非致命分离）**：
  - **安全校验失败**（重解析点 / junction、非磁盘文件、路径异常、读取前后文件版本或父目录身份变化）：**不吞异常**，直接向上抛出并走配置读取失败流程（`refresh()` 失败并复位 `tun_enabled = None`）。
  - **非致命缺失或格式不符合**（`clash-verge.yaml` 不存在即 `winerror == 2`、`parse_yaml` 失败、顶层非映射、`tun` 缺失或非映射、`enable` 缺失或 `type(val) is not bool`）：统一降级为 `None`，**绝不阻断** `refresh()`、测速、刷新策略组或切换。
- **生命周期与纯内存只读**：
  - `Desktop.__init__` 初始化为 `None`。
  - `refresh()` 成功时更新为 `True` / `False` / `None`，失败分支同步复位为 `None`。
  - `refresh_groups()` 在校验安全可读后可顺带更新该值；若仅因本地 Named Pipe / AF_UNIX 控制器未启动导致 `refresh_groups()` 失败，**不得擦除** `refresh()` 已安全读出的 `tun_enabled`。
  - `get_state()` 必须保持**纯内存只读**，不触发磁盘 I/O（适配前端 1200ms 轮询）。

### 2.3 `outcome_unknown` 与 `pending` 门禁规则
1. **`SwitchLatch.outcome_unknown == True`（会话级不可自动清除的全局写门禁）**
   - **严格拒绝**：全部 4 条控制器写路径（带 `auto_switch=True` 的自动切换、`switch_manual`、`undo_switch`、`continue_pending`），以及 `refresh_groups()`、`set_schedule`（`auto_switch=True`）。
   - **明确放行**：**纯测速（`auto_switch=False`）仍允许运行**，包括 `start`、`start_funnel`，以及 `retest_node`（当 `auto_switch=False` 且 `group=""` 时；若传入非空 `group`，仍按既有契约复核策略组并在 `outcome_unknown` 下拒绝）。
2. **`pending`（跨订阅最优节点待续接状态）**
   - **不是**全局写门禁，仅约束 `continue_pending`：当 `pending is None` 或当前激活订阅与 `pending.uid` 不一致（`pending.ready == False`）或未 `refresh_groups` 时拒绝；用户到 Clash Verge 手动切换至目标订阅并完成 `refresh()` + `refresh_groups()` 使 `pending.ready == True`（且 `outcome_unknown == False`）后，`continue_pending` 放行并在切换成功或确认已在目标节点时清空 `pending`。
   - 任意新测速任务启动（`_launch()`）时清空旧 `pending`。

### 2.4 并发重入、参数校验与定时锁边界
- `start()` / `start_funnel()` 在 `running` 或 `refreshing` 期间拒绝重入，且在参数校验、节点校验或并发重入失败路径上**不得提前调用 `_invalidate()`**，以免破坏在途任务的 `generation`、`undo`、`pending` 或已启用定时。
- `set_schedule(0)` 与 `_timer()` 线程对 `scheduled_options`、`schedule` 与 `capture` 的读写必须处于同一把锁（`self.lock`）保护下，防止撤防与定时触发竞态导致状态撕裂。

---

## 3. 前端交互与 DOM 契约（`Windows/ui/*`）

### 3.1 统一页内轻确认组件（替换全部 `window.confirm`）
- **固定 DOM 结构**：
  - `#confirm-dialog`（默认 `hidden = true`，`role="dialog"`，`aria-modal="true"`）
  - `#confirm-title`、`#confirm-message`、`#confirm-accept`、`#confirm-cancel`
- **入口触发矩阵**：

| 操作入口 | 是否需页内确认 | 确认文案必须包含信息 | 取消 / Esc / 变更作废行为 |
|---|---|---|---|
| `#start` / `#dock-start`（开始测速） | 免确认 | — | 直接校验并提交 |
| `#stop` / `#dock-stop`（停止） | 免确认 | — | 直接停止 |
| `#stress`（加压验证 · 20M） | **需确认** | 全勾选节点、20 MiB / 并发 2 / 精确模式产生实际流量，及当前自动切换开关与策略组 | 取消不改参数、不调桥接 |
| `#funnel`（智能两段优选） | **需确认** | 1 MiB 初筛 + 最多 Top N（≤10）20 MiB 复测、估算流量非上限、自动切换状态 | 取消不调桥接 |
| `#retest-best` / 右键 `#menu-retest` | **需确认** | 目标节点名、20 MiB 实际流量、自动切换及目标策略组（若有效） | 取消不调桥接 |
| `#manual` / `#switch-best` / 行双击 / 右键 `#menu-switch` | **需确认** | 目标节点名与目标策略组名 | 取消不调桥接 |
| `#undo`（切回原节点） | **需确认** | 目标策略组名与切回节点名 | 取消不调桥接 |
| `#continue`（继续应用最优） | **需确认** | 目标策略组名与上轮最优节点名 | 取消不调桥接 |
| `#arm`（启用 / 关闭定时） | **`minutes > 0` 需确认；`minutes === 0` 免确认** | 定时间隔、每轮预计流量、自动切换状态、仅应用运行期生效 | 取消不启用定时；`0` 直接撤防 |

- **交互防错硬性要求**：
  1. 同一时刻最多存在一个打开的确认框；点击 `#confirm-accept` 后**最多提交一次**桥接调用（防连击/重复提交）。
  2. 支持 `Escape` 键关闭及 `#confirm-cancel` 取消；取消或 `Esc` 关闭后不触发桥接调用，并将键盘焦点恢复至原触发控件（若该控件仍在文档中且未禁用）。
  3. 打开弹窗时记录目标与上下文快照（如 `options`、目标节点 `uid/name`、`group`、`pending`/`undo` 引用）；若确认前参数被修改、`state.running` 变为 `true`、目标节点/策略组或 `pending`/`undo` 失效，或 `outcome_unknown` 锁生效，**旧确认自动作废并关闭**，禁止拿着旧确认执行新目标。

### 3.2 TUN 非阻塞提醒与 `outcome_unknown` 前端联动
- **TUN 提醒 DOM**：`#tun-notice`（默认 `hidden = true`）。仅当 `state.tun_enabled === true` 时显示非阻塞提示（建议先手动关闭 TUN，测完恢复）；`false` / `null` / `undefined` 一律隐藏。前端不读本地配置文件，也不因 TUN 开启禁用测速。
- **`outcome_unknown` 联动兜底**：
  - `state.outcome_unknown === true` 时，`render()` 同步禁用 `#manual`、`#switch-best`、`#undo`、`#continue`、`#auto`、`#refresh-groups`、`#group` 与右键 `#menu-switch`，**同时复位 `#auto.checked = false`**。
  - `options()` 与 `retestRow()` 在 `state.outcome_unknown === true` 时强制兜底传出 `auto_switch: false` 且 `group: ""`（即使用户触发前曾选过策略组或测试桩预填了 `group`），确保 `#start` / `#stress` / `#funnel` / `#retest-best` / `#menu-retest` 纯测速不被后端 `if auto or group:` 误锁死；轻确认文案同步按实际生效的无自动切换参数展示。
  - `state.outcome_unknown === false` 时，`retestRow()` 保留透传 `$("group").value`，维持原有的策略组复核语义。

### 3.3 异步轮询防覆盖与视口布局
- `refreshState()`（1200ms 周期轮询）必须防乱序/过期响应覆盖刚执行完成的新操作状态；`busy` 锁与后台 `running` 期间正确锁定冲突交互并在失败后恢复按钮可用态。
- 表格行渲染尽量做增量更新或保持滚动与焦点稳定，避免 1.2s 轮询全表重绘造成行选中/右键菜单丢失。
- 在 `1080×700` 最小窗口与高 DPI 缩放下，顶栏、确认弹窗、`#tun-notice`、表格区与底栏不出现重叠、按钮裁切或不可操作溢出。

---

## 4. 前置条件、构建与交付约束

- **运行时与构建前置条件**：目标机须为 Windows x64（`AMD64`）并安装 **Microsoft Edge WebView2 Runtime**（打包目录仅包含 `WebView2Loader.dll` 与托管桥接 DLL，**不含 WebView2 Runtime 本体**；缺 Runtime 时 `--smoke-test` 仍可返回 `0`，但真实 GUI 启动会失败）。打包后无需用户预装 Python/Go，须**整文件夹**发布 `ClashSpeedHelper/`（含 `ClashSpeedHelper.exe`、`clash-speedtest.exe`、`_internal/`、`worker-source-notices/`）。源码构建需 Windows x64、Python 3.12（`Windows/requirements-windows.txt`）、PowerShell 5.1+、Git、`curl.exe`、`tar.exe`、GNU `patch.exe`（详见 `README.md`、`docs/MULTI_SUBSCRIPTION.md` 与 `Scripts/build-multi.ps1`）。
- **开源与未签名边界**：`worker-source-notices/` 与 `UPSTREAM-GPL-3.0.txt`（随包 5 个文件由 `build-multi.ps1` 与 `install-multi.ps1` 共同校验携带）**仅适用于上游 `faceair/clash-speedtest`（`v1.8.6`）worker 组件及 `verified-worker.patch`**，不代表本仓库其余部分许可证（本项目自身许可证尚未指定，见 `docs/THIRD_PARTY.md`）。产物未签名，禁止通过关闭系统防护绕过 Windows 安全提示。
- **安装脚本（`Scripts/install-multi.ps1`）边界**：强制校验 `Windows_NT` + `AMD64` 与无重解析点路径，构建/校验目录内包含完整 5 个通知文件及两份可执行文件并执行 `--smoke-test`，幂等替换 `<InstallRoot>\ClashSpeedHelper` 并清理旧安装残留，不得读写 Clash Verge 配置目录或修改系统安全设置。

---

## 5. 分层验收标准（`Given-When-Then`）

> **门禁原则**：`Tests/test_windows_*.py` 均为隔离单测（使用临时目录、Mock/Synthetic Pipe/AF_UNIX 与 JS 驱动），**不能替代**真实产物冒烟与真机 WebView2 交互验证。终验必须同时集齐以下三层实测证据：
> 1. **Layer A（隔离单测）**：系统 Python（无 `pywebview`）可导入并跑通 `Tests/test_windows_*.py`；`.\build\windows-env\Scripts\python.exe -B -m unittest discover -s Tests -p "test_*.py" -v` 全通过（macOS/POSIX 用例按平台 skip；`test_reparse_profile_rejected` 若因非提权权限 skip，须单独记为环境未决项或在提权/开发者模式环境复验）。
> 2. **Layer B（Worker 与离线打包冒烟）**：`Worker/self-test.py` 通过；`Scripts/build-multi.ps1` / `install-multi.ps1` 产物目录执行 `ClashSpeedHelper.exe --smoke-test` 返回 `0`（全程离线、不读写 `%APPDATA%`、不连接真实 Clash Verge 管道；注意 `--smoke-test` 在 `import webview` 前返回，**不验证** WebView2 Runtime 与 GUI 启动）。
> 3. **Layer C（真机 WebView2 交互闭环）**：确认目标机已安装 WebView2 Runtime，并使用隔离 `--profile-root`（及受控/合成环境）在真实 `edgechromium` 桌面窗体验证轻确认交互、`tun_enabled === true` 展示、`pending` 跨订阅续接与 `outcome_unknown` 写锁路径。

### 验收清单

1. **无头桥接与打包离线冒烟**
   - **Given** 运行环境未安装 `pywebview`（或处于打包后的 `ClashSpeedHelper.exe --smoke-test`）
   - **When** 导入 `Windows.run.PublicApi` 或执行 `ClashSpeedHelper.exe --smoke-test`
   - **Then** 导入不抛 `ModuleNotFoundError`；`--smoke-test` 在本地 UI 与 `clash-speedtest.exe` 齐备时退出码为 `0`，全程不发起网络请求、不读取 `%APPDATA%`、不创建 GUI 窗口（GUI 与 WebView2 Runtime 可用性由 Layer C 单独验收）。

2. **`tun_enabled` 三态读取、纯内存轮询与安全阻断**
   - **Given** 隔离 `--profile-root` 下存在有效 `profiles.yaml`
   - **When** `clash-verge.yaml` 分别为：(a) `tun: {enable: true}`、(b) `tun: {enable: false}`、(c) 文件缺失或 `tun.enable: "true"` / 非法 YAML、(d) 存在重解析点（symlink/junction）
   - **Then** (a)(b) 返回 `True`/`False` 且仅当 `=== true` 时前端展示非阻塞 `#tun-notice`；(c) 降级为 `None` 且 `refresh()`、测速与切换均不被阻断；(d) 按安全校验失败阻断 `refresh()` 并复位 `tun_enabled = None`；所有场景下 `get_state()` 不读盘、不泄露 `secret` 或控制器路径。

3. **统一页内轻确认覆盖、取消/Esc 与快照作废**
   - **Given** 应用处于可操作状态，无原生 `window.confirm` 弹窗
   - **When** 依次触发 `#stress`、`#funnel`、`#retest-best`、`#menu-retest`、`#manual`、`#switch-best`、行双击、`#menu-switch`、`#undo`、`#continue`、`#arm`（`minutes > 0`）
   - **Then** 均弹出 `#confirm-dialog` 展示目标与流量/策略组摘要；点 `#confirm-cancel` 或按 `Escape` 不触发任何桥接调用并恢复焦点至原触发控件；连击 `#confirm-accept` 仅触发一次桥接；若弹窗期间参数、行数据、`pending`/`undo` 变动或 `state.running` 变为 `true`，旧弹窗自动作废关闭且不提交；`#arm` 在 `minutes === 0` 时免弹窗直接撤防。

4. **`stress` 与 `funnel` 执行契约**
   - **Given** 已勾选至少一个可测节点且完成轻确认
   - **When** 通过 `#stress` 或 `#funnel` 确认启动
   - **Then** `#stress` 按 `parallel=2, download_bytes=20*1048576, mode="accurate"` 启动测速；`#funnel` 先以 1 MiB / 并发 10 初筛，仅当全部初筛结果完整且无系统级失败/取消时，晋级至多 `min(top_count, 10)` 个可评分合格节点执行 20 MiB / 并发 2 / 4 流复测；停止或任一校验失败绝不进入二阶段或自动切换。

5. **并发重入与定时撤防锁边界**
   - **Given** 后台已有测速/刷新正在进行，或已启用定时任务
   - **When** 并发调用 `start()` / `start_funnel()`，或并发执行 `set_schedule(0)` 与 `_timer()` 触发窗口
   - **Then** 重入请求返回 `ok: false` 且不触发 `_invalidate()`，在途 `generation`、`undo`、`pending` 与定时完好；`set_schedule(0)` 与 `_timer()` 不发生未加锁写撕裂或已撤防任务误发射。

6. **`pending` 跨订阅受控续接**
   - **Given** 测速最高分节点属于非当前激活订阅，`auto_switch=true`
   - **When** 测速结束产生 `pending`；用户尚未切换 Clash Verge 订阅时点击确认执行 `continue_pending`；随后在 Clash Verge 切至目标订阅并执行「刷新」+「刷新策略组」后再确认「继续应用最优」
   - **Then** 未切回目标订阅前 `pending.ready === false`，按钮禁用且后端拒绝 `continue_pending`；切回并刷新使 `pending.ready === true` 后，确认提交一次即校验组快照并切换成功、清空 `pending`（若配置/节点哈希变化则自动作废 `pending`）。

7. **`outcome_unknown` 全局锁写与纯测速可用**
   - **Given** 此前已选择目标策略组并开启 `#auto`，某次 `PUT` 切换发生超时/状态不明触发 `SwitchLatch.outcome_unknown = True`
   - **When** 尝试调用 `switch_manual`、`undo_switch`、`continue_pending`、`refresh_groups`、带 `auto_switch=True` 的任务，或点击 `#start` / `#stress` / `#funnel` / `#retest-best` / `#menu-retest` 进行纯测速
   - **Then** 全部控制器写路径与 `refresh_groups` 严格拒绝且不发起 `PUT`；前端自动将 `#auto.checked` 置 `false` 并禁用写控件与 `#group` / `#refresh-groups`，同时在 `options()` 与 `retestRow()` 兜底传出 `auto_switch: false, group: ""`，使全部纯测速（含单节点复测）仍可正常启动与完成。
