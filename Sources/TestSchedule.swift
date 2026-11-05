import Foundation

/// An in-memory, deterministic scheduling state machine; it never creates a
/// timer, reads the clock, starts a process, touches a profile or controller,
/// accesses the network, or persists state. A new instance is always disarmed.
///
/// Integration contract:
/// - Serialize ALL access on one owner/queue, including timer callbacks, manual
///   starts, process completions, and disarming. This type is not thread-safe.
/// - Feed all `now` values from the same monotonic clock. Production integrations
///   must use `monotonicNow()` consistently, not `Date()`: changing the wall
///   clock must not cause a premature recurring network run. The uptime epoch
///   is system-boot-relative; never persist or restore an armed schedule.
/// - Before each arm/re-arm, the UI must visibly disclose the interval, estimated
///   per-run data cost, whether auto-switch is enabled, and that the app must
///   remain open. A deliberate primary Enable action authorizes recurring runs;
///   no second confirmation dialog is required. If choices change or consent
///   is revoked, disarm immediately; a fresh Enable action is required to re-arm.
///   This model cannot obtain or verify consent by itself.
/// - Do not restore an armed state at launch. On app close, disarm and cancel
///   the UI timer; do not persist/re-arm. Disarming does not stop an active run.
/// - Reserve *every* manual test with `beginManualRun` and start scheduled tests
///   only when `pollDue` returns `.started`. Reserve and actually start the
///   workflow on the same serialized owner, so disarm cannot race a deferred
///   start; never start a saved token after disarming. Pass `externalBusy: true`
///   whenever a test not reserved through this instance is running. All test
///   starts must follow this contract for the no-overlap guarantee to hold.
/// - Report actual termination (including failure, cancellation, or failure to
///   launch) using the matching token. Keep it reserved through any switching
///   phase as well. Do not replace the instance while a run is active.
///
/// Deadlines are fixed delays rather than a calendar-aligned series. A late
/// poll starts at most one run; the next deadline is the poll time + interval.
/// A due poll while busy skips that occurrence and also sets the next deadline
/// to poll time + interval; nothing is queued. A matched completion resets the
/// deadline to completion time + interval, even if the run outlasted several
/// intervals. Thus an immediately following poll never catches up missed runs.
/// A backward clock movement observed at a poll rebases to now + interval and
/// does not start a run. A forward jump can start at most one run. Unobserved
/// clock movements cannot be detected using wall-clock dates alone.
public final class TestSchedule {
    /// An opaque reservation. Identity equality makes stale/cross-instance
    /// completion events harmless without random values or a wrapping counter.
    public final class RunToken: Equatable {
        fileprivate init() {}
        public static func == (lhs: RunToken, rhs: RunToken) -> Bool { lhs === rhs }
    }

    public enum ArmError: Error, Equatable {
        case invalidInterval
        case invalidDateOrDeadline
    }

    public enum PollResult: Equatable {
        case disarmed
        case notDue
        case clockMovedBackwards
        case skippedBusy
        case started(RunToken)
        /// The date or the next deadline is not representable; schedule disarmed.
        case invalidTime
    }

    public enum FinishResult: Equatable {
        case finished
        /// Token is stale or belongs to another instance; state is unchanged.
        case ignored
        /// The run is finished, but the new deadline is invalid; schedule disarmed.
        case finishedAndDisarmedInvalidTime
    }

    /// Nil means disarmed. No interval or next deadline is persisted.
    public private(set) var interval: TimeInterval?
    public private(set) var nextFire: Date?
    /// Includes manual and scheduled reservations, even after `disarm()`.
    public private(set) var activeRun: RunToken?
    public var isArmed: Bool { interval != nil }
    private var lastObserved: Date?

    /// A monotonic, system-boot-relative sample represented as a Date only so
    /// the deterministic state machine can perform deadline arithmetic. Do not
    /// mix these values with wall-clock dates or persist them across launches.
    public static func monotonicNow() -> Date {
        Date(timeIntervalSinceReferenceDate: ProcessInfo.processInfo.systemUptime)
    }

    public init() {}

    /// Arms only after caller-obtained consent. The first deadline is strictly
    /// after `now`. Invalid values fail closed: any previous schedule is
    /// disarmed and an error is thrown, but an active run remains reserved.
    public func arm(interval: TimeInterval, now: Date) throws {
        guard interval.isFinite, interval > 0 else {
            disarm()
            throw ArmError.invalidInterval
        }
        guard let deadline = Self.deadline(after: now, interval: interval) else {
            disarm()
            throw ArmError.invalidDateOrDeadline
        }
        self.interval = interval
        nextFire = deadline
        lastObserved = now
    }

    /// Prevents future scheduled starts but does not cancel or forget an
    /// in-flight reservation. Timer cancellation is the UI owner's job.
    public func disarm() {
        interval = nil
        nextFire = nil
        lastObserved = nil
    }

    /// Clears the schedule just like disarm; an actual run stays reserved until
    /// its matching completion, even if reset is followed by another arm.
    public func reset() { disarm() }

    /// Reserves the one allowed workflow for a manually confirmed test. Returns
    /// nil if a reservation or other known external test is already active.
    public func beginManualRun(externalBusy: Bool = false) -> RunToken? {
        guard activeRun == nil, !externalBusy else { return nil }
        let token = RunToken()
        activeRun = token
        return token
    }

    /// Processes an explicit wall-clock sample. A started result is a
    /// reservation, not a process launch. `externalBusy` is a conservative
    /// extra guard, not a replacement for reserving every manual test.
    public func pollDue(now: Date, externalBusy: Bool = false) -> PollResult {
        guard let interval = interval, let fire = nextFire else { return .disarmed }
        guard now.timeIntervalSinceReferenceDate.isFinite else {
            disarm()
            return .invalidTime
        }
        if let last = lastObserved, now < last {
            guard let deadline = Self.deadline(after: now, interval: interval) else {
                disarm()
                return .invalidTime
            }
            lastObserved = now
            nextFire = deadline
            return .clockMovedBackwards
        }
        lastObserved = now
        guard now >= fire else { return .notDue }
        guard let deadline = Self.deadline(after: now, interval: interval) else {
            disarm()
            return .invalidTime
        }
        nextFire = deadline
        guard activeRun == nil, !externalBusy else { return .skippedBusy }
        let token = RunToken()
        activeRun = token
        return .started(token)
    }

    /// Completes only the matching reservation. Stale completion does nothing.
    /// If armed, the next fire is reset to one full interval after `now`; an
    /// invalid date/deadline disarms instead. When already disarmed, `now` is
    /// ignored, because there is no deadline to compute.
    @discardableResult
    public func finishRun(_ token: RunToken, now: Date) -> FinishResult {
        guard activeRun == token else { return .ignored }
        activeRun = nil
        guard let interval = interval else { return .finished }
        guard let deadline = Self.deadline(after: now, interval: interval) else {
            disarm()
            return .finishedAndDisarmedInvalidTime
        }
        nextFire = deadline
        lastObserved = now
        return .finished
    }

    /// Reject non-finite or non-advancing floating-point arithmetic, rather
    /// than creating a deadline that could immediately fire in a tight loop.
    private static func deadline(after now: Date, interval: TimeInterval) -> Date? {
        let base = now.timeIntervalSinceReferenceDate
        let value = base + interval
        guard base.isFinite, value.isFinite, value > base else { return nil }
        return Date(timeIntervalSinceReferenceDate: value)
    }
}
