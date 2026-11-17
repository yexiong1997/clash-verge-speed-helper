"""Bounded Windows worker processes using a job object per measurement."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import _winapi
import msvcrt
import os
import subprocess
import threading
import time

import pywintypes
import win32api
import win32con
import win32event
import win32file
import win32job
import win32pipe
import win32process

from .measurement import decode_measurement

MAX_OUTPUT = 1_048_576
MAX_TOTAL_JOBS = 2000
MAX_COPIED_BYTES = 64 * 1024 * 1024
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_SPAWN_LOCK = threading.Lock()


@dataclass(frozen=True)
class Job:
    uid: str
    name: str
    yaml: bytes
    download_bytes: int
    streams: int
    latency_timeout: str
    download_timeout: str
    server_url: str = "https://speed.cloudflare.com"


def _literal_regex(name):
    meta = set(r"\.+*?()|[]{}^$")
    result = [r"\A"]
    for char in name:
        if ord(char) > 127:
            result.append("\\x{" + format(ord(char), "x") + "}")
        else:
            result.append(("\\" if char in meta else "") + char)
    return "".join(result) + r"\z"


def _pipe():
    security = pywintypes.SECURITY_ATTRIBUTES()
    security.bInheritHandle = True
    return win32pipe.CreatePipe(security, 0)


def _stream(handle, flags):
    return os.fdopen(msvcrt.open_osfhandle(handle.Detach(), flags), "rb" if flags == os.O_RDONLY else "wb", buffering=0)


class MultiBatchRunner:
    def __init__(self, executable, jobs, parallel, watchdog=180, prelaunch=None):
        self.executable = str(executable)
        self.jobs = tuple(jobs)
        self.parallel = min(10, max(1, int(parallel)))
        self.watchdog = min(86400, max(.05, float(watchdog)))
        self.prelaunch = prelaunch or (lambda job: True)
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._jobs = set()
        if not 0 < len(self.jobs) <= MAX_TOTAL_JOBS:
            raise ValueError("invalid batch")
        if sum(len(job.yaml) for job in self.jobs) > MAX_COPIED_BYTES:
            raise ValueError("batch too large")

    def cancel(self):
        self.cancelled.set()
        with self._lock:
            for handle in self._jobs:
                try:
                    win32job.TerminateJobObject(handle, 1)
                except Exception:
                    pass

    def run(self, on_result):
        with ThreadPoolExecutor(max_workers=self.parallel, thread_name_prefix="speed-worker") as pool:
            futures = {pool.submit(self._one, job): job for job in self.jobs}
            for future in as_completed(futures):
                job = futures[future]
                try:
                    status, measurement = future.result()
                except Exception:
                    status, measurement = "worker_failed", None
                on_result(job, status, measurement)

    def _one(self, job):
        if self.cancelled.is_set():
            return "cancelled", None
        if not job.name or not job.yaml or not (1 <= job.streams <= 128) or job.download_bytes < job.streams:
            return "invalid_job", None
        try:
            if not self.prelaunch(job):
                self.cancel()
                return "profile_changed", None
        except Exception:
            self.cancel()
            return "profile_changed", None
        if self.cancelled.is_set():
            return "cancelled", None
        handles = []
        process = None
        main_thread = None
        job_handle = None
        assigned = False
        threads = []
        stdout = bytearray()
        overflow = [False]
        input_ok = [True]
        try:
            job_handle = win32job.CreateJobObject(None, "")
            limits = win32job.QueryInformationJobObject(job_handle, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(job_handle, win32job.JobObjectExtendedLimitInformation, limits)
            args = [self.executable, "-c", "-", "-f", _literal_regex(job.name),
                    "-download-size", str(job.download_bytes), "-concurrent", str(job.streams),
                    "-timeout", job.download_timeout, "-max-latency", job.latency_timeout,
                    "-rename=false", "-server-url", job.server_url, "-json-v1"]
            with _SPAWN_LOCK:
                child_handles = []
                try:
                    stdin_r, stdin_w = _pipe()
                    handles.extend((stdin_r, stdin_w))
                    child_handles.append(stdin_r)
                    stdout_r, stdout_w = _pipe()
                    handles.extend((stdout_r, stdout_w))
                    child_handles.append(stdout_w)
                    stderr_r, stderr_w = _pipe()
                    handles.extend((stderr_r, stderr_w))
                    child_handles.append(stderr_w)
                    for handle in (stdin_w, stdout_r, stderr_r):
                        win32api.SetHandleInformation(handle, win32con.HANDLE_FLAG_INHERIT, 0)
                    startup = subprocess.STARTUPINFO()
                    startup.dwFlags |= win32con.STARTF_USESTDHANDLES
                    startup.hStdInput, startup.hStdOutput, startup.hStdError = map(int, child_handles)
                    startup.lpAttributeList = {"handle_list": list(map(int, child_handles))}
                    process, main_thread, _pid, _tid = _winapi.CreateProcess(
                        self.executable, subprocess.list2cmdline(args), None, None, True,
                        win32con.CREATE_NO_WINDOW | win32con.CREATE_SUSPENDED | _EXTENDED_STARTUPINFO_PRESENT,
                        None, None, startup)
                finally:
                    close_failed = False
                    for handle in child_handles:
                        try:
                            handle.Close()
                        except Exception:
                            close_failed = True
                    if close_failed:
                        raise OSError("could not close child pipe handles")
            try:
                win32job.AssignProcessToJobObject(job_handle, process)
                assigned = True
                with self._lock:
                    self._jobs.add(job_handle)
                    if self.cancelled.is_set():
                        win32job.TerminateJobObject(job_handle, 1)
                    else:
                        win32process.ResumeThread(main_thread)
            finally:
                win32api.CloseHandle(main_thread)
                main_thread = None
            stdin_file, stdout_file, stderr_file = (_stream(stdin_w, os.O_WRONLY),
                                                    _stream(stdout_r, os.O_RDONLY),
                                                    _stream(stderr_r, os.O_RDONLY))

            def write_input():
                try:
                    with stdin_file:
                        remaining = memoryview(job.yaml)
                        while remaining:
                            written = stdin_file.write(remaining)
                            if not written:
                                raise OSError("worker input failed")
                            remaining = remaining[written:]
                except Exception:
                    input_ok[0] = False

            def drain(stream, keep):
                try:
                    with stream:
                        while True:
                            chunk = stream.read(65536)
                            if not chunk:
                                break
                            if keep:
                                if len(stdout) + len(chunk) > MAX_OUTPUT:
                                    overflow[0] = True
                                elif not overflow[0]:
                                    stdout.extend(chunk)
                except Exception:
                    if keep:
                        overflow[0] = True

            threads = [threading.Thread(target=write_input, daemon=True),
                       threading.Thread(target=drain, args=(stdout_file, True), daemon=True),
                       threading.Thread(target=drain, args=(stderr_file, False), daemon=True)]
            for thread in threads:
                thread.start()
            deadline = time.monotonic() + self.watchdog
            while win32event.WaitForSingleObject(process, 50) != win32event.WAIT_OBJECT_0:
                if self.cancelled.is_set() or time.monotonic() >= deadline:
                    win32job.TerminateJobObject(job_handle, 1)
                    win32event.WaitForSingleObject(process, 3000)
                    return ("cancelled" if self.cancelled.is_set() else "timeout"), None
            for thread in threads:
                thread.join(3)
            if any(thread.is_alive() for thread in threads):
                win32job.TerminateJobObject(job_handle, 1)
                return "worker_failed", None
            if self.cancelled.is_set():
                return "cancelled", None
            if win32process.GetExitCodeProcess(process) != 0 or not input_ok[0] or overflow[0]:
                return "worker_failed", None
            measurement = decode_measurement(bytes(stdout), job.name, job.download_bytes, job.streams)
            return ("ok", measurement) if measurement else ("invalid_output", None)
        except Exception:
            if process is not None and not assigned:
                try:
                    win32process.TerminateProcess(process, 1)
                except Exception:
                    pass
            return "worker_failed", None
        finally:
            if main_thread is not None:
                win32api.CloseHandle(main_thread)
            if job_handle is not None:
                with self._lock:
                    self._jobs.discard(job_handle)
                job_handle.Close()
            if process is not None:
                win32api.CloseHandle(process)
            for handle in handles:
                try:
                    handle.Close()
                except Exception:
                    pass
