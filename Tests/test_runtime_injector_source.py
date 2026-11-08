#!/usr/bin/env python3
"""Static contract checks for the official Clash Verge runtime bridge."""
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
SOURCE = (PROJECT / "Sources/ClashRuntimeInjector.swift").read_text(encoding="utf-8")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    required = [
        '"/Applications/Clash Verge.app"',
        'io.github.clash-verge-rev.clash-verge-rev',
        'JPH3Z7PPBB',
        'patch_profiles_config',
        'tauri://localhost',
        '127.0.0.1',
        '/private/tmp/clash-verge-runtime-state.json',
        'kAXPressAction',
        'CGEvent',
    ]
    for needle in required:
        require(needle in SOURCE, f"runtime injector lost required contract: {needle}")
    require('clash.runtime.bridge.accept' in SOURCE,
            "runtime bridge must accept sockets on a queue separate from request handling")
    require('acceptWork.async { [weak self] in self?.acceptLoop() }' in SOURCE,
            "runtime bridge accept loop must not block its request queue")
    forbidden = ["DYLD_INSERT_LIBRARIES", "task_for_pid", "codesign --force --sign"]
    for needle in forbidden:
        require(needle not in SOURCE, f"runtime injector must not use invasive path: {needle}")
    for script in (PROJECT / "Scripts/build.sh", PROJECT / "Scripts/build-multi.sh"):
        text = script.read_text(encoding="utf-8")
        require("ClashRuntimeInjector.swift" in text, f"build script does not compile runtime injector: {script}")
    single = (PROJECT / "Sources/main.swift").read_text(encoding="utf-8")
    require("ClashRuntimeInjector.shared.start" in single, "single-subscription app does not start runtime injection")
    require("ClashRuntimeInjector.shared.stop" in single, "single-subscription app does not stop runtime injection")
    multi = (PROJECT / "Sources/MultiMain.swift").read_text(encoding="utf-8")
    require("runtimeInjector.start" in multi, "multi-subscription app does not start runtime injection")
    require("runtimeInjector.stop" in multi, "multi-subscription app does not stop runtime injection")
    print("Runtime injector source contract PASS")


if __name__ == "__main__":
    main()
