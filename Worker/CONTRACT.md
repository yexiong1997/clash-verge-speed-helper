# Verified worker contract (v1)

## Provenance and build

This worker is built from faceair/clash-speedtest v1.8.6, clean upstream commit `d3b9b151893fa7415fc4197242aadedc25e40391`, using the self-contained source patch in this directory and `Scripts/build-verified-worker.sh`. The build script downloads a pinned Go >=1.25 release only under `build/`, verifies its SHA-256 against a fixed digest obtained from official go.dev release metadata, verifies the pinned upstream source archive, and uses Go module checksums. It does not install Go or change the global PATH. The output is `build/verified-worker/clash-speedtest` (native host architecture); no prebuilt binary is committed.

Upstream is GPLv3 licensed; the patch is derived from that upstream source and is provided here as source, with upstream attribution. This notice does not declare that the containing project is GPL-licensed and does not assert legal compliance. Consult counsel about redistribution obligations.

## Interface

The caller supplies a Clash configuration on standard input and executes:

```
clash-speedtest -c /dev/stdin -f '<exact-name-regex>' -server-url '<http(s)-url>' -download-size <bytes> -concurrent <streams> -timeout <duration> -max-latency <duration> -rename=false -json-v1
```

The caller must construct an exact-name regex (Go regexp syntax). There is at most one selected node per invocation; no concurrent node tests. Do not persist the configuration. The worker accepts only `proxies` directly present on stdin; nonempty `proxy-providers` are rejected rather than downloaded. Input is limited to 32 MiB, streams to 1–128, and both durations must be positive; download-size must be at least the stream count. Node endpoint fields are validated before adapter construction as specified below; unsupported or ambiguous forms fail closed. SSH and WireGuard nodes are rejected entirely, and HTTP nodes with any custom `headers` field or decoder-equivalent alias are rejected entirely, before adapter construction. The process isolates the dependency home before its initialization and does not read the user's Clash configuration. Successful stdout consists of exactly one JSON line with no other output:

```
{"version":1,"node_name":"...","node_type":"...","head_attempts":6,"head_successes":0,"latency_ms":0,"jitter_ms":0,"expected_bytes":0,"received_bytes":0,"download_complete":false,"download_bytes_per_second":0}
```

All numeric fields are finite JSON numbers; byte counts and attempt counts are nonnegative integers. `expected_bytes` is the sum of requested stream sizes (integer division `download-size / concurrent` for each stream). `received_bytes` is the total actual bytes read, at most one excess byte per stream for oversized bodies. Content is discarded. `download_complete` is true only if every stream returns success status, valid range metadata if 206, reaches EOF without error, and returns exactly its requested bytes. Otherwise speed is zero. Speed is aggregate bytes/elapsed seconds for a complete download. HEAD attempts total six; only non-redirected 2xx responses count as successes; latency and jitter summarize successful HEADs. Redirects must not be followed. No available matched node, invalid arguments/configuration, or unrecoverable setup failure returns nonzero and no JSON; error output is generic only. A selected but unhealthy node still yields a JSON result if processing reaches measurement.

Stdout and stderr do not include raw configuration, proxy addresses, credentials, or raw underlying errors. In particular, `Result.ProxyConfig` is never serialized. `node_name` is intentionally echoed as required by the contract; a caller must not place an address or secret in a node name. No stderr diagnostic is emitted for runtime failures. Use only isolated synthetic fixtures and localhost for tests; never read real user app/Clash configuration or use real user nodes. Fetching public go.dev, GitHub and Go module artifacts is allowed solely for building.

If the provided URL has no path other than `/` and no query, it is treated like the upstream download-server base URL and each request targets `/__down?bytes=<per-stream-size>`. Otherwise the URL is used directly and each GET sends `Range: bytes=0-<per-stream-size-1>`. Response bodies are discarded; the timeout bounds each HEAD/GET independently. A failed HEAD does not suppress the GET measurement.

## Node endpoint validation

Validation occurs on the selected node before `adapter.ParseProxy`, which can construct active transports. A node must have one of these Mihomo outbound types: `ss`, `ssr`, `snell`, `socks5`, `http`, `vmess`, `vless`, `trojan`, `hysteria`, `hysteria2`, `tuic`, `mieru`, `anytls`, or `sudoku`. SSH and WireGuard are always rejected before construction: SSH can otherwise expose passwords to an unverified host key, and WireGuard is unavailable in the default build (no `with_gvisor` tag) and may allocate an unbounded number of workers. This rejection is independent of build tags and node options. The relevant pinned Mihomo dependency is v1.19.19. Ordinary nodes require a nonempty string `server` and a YAML integer `port` in 1–65535 inclusive. Quoted integers, floats, booleans, null, zero, negatives, and overflow are rejected. A server and port are still subject to the adapter's own protocol-specific validation.

The only accepted alternate endpoint forms are:

- `hysteria` and `hysteria2` may use `ports` in place of, or in addition to, `port`. Its textual syntax consists of at most 28 nonempty comma-separated decimal port numbers or ascending `start-end` ranges; `hysteria2` also accepts `/` separators. Every number is ASCII decimal in 1–65535. A single bare number in `ports` is rejected: use numeric `port`, or an explicit range/list instead. All supplied endpoint fields must independently be valid.
- `mieru` may use exactly one of numeric `port` or textual `port-range`; the latter must be exactly one ascending `start-end` range, with ASCII decimal endpoints in 1–65535.

A strict YAML preflight rejects duplicate mapping keys anywhere in the input, including otherwise ignored sections, to avoid silently overwriting malformed endpoints. Merge/anchor configurations that the strict decoder considers duplicates are also rejected. All other `ports`, `port-range` and `peers` placements are rejected. Port-like keys (`port`, `ports`, `port-range`, `peers`, `endpoint`, `endpoints`, `server-port`, and `server-ports`) are also rejected in all other nested maps and arrays, including otherwise opaque option/header maps. Case and underscore/hyphen aliases are recognized like Mihomo; duplicate aliases for endpoint fields or server fields fail closed. Port ranges with whitespace, brackets, signs, trailing text, empty elements, descending endpoints, or more than 28 segments are rejected even if an upstream parser accepts them. A valid ordinary Vless, Hysteria2, or Shadowsocks node with its numeric `port` remains supported; protocol validity and connectivity are still determined by Mihomo. These restrictions intentionally favor fail-closed behavior over accepting every upstream spelling.

For an HTTP proxy node, the worker rejects the presence of the root `headers` field in any form, including empty or null values, benign headers, non-string values, case variants and Unicode case-folded aliases such as `headerſ`. This is a deliberate compatibility limitation: Mihomo concatenates these values into CONNECT requests, and rejecting the entire field avoids header-name/value control-character and CRLF injection, including weakly decoded forms. HTTP nodes without that custom field remain supported; this restriction does not ban headers in other protocol-specific nested transport options.

## Trust boundary and network scope

Endpoint validation checks syntax and selected protocol options; **it is not an egress firewall or a LAN/private-address filter**. A tested subscription's nodes and the configured download target can contact arbitrary reachable endpoints, including local/private networks and DNS-selected addresses. Use only trusted subscriptions and trusted download targets, and apply a separately managed, verified network policy when isolation is required. No CIDR, DNS, or sandbox guarantee is made for normal worker execution. Sandboxes and loopback restrictions mentioned here apply only to the synthetic tests, not to unattended production runs.

## Verification scope

A conforming implementation rejects redirected HEADs, non-2xx responses, truncated, oversized, timed-out and otherwise errored streams and invalid or mismatched `Content-Range` for 206 responses. It must not accept a complete-looking prefix without verifying EOF. HTTP client settings and each stream's byte limit must enforce these checks without emitting response content. Tests must not rely on any existing installed app, Clash configuration, proxy, service, or credential.

The contract is the intended interface; a build or test failure must be reported as such rather than represented as a verified result.
