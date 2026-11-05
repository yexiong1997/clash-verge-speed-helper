import Foundation
import Dispatch
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

/// Per-node arguments for the source-built verified worker. These are passed as
/// individual argv entries, never through a shell. Nil serverURL uses the same
/// Cloudflare target and per-stream byte calculation as the existing helper.
public struct MultiBatchOptions: Sendable {
    public let downloadBytes: Int
    public let streams: Int
    public let latencyTimeout: String
    public let downloadTimeout: String
    public let serverURL: String?

    public init(downloadBytes: Int, streams: Int, latencyTimeout: String,
                downloadTimeout: String, serverURL: String? = nil) {
        self.downloadBytes = downloadBytes
        self.streams = streams
        self.latencyTimeout = latencyTimeout
        self.downloadTimeout = downloadTimeout
        self.serverURL = serverURL
    }
}

/// profileIdentity is opaque bytes, not a Swift String: canonically equivalent
/// Unicode spellings remain distinct. index is caller-supplied and returned
/// unchanged; callers should assign a distinct index to every submitted job.
/// Both Data values are copied, including Data backed by externally owned memory.
public struct MultiBatchJob: Sendable {
    public let profileIdentity: Data
    public let nodeName: String
    public let yamlSnapshot: Data
    public let index: Int
    public let options: MultiBatchOptions

    public init(profileIdentity: Data, nodeName: String, yamlSnapshot: Data,
                index: Int, options: MultiBatchOptions) {
        self.profileIdentity = profileIdentity.withUnsafeBytes { Data($0) }
        self.nodeName = nodeName
        self.yamlSnapshot = yamlSnapshot.withUnsafeBytes { Data($0) }
        self.index = index
        self.options = options
    }
}

/// Failures intentionally contain no worker output, stderr, exception, or path.
public enum MultiBatchFailure: Equatable, Sendable {
    case invalidJob
    case launchFailed
    case inputFailed
    case workerFailed
    case invalidOutput
}

public enum MultiBatchResult: Equatable, Sendable {
    /// Exactly one UTF-8 JSON line, validated against the verified-worker schema.
    /// A valid incomplete measurement can still be displayed, but is not eligible
    /// for scoring or automatic selection.
    case success([String])
    case failure(MultiBatchFailure)
    case cancelled
}

public struct MultiBatchSummary: Sendable {
    public let cancelled: Bool
    /// Jobs for which a result callback was delivered, including launch errors.
    public let attempted: Int
    /// Successfully spawned processes observed before the batch finished.
    public let launched: Int
    public let peakWorkers: Int
}

/// One batch owns one global 1...10-clamped budget across all profiles.
/// Callers must prevent overlapping batches if they need a shared budget.
/// All callbacks and both pre-launch hooks run on the main thread. They receive
/// only the original job, never worker output; false cancels the batch without
/// attempting that job. Callback reentry is deferred. The async hook must return
/// promptly: callers should do any blocking work off-main and invoke completion
/// on the main thread. Completion is also safe from other threads; only the
/// first call to claim its one-shot gate is used, with no order guaranteed for
/// concurrent calls. A pending check occupies one budget slot.
/// Cancellation discards pending checks without waiting for their completion.
/// The worker watchdog does not cover a pending check; absent cancellation, a
/// check that never calls completion prevents the batch from finishing.
///
/// The monotonic watchdog covers launch, stdin, stdout, and direct-child exit.
/// Timeout is reported as failure(.workerFailed); cancellation as .cancelled.
/// Cancellation or timeout requests SIGTERM, followed by SIGKILL after grace.
/// The runner owns/reaps only the direct worker, not arbitrary descendants. A
/// launch already in the OS when cancellation occurs can complete afterwards;
/// it is then immediately stopped, and no queued launch is scheduled. If the
/// OS fails to complete launch or reap after escalation, callbacks still finish
/// and the background cleanup continues; the remaining queue is cancelled.
/// These bounds require the main queue and OS to be scheduled. Callbacks and the
/// supplied pre-launch hook must themselves return. As with any waitpid-based
/// child owner, no other component may reap this runner's children. On systems
/// without atomic pipe2, unrelated fork/spawn calls must not race pipe creation:
/// FD_CLOEXEC alone cannot close that process-wide descriptor inheritance race.
public final class MultiBatchRunner: @unchecked Sendable {
    public typealias ResultCallback = (MultiBatchJob, MultiBatchResult) -> Void
    public typealias FinishCallback = (MultiBatchSummary) -> Void
    public typealias PreLaunch = (MultiBatchJob) -> Bool
    public typealias AsyncPreLaunch = (MultiBatchJob, @escaping @Sendable (Bool) -> Void) -> Void

    private let executable: URL
    private let jobs: [MultiBatchJob]
    private let limit: Int
    private let watchdog: TimeInterval
    private let terminationGrace: TimeInterval
    private let preLaunch: PreLaunch
    private let asyncPreLaunch: AsyncPreLaunch?
    private let onResult: ResultCallback
    private let onFinish: FinishCallback

    // Only the cancellation flag and the scheduling decision need this lock.
    // No potentially blocking OS call is made while it is held.
    private let cancellationLock = NSLock()
    private var cancellationRequested = false
    // Keep the runner alive through its callbacks, but not through an OS spawn
    // that remains stuck after bounded logical completion.
    private var keepAlive: MultiBatchRunner?
    private var started = false
    private var finished = false
    private var pumping = false
    private var pumpScheduled = false
    private var callbackDepth = 0
    private var next = 0
    private var attempted = 0
    private var launched = 0
    private var peakWorkers = 0
    private struct Active {
        let worker: DirectWorker
        var spawned: Bool
    }
    private var active: [UUID: Active] = [:]
    private var pending: [(UUID, MultiBatchJob, DirectWorker.Completion)] = []
    private struct Preparation {
        let job: MultiBatchJob
        var decision: Bool?
    }
    private final class PreparationGate: @unchecked Sendable {
        private let lock = NSLock()
        private var claimed = false
        func claim() -> Bool {
            lock.lock()
            defer { lock.unlock() }
            guard !claimed else { return false }
            claimed = true
            return true
        }
    }
    // Reservations are keyed independently of the caller's job index. All
    // preparation state, like active and pending, is owned by the main thread.
    private var preparing: [UUID: Preparation] = [:]
    private var pendingPreparations: [UUID] = []

    /// watchdog defaults to 180 seconds and terminationGrace to 2 seconds.
    /// Values must be finite and positive; invalid values use these defaults.
    /// Values over 24 hours are clamped to 24 hours. Short values are intended
    /// for synthetic tests, not production measurements.
    public init(executable: URL, jobs: [MultiBatchJob], parallel: Int,
                watchdog: TimeInterval = 180, terminationGrace: TimeInterval = 2,
                preLaunch: @escaping PreLaunch = { _ in true },
                onResult: @escaping ResultCallback, onFinish: @escaping FinishCallback) {
        self.executable = executable
        self.jobs = jobs
        self.limit = max(1, min(10, parallel))
        self.watchdog = Self.duration(watchdog, defaultValue: 180)
        self.terminationGrace = Self.duration(terminationGrace, defaultValue: 2)
        self.preLaunch = preLaunch
        self.asyncPreLaunch = nil
        self.onResult = onResult
        self.onFinish = onFinish
    }

    /// Alternative to the synchronous preLaunch initializer. The hook is called
    /// on main and should schedule blocking validation elsewhere. Completion may
    /// be called synchronously, more than once, or from any thread. A false
    /// decision cancels the batch without delivering a result for that job.
    public init(executable: URL, jobs: [MultiBatchJob], parallel: Int,
                watchdog: TimeInterval = 180, terminationGrace: TimeInterval = 2,
                asyncPreLaunch: @escaping AsyncPreLaunch,
                onResult: @escaping ResultCallback, onFinish: @escaping FinishCallback) {
        self.executable = executable
        self.jobs = jobs
        self.limit = max(1, min(10, parallel))
        self.watchdog = Self.duration(watchdog, defaultValue: 180)
        self.terminationGrace = Self.duration(terminationGrace, defaultValue: 2)
        self.preLaunch = { _ in true }
        self.asyncPreLaunch = asyncPreLaunch
        self.onResult = onResult
        self.onFinish = onFinish
    }

    private static func duration(_ value: TimeInterval, defaultValue: TimeInterval) -> TimeInterval {
        guard value.isFinite && value > 0 else { return defaultValue }
        return max(0.001, min(value, 86_400))
    }

    public func start() {
        if Thread.isMainThread { startOnMain() }
        else { DispatchQueue.main.async { self.startOnMain() } }
    }

    public func cancel() {
        cancellationLock.lock()
        let firstRequest = !cancellationRequested
        cancellationRequested = true
        cancellationLock.unlock()
        guard firstRequest else { return }
        if Thread.isMainThread { cancelOnMain() }
        else { DispatchQueue.main.async { self.cancelOnMain() } }
    }

    private var isCancellationRequested: Bool {
        cancellationLock.lock()
        defer { cancellationLock.unlock() }
        return cancellationRequested
    }

    private func startOnMain() {
        precondition(Thread.isMainThread)
        guard !started && !finished else { return }
        keepAlive = self
        started = true
        pump()
    }

    private func cancelOnMain() {
        precondition(Thread.isMainThread)
        guard !finished else { return }
        if !started { keepAlive = self }
        started = true
        // A caller's read may never return. Invalidate reservations immediately;
        // late completions hold only a weak reference and cannot launch a worker.
        preparing.removeAll()
        pendingPreparations.removeAll()
        for item in active.values { item.worker.cancel() }
        finishIfDone()
    }

    private func pump() {
        precondition(Thread.isMainThread)
        guard started && !finished && !pumping && callbackDepth == 0 else { return }
        pumping = true
        defer { pumping = false; finishIfDone() }
        var operations = 0
        while true {
            if !pending.isEmpty {
                let (id, job, outcome) = pending.removeFirst()
                deliverWorker(id: id, job: job, outcome: outcome)
                operations += 1
            } else if !pendingPreparations.isEmpty {
                let id = pendingPreparations.removeFirst()
                guard let preparation = preparing.removeValue(forKey: id),
                      let decision = preparation.decision else { continue }
                if !isCancellationRequested {
                    if decision { _ = launchNext(preparation.job, reserved: true) }
                    else { cancel() }
                }
                operations += 1
            } else {
                guard !isCancellationRequested && active.count + preparing.count < limit && next < jobs.count else { break }
                let job = jobs[next]
                if let asyncPreLaunch {
                    guard prepareNext(job, using: asyncPreLaunch) else { break }
                } else {
                    guard preLaunch(job) else { cancel(); break }
                    guard !isCancellationRequested else { break }
                    guard launchNext(job) else { break }
                }
                operations += 1
            }
            // Even thousands of synchronous invalid jobs yield to Stop/events.
            if operations >= 16 {
                if !pumpScheduled {
                    pumpScheduled = true
                    DispatchQueue.main.async {
                        self.pumpScheduled = false
                        self.pump()
                    }
                }
                break
            }
        }
    }

    private func prepareNext(_ job: MultiBatchJob, using hook: AsyncPreLaunch) -> Bool {
        precondition(Thread.isMainThread)
        let id = UUID()
        cancellationLock.lock()
        guard !cancellationRequested else { cancellationLock.unlock(); return false }
        next += 1
        preparing[id] = Preparation(job: job, decision: nil)
        cancellationLock.unlock()
        guard !isCancellationRequested else { return false }
        let gate = PreparationGate()
        hook(job) { [weak self] decision in
            // Drop duplicates before enqueueing, even if they race from several
            // threads. Always defer the winning decision, including on main.
            guard gate.claim() else { return }
            DispatchQueue.main.async { [weak self] in
                self?.preparationFinished(id: id, decision: decision)
            }
        }
        return true
    }

    private func preparationFinished(id: UUID, decision: Bool) {
        precondition(Thread.isMainThread)
        guard !finished && !isCancellationRequested, var preparation = preparing[id],
              preparation.decision == nil else { return }
        preparation.decision = decision
        preparing[id] = preparation
        pendingPreparations.append(id)
        pump()
    }

    private func finishIfDone() {
        precondition(Thread.isMainThread)
        guard started && !finished && !pumping && callbackDepth == 0 && active.isEmpty && pending.isEmpty &&
              preparing.isEmpty && pendingPreparations.isEmpty else { return }
        let cancelled = isCancellationRequested
        guard cancelled || next == jobs.count else { return }
        finished = true
        keepAlive = nil
        onFinish(MultiBatchSummary(cancelled: cancelled, attempted: attempted,
                                   launched: launched, peakWorkers: peakWorkers))
    }

    private func deliver(_ job: MultiBatchJob, _ result: MultiBatchResult) {
        callbackDepth += 1
        onResult(job, result)
        callbackDepth -= 1
    }

    private func launchNext(_ job: MultiBatchJob, reserved: Bool = false) -> Bool {
        precondition(Thread.isMainThread)
        let valid = Self.valid(job: job) && executable.isFileURL
        if !valid {
            cancellationLock.lock()
            guard !cancellationRequested else { cancellationLock.unlock(); return false }
            if !reserved { next += 1 }
            attempted += 1
            cancellationLock.unlock()
            deliver(job, .failure(.invalidJob))
            return true
        }
        let options = job.options
        let target = options.serverURL ??
            "https://speed.cloudflare.com/__down?bytes=\(options.downloadBytes / options.streams)"
        let args = ["-c", "/dev/stdin", "-f", Self.literalRegex(job.nodeName),
                    "-download-size", String(options.downloadBytes),
                    "-concurrent", String(options.streams), "-timeout", options.downloadTimeout,
                    "-max-latency", options.latencyTimeout, "-rename=false", "-server-url", target, "-json-v1"]
        let id = UUID()
        let worker = DirectWorker(executable: executable, arguments: args, job: job,
                                  watchdog: watchdog, grace: terminationGrace,
                                  shouldCancel: { [weak self] in self?.isCancellationRequested ?? true },
                                  onSpawn: { [weak self] in
            DispatchQueue.main.async { [weak self] in self?.workerSpawned(id) }
        }, onComplete: { [weak self] outcome in
            DispatchQueue.main.async { [weak self] in self?.workerFinished(id: id, job: job, outcome: outcome) }
        })
        cancellationLock.lock()
        guard !cancellationRequested else { cancellationLock.unlock(); return false }
        if !reserved { next += 1 }
        attempted += 1
        active[id] = Active(worker: worker, spawned: false)
        cancellationLock.unlock()
        worker.start()
        return true
    }

    private func workerSpawned(_ id: UUID) {
        precondition(Thread.isMainThread)
        guard var item = active[id], !item.spawned else { return }
        item.spawned = true
        active[id] = item
        launched += 1
        peakWorkers = max(peakWorkers, active.values.filter { $0.spawned }.count)
    }

    private func workerFinished(id: UUID, job: MultiBatchJob, outcome: DirectWorker.Completion) {
        precondition(Thread.isMainThread)
        guard active[id] != nil else { return }
        if outcome.abandoned && !isCancellationRequested {
            // An unresolved spawn could still start later. Stop the queue rather
            // than release its reservation and exceed the concurrency budget.
            cancel()
        }
        pending.append((id, job, outcome))
        pump()
    }

    private func deliverWorker(id: UUID, job: MultiBatchJob, outcome: DirectWorker.Completion) {
        guard active.removeValue(forKey: id) != nil else { return }
        let result: MultiBatchResult
        if outcome.abandoned && outcome.result == .failure(.workerFailed) {
            result = outcome.result
        } else if isCancellationRequested {
            result = .cancelled
        } else {
            result = outcome.result
        }
        deliver(job, result)
    }

    private static func valid(job: MultiBatchJob) -> Bool {
        let options = job.options
        guard !job.nodeName.isEmpty,
              job.nodeName.rangeOfCharacter(from: .controlCharacters) == nil,
              options.streams > 0, options.streams <= 128, options.downloadBytes >= options.streams,
              !options.latencyTimeout.isEmpty, !options.downloadTimeout.isEmpty else { return false }
        let args = [options.latencyTimeout, options.downloadTimeout] + (options.serverURL.map { [$0] } ?? [])
        return args.allSatisfy { !$0.utf8.contains(0) } && (options.serverURL?.isEmpty != true)
    }

    private static func literalRegex(_ name: String) -> String {
        let metacharacters = Set("\\.+*?()|[]{}^$".unicodeScalars)
        var expression = "\\A"
        for scalar in name.unicodeScalars {
            if scalar.value > 0x7f {
                // Go/RE2's scalar escape is ASCII-only. Passing raw non-ASCII
                // argv can be canonically coalesced by Foundation when two
                // distinct node names compare equal as Swift Strings.
                expression += "\\x{" + String(scalar.value, radix: 16) + "}"
            } else {
                if metacharacters.contains(scalar) { expression.append("\\") }
                expression.unicodeScalars.append(scalar)
            }
        }
        return expression + "\\z"
    }

    fileprivate static func parseOutput(_ data: Data, job: MultiBatchJob) -> [String]? {
        guard VerifiedMeasurement.decode(data, expectedName: job.nodeName,
                                         requestedBytes: job.options.downloadBytes,
                                         streams: job.options.streams) != nil,
              let text = String(data: data, encoding: .utf8) else { return nil }
        return [text]
    }

 }

#if canImport(Glibc)
// Glibc exposes this GNU extension even on Swift toolchains which omit it from
// the imported module. It performs the chdir in the spawned child only.
@_silgen_name("posix_spawn_file_actions_addchdir_np")
private func spawnAddChdir(_ actions: UnsafeMutablePointer<posix_spawn_file_actions_t>,
                           _ path: UnsafePointer<CChar>) -> Int32
#endif

/// One owner performs all waitpid and signal operations for its direct child.
/// Until waitpid reaps it, the PID cannot be reused. No delayed signal captures
/// a bare PID, and no signal is sent after waitpid reports exit or ECHILD.
private final class DirectWorker: @unchecked Sendable {
    // Older macOS lacks atomic pipe2. Serializing this component's pipe setup
    // and spawn prevents its own workers inheriting one another's descriptors.
    private static let spawnLock = NSLock()
    struct Completion: Sendable {
        let result: MultiBatchResult
        let abandoned: Bool
    }
    private enum StopReason { case cancelled, timedOut }
    private let executable: URL
    private let arguments: [String]
    private let job: MultiBatchJob
    private let deadline: DispatchTime
    private let grace: TimeInterval
    private let shouldCancel: @Sendable () -> Bool
    private let onSpawn: @Sendable () -> Void
    private let onComplete: @Sendable (Completion) -> Void
    private let lock = NSLock()
    private var stop: StopReason?
    private var completed = false

    init(executable: URL, arguments: [String], job: MultiBatchJob,
         watchdog: TimeInterval, grace: TimeInterval,
         shouldCancel: @escaping @Sendable () -> Bool,
         onSpawn: @escaping @Sendable () -> Void,
         onComplete: @escaping @Sendable (Completion) -> Void) {
        self.executable = executable
        self.arguments = arguments
        self.job = job
        self.deadline = .now() + Self.interval(watchdog)
        self.grace = grace
        self.shouldCancel = shouldCancel
        self.onSpawn = onSpawn
        self.onComplete = onComplete
    }

    private static func interval(_ seconds: TimeInterval) -> DispatchTimeInterval {
        .nanoseconds(Int(seconds * 1_000_000_000))
    }

    func start() {
        DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: deadline) { [weak self] in
            self?.requestStop(.timedOut)
        }
        DispatchQueue.global(qos: .userInitiated).async { self.run() }
    }

    func cancel() { requestStop(.cancelled) }

    private func requestStop(_ reason: StopReason) {
        lock.lock()
        guard !completed && stop == nil else { lock.unlock(); return }
        stop = reason
        lock.unlock()
        // This independent fallback also handles a spawn/OS wait that never
        // returns. The operation still owns any eventual child and reaps it.
        DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + Self.interval(grace + 0.5)) { [weak self] in
            self?.complete(.failure(.workerFailed), abandoned: true)
        }
    }

    private func stopReason() -> StopReason? {
        if shouldCancel() { requestStop(.cancelled) }
        if DispatchTime.now() >= deadline { requestStop(.timedOut) }
        lock.lock(); defer { lock.unlock() }
        return stop
    }

    private func complete(_ proposed: MultiBatchResult, abandoned: Bool = false) {
        lock.lock()
        guard !completed else { lock.unlock(); return }
        // Timer execution may be delayed: a result completed after the monotonic
        // deadline cannot win merely because the timer hasn't run yet.
        let result: MultiBatchResult
        if let stop {
            result = stop == .cancelled ? .cancelled : .failure(.workerFailed)
        } else if DispatchTime.now() >= deadline {
            result = .failure(.workerFailed)
        } else {
            result = proposed
        }
        completed = true
        lock.unlock()
        onComplete(Completion(result: result, abandoned: abandoned))
    }

    private static func closeFD(_ fd: inout Int32) {
        if fd >= 0 { _ = close(fd); fd = -1 }
    }

    private static func makePipe() -> [Int32]? {
        var fds: [Int32] = [-1, -1]
#if canImport(Darwin)
        // pipe2 was added to macOS after older supported SDKs. Resolve it at
        // runtime when available, without making it a deployment requirement.
        if let symbol = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "pipe2") {
            typealias AtomicPipe = @convention(c) (UnsafeMutablePointer<Int32>, Int32) -> Int32
            let atomicPipe = unsafeBitCast(symbol, to: AtomicPipe.self)
            guard atomicPipe(&fds, O_CLOEXEC) == 0 else { return nil }
        } else {
            guard pipe(&fds) == 0 else { return nil }
        }
#else
        guard pipe(&fds) == 0 else { return nil }
#endif
        for index in 0..<2 {
            // Spawn actions must not inadvertently close 0, 1, or 2.
            if fds[index] < 3 {
                let duplicate = fcntl(fds[index], F_DUPFD_CLOEXEC, 3)
                _ = close(fds[index])
                fds[index] = duplicate
            } else {
                let flags = fcntl(fds[index], F_GETFD)
                if flags < 0 || fcntl(fds[index], F_SETFD, flags | FD_CLOEXEC) != 0 {
                    _ = close(fds[index]); fds[index] = -1
                }
            }
            if fds[index] < 0 {
                for fd in fds where fd >= 0 { _ = close(fd) }
                return nil
            }
        }
        return fds
    }

    private static func nonblocking(_ fd: Int32) -> Bool {
        let flags = fcntl(fd, F_GETFL)
        return flags >= 0 && fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0
    }

    private static func suppressSIGPIPE(_ fd: Int32) -> Bool {
#if canImport(Darwin)
        return fcntl(fd, F_SETNOSIGPIPE, 1) == 0
#else
        return true
#endif
    }

    private static func writePiece(_ data: Data, offset: Int, fd: Int32) -> Int {
#if canImport(Glibc)
        var mask = sigset_t(), previous = sigset_t(), pending = sigset_t()
        guard sigemptyset(&mask) == 0, sigaddset(&mask, SIGPIPE) == 0,
              pthread_sigmask(SIG_BLOCK, &mask, &previous) == 0 else { return -1 }
        let hadPending = sigpending(&pending) == 0 && sigismember(&pending, SIGPIPE) == 1
#endif
        let n = data.withUnsafeBytes { raw in
            write(fd, raw.baseAddress!.advanced(by: offset), raw.count - offset)
        }
        let error = errno
#if canImport(Glibc)
        if n < 0 && error == EPIPE && !hadPending && sigpending(&pending) == 0 && sigismember(&pending, SIGPIPE) == 1 {
            var received: Int32 = 0
            _ = sigwait(&mask, &received)
        }
        _ = pthread_sigmask(SIG_SETMASK, &previous, nil)
#endif
        errno = error
        return n
    }

    private func run() {
        if stopReason() != nil { complete(.cancelled); return }
        Self.spawnLock.lock()
        var spawnLockHeld = true
        defer { if spawnLockHeld { Self.spawnLock.unlock() } }
        if stopReason() != nil { complete(.cancelled); return }
        guard let inPipe = Self.makePipe() else { complete(.failure(.launchFailed)); return }
        var inputRead = inPipe[0], inputWrite = inPipe[1]
        defer { Self.closeFD(&inputRead); Self.closeFD(&inputWrite) }
        guard let outPipe = Self.makePipe() else { complete(.failure(.launchFailed)); return }
        var outputRead = outPipe[0], outputWrite = outPipe[1]
        defer { Self.closeFD(&outputRead); Self.closeFD(&outputWrite) }
        guard Self.suppressSIGPIPE(inputWrite), Self.nonblocking(inputWrite), Self.nonblocking(outputRead) else {
            complete(.failure(.launchFailed)); return
        }
#if canImport(Darwin)
        var actions: posix_spawn_file_actions_t?
#else
        var actions = posix_spawn_file_actions_t()
#endif
        guard posix_spawn_file_actions_init(&actions) == 0 else { complete(.failure(.launchFailed)); return }
        defer { posix_spawn_file_actions_destroy(&actions) }
        var rc = posix_spawn_file_actions_adddup2(&actions, inputRead, STDIN_FILENO)
        if rc == 0 { rc = posix_spawn_file_actions_adddup2(&actions, outputWrite, STDOUT_FILENO) }
        if rc == 0 { rc = posix_spawn_file_actions_addopen(&actions, STDERR_FILENO, "/dev/null", O_WRONLY, 0) }
        for fd in [inputRead, inputWrite, outputRead, outputWrite] where rc == 0 {
            rc = posix_spawn_file_actions_addclose(&actions, fd)
        }
        if rc == 0 {
            rc = FileManager.default.temporaryDirectory.path.withCString { path in
#if canImport(Darwin)
                posix_spawn_file_actions_addchdir_np(&actions, path)
#else
                spawnAddChdir(&actions, path)
#endif
            }
        }
        guard rc == 0 else { complete(.failure(.launchFailed)); return }
#if canImport(Darwin)
        var attributes: posix_spawnattr_t?
#else
        var attributes = posix_spawnattr_t()
#endif
        guard posix_spawnattr_init(&attributes) == 0 else { complete(.failure(.launchFailed)); return }
        defer { posix_spawnattr_destroy(&attributes) }
        // A dispatch worker may block signals; do not inherit that mask. Also
        // reset dispositions which the embedding application might ignore.
        var mask = sigset_t(), defaults = sigset_t()
        guard sigemptyset(&mask) == 0, sigemptyset(&defaults) == 0,
              sigaddset(&defaults, SIGTERM) == 0, sigaddset(&defaults, SIGPIPE) == 0,
              posix_spawnattr_setsigmask(&attributes, &mask) == 0,
              posix_spawnattr_setsigdefault(&attributes, &defaults) == 0,
              posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF)) == 0 else {
            complete(.failure(.launchFailed)); return
        }
        var argv = ([executable.path] + arguments).map { strdup($0) }
        defer { for entry in argv { free(entry) } }
        guard argv.allSatisfy({ $0 != nil }) else { complete(.failure(.launchFailed)); return }
        argv.append(nil)
        if stopReason() != nil { complete(.cancelled); return }
        var pid: pid_t = 0
        let spawnResult = executable.path.withCString { path in
            argv.withUnsafeMutableBufferPointer { buffer in
                posix_spawn(&pid, path, &actions, &attributes, buffer.baseAddress!, environ)
            }
        }
        if spawnResult != 0 { complete(.failure(.launchFailed)); return }
        Self.closeFD(&inputRead)
        Self.closeFD(&outputWrite)
        spawnLockHeld = false
        Self.spawnLock.unlock()
        onSpawn()

        var offset = 0
        var inputOK = false
        var outputOK = true
        var output = Data()
        var buffer = [UInt8](repeating: 0, count: 16_384)
        var status: Int32 = 0
        var reaped = false
        var waitFailed = false
        var termAt: DispatchTime?
        var sentKill = false
        while true {
            let reason = stopReason()
            // Reap first. A PID is only signalled while still owned/unreaped.
            if !reaped {
                let waited = waitpid(pid, &status, WNOHANG)
                if waited == pid { reaped = true }
                else if waited < 0 && errno != EINTR { reaped = true; waitFailed = true }
            }
            if reason != nil {
                if !reaped {
                    if termAt == nil {
                        _ = kill(pid, SIGTERM)
                        termAt = .now()
                    } else if !sentKill && DispatchTime.now() >= termAt! + Self.interval(grace) {
                        _ = kill(pid, SIGKILL)
                        sentKill = true
                    }
                }
                Self.closeFD(&inputWrite)
                Self.closeFD(&outputRead)
                if reaped { complete(.cancelled); return }
            } else {
                if inputWrite >= 0 {
                    if offset == job.yamlSnapshot.count {
                        inputOK = true; Self.closeFD(&inputWrite)
                    } else {
                        let n = Self.writePiece(job.yamlSnapshot, offset: offset, fd: inputWrite)
                        if n > 0 { offset += n }
                        else if n == 0 || (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
                            Self.closeFD(&inputWrite)
                        }
                    }
                }
                if outputRead >= 0 {
                    let n = buffer.withUnsafeMutableBytes { read(outputRead, $0.baseAddress, $0.count) }
                    if n > 0 {
                        if output.count + n <= 1_048_576 { output.append(contentsOf: buffer[0..<n]) }
                        else { outputOK = false }
                    } else if n == 0 { Self.closeFD(&outputRead) }
                    else if errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR {
                        outputOK = false; Self.closeFD(&outputRead)
                    }
                }
                if reaped && inputWrite < 0 && outputRead < 0 {
                    let result: MultiBatchResult
                    if !inputOK { result = .failure(.inputFailed) }
                    else if waitFailed || (status & 0x7f) != 0 || ((status >> 8) & 0xff) != 0 {
                        result = .failure(.workerFailed)
                    } else if !outputOK { result = .failure(.invalidOutput) }
                    else if let fields = MultiBatchRunner.parseOutput(output, job: job) { result = .success(fields) }
                    else { result = .failure(.invalidOutput) }
                    complete(result)
                    return
                }
            }
            var pollFDs: [pollfd] = []
            if inputWrite >= 0 { pollFDs.append(pollfd(fd: inputWrite, events: Int16(POLLOUT), revents: 0)) }
            if outputRead >= 0 { pollFDs.append(pollfd(fd: outputRead, events: Int16(POLLIN), revents: 0)) }
            _ = poll(&pollFDs, nfds_t(pollFDs.count), 20)
        }
    }
}
