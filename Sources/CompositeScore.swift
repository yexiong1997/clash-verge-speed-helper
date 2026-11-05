import Foundation

/// Computes a transparent composite score from measurements only.
///
/// This namespace is independent of the app, networking, node names, and
/// configuration. It does not perform measurements, select candidates, or
/// break ties; those decisions belong to the caller.
public enum CompositeScore {
    /// Returns a score in `0...100`, or `nil` if a measurement is ineligible.
    ///
    /// Inputs are raw numerical measurements, not formatted display strings:
    ///
    /// - `downloadBytesPerSecond` is the download rate in bytes per second;
    ///   it must be finite and greater than zero. A mebibyte is 1,048,576 bytes.
    /// - `httpLatencyMilliseconds` is HTTP latency in milliseconds; it must be
    ///   finite and greater than zero.
    /// - `httpJitterMilliseconds` is HTTP jitter in milliseconds; it must be
    ///   finite and nonnegative.
    /// - `failurePercent` is a percentage, not a fraction, in the inclusive
    ///   range `0...50`; it must be finite. For example, one failed request
    ///   out of six is `100.0 / 6.0` percent. The caller is responsible for
    ///   calculating this value from six HEAD requests; the scoring function
    ///   does not validate the sample count or restrict percentage increments.
    ///
    /// The fixed, equally reproducible formula is the sum of:
    ///
    /// - Download: `40 * min(1, log1p(MiBps) / log1p(25))`.
    /// - Latency: `30 * clamp(1 - latencyMilliseconds / 500, 0, 1)`.
    /// - Success: `20 * clamp(1 - failurePercent / 100, 0, 1)`.
    /// - Jitter: `10 * clamp(1 - jitterMilliseconds / 100, 0, 1)`.
    ///
    /// When the worker reports jitter as unavailable, the UI may substitute
    /// zero only if at least three HEAD requests succeeded, and must disclose
    /// that the jitter score is an assumption rather than a measurement. This
    /// function cannot distinguish an assumed zero from a measured one.
    /// Invalid inputs are rejected before the formula is evaluated; clamping
    /// the score components does not make invalid measurements eligible.
    public static func score(
        downloadBytesPerSecond: Double,
        httpLatencyMilliseconds: Double,
        httpJitterMilliseconds: Double,
        failurePercent: Double
    ) -> Double? {
        guard downloadBytesPerSecond.isFinite, downloadBytesPerSecond > 0,
              httpLatencyMilliseconds.isFinite, httpLatencyMilliseconds > 0,
              httpJitterMilliseconds.isFinite, httpJitterMilliseconds >= 0,
              failurePercent.isFinite, failurePercent >= 0, failurePercent <= 50
        else { return nil }

        let mebibytesPerSecond = downloadBytesPerSecond / 1_048_576.0
        let download = 40.0 * min(1.0, log1p(mebibytesPerSecond) / log1p(25.0))
        let latency = 30.0 * clamp01(1.0 - httpLatencyMilliseconds / 500.0)
        let success = 20.0 * clamp01(1.0 - failurePercent / 100.0)
        let jitter = 10.0 * clamp01(1.0 - httpJitterMilliseconds / 100.0)
        return download + latency + success + jitter
    }

    private static func clamp01(_ value: Double) -> Double {
        return min(1.0, max(0.0, value))
    }
}
