import AppKit
import Darwin
import Foundation

@main
enum RuntimeBrokerMain {
    private static let officialPath = "/Applications/Clash Verge.app"
    private static let officialID = "io.github.clash-verge-rev.clash-verge-rev"

    static func main() {
        umask(0o077)
        do {
            switch CommandLine.arguments.dropFirst().first {
            case "--status":
                let value = try brokerRequest(path: "/status", method: "GET")
                printJSON(value)
            case "--shutdown":
                _ = try brokerRequest(path: "/status", method: "GET")
                _ = try brokerRequest(path: "/shutdown", method: "POST")
            case "--clash-running":
                printJSON(["running": officialApplication() != nil])
            case "--quit-clash":
                if let application = officialApplication() {
                    guard application.terminate() else { throw failure("Clash Verge 拒绝退出，未修改配置。") }
                    let deadline = Date().addingTimeInterval(15)
                    while officialApplication() != nil && Date() < deadline {
                        RunLoop.current.run(until: Date().addingTimeInterval(0.1))
                    }
                    guard officialApplication() == nil else { throw failure("Clash Verge 尚未退出，未修改配置。") }
                }
            case "--launch-clash":
                try launchOfficialApplication()
            case nil:
                try serve()
            default:
                throw failure("不支持的后台程序参数。")
            }
        } catch {
            FileHandle.standardError.write(Data((error.localizedDescription + "\n").utf8))
            exit(1)
        }
    }

    private static func officialApplication() -> NSRunningApplication? {
        NSWorkspace.shared.runningApplications.first {
            $0.bundleIdentifier == officialID && $0.bundleURL?.standardizedFileURL.path == officialPath
        }
    }

    private static func launchOfficialApplication() throws {
        if officialApplication() != nil { return }
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = false
        var finished = false
        var launchError: Error?
        NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: officialPath), configuration: configuration) { _, error in
            DispatchQueue.main.async { launchError = error; finished = true }
        }
        let deadline = Date().addingTimeInterval(15)
        while !finished && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.1)) }
        if let launchError { throw launchError }
        guard finished else { throw failure("Clash Verge 启动请求超时，请手动打开官方应用。") }
    }

    private static func brokerRequest(path: String, method: String) throws -> [String: Any] {
        guard let descriptor = RuntimeBrokerSupport.readDescriptor() else { throw failure("后台桥接未运行。") }
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(descriptor.port)\(path)")!)
        request.httpMethod = method
        request.timeoutInterval = 3
        request.setValue("Bearer \(descriptor.token)", forHTTPHeaderField: "Authorization")
        let done = DispatchSemaphore(value: 0)
        let lock = NSLock()
        var outcome: Result<[String: Any], Error>?
        let task = URLSession.shared.dataTask(with: request) { data, response, error in
            lock.lock(); defer { lock.unlock(); done.signal() }
            if let error { outcome = .failure(error); return }
            guard let response = response as? HTTPURLResponse, response.statusCode == 200,
                  let data, let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                outcome = .failure(failure("后台桥接拒绝请求，可能仍在处理切换。")); return
            }
            if path == "/status", value["sessionID"] as? String != descriptor.sessionID {
                outcome = .failure(failure("后台桥接会话已变化。")); return
            }
            outcome = .success(value)
        }
        task.resume()
        guard done.wait(timeout: .now() + 4) == .success else {
            task.cancel(); throw failure("后台桥接响应超时。")
        }
        lock.lock(); let result = outcome; lock.unlock()
        return try result!.get()
    }

    private static func serve() throws {
        let root = RuntimeBrokerSupport.root
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        var info = stat()
        guard lstat(root.path, &info) == 0, (info.st_mode & S_IFMT) == S_IFDIR,
              info.st_uid == getuid(), info.st_mode & 0o077 == 0 else {
            throw failure("后台目录必须为当前用户的私有目录。")
        }
        let lockPath = root.appendingPathComponent("broker.lock")
        let lockFD = open(lockPath.path, O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard lockFD >= 0 else { throw failure("无法打开后台单实例锁。") }
        defer { close(lockFD) }
        guard fstat(lockFD, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
              info.st_uid == getuid(), info.st_mode & 0o077 == 0 else { throw failure("后台锁文件权限无效。") }
        guard flock(lockFD, LOCK_EX | LOCK_NB) == 0 else {
            if errno == EWOULDBLOCK { return }
            throw failure("无法获取后台单实例锁。")
        }
        let sessionID = UUID().uuidString
        var bridge: RuntimeBridge?
        let shutdown: () -> Void = {
            DispatchQueue.main.async {
                bridge?.stop()
                if RuntimeBrokerSupport.readDescriptor()?.sessionID == sessionID {
                    try? FileManager.default.removeItem(at: RuntimeBrokerSupport.manifest)
                }
                exit(0)
            }
        }
        bridge = try RuntimeBridge(sessionID: sessionID, shutdown: shutdown)
        let descriptor = RuntimeBrokerDescriptor(protocolVersion: 1, sessionID: sessionID,
                                                 port: bridge!.port, token: bridge!.token, pid: getpid())
        if lstat(RuntimeBrokerSupport.manifest.path, &info) == 0,
           (info.st_mode & S_IFMT) != S_IFREG || info.st_uid != getuid() {
            throw failure("后台状态文件不是当前用户的普通文件。")
        }
        try JSONEncoder().encode(descriptor).write(to: RuntimeBrokerSupport.manifest, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: RuntimeBrokerSupport.manifest.path)
        signal(SIGTERM, SIG_IGN)
        signal(SIGINT, SIG_IGN)
        let terminate = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        let interrupt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        terminate.setEventHandler(handler: shutdown)
        interrupt.setEventHandler(handler: shutdown)
        terminate.resume(); interrupt.resume()
        withExtendedLifetime((bridge, terminate, interrupt)) { dispatchMain() }
    }

    private static func failure(_ message: String) -> NSError {
        NSError(domain: "ClashRuntimeBroker", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }

    private static func printJSON(_ value: [String: Any]) {
        if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
           let text = String(data: data, encoding: .utf8) { print(text) }
    }
}
