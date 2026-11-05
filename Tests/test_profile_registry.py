#!/usr/bin/env python3
"""Standalone, private synthetic registry tests. No real config or network.

Run on macOS: python3 -B Tests/test_profile_registry.py
The only compiled program and every profile/registry it reads live in a 0700
TemporaryDirectory; the root is always explicitly passed over stdin.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

PROJECT = Path(__file__).resolve().parent.parent
SOURCE = PROJECT / "Sources" / "ProfileRegistry.swift"

DRIVER = r'''
import Foundation

@main struct RegistryFixtureDriver {
    static func main() throws {
        let data = FileHandle.standardInput.readDataToEndOfFile()
        let input = try JSONSerialization.jsonObject(with: data) as! [String: String]
        let root = input["url"].flatMap { URL(string: $0) } ?? URL(fileURLWithPath: input["root"]!, isDirectory: true)
        var response: [String: Any]
        do {
            let items = try ProfileRegistry.options(root: root)
            response = ["status": "ok", "items": items.map {
                ["uid": $0.uid, "name": $0.name, "path": $0.path,
                 "id": $0.id.map { String(format: "%02x", $0) }.joined(),
                 "current": $0.isCurrent] as [String: Any]
            }]
            // Equatable must also compare byte-distinct canonically equivalent
            // values as distinct, rather than Swift's default String equality.
            if items.count > 1 {
                response["firstTwoEqual"] = (items[0] == items[1])
                let a = ProfileOption(uid: items[0].uid, name: "same", path: "same", isCurrent: false)
                let b = ProfileOption(uid: items[1].uid, name: "same", path: "same", isCurrent: false)
                response["sameOtherFieldsEqual"] = (a == b)
            }
        } catch {
            response = ["status": "error", "error": "\(error) \((error as NSError).localizedDescription)"]
        }
        let bytes = try JSONSerialization.data(withJSONObject: response, options: [.sortedKeys])
        FileHandle.standardOutput.write(bytes)
    }
}
'''


def item(uid="one", name="Subscription", file="one.yaml", kind="remote", **other):
    return dict(uid=uid, name=name, file=file, type=kind, **other)


class ProfileRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # This root contains synthetic inputs, the test driver, compiler output,
        # child HOME and all test-specific scratch files. No default root used.
        cls.temp = tempfile.TemporaryDirectory(prefix="profile-registry-test-", dir="/private/tmp")
        cls.work = Path(cls.temp.name)
        if cls.work.stat().st_mode & 0o077:
            raise RuntimeError("fixture root is not private")
        cls.home = cls.work / "home"
        cls.home.mkdir(mode=0o700)
        cls.env = os.environ.copy()
        cls.env.update(HOME=str(cls.home), TMPDIR=str(cls.work),
                       CLANG_MODULE_CACHE_PATH=str(cls.work / "clang-cache"),
                       SWIFT_MODULECACHE_PATH=str(cls.work / "swift-cache"))
        driver = cls.work / "RegistryFixtureDriver.swift"
        driver.write_text(DRIVER, encoding="utf-8")
        cls.binary = cls.work / "registry-test"
        subprocess.run(["/usr/bin/swiftc", "-swift-version", "6", str(SOURCE), str(driver), "-o", str(cls.binary)],
                       check=True, env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix="case-", dir=self.work)
        self.base = Path(self.fixture.name)
        self.root = self.base / "config"
        self.root.mkdir(mode=0o700)
        self.profiles = self.root / "profiles"
        self.profiles.mkdir(mode=0o700)

    def tearDown(self):
        self.fixture.cleanup()

    def create_profile(self, filename="one.yaml", text="proxies:\n  - name: same-node\n"):
        # The fixture helper itself must never follow a malicious registry path.
        if not filename or filename in (".", "..") or "/" in filename or "\\" in filename:
            raise AssertionError("unsafe fixture filename")
        (self.profiles / filename).write_text(text, encoding="utf-8")

    def registry(self, entries, *, current=...):
        data = {"items": entries}
        if current is not ...:
            data["current"] = current
        # Escape Unicode so YAML line-break scalars such as U+0085 are not
        # normalized to spaces before the reader can reject them.
        self.raw_registry(json.dumps(data, ensure_ascii=True))

    def raw_registry(self, text):
        (self.root / "profiles.yaml").write_text(text, encoding="utf-8")

    def run_reader(self, root=None, env=None, url=None):
        root = self.root if root is None else root
        payload = {"url": url} if url is not None else {"root": str(root)}
        result = subprocess.run([str(self.binary)], input=json.dumps(payload).encode(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, env=env or self.env)
        self.assertTrue(result.returncode == 0, "fixture driver failed")
        self.assertTrue(not result.stderr, "fixture driver wrote diagnostics")
        try:
            parsed = json.loads(result.stdout)
        except (ValueError, UnicodeDecodeError):
            self.fail("fixture driver did not return JSON")
        return parsed, result.stdout

    def assert_rejected(self, root=None):
        result, _ = self.run_reader(root)
        self.assertTrue(result.get("status") == "error", "invalid fixture was accepted")
        self.assertTrue(result.get("error", "").startswith(("invalidRegistry ", "unavailable ")),
                        "unexpected unsanitized error")

    def test_nonlocal_or_ambiguous_root_urls_are_rejected(self):
        # URL validation must occur without a network attempt or real config.
        urls = ["https://synthetic.invalid/", "file://synthetic.invalid" + str(self.root),
                self.root.as_uri() + "?query", self.root.as_uri() + "#fragment"]
        for url in urls:
            result, _ = self.run_reader(url=url)
            self.assertTrue(result.get("status") == "error", "invalid root URL accepted")

    def test_multiple_options_with_same_names_and_byte_exact_ids(self):
        # Both contents have the same node name, but the registry must keep the
        # subscriptions distinct; canonical equivalents are separate IDs too.
        for filename in ("one.yaml", "two.yaml", "three.yaml", "four.yaml"):
            self.create_profile(filename)
        composed, decomposed = "é", "e\u0301"
        entries = [item(composed, "Same display", "one.yaml"),
                   item(decomposed, "Same display", "two.yaml", "local"),
                   item("third", "Same display", "three.yaml"),
                   item("fourth", "Same display", "four.yaml", "local"),
                   {"type": "script", "uid": "script", "file": "../ignored", "url": "unused"},
                   {"type": "merge", "file": ["ignored", "non-string"]}]
        self.registry(entries, current=decomposed)
        result, _ = self.run_reader()
        self.assertTrue(result.get("status") == "ok", "valid profiles rejected")
        options = result["items"]
        self.assertTrue(len(options) == 4 and not result["firstTwoEqual"] and not result["sameOtherFieldsEqual"],
                        "identities collapsed")
        self.assertTrue([x["uid"].encode() for x in options] == [composed.encode(), decomposed.encode(), b"third", b"fourth"])
        self.assertTrue([x["id"] for x in options] == [s.encode().hex() for s in (composed, decomposed, "third", "fourth")])
        self.assertTrue([x["current"] for x in options] == [False, True, False, False], "wrong current identity")
        self.assertTrue(all(x["name"] == "Same display" for x in options))
        for option, filename in zip(options, ("one.yaml", "two.yaml", "three.yaml", "four.yaml")):
            # Foundation can spell /private/tmp as /tmp; both are the same
            # caller-selected directory, while filename bytes must survive.
            self.assertTrue(os.path.samefile(option["path"], self.profiles / filename))
            self.assertTrue(os.fsencode(os.path.basename(option["path"])) == filename.encode())
        self.assertTrue(all("same-node" in (self.profiles / x).read_text() for x in ("one.yaml", "two.yaml")))

    def test_ignores_known_auxiliary_resources_but_rejects_collisions(self):
        self.create_profile()
        for kind in ("rules", "proxies", "groups"):
            with self.subTest(kind=kind):
                self.registry([item(), {"type": kind, "uid": "aux", "file": "aux.yaml"}], current="one")
                result, _ = self.run_reader()
                self.assertEqual(result.get("status"), "ok")
                self.assertEqual(len(result["items"]), 1)
                for bad in ({"type": kind, "uid": "one"},
                            {"type": kind, "uid": "aux", "file": "one.yaml"}):
                    self.registry([item(), bad], current="one")
                    self.assert_rejected()

    def test_absent_null_and_invalid_current(self):
        self.create_profile()
        for current in (..., None):
            with self.subTest(current=current):
                self.registry([item()], current=current)
                result, _ = self.run_reader()
                self.assertTrue(result.get("status") == "ok" and not result["items"][0]["current"])
        for current in ("missing", "", "one\n", 7, ["one"], "script"):
            with self.subTest(current=current):
                self.registry([item(), {"type": "script", "uid": "script"}], current=current)
                self.assert_rejected()
        # Canonical equivalence is not a byte-exact current match.
        self.registry([item(uid="é")], current="e\u0301")
        self.assert_rejected()

    def test_reject_invalid_uid_name_and_filenames(self):
        self.create_profile()
        for field in ("uid", "name", "file"):
            for bad in ("", "\n", "a\x00b", "a\u007fb", "a\u0085b", None, 3):
                with self.subTest(field=field, kind=repr(bad)):
                    entry = item()
                    entry[field] = bad
                    self.registry([entry])
                    self.assert_rejected()
        for bad in (".", "..", "../outside.yaml", "sub/file.yaml", "/etc/passwd", "x\\y.yaml"):
            with self.subTest(file=bad):
                self.registry([item(file=bad)])
                self.assert_rejected()
        for field in ("uid", "name", "file"):
            entry = item()
            del entry[field]
            self.registry([entry])
            self.assert_rejected()

    def test_reject_missing_unavailable_nonregular_and_symlink_files(self):
        self.registry([item()])
        self.assert_rejected()  # missing
        (self.profiles / "one.yaml").mkdir()
        self.assert_rejected()  # directory
        (self.profiles / "one.yaml").rmdir()
        os.mkfifo(self.profiles / "one.yaml")
        self.assert_rejected()  # FIFO must not block
        (self.profiles / "one.yaml").unlink()
        sentinel = self.base / "outside.yaml"
        sentinel.write_text("synthetic outside sentinel", encoding="utf-8")
        (self.profiles / "one.yaml").symlink_to(sentinel)
        self.assert_rejected()  # outside symlink
        (self.profiles / "one.yaml").unlink()
        self.create_profile("real.yaml")
        (self.profiles / "one.yaml").symlink_to("real.yaml")
        self.assert_rejected()  # even an in-directory symlink is forbidden
        self.assertTrue(sentinel.read_text() == "synthetic outside sentinel")

    def test_reject_symlinked_registry_profiles_directory_or_root(self):
        self.create_profile()
        self.registry([item()])
        self.profiles.rename(self.root / "real-profiles")
        self.profiles.symlink_to("real-profiles", target_is_directory=True)
        self.assert_rejected()
        self.profiles.unlink()
        (self.root / "real-profiles").rename(self.profiles)
        (self.root / "profiles.yaml").rename(self.root / "registry-real.yaml")
        (self.root / "profiles.yaml").symlink_to("registry-real.yaml")
        self.assert_rejected()
        (self.root / "profiles.yaml").unlink()
        (self.root / "registry-real.yaml").rename(self.root / "profiles.yaml")
        link = self.base / "config-link"
        link.symlink_to(self.root, target_is_directory=True)
        self.assert_rejected(link)

    def test_reject_duplicate_ids_files_inodes_and_ignored_collisions(self):
        self.create_profile("one.yaml")
        self.create_profile("two.yaml")
        cases = [
            [item(), item("one", "second", "two.yaml", "local")],
            [item(), item("two", "second", "one.yaml", "local")],
            [item(), {"type": "script", "uid": "one"}],
            [item(), {"type": "merge", "uid": "other", "file": "one.yaml"}],
        ]
        for entries in cases:
            with self.subTest(kind=cases.index(entries)):
                self.registry(entries, current="one")
                self.assert_rejected()
        os.link(self.profiles / "one.yaml", self.profiles / "hardlink.yaml")
        self.registry([item(), item("two", "second", "hardlink.yaml", "local")])
        self.assert_rejected()  # distinct names, same dev/inode

    def test_bad_yaml_shapes_types_aliases_and_duplicate_keys(self):
        self.create_profile()
        bad_documents = [
            "items: [", "", "null", "[]", "items: {}", "items: [null]",
            "items: [{uid: one, name: Subscription, file: one.yaml}]",
            "items: [{type: unknown, uid: one, name: Subscription, file: one.yaml}]",
            "current: one\ncurrent: two\nitems: []\n",
            "items: [{type: remote, uid: one, uid: two, name: Subscription, file: one.yaml}]\n",
            "items: []\n---\nitems: []\n",
            "items: &items []\nother: *items\n",
        ]
        for i, document in enumerate(bad_documents):
            with self.subTest(document=i):
                self.raw_registry(document)
                self.assert_rejected()
        # A deliberately oversized registry is rejected without parsing.
        self.raw_registry("#" + "x" * (4 * 1024 * 1024))
        self.assert_rejected()

    def test_privacy_read_only_and_hostile_ruby_environment(self):
        secret = "SYNTH_SECRET_a9b821"
        self.create_profile(text="proxies:\n  - name: same-node\n  # " + secret + "\n")
        self.registry([item(url="https://synthetic.invalid/" + secret, password=secret)], current="one")
        before = {p: hashlib.sha256(p.read_bytes()).digest() for p in (self.root / "profiles.yaml", self.profiles / "one.yaml")}
        evil = self.base / "evil.rb"
        marker = self.base / "ruby-injection-marker"
        evil.write_text("File.write(" + json.dumps(str(marker)) + ", 'bad')\n", encoding="utf-8")
        env = self.env.copy()
        env.update(RUBYOPT="-r" + str(evil), RUBYLIB=str(self.base), GEM_HOME=str(self.base))
        result, raw = self.run_reader(env=env)
        self.assertTrue(result.get("status") == "ok", "valid private registry rejected")
        self.assertTrue(secret.encode() not in raw, "sensitive metadata escaped")
        self.assertTrue(not marker.exists(), "Ruby inherited an unsafe startup environment")
        self.assertTrue(all(hashlib.sha256(p.read_bytes()).digest() == h for p, h in before.items()), "fixture was modified")
        self.raw_registry("items: [ # " + secret)
        _, raw = self.run_reader(env=env)
        self.assertTrue(secret.encode() not in raw, "parser diagnostics escaped")

    def test_yaml_and_secrets_never_enter_ruby_argv(self):
        # Long, safe synthetic YAML keeps the child alive long enough to sample
        # its *actual* process argv. Only inspect this driver's Ruby child.
        secret = "SYNTH_ARGV_SECRET_d1fa52"
        self.create_profile()
        entries = [item(url="https://synthetic.invalid/" + secret)]
        entries.extend({"type": "script", "comment": "padding"} for _ in range(12000))
        self.registry(entries, current="one")
        proc = subprocess.Popen([str(self.binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=self.env)
        try:
            proc.stdin.write(json.dumps({"root": str(self.root)}).encode())
            proc.stdin.close()
            proc.stdin = None
            seen = False
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and proc.poll() is None:
                ps = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,command="], check=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3).stdout
                for line in ps.splitlines():
                    columns = line.strip().split(None, 2)
                    if len(columns) == 3 and columns[1] == str(proc.pid).encode() and b"ruby" in columns[2]:
                        # Inspect the entire (possibly multiline) untruncated
                        # command, not merely its first line from the listing.
                        argv = subprocess.run(["/bin/ps", "-ww", "-p", columns[0].decode(), "-o", "command="],
                                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3)
                        if argv.returncode != 0 or not argv.stdout:
                            continue  # child exited between the two samples
                        seen = True
                        self.assertTrue(secret.encode() not in argv.stdout, "secret in Ruby argv")
                        self.assertTrue(b"synthetic.invalid" not in argv.stdout, "URL in Ruby argv")
                if not seen:
                    time.sleep(0.005)
            stdout, stderr = proc.communicate(timeout=15)
            self.assertTrue(seen, "did not observe Ruby argv")
            self.assertTrue(proc.returncode == 0 and not stderr, "privacy fixture failed")
            self.assertTrue(json.loads(stdout).get("status") == "ok", "large valid fixture rejected")
            self.assertTrue(secret.encode() not in stdout, "secret in output")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()


if __name__ == "__main__":
    unittest.main(verbosity=2)
