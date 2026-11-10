import AppKit
import Foundation
import Darwin

// The configuration stays in memory, is parsed locally, and is delivered to
// each local worker via stdin. No worker is started before user confirmation.
private let profileScript = #"""
require 'yaml'
require 'json'
root = File.join(Dir.home, 'Library', 'Application Support', 'io.github.clash-verge-rev.clash-verge-rev')
def parse_yaml(bytes)
  YAML.safe_load(bytes, [], [], true)
end
begin
  mode = ARGV[0]
  if mode == 'current'
    data = parse_yaml(File.read(File.join(root, 'profiles.yaml'), encoding: 'UTF-8')) || {}
    current = data['current']
    items = data['items'].is_a?(Array) ? data['items'] : []
    item = items.find { |x| x.is_a?(Hash) && x['uid'] == current }
    raise 'no current profile' unless item
    file = item['file']
    raise 'invalid filename' unless file.is_a?(String) && File.basename(file) == file && !file.empty?
    puts JSON.generate({path: File.join(root, 'profiles', file), name: (item['name'] || file).to_s})
  elsif mode == 'socket'
    data = parse_yaml(File.read(File.join(root, 'clash-verge.yaml'), encoding: 'UTF-8')) || {}
    socket = data.is_a?(Hash) ? data['external-controller-unix'] : nil
    puts JSON.generate({socket: socket})
  elsif mode == 'inspect' || mode == 'inspect-stdin'
    bytes = mode == 'inspect-stdin' ? STDIN.read : File.read(ARGV[1], encoding: 'UTF-8')
    data = parse_yaml(bytes)
    raise 'not a mapping' unless data.is_a?(Hash)
    proxies = data['proxies'].is_a?(Array) ? data['proxies'] : []
    providers = data['proxy-providers'].is_a?(Hash) ? data['proxy-providers'].size : 0
    names = proxies.map { |p| p.is_a?(Hash) ? p['name'] : nil }
    tun = nil
    begin
      generated = parse_yaml(File.read(File.join(root, 'clash-verge.yaml'), encoding: 'UTF-8'))
      tun = generated['tun']['enable'] if generated.is_a?(Hash) && generated['tun'].is_a?(Hash)
    rescue StandardError
    end
    puts JSON.generate({count: proxies.size, providers: providers, names: names, tun: tun})
  else
    raise 'invalid action'
  end
rescue StandardError
  puts JSON.generate({error: true})
  exit 1
end
"""#

private func literalRegex(_ text: String) -> String {
    // Match the exact UTF-8 node name even when a regex metacharacter is
    // followed by a combining scalar and forms one extended grapheme cluster.
    let metacharacters = Set("\\.+*?()|[]{}^$".unicodeScalars)
    var result = "\\A"
    for scalar in text.unicodeScalars {
        if metacharacters.contains(scalar) { result.append("\\") }
        result.unicodeScalars.append(scalar)
    }
    return result + "\\z"
}

private func inspect(_ args: [String], data: Data? = nil) -> [String: Any]? {
    let p = Process(); p.executableURL = URL(fileURLWithPath: "/usr/bin/ruby")
    p.arguments = ["-e", profileScript] + args
    let output = Pipe(); p.standardOutput = output; p.standardError = FileHandle.nullDevice
    let input = Pipe()
    if data != nil { p.standardInput = input }
    do {
        try p.run()
        if let bytes = data { try input.fileHandleForWriting.write(contentsOf: bytes); try input.fileHandleForWriting.close() }
        let bytes = output.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        guard p.terminationStatus == 0 else { return nil }
        return try JSONSerialization.jsonObject(with: bytes) as? [String: Any]
    } catch { if p.isRunning { p.terminate() }; return nil }
}

// Subscription metadata can be returned as if it were a proxy. Never let an
// obvious informational entry become an automatic-switch candidate.
private func isInformationalNodeName(_ name: String) -> Bool {
    name.contains("剩余流量") || name.contains("到期")
}

private func usableNames(_ info: [String: Any]) -> [String]? {
    guard let count = info["count"] as? Int, let providerCount = info["providers"] as? Int,
          providerCount == 0, let names = info["names"] as? [String], names.count == count, !names.isEmpty,
          Set(names).count == names.count else { return nil }
    guard names.allSatisfy({ !$0.isEmpty && $0.rangeOfCharacter(from: .controlCharacters) == nil }) else { return nil }
    return names
}

// Trust only a unique, currently existing, same-user Unix socket. The service
// path differs from the generated config path when Verge runs in service mode.
private struct LocalControllerIdentity: Equatable, Sendable {
    let path: String
    let device: dev_t
    let inode: ino_t
    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.path.utf8.elementsEqual(rhs.path.utf8) && lhs.device == rhs.device && lhs.inode == rhs.inode
    }
}

private func localController() -> LocalControllerIdentity? {
    var paths = ["/var/run/clash-verge-service/users/\(getuid())/verge-mihomo.sock"]
    if let info = inspect(["socket"]), let path = info["socket"] as? String { paths.append(path) }
    var candidates: [LocalControllerIdentity] = []
    for path in paths where path.hasPrefix("/") && path.rangeOfCharacter(from: .controlCharacters) == nil {
        var item = stat()
        let status = path.withCString { lstat($0, &item) }
        guard status == 0, (item.st_mode & mode_t(S_IFMT)) == mode_t(S_IFSOCK), item.st_uid == getuid() else { continue }
        let candidate = LocalControllerIdentity(path: path, device: item.st_dev, inode: item.st_ino)
        if !candidates.contains(where: { $0.device == candidate.device && $0.inode == candidate.inode }) {
            candidates.append(candidate)
        }
    }
    // Multiple active-looking sockets are ambiguous; do not guess which core is live.
    return candidates.count == 1 ? candidates[0] : nil
}

private func isCurrentProfile(path: String, snapshot: Data? = nil) -> Bool {
    guard let info = inspect(["current"]), let current = info["path"] as? String,
          URL(fileURLWithPath: current).standardizedFileURL.path == URL(fileURLWithPath: path).standardizedFileURL.path else { return false }
    guard let snapshot = snapshot else { return true }
    guard let currentData = try? Data(contentsOf: URL(fileURLWithPath: current)) else { return false }
    return currentData == snapshot
}

private struct SwitchPlan: Sendable {
    let controller: LocalControllerIdentity
    let group: String
    let expectedCurrent: String
    let directMembers: [[UInt8]]
    let profilePath: String
    let snapshot: Data
}

// Main-thread-owned bounded scheduler. Each child handles one exact node and has
// private stdin/stdout; result callbacks are serialized onto the main thread.
private final class BatchRunner {
    typealias ResultCallback = (Int, String, [String]?) -> Void
    private let executable: URL
    private let config: Data
    private let names: [String]
    private let size: Int
    private let parallel: Int
    private let streams: Int
    private let latencyTimeout: String
    private let downloadTimeout: String
    private let serverURL: String?
    private let onResult: ResultCallback
    private let onFinish: (Bool, Int, Int) -> Void
    private var next = 0
    private var active: [UUID: Process] = [:]
    private var wasCancelled = false
    private var didFinish = false
    private(set) var peakWorkers = 0
    private(set) var launched = 0

    init(executable: URL, config: Data, names: [String], size: Int, parallel: Int,
         streams: Int, latencyTimeout: String, downloadTimeout: String, serverURL: String? = nil,
         onResult: @escaping ResultCallback, onFinish: @escaping (Bool, Int, Int) -> Void) {
        self.executable = executable; self.config = config; self.names = names; self.size = size
        self.parallel = max(1, parallel); self.streams = streams; self.latencyTimeout = latencyTimeout
        self.downloadTimeout = downloadTimeout; self.serverURL = serverURL
        self.onResult = onResult; self.onFinish = onFinish
    }
    func start() { schedule() }
    func cancel() {
        guard !didFinish else { return }
        wasCancelled = true
        for p in active.values where p.isRunning { p.terminate() }
        completeIfDone()
    }
    private func schedule() {
        guard !didFinish else { return }
        while !wasCancelled && active.count < parallel && next < names.count {
            let index = next; next += 1
            launch(index: index, name: names[index])
        }
        completeIfDone()
    }
    private func completeIfDone() {
        guard !didFinish && active.isEmpty && (wasCancelled || next >= names.count) else { return }
        didFinish = true; onFinish(wasCancelled, launched, peakWorkers)
    }
    private func launch(index: Int, name: String) {
        let p = Process(); p.executableURL = executable
        p.arguments = ["-c", "/dev/stdin", "-f", literalRegex(name), "-download-size", String(size),
                       "-concurrent", String(streams), "-timeout", downloadTimeout,
                       "-max-latency", latencyTimeout, "-rename=false"]
        // Cloudflare's endpoint normally generates the requested bytes per GET.
        // Upstream does not cap body reads, so this remains an estimate, not a hard limit.
        let target = serverURL ?? "https://speed.cloudflare.com/__down?bytes=\(size / streams)"
        p.arguments! += ["-server-url", target]
        let input = Pipe(); let output = Pipe()
        p.standardInput = input; p.standardOutput = output; p.standardError = FileHandle.nullDevice
        p.currentDirectoryURL = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
        let id = UUID()
        do { try p.run() } catch { onResult(index, name, nil); return }
        active[id] = p; launched += 1; peakWorkers = max(peakWorkers, active.count)
        let bytes = config
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do { try input.fileHandleForWriting.write(contentsOf: bytes) } catch { }
            try? input.fileHandleForWriting.close()
            let outputData = output.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit(); let code = p.terminationStatus
            DispatchQueue.main.async { self?.workerFinished(id: id, index: index, name: name, output: outputData, code: code) }
        }
    }
    private func workerFinished(id: UUID, index: Int, name: String, output: Data, code: Int32) {
        guard active.removeValue(forKey: id) != nil else { return }
        let text = String(data: output, encoding: .utf8) ?? ""
        let lines = text.split(separator: "\n").map(String.init)
        let header = ["序号", "节点名称", "类型", "延迟", "抖动", "丢包率", "下载速度"]
        let fields = lines.count == 2 ? lines[1].components(separatedBy: "\t") : []
        if code == 0, lines.count == 2, lines[0].components(separatedBy: "\t") == header,
           fields.count == 7, fields[0] == "1.", fields[1].utf8.elementsEqual(name.utf8) {
            onResult(index, name, fields)
        } else if !wasCancelled { onResult(index, name, nil) }
        schedule()
    }
}

private struct Row {
    let number: String
    let name: String
    let type: String
    let latency: String
    let jitter: String
    let failures: String
    let speed: String
    var speedValue: Double {
        let s = speed.uppercased().replacingOccurrences(of: " ", with: "")
        guard s.range(of: "^[0-9]+(?:\\.[0-9]+)?(?:TB/S|GB/S|MB/S|KB/S|B/S)$", options: .regularExpression) != nil,
              let range = s.range(of: "^[0-9]+(?:\\.[0-9]+)?", options: .regularExpression),
              let value = Double(s[range]), value.isFinite, value > 0 else { return -1 }
        let multiplier: Double
        if s.hasSuffix("TB/S") { multiplier = 1024 * 1024 * 1024 * 1024 }
        else if s.hasSuffix("GB/S") { multiplier = 1024 * 1024 * 1024 }
        else if s.hasSuffix("MB/S") { multiplier = 1024 * 1024 }
        else if s.hasSuffix("KB/S") { multiplier = 1024 }
        else if s.hasSuffix("B/S") { multiplier = 1 }
        else { return -1 }
        let converted = value * multiplier
        return converted.isFinite ? converted : -1
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, NSTableViewDataSource, NSTableViewDelegate {
    private var window: NSWindow!
    private let profileLabel = NSTextField(labelWithString: "正在查找 Clash Verge 当前订阅…")
    private let detailLabel = NSTextField(wrappingLabelWithString: "")
    private let statusLabel = NSTextField(labelWithString: "选择模式，然后点击“开始测速”。")
    private let sizePopup = NSPopUpButton(frame: .zero, pullsDown: false)
    private let modePopup = NSPopUpButton(frame: .zero, pullsDown: false)
    private let startButton = NSButton(title: "开始测速", target: nil, action: nil)
    private let stopButton = NSButton(title: "停止", target: nil, action: nil)
    private let chooseButton = NSButton(title: "选择其他配置…", target: nil, action: nil)
    private let refreshButton = NSButton(title: "使用当前订阅", target: nil, action: nil)
    private let copyButton = NSButton(title: "复制结果", target: nil, action: nil)
    private let switchCheck = NSButton(checkboxWithTitle: "测速后自动切换最快节点", target: nil, action: nil)
    private let groupPopup = NSPopUpButton(frame: .zero, pullsDown: false)
    private let groupRefreshButton = NSButton(title: "刷新策略组", target: nil, action: nil)
    private let table = NSTableView()
    private var profilePath: String?
    private var profileName = ""
    private var profileCount = 0
    private var providerCount = 0
    private var tunEnabled: Bool?
    private var rows: [Row] = []
    private var runner: BatchRunner?
    private var failureCount = 0
    private var busy = false
    private var groupGeneration = 0
    private var groups: [ProxyGroup] = []
    private var groupSnapshot: Data?
    private var availableController: LocalControllerIdentity?
    private var controllerClient: ClashController?
    private var switchPlan: SwitchPlan?
    private var runToken: UUID?
    private let sizes = [3, 5, 20, 50]
    private var quickMode: Bool { modePopup.indexOfSelectedItem == 0 }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        ClashRuntimeInjector.shared.start { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                if case .failure(let error) = result {
                    self.statusLabel.stringValue = "Clash Verge 运行时补丁未连接：\(error.localizedDescription)"
                }
            }
        }
        useCurrentProfile(nil); window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        canClose() ? .terminateNow : .terminateCancel
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool { canClose() }
    private func canClose() -> Bool {
        guard !busy || runner != nil else {
            statusLabel.stringValue = "正在核对或切换策略组，请等待操作完成后再关闭。"
            return false
        }
        return true
    }
    func applicationWillTerminate(_ notification: Notification) {
        runner?.cancel()
        ClashRuntimeInjector.shared.stop()
    }
    private func label(_ text: String, size: CGFloat, weight: NSFont.Weight = .regular) -> NSTextField {
        let field = NSTextField(labelWithString: text); field.font = .systemFont(ofSize: size, weight: weight); return field
    }
    private func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1080, height: 680), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Clash 节点测速助手（自动切换版）"; window.center(); window.minSize = NSSize(width: 800, height: 560); window.delegate = self
        let root = NSStackView(); root.orientation = .vertical; root.alignment = .leading; root.spacing = 12; root.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(root)
        NSLayoutConstraint.activate([root.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 24), root.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -24), root.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 20), root.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor, constant: -18)])
        root.addArrangedSubview(label("Clash 节点测速助手", size: 24, weight: .bold))
        let subtitle = label("快速模式同时测试 4 个节点；精确模式逐个测试", size: 13); subtitle.textColor = .secondaryLabelColor; root.addArrangedSubview(subtitle)
        let configRow = NSStackView(); configRow.orientation = .horizontal; configRow.spacing = 10
        profileLabel.font = .systemFont(ofSize: 13, weight: .medium); profileLabel.lineBreakMode = .byTruncatingMiddle; configRow.addArrangedSubview(profileLabel)
        chooseButton.target = self; chooseButton.action = #selector(chooseProfile(_:)); refreshButton.target = self; refreshButton.action = #selector(useCurrentProfile(_:))
        configRow.addArrangedSubview(chooseButton); configRow.addArrangedSubview(refreshButton); root.addArrangedSubview(configRow)
        configRow.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true; profileLabel.setContentHuggingPriority(.defaultLow, for: .horizontal)
        detailLabel.font = .systemFont(ofSize: 12); detailLabel.textColor = .secondaryLabelColor; detailLabel.maximumNumberOfLines = 4
        root.addArrangedSubview(detailLabel); detailLabel.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        let controls = NSStackView(); controls.orientation = .horizontal; controls.alignment = .centerY; controls.spacing = 8
        controls.addArrangedSubview(label("模式：", size: 13))
        modePopup.addItems(withTitles: ["快速（4 节点并行）", "精确（逐节点）"]); modePopup.target = self; modePopup.action = #selector(modeChanged(_:)); controls.addArrangedSubview(modePopup)
        controls.addArrangedSubview(label("每节点：", size: 13)); sizes.forEach { sizePopup.addItem(withTitle: "\($0) MiB") }; sizePopup.selectItem(at: 0); sizePopup.target = self; sizePopup.action = #selector(sizeChanged(_:)); controls.addArrangedSubview(sizePopup)
        startButton.target = self; startButton.action = #selector(start(_:)); startButton.keyEquivalent = "\r"
        stopButton.target = self; stopButton.action = #selector(stop(_:)); stopButton.isEnabled = false
        copyButton.target = self; copyButton.action = #selector(copyResults(_:)); copyButton.isEnabled = false
        controls.addArrangedSubview(startButton); controls.addArrangedSubview(stopButton); controls.addArrangedSubview(copyButton); root.addArrangedSubview(controls)
        let switchRow = NSStackView(); switchRow.orientation = .horizontal; switchRow.alignment = .centerY; switchRow.spacing = 10
        switchCheck.state = .on; switchRow.addArrangedSubview(switchCheck)
        switchRow.addArrangedSubview(label("策略组：", size: 13))
        groupPopup.addItem(withTitle: "正在查找策略组…"); groupPopup.isEnabled = false; switchRow.addArrangedSubview(groupPopup)
        groupRefreshButton.target = self; groupRefreshButton.action = #selector(refreshGroups(_:)); switchRow.addArrangedSubview(groupRefreshButton)
        root.addArrangedSubview(switchRow)
        let scroll = NSScrollView(); scroll.hasVerticalScroller = true; scroll.hasHorizontalScroller = true; scroll.borderType = .bezelBorder
        table.delegate = self; table.dataSource = self; table.usesAlternatingRowBackgroundColors = true; table.allowsColumnReordering = false
        let columns: [(String, String, CGFloat)] = [("number", "序号", 55), ("name", "节点名称", 350), ("type", "类型", 95), ("latency", "延迟", 90), ("jitter", "抖动", 80), ("failures", "请求失败率", 100), ("speed", "下载速度 ↓", 150)]
        for (identifier, title, width) in columns { let col = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(identifier)); col.title = title; col.width = width; col.minWidth = 45; table.addTableColumn(col) }
        scroll.documentView = table; root.addArrangedSubview(scroll); scroll.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 180).isActive = true; scroll.setContentHuggingPriority(.defaultLow, for: .vertical); scroll.setContentCompressionResistancePriority(.defaultLow, for: .vertical)
        statusLabel.font = .systemFont(ofSize: 12); statusLabel.textColor = .secondaryLabelColor; statusLabel.lineBreakMode = .byTruncatingTail
        root.addArrangedSubview(statusLabel); statusLabel.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
    }
    @objc private func useCurrentProfile(_ sender: Any?) {
        guard !busy, runner == nil else { return }
        guard let info = inspect(["current"]), let path = info["path"] as? String else {
            groupGeneration += 1; resetGroups("请先选择当前订阅")
            profilePath = nil; profileName = ""; profileCount = 0; providerCount = 0; tunEnabled = nil
            profileLabel.stringValue = "未找到当前订阅，请手动选择包含节点的 YAML"; detailLabel.stringValue = "不会上传配置或订阅链接。"; startButton.isEnabled = false; return
        }
        loadProfile(path: path, name: info["name"] as? String ?? URL(fileURLWithPath: path).lastPathComponent)
    }
    @objc private func chooseProfile(_ sender: Any?) {
        guard !busy, runner == nil else { return }
        let panel = NSOpenPanel(); panel.title = "选择包含节点的 Clash 配置"; panel.message = "请选择含 proxies 节点列表的本地 YAML。"; panel.allowsMultipleSelection = false; panel.canChooseDirectories = false; panel.allowedFileTypes = ["yaml", "yml"]
        if panel.runModal() == .OK, let url = panel.url { loadProfile(path: url.path, name: url.lastPathComponent) }
    }
    private func loadProfile(path: String, name: String) {
        guard let info = inspect(["inspect", path]), let count = info["count"] as? Int else {
            groupGeneration += 1; resetGroups("请先选择有效配置")
            profilePath = nil; profileName = ""; profileCount = 0; providerCount = 0; tunEnabled = nil
            profileLabel.stringValue = "配置无法读取，请选择其他 YAML"; detailLabel.stringValue = "配置内容不会被上传。"; startButton.isEnabled = false; return
        }
        profilePath = path; profileName = name; profileCount = count; providerCount = info["providers"] as? Int ?? 0; tunEnabled = info["tun"] as? Bool
        profileLabel.stringValue = "当前配置：\(name)（\(count) 个内联节点）"; updateDetails(); startButton.isEnabled = usableNames(info) != nil
        refreshGroups(nil)
    }
    private func resetGroups(_ text: String) {
        groups = []; groupSnapshot = nil; availableController = nil
        groupPopup.removeAllItems(); groupPopup.addItem(withTitle: text); groupPopup.isEnabled = false
    }
    @objc private func refreshGroups(_ sender: Any?) {
        guard !busy else { return }
        groupGeneration += 1; let generation = groupGeneration
        resetGroups("正在查找策略组…")
        // The release self-test is deliberately offline and never calls a controller.
        if CommandLine.arguments.contains("--self-test") { resetGroups("自检时不连接 Clash"); return }
        guard let path = profilePath, isCurrentProfile(path: path),
              let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let info = inspect(["inspect-stdin"], data: data), let names = usableNames(info) else {
            resetGroups("仅当前订阅支持自动切换"); return
        }
        let candidates = names.filter { !isInformationalNodeName($0) }
        guard !candidates.isEmpty else { resetGroups("没有可用于自动切换的节点"); return }
        guard let identity = localController() else { resetGroups("无法确定本机 Clash 控制接口"); return }
        let client = ClashController(settings: ControllerSettings(socket: identity.path, secret: ""))
        controllerClient = client
        client.eligibleGroups(nodeNames: Set(candidates)) { [weak self] result in
            guard let self = self, self.groupGeneration == generation else { return }
            guard self.localIdentityMatches(identity), self.profilePath == path,
                  isCurrentProfile(path: path, snapshot: data) else { self.resetGroups("当前订阅或 Clash 控制接口已变化，请刷新"); return }
            switch result {
            case .failure:
                self.resetGroups("无法读取 Clash 策略组，请刷新")
            case .success(let groups):
                guard !groups.isEmpty else { self.resetGroups("没有可切换的节点策略组"); return }
                self.groups = groups; self.groupSnapshot = data; self.availableController = identity
                self.groupPopup.removeAllItems(); self.groupPopup.addItem(withTitle: "请选择策略组")
                for group in groups {
                    let matching = names.filter { node in group.all.contains { $0.utf8.elementsEqual(node.utf8) } }.count
                    self.groupPopup.addItem(withTitle: "\(group.name)（\(matching)/\(names.count) 个节点）")
                }
                // Auto-suggest only a unique non-GLOBAL group containing every tested node;
                // the selection remains visible and is explicitly confirmed at start.
                let full = groups.enumerated().filter { _, group in
                    group.name != "GLOBAL" && names.allSatisfy { node in group.all.contains { $0.utf8.elementsEqual(node.utf8) } }
                }
                if full.count == 1 { self.groupPopup.selectItem(at: full[0].offset + 1) }
                else { self.groupPopup.selectItem(at: 0) }
                // A refresh may complete during a measurement. Store the revalidated
                // result but keep its controls disabled until the run finishes.
                self.groupPopup.isEnabled = !self.busy
            }
        }
    }
    private func localIdentityMatches(_ identity: LocalControllerIdentity) -> Bool { localController() == identity }
    @objc private func sizeChanged(_ sender: Any?) { updateDetails() }
    @objc private func modeChanged(_ sender: Any?) { sizePopup.selectItem(at: quickMode ? 0 : 2); updateDetails() }
    private func updateDetails() {
        let size = sizes[max(0, sizePopup.indexOfSelectedItem)]
        let mode = quickMode ? "快速模式最多同时测 4 个节点，结果可能互相影响，适合初筛。" : "精确模式逐个重测全部节点；速度是估算值。"
        let tun = tunEnabled == true ? "检测到配置启用 TUN，建议先手动关闭、测完恢复。" : "若 Clash 开启 TUN，建议先手动关闭、测完恢复。"
        if providerCount > 0 { detailLabel.stringValue = "此配置包含代理集合（proxy-providers），为避免自动联网或写缓存，请选择已展开节点的配置。" }
        else { detailLabel.stringValue = "测速目标：Cloudflare。下载目标约 \(profileCount * size) MiB（\(profileCount) × \(size) MiB）；服务器或代理异常时可能远超估计。\(mode) \(tun)" }
    }
    @objc private func start(_ sender: Any?) {
        guard !busy, runner == nil, let path = profilePath else { return }
        guard let snapshot = try? Data(contentsOf: URL(fileURLWithPath: path)), let info = inspect(["inspect-stdin"], data: snapshot), let names = usableNames(info) else {
            showError("配置无法读取、不含节点、含代理集合或节点名称重复/含控制字符；请选择有效配置。"); return
        }
        profileCount = names.count; providerCount = 0; tunEnabled = info["tun"] as? Bool; updateDetails()
        let candidates = names.filter { !isInformationalNodeName($0) }
        var target: (LocalControllerIdentity, String)?
        if switchCheck.state == .on {
            let index = groupPopup.indexOfSelectedItem - 1
            guard index >= 0, index < groups.count, let identity = availableController else {
                showError("请刷新并选择要自动切换的 Clash 策略组；也可以取消勾选，仅运行测速。"); return
            }
            guard groupSnapshot == snapshot else {
                groupGeneration += 1; resetGroups("订阅已变化，请刷新策略组")
                showError("订阅内容已变化，请刷新策略组后重试。"); return
            }
            guard localIdentityMatches(identity), isCurrentProfile(path: path, snapshot: snapshot) else {
                groupGeneration += 1; resetGroups("当前订阅或控制接口已变化，请刷新")
                showError("当前订阅或 Clash 控制接口已变化，请重新选择当前订阅并刷新策略组。"); return
            }
            target = (identity, groups[index].name)
        }
        let size = sizes[max(0, sizePopup.indexOfSelectedItem)]; let quick = quickMode
        let alert = NSAlert(); alert.alertStyle = .warning; alert.messageText = "开始批量测速？"
        let warning = quick ? "快速模式同时测试 4 个节点，共用本机带宽，测速值可能偏低；超时较短，慢节点可能误判失败。建议用精确模式复测。" : "精确模式逐个测试节点，耗时较长。"
        let switchWarning: String
        if let target = target {
            switchWarning = "\n\n测速全部结束后，将在该策略组的直接节点中，选择下载速度最高的有效候选，并在本机把“\(target.1)”切换到该节点。名称含“剩余流量”或“到期”的信息项仍参与测速，但不作为自动切换候选。若测速被停止、检测到当前订阅或该组当前选择已变化、所选节点不再是直接成员，或无法验证，则不会切换。节点按名称匹配；如果原来的选择是自动方式，切换后会变成固定节点，且仅影响经过该组的流量。检查与切换不是原子操作。"
        } else { switchWarning = "\n\n本次仅测速，不会切换节点。" }
        alert.informativeText = "配置：\(profileName)。将测试 \(names.count) 个节点，每个目标下载 \(size) MiB，合计约 \(names.count * size) MiB。这不是流量硬上限，服务器或代理异常时可能远超估计。\n\n\(warning)\n\n如果旧版助手仍在测速，请先停止旧版，以免重复消耗和互相影响。开启 TUN 时结果也可能受二次代理影响。\(switchWarning)"
        alert.addButton(withTitle: "开始测速"); alert.addButton(withTitle: "取消"); guard alert.runModal() == .alertFirstButtonReturn else { return }
        let token = UUID(); runToken = token; switchPlan = nil
        guard let target = target else { launch(snapshot: snapshot, names: names, size: size, quick: quick); return }
        // Obtain the group's current selection immediately before the test. A later
        // manual change causes a fail-closed skip, subject to the API's non-atomicity.
        setRunning(true); stopButton.isEnabled = false; statusLabel.stringValue = "正在核对 Clash 策略组…"
        let client = ClashController(settings: ControllerSettings(socket: target.0.path, secret: "")); controllerClient = client
        client.group(target.1) { [weak self] result in
            guard let self = self, self.runToken == token else { return }
            guard self.localIdentityMatches(target.0), isCurrentProfile(path: path, snapshot: snapshot),
                  case .success(let group) = result,
                  candidates.contains(where: { node in group.all.contains { $0.utf8.elementsEqual(node.utf8) } }) else {
                self.runToken = nil; self.setRunning(false); self.statusLabel.stringValue = "未开始测速：无法确认策略组或订阅已变化。"; self.showError("无法确认当前策略组或订阅，请刷新后重试。"); return
            }
            self.switchPlan = SwitchPlan(controller: target.0, group: group.name, expectedCurrent: group.now, directMembers: group.all.map { Array($0.utf8) }, profilePath: path, snapshot: snapshot)
            self.launch(snapshot: snapshot, names: names, size: size, quick: quick)
        }
    }
    private func launch(snapshot: Data, names: [String], size: Int, quick: Bool) {
        guard let executable = Bundle.main.url(forResource: "clash-speedtest", withExtension: nil) else { runToken = nil; switchPlan = nil; setRunning(false); showError("测速组件缺失，请重新安装助手。"); return }
        rows.removeAll(); failureCount = 0; table.reloadData(); setRunning(true)
        let batch = BatchRunner(executable: executable, config: snapshot, names: names, size: size * 1024 * 1024,
                                parallel: quick ? 4 : 1, streams: quick ? 2 : 4,
                                latencyTimeout: quick ? "1s" : "2s", downloadTimeout: "3s",
                                onResult: { [weak self] index, name, fields in self?.addResult(index: index, name: name, fields: fields) },
                                onFinish: { [weak self] cancelled, _, _ in self?.finished(cancelled: cancelled) })
        runner = batch; stopButton.isEnabled = true; statusLabel.stringValue = "测速中：正在同时处理最多 \(quick ? 4 : 1) 个节点，可随时停止。"; batch.start()
    }
    private func addResult(index: Int, name: String, fields: [String]?) {
        if let f = fields {
            let measured = Row(number: "\(index + 1).", name: name, type: f[2], latency: f[3], jitter: f[4], failures: f[5], speed: f[6])
            if measured.speedValue > 0 { rows.append(measured) }
            else {
                // The worker can exit 0 with an N/A result or an error in the speed column.
                // Avoid displaying raw errors which may include sensitive connection details.
                failureCount += 1
                rows.append(Row(number: measured.number, name: name, type: f[2], latency: f[3], jitter: f[4], failures: f[5], speed: "无有效速度"))
            }
        } else {
            failureCount += 1
            rows.append(Row(number: "\(index + 1).", name: name, type: "—", latency: "—", jitter: "—", failures: "—", speed: "未完成"))
        }
        rows.sort {
            if $0.speedValue != $1.speedValue { return $0.speedValue > $1.speedValue }
            return (Int($0.number.dropLast()) ?? Int.max) < (Int($1.number.dropLast()) ?? Int.max)
        }
        table.reloadData(); copyButton.isEnabled = !rows.isEmpty
        statusLabel.stringValue = "测速中：已处理 \(rows.count) / \(profileCount) 个节点；按下载速度排序。可随时停止。"
    }
    private func finished(cancelled: Bool) {
        runner = nil
        if cancelled {
            switchPlan = nil; runToken = nil; setRunning(false)
            statusLabel.stringValue = "已停止，保留 \(rows.count) 条已处理结果；未自动切换节点。"; return
        }
        guard let plan = switchPlan, let token = runToken else {
            switchPlan = nil; runToken = nil; setRunning(false)
            statusLabel.stringValue = "测速结束：\(rows.count) 条结果，\(failureCount) 项无有效速度；本次未启用自动切换。"; return
        }
        guard let best = rows.first(where: { row in
            row.speedValue > 0 && !isInformationalNodeName(row.name) &&
            plan.directMembers.contains(where: { $0.elementsEqual(row.name.utf8) })
        }), localIdentityMatches(plan.controller), isCurrentProfile(path: plan.profilePath, snapshot: plan.snapshot) else {
            switchPlan = nil; runToken = nil; setRunning(false)
            statusLabel.stringValue = "测速结束，但没有有效候选速度或订阅/控制接口已变化；未切换节点。"; return
        }
        // Retain the UI lock until switchAndVerify completes; never retry an uncertain PUT.
        stopButton.isEnabled = false; statusLabel.stringValue = "测速结束，正在核对并切换策略组…"
        let client = ClashController(settings: ControllerSettings(socket: plan.controller.path, secret: "")); controllerClient = client
        client.switchAndVerify(group: plan.group, node: best.name, expectedCurrent: plan.expectedCurrent,
                               prePutGuard: {
            isCurrentProfile(path: plan.profilePath, snapshot: plan.snapshot) && localController() == plan.controller
        }) { [weak self] outcome in
            guard let self = self, self.runToken == token else { return }
            self.switchPlan = nil; self.runToken = nil; self.setRunning(false)
            switch outcome {
            case .switched:
                self.statusLabel.stringValue = "测速结束：已将“\(plan.group)”切换到最快候选节点（\(best.speed)）。"
            case .confirmedNoOp:
                self.statusLabel.stringValue = "测速结束：策略组已是最快候选节点（\(best.speed)），无需切换。"
            case .notAttempted:
                self.statusLabel.stringValue = "测速结束，但订阅、控制接口、当前选择或节点成员已变化，或无法验证；未切换。"
            case .outcomeUnknown:
                self.statusLabel.stringValue = "测速结束，但切换结果无法确认；请打开 Clash 检查，助手不会自动重试。"
            }
        }
    }
    private func setRunning(_ running: Bool) {
        busy = running
        startButton.isEnabled = !running && profilePath != nil && profileCount > 0 && providerCount == 0
        stopButton.isEnabled = running && runner != nil
        chooseButton.isEnabled = !running; refreshButton.isEnabled = !running; sizePopup.isEnabled = !running; modePopup.isEnabled = !running
        switchCheck.isEnabled = !running; groupRefreshButton.isEnabled = !running; groupPopup.isEnabled = !running && !groups.isEmpty
        copyButton.isEnabled = !rows.isEmpty
    }
    @objc private func stop(_ sender: Any?) { guard let current = runner else { return }; stopButton.isEnabled = false; statusLabel.stringValue = "正在停止所有测速任务…"; current.cancel() }
    @objc private func copyResults(_ sender: Any?) {
        guard !rows.isEmpty else { return }; let content = (["节点名称\t类型\t延迟\t抖动\t请求失败率\t下载速度"] + rows.map { [$0.name, $0.type, $0.latency, $0.jitter, $0.failures, $0.speed].joined(separator: "\t") }).joined(separator: "\n")
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(content, forType: .string); statusLabel.stringValue = "已复制 \(rows.count) 条结果；结果包含节点名称，请注意隐私。"
    }
    private func showError(_ text: String) { let a = NSAlert(); a.alertStyle = .warning; a.messageText = text; a.runModal() }
    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard row < rows.count, let id = tableColumn?.identifier.rawValue else { return nil }; let item = rows[row]; let value: String
        switch id { case "number": value = item.number; case "name": value = item.name; case "type": value = item.type; case "latency": value = item.latency; case "jitter": value = item.jitter; case "failures": value = item.failures; case "speed": value = item.speed; default: value = "" }
        let cellID = NSUserInterfaceItemIdentifier("cell-" + id); let cell = (tableView.makeView(withIdentifier: cellID, owner: self) as? NSTableCellView) ?? NSTableCellView()
        if cell.textField == nil { let text = NSTextField(labelWithString: ""); text.lineBreakMode = .byTruncatingTail; text.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(text); cell.textField = text; NSLayoutConstraint.activate([text.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4), text.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4), text.centerYAnchor.constraint(equalTo: cell.centerYAnchor)]); cell.identifier = cellID }
        cell.textField?.stringValue = value; return cell
    }
    func selfTest() -> Int32 {
        guard let current = inspect(["current"]), let path = current["path"] as? String,
              let data = try? Data(contentsOf: URL(fileURLWithPath: path)), let info = inspect(["inspect-stdin"], data: data), let names = usableNames(info) else { print("Profile detection: FAILED"); return 1 }
        buildWindow(); useCurrentProfile(nil)
        guard profileCount == names.count, startButton.isEnabled, runner == nil, table.tableColumns.count == 7, quickMode else { print("Window preconditions: FAILED"); return 1 }
        addResult(index: 0, name: "测试甲", fields: ["1.", "测试甲", "Http", "10ms", "1ms", "0%", "10.00MB/s"])
        addResult(index: 1, name: "测试乙", fields: ["1.", "测试乙", "Http", "30ms", "2ms", "0%", "20.00MB/s"])
        guard rows.count == 2, rows[0].name == "测试乙", rows[1].name == "测试甲" else { print("Sorting: FAILED"); return 1 }
        addResult(index: 2, name: "失败样例", fields: ["1.", "失败样例", "Http", "N/A", "N/A", "100%", "N/A"])
        addResult(index: 3, name: "脱敏样例", fields: ["1.", "脱敏样例", "Http", "N/A", "N/A", "0%", "error: sensitive detail"])
        let tb = Row(number: "5.", name: "单位样例", type: "Http", latency: "", jitter: "", failures: "", speed: "1.00TB/s")
        guard failureCount == 2, rows.suffix(2).allSatisfy({ $0.speed == "无有效速度" }), tb.speedValue > rows[0].speedValue else { print("Result classification: FAILED"); return 1 }
        print("Profile detection: OK; inline nodes: \(names.count)")
        print("Window, sorting and invalid-result classification: OK; default parallel workers: 4")
        print("Real-node network requests: none")
        return 0
    }
}

#if DEBUG_LOCAL_TEST
// Only present in explicitly compiled developer test builds; never shipped.
private func localTest() -> Int32 {
    let args = CommandLine.arguments
    guard args.count >= 4, let config = try? Data(contentsOf: URL(fileURLWithPath: args[2])),
          let info = inspect(["inspect-stdin"], data: config), let names = usableNames(info),
          let exe = Bundle.main.url(forResource: "clash-speedtest", withExtension: nil) else { print("LOCAL TEST SETUP FAILED"); return 1 }
    let cancel = args.count > 4 && args[4] == "cancel"
    var completed: [String] = []; var done = false; var wasCancelled = false; var launched = 0; var peak = 0
    let batch = BatchRunner(executable: exe, config: config, names: names, size: 1048576, parallel: 4, streams: 2, latencyTimeout: "2s", downloadTimeout: "5s", serverURL: args[3],
        onResult: { _, name, fields in
            guard let f = fields else { print("Synthetic node \(name.debugDescription): missing/invalid row"); return }
            let sample = Row(number: f[0], name: name, type: f[2], latency: f[3], jitter: f[4], failures: f[5], speed: f[6])
            if sample.speedValue > 0 { completed.append(name) }
            else { print("Synthetic node \(name.debugDescription): \(f[6])") }
        },
        onFinish: { cancelled, n, p in wasCancelled = cancelled; launched = n; peak = p; done = true })
    batch.start()
    if cancel { DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { batch.cancel() } }
    let deadline = Date().addingTimeInterval(25)
    while !done && Date() < deadline { _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05)) }
    guard done, peak > 1, peak <= 4 else { batch.cancel(); print("LOCAL TEST SCHEDULER FAILED"); return 1 }
    if cancel { guard wasCancelled, launched <= 4 else { print("LOCAL TEST CANCEL FAILED"); return 1 } }
    else { guard !wasCancelled, Set(completed) == Set(names) else { print("LOCAL TEST RESULTS FAILED: \(completed.count)/\(names.count)"); return 1 } }
    print("Local scheduler: OK; peak workers: \(peak); launched: \(launched); valid results: \(completed.count); cancelled: \(wasCancelled)")
    return 0
}
#endif

signal(SIGPIPE, SIG_IGN)
let app = NSApplication.shared
let delegate = AppDelegate()
if CommandLine.arguments.contains("--self-test") { exit(delegate.selfTest()) }
#if DEBUG_LOCAL_TEST
if CommandLine.arguments.contains("--local-test") { exit(localTest()) }
#endif
app.delegate = delegate; app.setActivationPolicy(.regular); app.run()
