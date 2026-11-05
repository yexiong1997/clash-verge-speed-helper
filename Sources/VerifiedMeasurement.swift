import Foundation

/// A validated, typed measurement from exactly one verified-worker v1 JSON line.
///
/// This is a pure decoder: it does not run a worker, read a profile, or access a
/// controller. The caller must still check the worker's exit status and bound
/// its output while reading it. The maximum accepted `raw` size is 1,048,576
/// bytes, matching MultiBatchRunner's current stdout retention limit.
public struct VerifiedMeasurement: Sendable {
    public let nodeName: String
    public let nodeType: String
    public let headAttempts: Int
    public let headSuccesses: Int
    public let latencyMilliseconds: Double
    public let jitterMilliseconds: Double
    public let expectedBytes: Int
    public let receivedBytes: Int
    public let downloadComplete: Bool
    public let downloadBytesPerSecond: Double

    /// Decodes the worker's v1 object, or returns nil for any contract violation.
    ///
    /// `expectedName` is compared as exact UTF-8, not Swift's canonically
    /// equivalent String equality. `requestedBytes` and `streams` must be the
    /// arguments actually sent to the worker; the expected sum is the integer
    /// quotient times the stream count. The sum and its one-extra-byte-per-
    /// stream upper bound must fit in Int. Only a single object on one line is
    /// accepted, with an optional final LF or CRLF. Duplicate or unknown keys,
    /// non-JSON syntax, numeric type confusion, and trailing output are rejected.
    /// A valid incomplete result retains its measured counts but has zero speed.
    public static func decode(_ raw: Data, expectedName: String,
                              requestedBytes: Int, streams: Int) -> VerifiedMeasurement? {
        guard requestedBytes > 0, streams > 0 else { return nil }
        let perStream = requestedBytes / streams
        let (expected, productOverflow) = perStream.multipliedReportingOverflow(by: streams)
        guard !productOverflow, expected > 0 else { return nil }
        let (maximumReceived, sumOverflow) = expected.addingReportingOverflow(streams)
        guard !sumOverflow else { return nil }

        guard var parser = WorkerJSONParser(raw), let fields = parser.object(),
              fields.count == WorkerJSONParser.fieldNames.count,
              fields["version"]?.integer == 1,
              let nodeName = fields["node_name"]?.string,
              nodeName.utf8.elementsEqual(expectedName.utf8),
              let nodeType = fields["node_type"]?.string, !nodeType.isEmpty,
              nodeType.unicodeScalars.allSatisfy({
                  !CharacterSet.controlCharacters.contains($0) &&
                  !CharacterSet.newlines.contains($0)
              }),
              let headAttempts = fields["head_attempts"]?.integer, headAttempts == 6,
              let headSuccesses = fields["head_successes"]?.integer,
              (0...6).contains(headSuccesses),
              let latency = fields["latency_ms"]?.nonnegativeDouble,
              let jitter = fields["jitter_ms"]?.nonnegativeDouble,
              let expectedBytes = fields["expected_bytes"]?.integer,
              expectedBytes == expected,
              let receivedBytes = fields["received_bytes"]?.integer,
              receivedBytes >= 0, receivedBytes <= maximumReceived,
              let complete = fields["download_complete"]?.bool,
              let speed = fields["download_bytes_per_second"]?.nonnegativeDouble
        else { return nil }

        if complete {
            guard receivedBytes == expectedBytes, speed > 0 else { return nil }
        } else {
            guard speed == 0 else { return nil }
        }

        return VerifiedMeasurement(nodeName: nodeName, nodeType: nodeType,
                                   headAttempts: headAttempts, headSuccesses: headSuccesses,
                                   latencyMilliseconds: latency, jitterMilliseconds: jitter,
                                   expectedBytes: expectedBytes, receivedBytes: receivedBytes,
                                   downloadComplete: complete, downloadBytesPerSecond: speed)
    }

    /// CompositeScore is available only for a complete, exactly sized download
    /// with a positive finite rate and at least three successes out of six HEADs.
    /// The scorer may additionally return nil, e.g. for zero HTTP latency.
    public var score: Double? {
        guard downloadComplete, expectedBytes == receivedBytes,
              downloadBytesPerSecond.isFinite, downloadBytesPerSecond > 0,
              headAttempts == 6, (3...6).contains(headSuccesses) else { return nil }
        return CompositeScore.score(downloadBytesPerSecond: downloadBytesPerSecond,
                                    httpLatencyMilliseconds: latencyMilliseconds,
                                    httpJitterMilliseconds: jitterMilliseconds,
                                    failurePercent: Double(6 - headSuccesses) * 100.0 / 6.0)
    }
}

/// A deliberately small JSON parser for the flat v1 schema. Foundation's usual
/// dictionary decoders discard duplicate keys and can bridge JSON booleans as
/// numbers, so neither can enforce this boundary by itself. Number spellings
/// are retained to distinguish integer tokens from decimal/exponent tokens.
private struct WorkerJSONParser {
    static let fieldNames: Set<String> = [
        "version", "node_name", "node_type", "head_attempts", "head_successes",
        "latency_ms", "jitter_ms", "expected_bytes", "received_bytes",
        "download_complete", "download_bytes_per_second"
    ]
    private static let maximumOutputBytes = 1_048_576

    private var bytes: [UInt8]
    private var index = 0

    init?(_ data: Data) {
        guard !data.isEmpty, data.count <= Self.maximumOutputBytes else { return nil }
        bytes = Array(data)
        if bytes.last == 0x0a {
            bytes.removeLast()
            if bytes.last == 0x0d { bytes.removeLast() }
        }
        // The protocol is one JSON line, not a pretty-printed or multi-line
        // document. Escaped newlines inside strings remain ordinary bytes here.
        guard !bytes.contains(where: { $0 == 0x0a || $0 == 0x0d }) else { return nil }
    }

    mutating func object() -> [String: WorkerJSONValue]? {
        whitespace()
        guard consume(0x7b) else { return nil } // {
        whitespace()
        var result: [String: WorkerJSONValue] = [:]
        if consume(0x7d) { return nil } // An empty object cannot satisfy v1.
        while true {
            guard let key = string(), Self.fieldNames.contains(key), result[key] == nil else {
                return nil
            }
            whitespace()
            guard consume(0x3a) else { return nil } // :
            whitespace()
            guard let value = value() else { return nil }
            result[key] = value
            whitespace()
            if consume(0x7d) { break } // }
            guard consume(0x2c) else { return nil } // ,
            whitespace()
        }
        whitespace()
        return index == bytes.count ? result : nil
    }

    private mutating func whitespace() {
        while index < bytes.count && (bytes[index] == 0x20 || bytes[index] == 0x09) {
            index += 1
        }
    }

    private mutating func consume(_ byte: UInt8) -> Bool {
        guard index < bytes.count, bytes[index] == byte else { return false }
        index += 1
        return true
    }

    private mutating func value() -> WorkerJSONValue? {
        guard index < bytes.count else { return nil }
        switch bytes[index] {
        case 0x22:
            guard let value = string() else { return nil }
            return .string(value)
        case 0x74:
            guard literal([0x74, 0x72, 0x75, 0x65]) else { return nil }
            return .bool(true)
        case 0x66:
            guard literal([0x66, 0x61, 0x6c, 0x73, 0x65]) else { return nil }
            return .bool(false)
        case 0x2d, 0x30...0x39:
            guard let value = number() else { return nil }
            return .number(value)
        default:
            return nil
        }
    }

    private mutating func literal(_ literal: [UInt8]) -> Bool {
        guard bytes.count - index >= literal.count,
              bytes[index..<(index + literal.count)].elementsEqual(literal) else { return false }
        index += literal.count
        return true
    }

    private mutating func number() -> String? {
        let start = index
        _ = consume(0x2d) // optional minus
        guard index < bytes.count else { return nil }
        if consume(0x30) {
            // A leading zero cannot be followed by another integer digit.
            if index < bytes.count && (0x30...0x39).contains(bytes[index]) { return nil }
        } else {
            guard index < bytes.count, (0x31...0x39).contains(bytes[index]) else { return nil }
            index += 1
            while index < bytes.count && (0x30...0x39).contains(bytes[index]) { index += 1 }
        }
        if consume(0x2e) { // decimal point must have at least one digit
            let first = index
            while index < bytes.count && (0x30...0x39).contains(bytes[index]) { index += 1 }
            guard index > first else { return nil }
        }
        if index < bytes.count && (bytes[index] == 0x65 || bytes[index] == 0x45) {
            index += 1
            if index < bytes.count && (bytes[index] == 0x2b || bytes[index] == 0x2d) { index += 1 }
            let first = index
            while index < bytes.count && (0x30...0x39).contains(bytes[index]) { index += 1 }
            guard index > first else { return nil }
        }
        return String(decoding: bytes[start..<index], as: UTF8.self)
    }

    private mutating func string() -> String? {
        guard consume(0x22) else { return nil }
        var decoded: [UInt8] = []
        while index < bytes.count {
            let byte = bytes[index]
            index += 1
            // Foundation's string decoding can consume a leading U+FEFF as a
            // BOM. Swift decoding preserves it; a byte-for-byte round trip also
            // rejects malformed UTF-8 that decoding would otherwise repair.
            if byte == 0x22 {
                let text = String(decoding: decoded, as: UTF8.self)
                return Array(text.utf8) == decoded ? text : nil
            }
            guard byte >= 0x20 else { return nil }
            if byte != 0x5c { decoded.append(byte); continue }
            guard index < bytes.count else { return nil }
            let escaped = bytes[index]
            index += 1
            switch escaped {
            case 0x22, 0x5c, 0x2f: decoded.append(escaped)
            case 0x62: decoded.append(0x08)
            case 0x66: decoded.append(0x0c)
            case 0x6e: decoded.append(0x0a)
            case 0x72: decoded.append(0x0d)
            case 0x74: decoded.append(0x09)
            case 0x75:
                guard var scalar = hexQuad() else { return nil }
                if (0xd800...0xdbff).contains(scalar) {
                    guard consume(0x5c), consume(0x75), let low = hexQuad(),
                          (0xdc00...0xdfff).contains(low) else { return nil }
                    scalar = 0x10000 + ((scalar - 0xd800) << 10) + (low - 0xdc00)
                } else if (0xdc00...0xdfff).contains(scalar) {
                    return nil
                }
                guard let unicode = UnicodeScalar(scalar) else { return nil }
                decoded.append(contentsOf: String(unicode).utf8)
            default: return nil
            }
        }
        return nil
    }

    private mutating func hexQuad() -> UInt32? {
        guard bytes.count - index >= 4 else { return nil }
        var result: UInt32 = 0
        for _ in 0..<4 {
            let byte = bytes[index]
            index += 1
            let digit: UInt32
            switch byte {
            case 0x30...0x39: digit = UInt32(byte - 0x30)
            case 0x41...0x46: digit = UInt32(byte - 0x41 + 10)
            case 0x61...0x66: digit = UInt32(byte - 0x61 + 10)
            default: return nil
            }
            result = (result << 4) | digit
        }
        return result
    }
}

private enum WorkerJSONValue {
    case string(String)
    case number(String)
    case bool(Bool)

    var string: String? {
        if case let .string(value) = self { return value }
        return nil
    }

    var bool: Bool? {
        if case let .bool(value) = self { return value }
        return nil
    }

    var integer: Int? {
        guard case let .number(value) = self,
              !value.contains("."), !value.contains("e"), !value.contains("E") else { return nil }
        return Int(value)
    }

    var nonnegativeDouble: Double? {
        guard case let .number(value) = self, let number = Double(value),
              number.isFinite, number >= 0 else { return nil }
        // A nonzero JSON number which underflows to Double zero must not turn
        // into an allowed zero-speed incomplete measurement (or hide a negative).
        if number == 0 {
            let mantissa = value.prefix { $0 != "e" && $0 != "E" }
            guard !mantissa.utf8.contains(where: { (0x31...0x39).contains($0) }) else { return nil }
        }
        return number
    }
}
