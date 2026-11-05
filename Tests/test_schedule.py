#!/usr/bin/env python3
"""Compile/run a pure Swift scheduling harness in a temporary directory.

State-machine dates are synthetic; a clock check samples system uptime only.
No network, controller, profile, timers, or app is used.
Run: python3 -B Tests/test_schedule.py
"""
import os
from pathlib import Path
import subprocess
import tempfile


PROJECT = Path(__file__).resolve().parent.parent
SOURCE = PROJECT / "Sources" / "TestSchedule.swift"

SWIFT = r'''
import Foundation

@main
@MainActor
struct ScheduleTests {
    static var checks = 0

    static func check(_ condition: @autoclosure () -> Bool, _ message: String,
                      file: StaticString = #fileID, line: UInt = #line) {
        checks += 1
        guard condition() else { fatalError("\(file):\(line): \(message)") }
    }

    static func d(_ seconds: Double) -> Date {
        Date(timeIntervalSinceReferenceDate: seconds)
    }

    static func started(_ result: TestSchedule.PollResult,
                        file: StaticString = #fileID, line: UInt = #line) -> TestSchedule.RunToken {
        checks += 1
        guard case .started(let token) = result else {
            fatalError("\(file):\(line): expected a scheduled start, got \(result)")
        }
        return token
    }

    static func manual(_ schedule: TestSchedule,
                       file: StaticString = #fileID, line: UInt = #line) -> TestSchedule.RunToken {
        checks += 1
        guard let token = schedule.beginManualRun() else {
            fatalError("\(file):\(line): expected a manual reservation")
        }
        return token
    }

    static func expectArmError(_ schedule: TestSchedule, interval: Double, now: Date,
                               _ expected: TestSchedule.ArmError) throws {
        do {
            try schedule.arm(interval: interval, now: now)
            fatalError("invalid schedule unexpectedly armed")
        } catch let error as TestSchedule.ArmError {
            check(error == expected, "wrong arm error")
        }
        check(!schedule.isArmed && schedule.interval == nil && schedule.nextFire == nil,
              "invalid arm must disarm")
    }

    static func defaultsAndLifecycle() throws {
        let s = TestSchedule()
        check(!s.isArmed && s.interval == nil && s.nextFire == nil && s.activeRun == nil,
              "fresh state must be disarmed")
        check(s.pollDue(now: d(1_000_000_000)) == .disarmed, "must not arm on startup")
        try s.arm(interval: 60, now: d(1000))
        check(s.isArmed && s.interval == 60 && s.nextFire == d(1060), "first deadline")
        check(s.pollDue(now: d(1059)) == .notDue, "do not start early")
        let first = started(s.pollDue(now: d(1060)))
        check(s.activeRun == first && s.nextFire == d(1120), "reserve and set next deadline")
        check(s.beginManualRun() == nil, "manual run cannot overlap scheduled run")
        check(s.pollDue(now: d(1060)) == .notDue, "same instant is not due twice")
        check(s.pollDue(now: d(1120)) == .skippedBusy, "busy occurrence skipped")
        check(s.nextFire == d(1180), "busy occurrence rebased")
        check(s.pollDue(now: d(1120)) == .notDue, "skip cannot be queued")
        check(s.pollDue(now: d(10_000)) == .skippedBusy, "large busy jump skips once")
        check(s.nextFire == d(10_060), "large busy jump rebases from observation")
        check(s.finishRun(first, now: d(10_001)) == .finished, "finish matching run")
        check(s.activeRun == nil && s.nextFire == d(10_061), "completion rebases")
        check(s.pollDue(now: d(10_001)) == .notDue, "no completion backlog")
        let second = started(s.pollDue(now: d(10_061)))
        check(first != second, "tokens differ across runs")
        let deadline = s.nextFire
        check(s.finishRun(first, now: d(10_062)) == .ignored, "duplicate completion ignored")
        check(s.activeRun == second && s.nextFire == deadline, "stale completion cannot unlock")
        check(s.finishRun(second, now: d(10_063)) == .finished, "finish new run")
        s.disarm()
        check(s.pollDue(now: d(1e9)) == .disarmed, "disarm blocks all later polls")
        let relaunch = TestSchedule()
        check(relaunch.pollDue(now: d(1e9)) == .disarmed && relaunch.nextFire == nil,
              "new instance never restores a schedule")
    }

    static func manualAndExternalBusy() throws {
        let s = TestSchedule()
        try s.arm(interval: 60, now: d(0))
        check(s.beginManualRun(externalBusy: true) == nil, "external busy blocks manual reservation")
        let first = manual(s)
        check(s.beginManualRun() == nil, "manual runs cannot overlap")
        check(s.pollDue(now: d(59)) == .notDue, "manual process does not advance early deadline")
        check(s.pollDue(now: d(60)) == .skippedBusy && s.nextFire == d(120),
              "due manual run skips and rebases")
        check(s.finishRun(first, now: d(61)) == .finished && s.nextFire == d(121),
              "manual completion postpones by a full interval")
        check(s.pollDue(now: d(120)) == .notDue, "completion, not skipped deadline, determines next")
        let second = started(s.pollDue(now: d(121)))
        check(s.finishRun(second, now: d(122)) == .finished, "scheduled finish")
        check(s.pollDue(now: d(182), externalBusy: true) == .skippedBusy,
              "known untracked activity blocks due start")
        check(s.nextFire == d(242) && s.activeRun == nil, "external skip does not reserve or queue")
        check(s.pollDue(now: d(182)) == .notDue, "external busy does not queue")
        let third = started(s.pollDue(now: d(242)))
        check(s.finishRun(third, now: d(243)) == .finished, "finish after external busy")

        // No timer polls at all while this manual run spans many intervals.
        let noPoll = manual(s)
        check(s.finishRun(noPoll, now: d(5_000)) == .finished && s.nextFire == d(5_060),
              "completion still discards missed deadlines without intervening polls")
        check(s.pollDue(now: d(5_000)) == .notDue, "no immediately queued run")
    }

    static func disarmResetAndStaleCompletion() throws {
        let s = TestSchedule()
        try s.arm(interval: 5, now: d(0))
        let first = started(s.pollDue(now: d(5)))
        s.disarm()
        check(!s.isArmed && s.activeRun == first, "disarm preserves actual run")
        check(s.pollDue(now: d(500)) == .disarmed && s.beginManualRun() == nil,
              "disarm cannot allow an overlapping manual run")
        try s.arm(interval: 10, now: d(500))
        check(s.pollDue(now: d(510)) == .skippedBusy, "rearm preserves previous active run")
        s.reset()
        check(!s.isArmed && s.activeRun == first, "reset cannot forget a running process")
        check(s.finishRun(first, now: d(.nan)) == .finished, "disarmed finish needs no date")
        check(s.activeRun == nil && s.pollDue(now: d(1000)) == .disarmed, "reset stays disarmed")
        let second = manual(s)
        try s.arm(interval: 10, now: d(1000))
        let next = s.nextFire
        check(s.finishRun(first, now: d(.nan)) == .ignored, "stale invalid completion ignored first")
        check(s.activeRun == second && s.nextFire == next, "stale completion leaves all state intact")

        let other = TestSchedule()
        let foreign = manual(other)
        check(foreign != second, "cross-instance tokens differ")
        check(s.finishRun(foreign, now: d(1001)) == .ignored, "foreign completion ignored")
        check(s.activeRun == second && s.nextFire == next, "foreign token cannot release reservation")
        check(other.finishRun(foreign, now: d(1)) == .finished, "foreign owner can finish")
        check(s.finishRun(second, now: d(1002)) == .finished && s.nextFire == d(1012),
              "own completion rebases")
    }

    static func clockJumps() throws {
        let s = TestSchedule()
        try s.arm(interval: 10, now: d(100))
        check(s.pollDue(now: d(106)) == .notDue, "baseline sample")
        check(s.pollDue(now: d(95)) == .clockMovedBackwards && s.nextFire == d(105),
              "backward observation rebases without firing")
        check(s.pollDue(now: d(104)) == .notDue, "backward rebase not early")
        let one = started(s.pollDue(now: d(105)))
        check(s.finishRun(one, now: d(90)) == .finished && s.nextFire == d(100),
              "backward completion also rebases")
        check(s.pollDue(now: d(89)) == .clockMovedBackwards && s.nextFire == d(99),
              "repeated backward movement rebases again")
        let two = started(s.pollDue(now: d(1_000_000)))
        check(s.nextFire == d(1_000_010), "forward jump starts at most one and rebases")
        check(s.pollDue(now: d(1_000_000)) == .notDue, "no repeated catch-up")
        check(s.pollDue(now: d(1_000_030)) == .skippedBusy && s.nextFire == d(1_000_040),
              "another large jump while busy skips")
        check(s.finishRun(two, now: d(1_000_031)) == .finished && s.nextFire == d(1_000_041),
              "completion after jump sets full delay")
        check(s.pollDue(now: d(1_000_031)) == .notDue, "no post-wake queue")
    }

    static func invalidInput() throws {
        let s = TestSchedule()
        for invalid: Double in [0, -1, .nan, .infinity, -.infinity] {
            try s.arm(interval: 1, now: d(0))
            try expectArmError(s, interval: invalid, now: d(0), .invalidInterval)
        }
        for invalidDate: Double in [.nan, .infinity, -.infinity] {
            try s.arm(interval: 1, now: d(0))
            try expectArmError(s, interval: 1, now: d(invalidDate), .invalidDateOrDeadline)
        }
        try s.arm(interval: 1, now: d(0))
        try expectArmError(s, interval: .leastNonzeroMagnitude, now: d(1_000_000), .invalidDateOrDeadline)
        try s.arm(interval: 1, now: d(0))
        try expectArmError(s, interval: .greatestFiniteMagnitude, now: d(.greatestFiniteMagnitude),
                           .invalidDateOrDeadline)

        try s.arm(interval: 10, now: d(0))
        let token = manual(s)
        try expectArmError(s, interval: -1, now: d(0), .invalidInterval)
        check(s.activeRun == token, "failed rearm does not forget active process")
        check(s.finishRun(token, now: d(1)) == .finished, "finish after failed rearm")

        try s.arm(interval: 10, now: d(0))
        let another = manual(s)
        check(s.pollDue(now: d(.nan)) == .invalidTime && !s.isArmed && s.activeRun == another,
              "invalid poll fails closed without forgetting active run")
        check(s.finishRun(another, now: d(1)) == .finished, "finish after invalid poll")
        try s.arm(interval: 10, now: d(0))
        check(s.pollDue(now: d(.greatestFiniteMagnitude)) == .invalidTime && !s.isArmed,
              "unrepresentable next deadline fails closed before start")
        try s.arm(interval: 10, now: d(0))
        let last = manual(s)
        check(s.finishRun(last, now: d(.infinity)) == .finishedAndDisarmedInvalidTime,
              "invalid completion is reported")
        check(!s.isArmed && s.activeRun == nil && s.nextFire == nil,
              "invalid completion releases process and disarms")
    }

    // Deterministic stress, not a source of production randomness: explore
    // interleaved arm/disarm, manual and scheduled starts, stale completions,
    // external busy and forward/backward wall-clock samples.
    static func interleavings() throws {
        let s = TestSchedule()
        var seed: UInt64 = 0x1234_5678_9abc_def0
        var oldToken: TestSchedule.RunToken?
        func sample() -> UInt64 {
            seed = seed &* 6_364_136_223_846_793_005 &+ 1_442_695_040_888_963_407
            return seed
        }
        for _ in 0..<5_000 {
            let operation = sample() % 7
            let seconds = Double(Int(sample() % 2_001) - 1_000)
            let now = d(seconds)
            let busy = (sample() & 1) != 0
            let before = s.activeRun
            switch operation {
            case 0:
                try s.arm(interval: 10, now: now)
                check(s.nextFire == d(seconds + 10) && s.activeRun == before, "arm invariant")
            case 1:
                s.disarm()
                check(s.activeRun == before, "disarm reservation invariant")
            case 2:
                s.reset()
                check(s.activeRun == before, "reset reservation invariant")
            case 3:
                let token = s.beginManualRun(externalBusy: busy)
                check((token != nil) == (before == nil && !busy), "manual exclusivity")
                if let token = token { check(s.activeRun == token, "manual token installed") }
            case 4, 5:
                let armed = s.isArmed
                let result = s.pollDue(now: now, externalBusy: busy)
                switch result {
                case .started(let token):
                    check(armed && before == nil && !busy, "scheduled exclusivity")
                    check(s.activeRun == token && s.nextFire == d(seconds + 10), "scheduled rebase")
                case .skippedBusy:
                    check(armed && (before != nil || busy), "skip requires busy")
                    check(s.activeRun == before && s.nextFire == d(seconds + 10), "skip rebase")
                case .clockMovedBackwards:
                    check(armed && s.activeRun == before && s.nextFire == d(seconds + 10), "backward rebase")
                case .notDue:
                    check(armed && s.activeRun == before, "waiting preserves reservation")
                case .disarmed:
                    check(!armed && s.activeRun == before, "disarmed poll preserves reservation")
                case .invalidTime:
                    fatalError("finite stress dates are representable")
                }
            default:
                if let active = before {
                    check(s.finishRun(active, now: now) == .finished, "matching completion")
                    oldToken = active
                    check(s.activeRun == nil, "completion clears reservation")
                    if s.isArmed { check(s.nextFire == d(seconds + 10), "completion rebase") }
                } else if let old = oldToken {
                    let deadline = s.nextFire
                    check(s.finishRun(old, now: now) == .ignored, "stale completion ignored")
                    check(s.activeRun == nil && s.nextFire == deadline, "stale completion no mutation")
                }
            }
            check(s.isArmed == (s.interval != nil), "interval armed invariant")
            check(s.isArmed == (s.nextFire != nil), "deadline armed invariant")
        }
    }

    static func productionClockUsesUptime() {
        let before = ProcessInfo.processInfo.systemUptime
        let first = TestSchedule.monotonicNow().timeIntervalSinceReferenceDate
        let second = TestSchedule.monotonicNow().timeIntervalSinceReferenceDate
        let after = ProcessInfo.processInfo.systemUptime
        check(first >= before - 0.01 && second <= after + 0.01,
              "production scheduling clock must use system uptime, not wall time")
        check(second >= first, "production clock samples must not move backward")
    }

    static func main() throws {
        productionClockUsesUptime()
        try defaultsAndLifecycle()
        try manualAndExternalBusy()
        try disarmResetAndStaleCompletion()
        try clockJumps()
        try invalidInput()
        try interleavings()
        print("Schedule state-machine tests passed (\(checks) checks, 5000 deterministic interleavings).")
    }
}
'''


def main():
    with tempfile.TemporaryDirectory(prefix="clash-schedule-test-") as directory:
        root = Path(directory)
        harness = root / "ScheduleTests.swift"
        binary = root / "schedule-tests"
        cache = root / "module-cache"
        home = root / "home"
        cache.mkdir()
        home.mkdir()
        harness.write_text(SWIFT, encoding="utf-8")
        env = os.environ.copy()
        env.update(HOME=str(home), TMPDIR=str(root), CLANG_MODULE_CACHE_PATH=str(cache),
                   SWIFT_MODULECACHE_PATH=str(cache))
        subprocess.run(["swiftc", "-swift-version", "5", "-warnings-as-errors", "-module-cache-path", str(cache),
                        str(SOURCE), str(harness), "-o", str(binary)],
                       check=True, env=env, timeout=120)
        subprocess.run([str(binary)], check=True, env=env, timeout=30)


if __name__ == "__main__":
    main()
