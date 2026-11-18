#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build an OSS Qt kit module by module, in time-boxed stages.

A GitHub-hosted job stops after 6 hours, but a full Qt build takes longer.
This driver splits the build into "items" (configure + build + install of one
Qt module, or the docs of one module) and runs as many items as fit in one
job. Between jobs, the only state is the install prefix and a JSON progress
file, so the next job can continue where the previous job stopped.

Subcommands:
  plan          Resolve the module order and the dependency pins for a kit.
  setup-deps    Download and install the third-party dependencies of a kit.
  stage         Restore the prefix, run items until the deadline, save it.
  restore       Extract a saved prefix (for example the Android host Qt).
  package       Make the release archive from the finished prefix.
  import-qt5    Make the "modules" section of versions/<ver>.json from a
                qt5.git checkout.

Only the Python standard library is used. Python 3.12 or later is required.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path
from time import monotonic, sleep
from typing import NoReturn

ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = os.name == "nt"
PREFIX_MARKER = ".qtci-prefix"
WORK_MARKER = ".qtci-work"
PREFIX_ARCHIVE = "prefix.tar.zst"
PREFIX_ARCHIVE_PLAIN = "prefix.tar"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> NoReturn:
    print(f"::error::{msg}", flush=True)
    sys.exit(1)


def group(title: str) -> None:
    print(f"::group::{title}", flush=True)


def endgroup() -> None:
    print("::endgroup::", flush=True)


def gh_append(var: str, text: str) -> None:
    """Append text to a GitHub Actions file (GITHUB_ENV, GITHUB_OUTPUT, ...)."""
    path = os.environ.get(var)
    if not path:
        log(f"[{var}] {text.strip()}")
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")


def set_output(name: str, value: str) -> None:
    gh_append("GITHUB_OUTPUT", f"{name}={value}")


def set_env(name: str, value: str) -> None:
    os.environ[name] = value
    gh_append("GITHUB_ENV", f"{name}={value}")


class DeadlineExceeded(RuntimeError):
    """A command was stopped because the job deadline was reached."""


class InstallCheckFailed(RuntimeError):
    """The install of a module did not add what it must add.

    The build directory cannot fix this (configure left a part out), so it
    is not kept for a later job to continue.
    """


class FailedNearDeadline(DeadlineExceeded):
    """A build failed when too little time was left to retry it.

    It is handled as a deadline stop, but it does not say that the item is
    slow, so it does not change the calibration.
    """


# Unix time at which run() stops a command. The stage sets it to its deadline,
# so that a long item is stopped while the job still has time to save the
# items that finished before it. A job timeout would lose them.
COMMAND_DEADLINE: float | None = None


def _signal_group(pgid: int, sig: int) -> None:
    try:
        getattr(os, "killpg")(pgid, sig)  # POSIX only
    except ProcessLookupError:
        pass


def _kill_session(sid: int) -> None:
    """Kill every process of a session.

    pkill -s works on Linux and macOS. Without pkill, Linux uses /proc.
    """
    if shutil.which("pkill"):
        subprocess.run(["pkill", "-KILL", "-s", str(sid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            # Fields after the command name (which can contain spaces):
            # state ppid pgrp session ...
            session = int(stat[stat.rindex(")") + 2:].split()[3])
            if session == sid:
                os.kill(int(entry.name), 9)
        except (OSError, ValueError, IndexError):
            pass


# --- Windows job objects ---------------------------------------------------
# A job object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE holds a command and
# every process that it starts, also detached helpers such as mspdbsrv.exe,
# which taskkill /T does not find. Closing the job ends all of them.

def _new_job(proc: subprocess.Popen):
    """Put the process in a new kill-on-close job. Return the handle or None."""
    try:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits),
                        ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
        if ok:
            ok = kernel32.AssignProcessToJobObject(
                wintypes.HANDLE(job), wintypes.HANDLE(int(getattr(proc, "_handle"))))
        if not ok:
            kernel32.CloseHandle(wintypes.HANDLE(job))
            return None
        return job
    except (OSError, AttributeError, ValueError):
        return None


def _close_job(job) -> None:
    """Close a job handle; KILL_ON_JOB_CLOSE ends every process in the job."""
    if job:
        import ctypes
        from ctypes import wintypes
        ctypes.WinDLL("kernel32").CloseHandle(wintypes.HANDLE(job))


def kill_tree(proc: subprocess.Popen, grace: float = 30) -> None:
    # A signal during the cleanup (the runner sends SIGINT, then SIGTERM
    # after some seconds) must not interrupt it, or the compilers keep
    # running. Defer the signals while the cleanup runs, and raise the first
    # one after it, so that a cancel during a deadline stop is still a cancel.
    deferred: list[int] = []

    def defer(signum, _frame) -> None:
        deferred.append(signum)

    held = {}
    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                held[name] = signal.signal(getattr(signal, name), defer)
            except (ValueError, OSError):
                pass
    try:
        if IS_WINDOWS:
            job = getattr(proc, "qtci_job", None)
            if job:
                _close_job(job)
                setattr(proc, "qtci_job", None)
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            # The command runs in its own session (see run()). ninja starts
            # each compiler in a process group of its own, so SIGKILL to the
            # group of the command does not reach them. SIGTERM lets ninja
            # stop its compilers first; then SIGKILL stops what is left.
            try:
                _signal_group(proc.pid, 15)
                try:
                    proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    pass
            finally:
                _signal_group(proc.pid, 9)
                _kill_session(proc.pid)
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            log(f"warning: process {proc.pid} did not stop")
        if not IS_WINDOWS:
            # Compilers that ninja started can still run after ninja exited.
            _kill_session(proc.pid)
    finally:
        for name, handler in held.items():
            try:
                signal.signal(getattr(signal, name), handler)
            except (ValueError, OSError):
                pass
    if deferred:
        raise_system_exit(deferred[0], None)


def wait_for(proc: subprocess.Popen, timeout: float | None) -> int:
    """Wait for a process: poll it, and sleep in short steps.

    On Windows, Popen.wait() can hold back Ctrl+C until the process ends, so
    a cancelled job would not reach kill_tree. time.sleep() is interrupted
    by Ctrl+C and Ctrl+Break, so the signal handler runs within one step.
    The steps use the real monotonic clock.
    """
    end = None if timeout is None else monotonic() + timeout
    while True:
        returncode = proc.poll()
        if returncode is not None:
            return returncode
        left = None if end is None else end - monotonic()
        if left is not None and left <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout or 0)
        sleep(0.5 if left is None else min(0.5, left))


def _copy_output(stream, log_path: Path) -> None:
    """Copy the output of a command to the job log and to a file."""
    with open(log_path, "wb") as out:
        for line in iter(stream.readline, b""):
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            out.write(line)


def run(cmd: list, cwd: Path | None = None, env: dict | None = None,
        check: bool = True, log_path: Path | None = None,
        input_text: str | None = None) -> int:
    """Run a command; stop it at COMMAND_DEADLINE or on a signal.

    With log_path, the output also goes to that file. input_text goes to the
    standard input of the command.
    """
    cmd = [str(c) for c in cmd]
    log("+ " + subprocess.list2cmdline(cmd))
    if IS_WINDOWS and env is not None:
        # On Windows, Popen looks up the program in the PATH of this process,
        # not in env["PATH"]. The MSVC environment (vcvars64) adds cmake to
        # env["PATH"] only, so resolve the program there.
        found = shutil.which(cmd[0], path=env.get("PATH"))
        if found:
            cmd[0] = found
    # On POSIX, a new session lets kill_tree stop the compilers that the
    # command starts, for example the processes of ninja. On Windows, a job
    # object holds the command and every process that it starts (see
    # _new_job); taskkill /T is the fallback.
    group_args: dict = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS
                        else {"start_new_session": True})
    if log_path is not None:
        sys.stdout.flush()
        group_args.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if input_text is not None:
        group_args["stdin"] = subprocess.PIPE
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, **group_args)
    if IS_WINDOWS:
        setattr(proc, "qtci_job", _new_job(proc))
    if input_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(input_text.encode())
            proc.stdin.close()
        except OSError:
            pass  # the command did not read all of it
    copier = None
    if log_path is not None:
        import threading
        copier = threading.Thread(target=_copy_output, args=(proc.stdout, log_path),
                                  daemon=True)
        copier.start()
    try:
        timeout = None if COMMAND_DEADLINE is None else max(COMMAND_DEADLINE - time.time(), 1)
        returncode = wait_for(proc, timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        raise DeadlineExceeded(f"stopped at the job deadline: {cmd[0]}") from None
    except BaseException:
        # A cancel: the runner ends the job a few seconds after its first
        # signal, so the processes get only a short grace time.
        kill_tree(proc, grace=3)
        raise
    finally:
        # Helpers that the command left running (for example mspdbsrv.exe)
        # would keep files in the work directory locked, and a helper that
        # holds the output pipe would make the join below wait. End them
        # first: on Windows, closing the job ends them; on POSIX, they are
        # in the session of the command.
        if IS_WINDOWS:
            _close_job(getattr(proc, "qtci_job", None))
            setattr(proc, "qtci_job", None)
        elif proc.poll() is not None:
            _kill_session(proc.pid)
        if copier is not None:
            copier.join(timeout=30)
    if check and returncode != 0:
        raise RuntimeError(f"command failed ({returncode}): {cmd[0]}")
    return returncode


def capture(cmd: list, cwd: Path | None = None, env: dict | None = None) -> str:
    return subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout


def _urlopen_with_retry(url: str):
    headers = {"User-Agent": "vivace-qt-build"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(1, 4):
        try:
            return urllib.request.urlopen(req, timeout=120)
        except OSError as exc:
            if attempt == 3:
                raise
            log(f"request failed ({exc}); retrying in {attempt * 10} s")
            time.sleep(attempt * 10)
    raise AssertionError("unreachable")


def http_get(url: str) -> bytes:
    with _urlopen_with_retry(url) as resp:
        return resp.read()


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    with _urlopen_with_retry(url) as resp, open(partial, "wb") as f:
        shutil.copyfileobj(resp, f, 1 << 20)
    partial.replace(dest)


def file_digest(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rmtree(path: Path) -> None:
    if not path.exists():
        return

    def onexc(func, p, exc):
        # Retry only a delete that failed for a missing write permission: on
        # Windows the file is read-only, on POSIX its directory is. Anything
        # else (for example a failed os.open) re-raises the original error.
        if func not in (os.unlink, os.remove, os.rmdir) or not isinstance(exc, PermissionError):
            raise exc
        try:
            os.chmod(os.path.dirname(p), 0o700)
            if not os.path.islink(p):
                os.chmod(p, 0o700)
        except OSError:
            raise exc from None
        func(p)

    shutil.rmtree(path, onexc=onexc)


def rmtree_marker_last(path: Path, marker: str) -> None:
    """Delete a directory, its marker file last.

    If a locked file stops the deletion, the marker is still there, so a
    later run can still identify the directory as its own and delete it.
    """
    if not path.exists():
        return
    for child in path.iterdir():
        if child.name == marker:
            continue
        if child.is_dir() and not child.is_symlink():
            rmtree(child)
        else:
            try:
                child.unlink()
            except PermissionError:
                os.chmod(child, 0o700)
                child.unlink()
    rmtree(path)


def try_rmtree(path: Path) -> None:
    """Like rmtree, but only warn on an error (for cleanup that must not fail a job)."""
    try:
        rmtree(path)
    except OSError as exc:
        log(f"::warning::could not delete {path}: {exc}")


def exe(name: str) -> str:
    return name + ".bat" if IS_WINDOWS else name


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def load_version(version: str) -> dict:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        die(f"'{version}' is not a Qt version such as 6.12.0")
    path = ROOT / "versions" / f"{version}.json"
    if not path.is_file():
        die(f"no configuration for Qt {version}: {path} is missing")
    return json.loads(path.read_text(encoding="utf-8"))


def require_on_self_hosted(name: str) -> None:
    """Refuse a default directory on a self-hosted runner.

    The defaults (C:/Qt, ~/Qt, C:/w, ~/w) can hold the user's own files, for
    example a Qt installation. The runner sets RUNNER_ENVIRONMENT.
    """
    if os.environ.get("RUNNER_ENVIRONMENT") == "self-hosted":
        die(f"Set the {name} environment variable on this self-hosted runner to a "
            "directory that only this build uses. The tool deletes files in it.")


def default_prefix_root() -> str:
    if os.environ.get("QTCI_PREFIX_ROOT"):
        return os.environ["QTCI_PREFIX_ROOT"]
    require_on_self_hosted("QTCI_PREFIX_ROOT")
    if IS_WINDOWS:
        return "C:/Qt"
    if sys.platform == "darwin":
        return str(Path.home() / "Qt")
    return "/opt/Qt"


def work_dir() -> Path:
    if os.environ.get("QTCI_WORK"):
        return Path(os.environ["QTCI_WORK"])
    require_on_self_hosted("QTCI_WORK")
    # A short root keeps Windows paths under MAX_PATH; QtWebEngine needs it.
    return Path("C:/w") if IS_WINDOWS else Path.home() / "w"


def placeholders(cfg: dict, kit: dict) -> dict:
    """Values for {name} placeholders in the kit configuration.

    Values come from QTCI_* environment variables that setup-deps writes.
    """
    values = {
        "qt_version": cfg["qt_version"],
        "prefix_root": default_prefix_root(),
    }
    values["prefix"] = kit["prefix"].format(**values)
    for key, env in (("ffmpeg_dir", "QTCI_FFMPEG_DIR"),
                     ("openssl_dir", "QTCI_OPENSSL_DIR"),
                     ("pg_root", "QTCI_PG_ROOT"),
                     ("vulkan_include", "QTCI_VULKAN_INCLUDE"),
                     ("android_sdk", "QTCI_ANDROID_SDK"),
                     ("android_ndk", "QTCI_ANDROID_NDK"),
                     ("host_qt", "QTCI_HOST_QT"),
                     ("mingw_dir", "QTCI_MINGW_DIR"),
                     ("bison", "QTCI_BISON"),
                     ("flex", "QTCI_FLEX")):
        values[key] = os.environ.get(env, "").replace("\\", "/")
    return values


# Placeholders that can be empty: PostgreSQL is optional, and WinFlexBison is
# installed only where QtWebEngine is built. An item that uses an empty one
# is dropped. Any other empty placeholder is an error.
OPTIONAL_PLACEHOLDERS = {"pg_root", "bison", "flex"}


def expand(items: list, values: dict) -> list:
    """Expand placeholders. Drop an item whose optional placeholder is empty."""
    out = []
    for item in items:
        names = re.findall(r"{(\w+)}", item)
        missing = [n for n in names if not values.get(n)]
        required = [n for n in missing if n not in OPTIONAL_PLACEHOLDERS]
        if required:
            raise RuntimeError(f"'{item}' needs {', '.join(required)}, which is not set "
                               "(setup-deps sets it)")
        if missing:
            log(f"::warning::dropping '{item}' because {', '.join(missing)} is not set")
            continue
        out.append(item.format(**values))
    return out


def kit_prefix(cfg: dict, kit: dict) -> Path:
    return Path(placeholders(cfg, kit)["prefix"])


# --------------------------------------------------------------------------
# Module order
# --------------------------------------------------------------------------

def module_order(cfg: dict, kit: dict, webengine: bool, skip: list[str]) -> list[str]:
    modules = dict(cfg["modules"])
    if webengine:
        modules.update(cfg.get("extra_modules", {}))
    excluded = set(cfg.get("skip_modules", [])) | set(kit.get("skip", [])) | set(skip)
    known = set(cfg["modules"]) | set(cfg.get("extra_modules", {}))
    unknown = [m for m in skip if m not in known]
    if unknown:
        die(f"unknown module(s) in skip list: {', '.join(unknown)}")
    selected = {m: v for m, v in modules.items() if m not in excluded}

    for name, info in selected.items():
        for dep in info.get("depends", []):
            if dep not in selected:
                die(f"{name} depends on {dep}, which is skipped for this kit")

    # "used_by" lists modules that use a module optionally but do not say so in
    # qt5.git, for example qtwebview, which has a QtWebEngine backend.
    used_by: dict[str, list[str]] = {}
    for name, info in selected.items():
        for user in info.get("used_by", []):
            used_by.setdefault(user, []).append(name)

    order: list[str] = []
    pending = list(selected)
    while pending:
        for name in pending:
            info = selected[name]
            deps = [d for d in info.get("depends", []) + info.get("recommends", [])
                    + used_by.get(name, []) if d in selected]
            if all(d in order for d in deps):
                order.append(name)
                pending.remove(name)
                break
        else:
            die(f"dependency cycle among: {', '.join(pending)}")
    return order


def dependents(cfg: dict, roots: set[str], order: list[str]) -> set[str]:
    """Return the roots and every module in order that uses them, directly or not."""
    users: dict[str, set[str]] = {}
    for name in order:
        info = cfg["modules"].get(name) or cfg["extra_modules"][name]
        for dep in info.get("depends", []) + info.get("recommends", []):
            users.setdefault(dep, set()).add(name)
        for user in info.get("used_by", []):
            users.setdefault(name, set()).add(user)
    found = set(roots) & set(order)
    pending = list(found)
    while pending:
        for user in users.get(pending.pop(), set()):
            if user in order and user not in found:
                found.add(user)
                pending.append(user)
    return found


def estimate_minutes(cfg: dict, kit: dict, kind: str, module: str) -> float:
    table = cfg["estimates"][kind]
    return table.get(module, table["default"]) * kit.get("estimate_factor", 1.0)


# --------------------------------------------------------------------------
# Toolchain environment
# --------------------------------------------------------------------------

def msvc_environment() -> dict:
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) \
        / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        die(f"vswhere.exe not found at {vswhere}")
    version_range = os.environ.get("QTCI_MSVC_VERSION_RANGE", "[17.0,18.0)")
    install = capture([vswhere, "-latest", "-products", "*", "-version", version_range,
                       "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                       "-property", "installationPath"]).strip()
    if not install:
        die(f"no Visual Studio {version_range} with the x64 C++ tools was found")
    vcvars = Path(install) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    log(f"Using MSVC environment from {vcvars}")
    out = subprocess.run(f'"{vcvars}" >nul && set', shell=True, check=True,
                         capture_output=True, text=True).stdout
    env = {}
    for line in out.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.upper()] = value
    return env


def kit_environment(cfg: dict, kit: dict) -> dict:
    values = placeholders(cfg, kit)
    env = {k.upper() if IS_WINDOWS else k: v for k, v in os.environ.items()}
    if kit["toolchain"] == "msvc":
        env.update(msvc_environment())
    path_key = "PATH"
    entries = env.get(path_key, "").split(os.pathsep)
    # Patterns use "/" and match a "/"-terminated copy of each PATH entry,
    # so "/mingw64/" removes C:\mingw64\bin but not ...\mingw1310_64\bin.
    for pattern in kit.get("path_remove", []):
        rx = re.compile(pattern, re.IGNORECASE)
        entries = [e for e in entries if not rx.search(e.replace("\\", "/") + "/")]
    prepend = [str(Path(p)) for p in expand(kit.get("path_prepend", []), values)]
    env[path_key] = os.pathsep.join(prepend + entries)
    for key, value in kit.get("env", {}).items():
        env[key] = value.format(**values)
    for key in kit.get("env_unset", []):
        env.pop(key, None)
    env.setdefault("PYTHONUTF8", "1")
    return env


# --------------------------------------------------------------------------
# Prefix save and restore
# --------------------------------------------------------------------------

def pack_prefix(prefix: Path, state_dir: Path, exclude=()) -> Path:
    """Save the prefix to the state directory, without the paths in exclude.

    exclude has the partial install of a failed item that could not be
    removed (a locked file can also fail to be read).
    """
    skip = {p.replace(os.sep, "/") for p in exclude}

    def keep(info: tarfile.TarInfo):
        name = info.name[2:] if info.name.startswith("./") else info.name
        return None if name in skip else info

    (prefix / PREFIX_MARKER).write_text("Created by vivace-qt-build tools/qtci.py\n")
    for name in (PREFIX_ARCHIVE, PREFIX_ARCHIVE_PLAIN):
        (state_dir / name).unlink(missing_ok=True)
    zstd = shutil.which("zstd")
    start = time.time()
    if zstd:
        out = state_dir / PREFIX_ARCHIVE
        proc = subprocess.Popen([zstd, "-q", "-T0", "-3", "-o", str(out)],
                                stdin=subprocess.PIPE)
        assert proc.stdin is not None
        with tarfile.open(fileobj=proc.stdin, mode="w|") as tar:
            tar.add(prefix, arcname=".", filter=keep)
        proc.stdin.close()
        if proc.wait() != 0:
            die("zstd failed while saving the prefix")
    else:
        out = state_dir / PREFIX_ARCHIVE_PLAIN
        with tarfile.open(out, mode="w") as tar:
            tar.add(prefix, arcname=".", filter=keep)
    log(f"Saved {prefix} to {out} ({out.stat().st_size / 2**30:.2f} GiB, "
        f"{time.time() - start:.0f} s)")
    return out


def restore_prefix(state_dir: Path, prefix: Path, keep_archive: bool = False) -> bool:
    """Replace the prefix with the saved one. Return False if none is saved.

    An existing prefix is always removed first, also when nothing is saved,
    so that a self-hosted runner never builds on top of an earlier run. The
    saved archive is deleted after extraction to free disk space, unless
    keep_archive is set.
    """
    archive = state_dir / PREFIX_ARCHIVE
    plain = state_dir / PREFIX_ARCHIVE_PLAIN
    if prefix.exists():
        if not (prefix / PREFIX_MARKER).is_file() and any(prefix.iterdir()):
            die(f"{prefix} exists and was not made by this tool. The tool does not "
                "overwrite it. Set QTCI_PREFIX_ROOT to a different directory.")
        rmtree_marker_last(prefix, PREFIX_MARKER)
    # Create and mark the prefix before anything is written into it, so that a
    # later run can replace it after a failure (a partial extraction, or a
    # first stage that fails before it saves anything).
    prefix.mkdir(parents=True)
    (prefix / PREFIX_MARKER).write_text("Created by vivace-qt-build tools/qtci.py\n")
    if not archive.is_file() and not plain.is_file():
        return False
    start = time.time()
    if archive.is_file():
        zstd = shutil.which("zstd")
        if not zstd:
            die("zstd is required to restore the prefix")
        proc = subprocess.Popen([zstd, "-q", "-d", "-c", str(archive)],
                                stdout=subprocess.PIPE)
        assert proc.stdout is not None
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                tar.extractall(prefix, filter="tar")
            # tarfile stops at the end-of-archive blocks. Read the rest of
            # the stream (padding), or zstd blocks on a full pipe and the
            # wait below never returns.
            while proc.stdout.read(1 << 20):
                pass
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            proc.stdout.close()
        if proc.wait() != 0:
            die("zstd failed while restoring the prefix")
        if not keep_archive:
            archive.unlink()
    else:
        with tarfile.open(plain, mode="r") as tar:
            tar.extractall(prefix, filter="tar")
        if not keep_archive:
            plain.unlink()
    (prefix / PREFIX_MARKER).write_text("Created by vivace-qt-build tools/qtci.py\n")
    log(f"Restored {prefix} ({time.time() - start:.0f} s)")
    return True


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state(state_dir: Path) -> dict:
    path = state_dir / "state.json"
    if not path.is_file():
        die(f"{path} is missing; run 'plan' first")
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(state_dir: Path, state: dict) -> None:
    (state_dir / "state.json").write_text(json.dumps(state, indent=2) + "\n",
                                          encoding="utf-8")


def item_id(item: dict) -> str:
    return f"{item['kind']}:{item['module']}"


MAIN_POOL = "main"
WEBENGINE_POOL = "webengine"


def remaining_items(state: dict, pool: str | None = None) -> list[dict]:
    """Return the items that are not built, in order, for one pool or for all."""
    done = set(state["done"])
    return [i for i in state["items"] if item_id(i) not in done
            and (pool is None or i.get("pool", MAIN_POOL) == pool)]


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------

def ffmpeg_target(kit: dict) -> str:
    """Return the build-ffmpeg.sh target of a kit: msvc, mingw, linux, macos, or android."""
    if kit["toolchain"] == "android":
        return "android"
    if kit["host"] == "windows":
        return kit["toolchain"]
    return kit["host"]


def ffmpeg_options(kit: dict) -> str:
    """Return the key of the configure options, parts, and checks of a kit.

    It is the build-ffmpeg.sh target, unless the kit names another option set
    (ffmpeg_options), for example for a CPU without the x86-only parts.
    """
    return kit.get("ffmpeg_options", ffmpeg_target(kit))


# The files that control the FFmpeg build. Their SHA-256 values are part of
# the recipe, and the source archives contain them.
FFMPEG_SCRIPTS = ("tools/build-ffmpeg.sh", "tools/qtci.py",
                  ".github/actions/build-ffmpeg/action.yml")


def ffmpeg_recipe(cfg: dict, kit: dict) -> dict:
    """Everything that decides the FFmpeg of a kit. Its hash is the cache key."""
    tools = cfg["toolchains"]["ffmpeg"]
    target = ffmpeg_target(kit)
    options = ffmpeg_options(kit)
    if options not in tools["parts"]:
        die(f"unknown ffmpeg_options {options}")

    def source(info: dict) -> dict:
        url = info["url"].format(version=info["version"])
        # "file" names a download whose URL ends with only a tag (v1.2.3.tar.gz).
        return {"version": info["version"], "sha256": info["sha256"], "url": url,
                "file": info.get("file", url.rsplit("/", 1)[1]).format(version=info["version"]),
                "license": info["license"], "notices": info.get("notices", [])}

    recipe = {
        "target": target,
        "ffmpeg": source(tools),
        "meson": tools["meson"],
        "ninja": tools["ninja"],
        "cmake": tools["cmake"],
        "configure": tools["configure"]["common"] + tools["configure"].get(options, []),
        "checks": tools["checks"].get(options, {}),
        # Every script of the FFmpeg build: a change of one must not reuse a
        # cached FFmpeg, and the source archive ships these exact files.
        "scripts": {rel: file_digest(ROOT / rel, "sha256") for rel in FFMPEG_SCRIPTS},
    }
    # Only when it differs, so the recipe content of the other kits does not
    # change. (Their cache keys change with every edit of this file.)
    if options != target:
        recipe["options"] = options
    for part in tools["parts"][options]:
        recipe[part] = source(tools[part])
    if target != "android":
        # kit_environment gives these to the FFmpeg build (CC, CXX, PATH).
        recipe["kit_env"] = {k: kit[k] for k in ("env", "env_unset", "path_prepend",
                                                 "path_remove") if k in kit}
    if target == "mingw":
        recipe["mingw_licenses"] = tools["mingw_licenses"]
        # The compiler of the MinGW FFmpeg and the source of its runtime libraries.
        recipe["mingw"] = cfg["toolchains"]["mingw"]
    if target == "android":
        recipe["ndk"] = cfg["toolchains"]["android_ndk"]
        recipe["android_abi"] = kit["android_abi"]
        recipe["android_api"] = tools["android_api"]
    return recipe


def ffmpeg_recipe_key(recipe: dict) -> str:
    return hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()[:16]


def resolve_git_head(repo: str, branch: str) -> str:
    data = json.loads(http_get(f"https://api.github.com/repos/{repo}/commits/{branch}"))
    return data["sha"]


def cmd_plan(args) -> None:
    if not args.check_only and not args.state:
        die("--state is required")
    cfg = load_version(args.qt_version)
    kit = cfg["kits"].get(args.kit) or die(f"unknown kit {args.kit}")
    skip = [m.strip() for m in args.skip.split(",") if m.strip()]
    webengine = args.webengine and "qtwebengine" not in kit.get("skip", [])
    if args.webengine and not webengine:
        log(f"note: QtWebEngine is not built for {args.kit}")
    docs = {"on": True, "off": False}.get(args.docs, kit.get("docs", False))
    # docs_unavailable gives the reason why a kit cannot build its docs (no
    # qdoc). Then docs=on does not apply to it, as webengine for a kit that
    # skips QtWebEngine.
    if docs and kit.get("docs_unavailable"):
        log(f"note: no docs for {args.kit}: {kit['docs_unavailable']}")
        docs = False
    order = module_order(cfg, kit, webengine, skip)
    # skip_modules can remove qtwebengine; then nothing needs its toolchain.
    webengine = webengine and "qtwebengine" in order

    # With --offload-webengine, QtWebEngine and every module that uses it are
    # built last, by a separate job on a runner without the 6-hour limit.
    offloaded = (dependents(cfg, {"qtwebengine"}, order)
                 if webengine and args.offload_webengine else set())
    # The webengine job runs on its own runner, never in the container of
    # the kit, so it would build with other libraries than the stage jobs.
    if offloaded and kit.get("container"):
        die(f"{args.kit} builds in the container {kit['container']}; its QtWebEngine "
            "cannot be built by the webengine job. Add qtwebengine to the skip list "
            "of the kit, or plan it without --offload-webengine.")
    items = []
    for pool in (MAIN_POOL, WEBENGINE_POOL):
        modules = [m for m in order if (m in offloaded) == (pool == WEBENGINE_POOL)]
        kinds = ("build", "docs") if docs else ("build",)
        items += [{"kind": kind, "module": m, "pool": pool} for kind in kinds for m in modules]

    # Stop before any build starts if a stage job cannot fit an item. The stage
    # repeats this check, but only when the item is first in a stage, which can
    # be after many hours of other builds.
    if args.stage_minutes:
        budget = args.stage_minutes - args.reserve_minutes
        for item in items:
            base = estimate_minutes(cfg, kit, item["kind"], item["module"])
            if item["pool"] == MAIN_POOL and base > budget:
                hint = (" Set QTCI_RUNNER_WEBENGINE_<KIT> to build QtWebEngine on a "
                        "self-hosted runner." if item["module"] == "qtwebengine" else "")
                die(f"{item_id(item)} needs about {base:.0f} min, but a stage job has only "
                    f"{budget:.0f} min.{hint} Or set QTCI_STAGE_MINUTES to a larger value "
                    "and use a runner without the 6-hour limit.")

    if args.check_only:
        # check-inputs of build.yml: the module order and the fit check only,
        # without the network lookups of the dependencies.
        log(f"Kit {args.kit}: the plan is valid ({len(order)} modules)")
        return

    tools = cfg["toolchains"]
    deps: dict = {"kit": args.kit, "qt_version": cfg["qt_version"]}
    deps["ffmpeg"] = ffmpeg_recipe(cfg, kit)
    if kit["toolchain"] == "android":
        deps["ndk"] = tools["android_ndk"]
        repo = tools["android_openssl"]["repo"]
        deps["android_openssl"] = {"repo": repo,
                                   "sha": resolve_git_head(repo, tools["android_openssl"]["branch"])}
        deps["host_kit"] = kit["host_kit"]
    if kit["toolchain"] in ("mingw", "android"):
        deps["mingw"] = tools["mingw"]
    if kit.get("libclang"):
        deps["libclang"] = tools["libclang"][kit["libclang"]]
    if kit.get("vulkan_headers"):
        deps["vulkan_headers"] = tools["vulkan_headers"]

    state = {
        "qt_version": cfg["qt_version"],
        "kit": args.kit,
        "docs": docs,
        "webengine": webengine,
        "offloaded": sorted(offloaded),
        "items": items,
        "done": [],
        "durations": {},
        "deps": deps,
        "planned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    out = Path(args.state)
    out.mkdir(parents=True, exist_ok=True)
    save_state(out, state)
    (out / "deps.json").write_text(json.dumps(deps, indent=2) + "\n", encoding="utf-8")

    log(f"Kit {args.kit}: {len(order)} modules, docs={'on' if docs else 'off'}, "
        f"webengine={'on' if webengine else 'off'}")
    log("Module order: " + " ".join(order))
    if offloaded:
        log("Built by the webengine job: " + " ".join(m for m in order if m in offloaded))
    set_output("host", kit["host"])
    set_output("toolchain", kit["toolchain"])
    # The container image of the ffmpeg, stage, and package jobs. Empty: the
    # jobs run directly on the runner.
    set_output("container", kit.get("container", ""))
    set_output("host_kit", deps.get("host_kit", ""))
    set_output("android_abi", kit.get("android_abi", ""))
    # "webengine" is true when the stage jobs build QtWebEngine themselves.
    set_output("webengine", "true" if webengine and not offloaded else "false")
    set_output("offload", "true" if offloaded else "false")
    # Kits that other kits use as their host Qt publish their finished state.
    is_host = any(k.get("host_kit") == args.kit for k in cfg["kits"].values())
    set_output("publish_state", "true" if is_host else "false")
    # The kit name keeps the caches of kits with the same recipe apart: the
    # cache of one kit is never restored on the runner of another.
    set_output("ffmpeg_key", f"ffmpeg-{args.kit}-{ffmpeg_recipe_key(deps['ffmpeg'])}")
    total = sum(estimate_minutes(cfg, kit, i["kind"], i["module"]) for i in items)
    where = " (webengine job: " + " ".join(m for m in order if m in offloaded) + ")" \
        if offloaded else ""
    gh_append("GITHUB_STEP_SUMMARY",
              f"### Plan for {args.kit}\n\n"
              f"- Modules ({len(order)}): {' '.join(order)}\n"
              f"- Docs: {'on' if docs else 'off'}; "
              f"QtWebEngine: {'on' if webengine else 'off'}{where}\n"
              f"- Estimated work on a 4-core runner: {total / 60:.1f} h\n")


# --------------------------------------------------------------------------
# setup-deps
# --------------------------------------------------------------------------

def extract_archive(archive: Path, dest: Path, strip_top: bool) -> None:
    tmp = dest.with_name(dest.name + ".tmp")
    rmtree(tmp)
    tmp.mkdir(parents=True)
    name = archive.name
    if name.endswith(".zip") or name.endswith(".7z"):
        seven = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")
        if not seven:
            die("7-Zip (7z, 7zz or 7za) is required")
        run([seven, "x", "-y", "-bd", f"-o{tmp}", archive])
    else:
        run(["tar", "-xf", archive, "-C", tmp])
    src = tmp
    if strip_top:
        children = list(tmp.iterdir())
        if len(children) == 1 and children[0].is_dir():
            src = children[0]
    rmtree(dest)
    shutil.move(str(src), str(dest))
    rmtree(tmp)


FFMPEG_LICENSE = "LGPL version 2.1 or later"
FFMPEG_CONFIG = Path("licenses") / "ffmpeg-config.txt"
FFMPEG_SOURCE = Path("licenses") / "FFMPEG-SOURCE.txt"
FFMPEG_STAMP = ".qtci-recipe.json"


def check_ffmpeg_license(ffmpeg_dir: Path) -> None:
    """Fail unless the FFmpeg in ffmpeg_dir reports the LGPL license.

    build-ffmpeg.sh copies the license that FFmpeg's configure script
    computed. An option that pulls in GPL or nonfree code changes it.
    """
    path = ffmpeg_dir / FFMPEG_CONFIG
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        die(f"{path} not found; tools/build-ffmpeg.sh did not make the FFmpeg in {ffmpeg_dir}")
    m = re.search(r'^#define FFMPEG_LICENSE "([^"]*)"', text, re.MULTILINE)
    found = m[1] if m else "no license line"
    if found != FFMPEG_LICENSE:
        die(f"the FFmpeg in {ffmpeg_dir} is not {FFMPEG_LICENSE} ({found}); "
            "Qt packages must not contain GPL or nonfree FFmpeg code")


FFMPEG_CHECKS = "FFMPEG-CHECKS.txt"
# The SHA-256 of each file of a built FFmpeg. A cache restore that stops
# part-way can leave the stamp without the rest of the tree, so the stamp
# alone does not prove a complete FFmpeg.
FFMPEG_MANIFEST = ".qtci-files.json"

# Each part of the FFmpeg build: its recipe key, its title, and the variable
# that passes its source directory to build-ffmpeg.sh.
FFMPEG_SOURCE_PARTS = (
    ("ffmpeg", "FFmpeg (LGPL-2.1-or-later), shared libraries and programs", "FF_SRC"),
    ("dav1d", "dav1d (BSD-2-Clause), linked statically into libavcodec", "DAV1D_SRC"),
    ("nv_codec_headers", "nv-codec-headers (MIT), headers only", "NVCODEC_SRC"),
    ("libvpl", "libvpl (MIT), linked statically", "LIBVPL_SRC"),
    ("amf_headers", "AMF headers (MIT), headers only", "AMF_SRC"),
    ("zlib", "zlib (Zlib), linked statically", "ZLIB_SRC"),
    ("libaribcaption", "libaribcaption (MIT), linked statically", "ARIBCAPTION_SRC"),
)


def ffmpeg_source_text(recipe: dict) -> str:
    lines = [
        f"The FFmpeg libraries in this package are licensed under the {FFMPEG_LICENSE}.",
        "You can replace them with your own build of the same FFmpeg version.",
        "",
        "vivace-qt-build built them with tools/build-ffmpeg.sh from these sources:",
        "",
    ]
    for key, title, _var in FFMPEG_SOURCE_PARTS:
        if key in recipe:
            info = recipe[key]
            lines += [f"{title} {info['version']}", f"  {info['url']}",
                      f"  SHA-256 {info['sha256']}", ""]
    lines += [
        "A GitHub release of this package also has these source archives, in the",
        "file next to the package whose name ends with -source.tar.",
        "",
        f"Target: {recipe['target']}",
        "FFmpeg configure options (ffmpeg-config.txt has the full configure line):",
    ]
    lines += [f"  {option}" for option in recipe["configure"]]
    return "\n".join(lines) + "\n"


def setup_ffmpeg(ffmpeg_dir: str) -> None:
    if not ffmpeg_dir:
        die("--ffmpeg-dir is required (the artifact of the ffmpeg job)")
    path = Path(ffmpeg_dir).resolve()
    check_ffmpeg_license(path)
    set_env("QTCI_FFMPEG_DIR", str(path))


def setup_libclang(deps: dict, root: Path) -> None:
    info = deps["libclang"]
    dest = root / "libclang"
    stamp = dest / ".qtci-source"
    name = info["url"].rsplit("/", 1)[1]
    if not (stamp.is_file() and stamp.read_text().strip() == name):
        archive = root / "downloads" / name
        if not archive.is_file():
            log(f"Downloading {info['url']}")
            download(info["url"], archive)
        digest = file_digest(archive, "sha1")
        if digest != info["sha1"]:
            archive.unlink()
            die(f"SHA-1 mismatch for {name}: got {digest}, expected {info['sha1']}")
        extract_archive(archive, dest, strip_top=True)
        archive.unlink()
        stamp.write_text(name + "\n")
    set_env("LLVM_INSTALL_DIR", str(dest))


def setup_vulkan_headers(deps: dict, root: Path) -> None:
    tag = deps["vulkan_headers"]
    dest = root / f"Vulkan-Headers-{tag}"
    if not (dest / "include" / "vulkan" / "vulkan.h").is_file():
        rmtree(dest)
        run(["git", "clone", "--quiet", "--depth", "1", "--branch", tag,
             "https://github.com/KhronosGroup/Vulkan-Headers.git", dest])
    set_env("QTCI_VULKAN_INCLUDE", str(dest / "include"))


def setup_mingw(deps: dict, root: Path) -> None:
    info = deps["mingw"]
    dest = root / "Qt" / "Tools" / info["dir"]
    if not (dest / "bin" / "g++.exe").is_file():
        # A virtual environment of its own: pip must not change the packages
        # of the Python that the runner shares between jobs.
        venv = root / "aqt-venv"
        python = venv / "Scripts" / "python.exe"
        if not python.is_file():
            run([sys.executable, "-m", "venv", venv])
        run([python, "-m", "pip", "install", "--quiet", f"aqtinstall=={info['aqtinstall']}"])
        run([python, "-m", "aqt", "install-tool", "windows", "desktop",
             info["tool"], info["variant"], "--outputdir", root / "Qt"])
    version = capture([dest / "bin" / "g++.exe", "--version"]).splitlines()[0]
    log(f"MinGW: {version}")
    set_env("QTCI_MINGW_DIR", str(dest))


WINDOWS_OPENSSL_DIRS = (r"C:\Program Files\OpenSSL-Win64", r"C:\Program Files\OpenSSL")


def setup_windows_openssl() -> None:
    # Qt 6.12 needs OpenSSL 3. An older OpenSSL in the first directory must
    # not hide OpenSSL 3 in the second one.
    for candidate in WINDOWS_OPENSSL_DIRS:
        header = Path(candidate) / "include" / "openssl" / "opensslv.h"
        try:
            text = header.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"#\s*define\s+OPENSSL_VERSION_MAJOR\s+3\b", text):
            set_env("QTCI_OPENSSL_DIR", candidate)
            return
    die("OpenSSL 3 headers not found; install OpenSSL 3 (choco install openssl)")


def space_free_dir(path: str, link: Path) -> str:
    """Return path, or a directory junction at link to it if path has a space.

    MinGW's windres splits include paths at spaces, so a resource file of a
    target that includes C:/Program Files/... does not compile.
    """
    if " " not in path:
        return path
    if " " in str(link):
        die(f"{link} contains a space; the MinGW build needs a deps directory without spaces")
    if os.path.isjunction(link) or os.path.islink(link):
        os.rmdir(link)  # a junction of an earlier job; the target stays
    elif os.path.lexists(link):
        die(f"{link} exists and is not a junction; delete it, then re-run the job")
    link.parent.mkdir(parents=True, exist_ok=True)
    run(["cmd", "/c", "mklink", "/J", str(link), str(Path(path))])
    return str(link)


def android_sdk() -> str:
    sdk = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
    if not sdk:
        die("ANDROID_SDK_ROOT is not set")
    return sdk


def ensure_ndk(sdk: str, version: str) -> Path:
    ndk = Path(sdk) / "ndk" / version
    if not (ndk / "build" / "cmake" / "android.toolchain.cmake").is_file():
        candidates = sorted(Path(sdk).glob("cmdline-tools/*/bin/" + exe("sdkmanager")))
        if not candidates:
            die(f"sdkmanager not found under {sdk}")
        cmd = [candidates[-1], f"ndk;{version}"]
        # run(): a cancel also stops the Java process of sdkmanager.
        run(cmd, input_text="y\n" * 20)
    return ndk


def setup_android(deps: dict, kit: dict, root: Path) -> None:
    sdk = android_sdk()
    ndk = ensure_ndk(sdk, deps["ndk"])
    set_env("QTCI_ANDROID_SDK", sdk)
    set_env("QTCI_ANDROID_NDK", str(ndk))
    java = os.environ.get("JAVA_HOME_17_X64")
    if java:
        set_env("JAVA_HOME", java)

    # OpenSSL: the same include/ + lib/ layout as C:/opt/OpenSSL-Android-<abi>.
    ossl = deps["android_openssl"]
    abi = kit["android_abi"]
    src = root / f"android_openssl-{ossl['sha'][:12]}"
    if not (src / "ssl_3" / abi / "libssl.a").is_file():
        rmtree(src)
        src.mkdir(parents=True)
        run(["git", "init", "--quiet"], cwd=src)
        run(["git", "remote", "add", "origin", f"https://github.com/{ossl['repo']}.git"], cwd=src)
        run(["git", "sparse-checkout", "set", "ssl_3/include", f"ssl_3/{abi}"], cwd=src)
        run(["git", "fetch", "--quiet", "--depth", "1", "origin", ossl["sha"]], cwd=src)
        run(["git", "checkout", "--quiet", "FETCH_HEAD"], cwd=src)
    dest = root / f"OpenSSL-Android-{abi}"
    rmtree(dest)
    shutil.copytree(src / "ssl_3" / "include", dest / "include")
    (dest / "lib").mkdir()
    for lib in (src / "ssl_3" / abi).iterdir():
        if lib.is_file():
            shutil.copy2(lib, dest / "lib" / lib.name)
    # OpenSSL 3 is Apache-2.0, and the kit links it statically: the license
    # text must ship with the kit (post_install copies it).
    if not (src / "LICENSE").is_file():
        die(f"{src / 'LICENSE'} not found; the Android kits must ship the OpenSSL license")
    shutil.copy2(src / "LICENSE", dest / "LICENSE")
    set_env("QTCI_OPENSSL_DIR", str(dest))


def cmd_setup_deps(args) -> None:
    work_dir()
    default_prefix_root()
    state = load_state(Path(args.state))
    cfg = load_version(state["qt_version"])
    kit = cfg["kits"][state["kit"]]
    deps = state["deps"]
    root = Path(args.deps_dir)
    root.mkdir(parents=True, exist_ok=True)

    if "ffmpeg" in deps:
        setup_ffmpeg(args.ffmpeg_dir)
    if kit["toolchain"] == "android":
        setup_android(deps, kit, root)
    if "libclang" in deps:
        setup_libclang(deps, root)
    if "vulkan_headers" in deps:
        setup_vulkan_headers(deps, root)
    if "mingw" in deps:
        setup_mingw(deps, root)
    if kit["host"] == "windows" and kit["toolchain"] in ("msvc", "mingw"):
        setup_windows_openssl()
        pg = os.environ.get("PGROOT")
        if pg and (Path(pg) / "include" / "libpq-fe.h").is_file():
            set_env("QTCI_PG_ROOT", pg)
        if kit["toolchain"] == "mingw":
            # Next to the deps directory, not in it: the dependency cache
            # saves that directory, and a restore turns a junction into a copy.
            links = root.resolve().parent / "qtci-links"
            for var, name in (("QTCI_OPENSSL_DIR", "openssl"), ("QTCI_PG_ROOT", "pg")):
                if os.environ.get(var):
                    set_env(var, space_free_dir(os.environ[var], links / name))
        if state["webengine"]:
            choco = Path(os.environ.get("ChocolateyInstall", r"C:\ProgramData\chocolatey"))
            tools = choco / "lib" / "winflexbison3" / "tools"
            # Set only when installed: with QtWebEngine on a separate runner,
            # the stage jobs do not install WinFlexBison.
            for var, name in (("QTCI_BISON", "win_bison.exe"), ("QTCI_FLEX", "win_flex.exe")):
                if (tools / name).is_file():
                    set_env(var, str(tools / name))
    if args.host_qt:
        set_env("QTCI_HOST_QT", str(Path(args.host_qt)))


# --------------------------------------------------------------------------
# build-ffmpeg
# --------------------------------------------------------------------------

def fetch_source(info: dict, downloads: Path) -> Path:
    """Download a source archive and check its SHA-256."""
    archive = downloads / info["file"]
    if archive.is_file() and file_digest(archive, "sha256") == info["sha256"]:
        return archive
    log(f"Downloading {info['url']}")
    download(info["url"], archive)
    digest = file_digest(archive, "sha256")
    if digest != info["sha256"]:
        archive.unlink()
        die(f"SHA-256 mismatch for {archive.name}: got {digest}, expected {info['sha256']}")
    return archive


def extract_source(archive: Path, dest: Path) -> Path:
    """Extract a source tarball into dest; return its single top directory."""
    rmtree(dest)
    dest.mkdir(parents=True)
    # tarfile, not tar: the tar of Git for Windows reads "C:" as a host name.
    with tarfile.open(archive) as tar:
        tar.extractall(dest, filter="data")
    children = [p for p in dest.iterdir() if p.is_dir()]
    if len(children) != 1:
        die(f"{archive.name} does not have exactly one top directory")
    return children[0]


def leading_comment(path: Path) -> str:
    """The comment at the top of a C header or an assembly file."""
    head = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("//", "/*", "*", ";")):
            break
        head.append(line)
    return "\n".join(head) + "\n"


def copy_ffmpeg_licenses(recipe: dict, sources: dict, out: Path) -> None:
    """Copy the license text of each part into <out>/licenses.

    A part whose license is in a source file (nv-codec-headers, AMF) gets
    the comment at the top of that file. "notices" are more source files
    whose license asks for their notice in copies (x86inc.asm, ISC).
    """
    lic = out / "licenses"
    lic.mkdir(parents=True, exist_ok=True)
    for key, _title, _var in FFMPEG_SOURCE_PARTS:
        if key not in recipe:
            continue
        name = key.replace("_", "-")
        src = sources[key] / recipe[key]["license"]
        if src.suffix in (".h", ".asm"):
            (lic / f"{name}-LICENSE.txt").write_text(leading_comment(src), encoding="utf-8")
        else:
            shutil.copy2(src, lic / f"{name}-{src.name}")
        for rel in recipe[key].get("notices", []):
            notice = sources[key] / rel
            if not notice.is_file():
                die(f"{notice} not found; its license notice must ship with FFmpeg")
            (lic / f"{name}-{notice.stem}-LICENSE.txt").write_text(leading_comment(notice),
                                                                   encoding="utf-8")
    if "ffmpeg" in sources:
        shutil.copy2(sources["ffmpeg"] / "LICENSE.md", lic / "ffmpeg-LICENSE.md")
    # A part can contain files of other authors with their own notice terms,
    # for example an ISC file in libaribcaption. Each different notice is
    # written once, with the files that carry it.
    for key, _title, _var in FFMPEG_SOURCE_PARTS:
        if key == "ffmpeg" or key not in sources:
            continue
        by_text: dict[str, list[str]] = {}
        for path in sorted(sources[key].rglob("*")):
            if path.suffix in NOTICE_EXTENSIONS and path.is_file():
                comment = comment_text(path)
                if BINARY_NOTICE.search(comment):
                    by_text.setdefault(comment, []).append(
                        path.relative_to(sources[key]).as_posix())
        if by_text:
            text = "\n".join(f"==== {', '.join(files)} ====\n{comment}"
                             for comment, files in by_text.items())
            (lic / f"{key.replace('_', '-')}-NOTICES.txt").write_text(text, encoding="utf-8")
    # The MinGW FFmpeg links static libraries of MinGW (the MinGW-w64
    # runtime, winpthreads, zlib, the GCC runtime); some need their notice
    # in binaries.
    for rel in recipe.get("mingw_licenses", []):
        src = Path(os.environ["QTCI_MINGW_DIR"]) / "licenses" / rel
        if not src.is_file():
            die(f"{src} not found; the MinGW FFmpeg must ship this license text")
        shutil.copy2(src, lic / f"mingw-{rel.replace('/', '-')}")


# License terms that ask for their notice in binary copies (BSD, MIT, ISC).
BINARY_NOTICE = re.compile(r"Redistributions in binary form|shall be included in all copies"
                           r"|appear in all copies|copyright notice and this permission notice"
                           r"|Permission is hereby granted|Permission to use, copy, modify"
                           r"|Independent JPEG Group|copyright notice must be maintained",
                           re.IGNORECASE)
# Compiled FFmpeg files that are known to carry such notices: the scan must
# find each of them that a build compiles, or the build fails.
FFMPEG_KNOWN_NOTICES = ("libavcodec/speexdec.c", "libavformat/oggdec.c",
                        "libavcodec/jrevdct.c", "libavutil/x86/x86inc.asm")
NOTICE_EXTENSIONS = (".c", ".cc", ".cpp", ".h", ".hpp", ".asm", ".S", ".m", ".mm")


def comment_text(path: Path, lines: int = 300) -> str:
    """The comment blocks with a notice in the first lines of a source file.

    A notice can follow another comment block, for example the MIT notice
    of the Ogg demuxer. Block comments and runs of line comments (//, ;, #)
    count as blocks; a block without a notice is left out.
    """
    text = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[:lines])
    blocks = re.findall(r"/\*.*?\*/", text, re.DOTALL)
    run: list[str] = []
    for line in text.splitlines() + [""]:
        stripped = line.strip()
        if stripped.startswith(("//", ";")) or (
                stripped.startswith("#") and not re.match(r"#\s*[a-z]", stripped)):
            run.append(line)
        elif run:
            blocks.append("\n".join(run))
            run = []
    return "\n".join(b for b in blocks if BINARY_NOTICE.search(b))


FFMPEG_NOTICES = Path("licenses") / "ffmpeg-THIRD-PARTY-NOTICES.txt"


def compiled_ffmpeg_sources(build: Path, src: Path) -> list[str]:
    """The FFmpeg source files (and the headers that they include) of a build.

    Each object file names its source file; its dependency file (.d) names
    the files that the source includes, for example x86inc.asm.
    """
    src_root = src.resolve()
    found: set[str] = set()
    for obj in build.rglob("*.o"):
        stem = obj.relative_to(build).as_posix()[:-2]
        for ext in (".c", ".asm", ".S", ".cpp", ".m"):
            if (src / (stem + ext)).is_file():
                found.add(stem + ext)
                break
        dep = obj.with_suffix(".d")
        if not dep.is_file():
            continue
        for token in dep.read_text(encoding="utf-8", errors="replace").replace("\\\n", " ").split():
            token = token.rstrip(":")
            if not token or token == "\\" or token.endswith(".o"):
                continue
            if IS_WINDOWS and re.match(r"/[A-Za-z]/", token):
                token = f"{token[1]}:{token[2:]}"  # an MSYS path, /c/... -> c:/...
            path = Path(token) if Path(token).is_absolute() else build / token
            try:
                found.add(path.resolve().relative_to(src_root).as_posix())
            except (ValueError, OSError):
                continue  # a generated or a system file
    return sorted(found)


def write_ffmpeg_notices(build: Path, src: Path, out: Path) -> int:
    """Collect the notices of the FFmpeg source files that the build used.

    Most FFmpeg files are LGPL, but some carry BSD, MIT, or ISC terms that
    ask for their notice in binary copies (for example the Speex decoder or
    x86inc.asm).
    """
    notices = []
    found = []
    compiled = compiled_ffmpeg_sources(build, src)
    for rel in compiled:
        comment = comment_text(src / rel)
        if BINARY_NOTICE.search(comment):
            notices.append(f"==== {rel} ====\n{comment}")
            found.append(rel)
    missed = [rel for rel in FFMPEG_KNOWN_NOTICES if rel in compiled and rel not in found]
    if missed:
        die(f"the notice scan did not find the notices of {', '.join(missed)}")
    text = ("These FFmpeg source files are compiled into this build, or included by\n"
            "compiled files, and carry license terms other than the LGPL that ask for\n"
            "their notice in binary copies.\n\n")
    (out / FFMPEG_NOTICES).write_text(text + "\n".join(notices), encoding="utf-8")
    return len(notices)


# The DLLs of a Windows FFmpeg must not need these: GPL encoders, or the
# runtime DLLs of MinGW, which the kits do not contain.
FORBIDDEN_DLLS = re.compile(r"x264|x265|libwinpthread|libgcc|libstdc\+\+", re.IGNORECASE)
# The MSVC FFmpeg links the static runtime (/MT): a part built with /MD
# still links, with only a warning, and then needs the runtime DLLs.
MSVC_RUNTIME_DLLS = re.compile(r"vcruntime|msvcp|ucrtbase|api-ms-win-crt-", re.IGNORECASE)


def listed_names(output: str) -> set[str]:
    """Names in the list of "ffmpeg -encoders", "-decoders", "-demuxers" or "-muxers".

    The list starts after a line of dashes; each line is "<flags> <name>[,<name>] <text>".
    """
    names: set[str] = set()
    started = False
    for line in output.splitlines():
        if not started:
            started = line.strip().startswith("--")
            continue
        fields = line.split()
        if len(fields) >= 2:
            names.update(fields[1].split(","))
    return names


def check_ffmpeg_programs(recipe: dict, out: Path, ff_run=None) -> None:
    """Run the checks of the recipe on the ffmpeg program; write FFMPEG-CHECKS.txt.

    1. -version shows no --enable-gpl or --enable-nonfree; -L names the LGPL.
    2. -encoders, -decoders, -demuxers, and -muxers list what the recipe needs.
    3. Windows: dumpbin /dependents shows no GPL encoder and no MinGW runtime DLL.
    """
    checks = recipe.get("checks")
    if not checks:
        return
    ffmpeg = out / "bin" / ("ffmpeg.exe" if IS_WINDOWS else "ffmpeg")
    if not ffmpeg.is_file():
        die(f"{ffmpeg} not found; the checks need the ffmpeg program")
    report: list[str] = []
    errors: list[str] = []

    def ff(*args: str) -> str:
        result = (ff_run or subprocess.run)([str(ffmpeg), "-hide_banner", *args], capture_output=True,
                                text=True, errors="replace")
        report.extend([f"$ ffmpeg -hide_banner {' '.join(args)}", result.stdout.rstrip(), ""])
        if result.returncode != 0:
            errors.append(f"ffmpeg {' '.join(args)} failed ({result.returncode}): "
                          f"{result.stderr.strip()[-500:]}")
        return result.stdout

    version = ff("-version")
    for option in ("--enable-gpl", "--enable-nonfree"):
        if option in version:
            errors.append(f"ffmpeg -version shows {option}")
    license_text = ff("-L")
    if not re.search(r"GNU Lesser General Public\s+License", license_text) or \
            "GNU General Public License as published" in license_text:
        errors.append("ffmpeg -L does not name the GNU Lesser General Public License")
    for kind in ("encoders", "decoders", "demuxers", "muxers"):
        found = listed_names(ff(f"-{kind}"))
        missing = [name for name in checks.get(kind, []) if name not in found]
        if missing:
            errors.append(f"ffmpeg -{kind} does not list: {', '.join(missing)}")
    if IS_WINDOWS:
        env = msvc_environment()
        for binary in sorted((out / "bin").glob("*.dll")) + sorted((out / "bin").glob("*.exe")):
            dumpbin = shutil.which("dumpbin", path=env.get("PATH")) or die("dumpbin not found")
            text = capture([dumpbin, "/nologo", "/dependents", binary], env=env)
            deps = [line.strip() for line in text.splitlines()
                    if line.strip().lower().endswith(".dll")
                    and not line.strip().startswith("Dump of file")]
            report.extend([f"$ dumpbin /dependents {binary.name}", *deps, ""])
            bad = [d for d in deps if FORBIDDEN_DLLS.search(d) or
                   (recipe["target"] == "msvc" and MSVC_RUNTIME_DLLS.search(d))]
            if bad:
                errors.append(f"{binary.name} depends on {', '.join(bad)}")
    result = "FAILED:\n" + "\n".join(f"  {e}" for e in errors) if errors else "PASSED"
    (out / FFMPEG_CHECKS).write_text(
        f"FFmpeg checks for {recipe['target']}: {result}\n\n" + "\n".join(report) + "\n",
        encoding="utf-8")
    if errors:
        # The job does not upload the report of a failed check, so the log
        # must show it.
        log((out / FFMPEG_CHECKS).read_text(encoding="utf-8"))
        for error in errors:
            log(f"::error::{error}")
        die(f"the FFmpeg checks failed; {out / FFMPEG_CHECKS} has the output")
    log(f"FFmpeg checks passed; {out / FFMPEG_CHECKS} has the output")


def ffmpeg_manifest(out: Path) -> dict:
    """The SHA-256 of each file in a built FFmpeg, or the target of a
    symbolic link. FFMPEG-CHECKS.txt is not in it: each run writes it again."""
    files = {}
    for path in sorted(out.rglob("*")):
        rel = path.relative_to(out).as_posix()
        if rel in (FFMPEG_STAMP, FFMPEG_MANIFEST, FFMPEG_CHECKS):
            continue
        if path.is_symlink():
            files[rel] = "link:" + os.readlink(path)
        elif path.is_file():
            files[rel] = file_digest(path, "sha256")
    return files


def write_ffmpeg_stamp(out: Path, stamp: str) -> None:
    """Mark a complete FFmpeg: the manifest first, the stamp last."""
    (out / FFMPEG_MANIFEST).write_text(json.dumps(ffmpeg_manifest(out), sort_keys=True),
                                       encoding="utf-8")
    (out / FFMPEG_STAMP).write_text(stamp, encoding="utf-8")


def ffmpeg_complete(out: Path, stamp: str) -> bool:
    """True if out has the stamp of this recipe and every file of its manifest."""
    try:
        if (out / FFMPEG_STAMP).read_text(encoding="utf-8") != stamp:
            return False
        saved = json.loads((out / FFMPEG_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return saved == ffmpeg_manifest(out)


def cmd_build_ffmpeg(args) -> None:
    state = load_state(Path(args.state))
    cfg = load_version(state["qt_version"])
    kit = cfg["kits"][state["kit"]]
    recipe = state["deps"]["ffmpeg"]
    out = Path(args.out).resolve()
    work = Path(args.work).resolve()
    stamp = json.dumps(recipe, sort_keys=True)

    # A restored cache has the stamp of the recipe it was built from and the
    # manifest of its files. It must still pass the license gate and the checks.
    if ffmpeg_complete(out, stamp):
        check_ffmpeg_license(out)
        check_ffmpeg_programs(recipe, out)
        log(f"The FFmpeg in {out} matches the recipe; it is not built again")
        return
    if (out / FFMPEG_STAMP).exists():
        log(f"The FFmpeg in {out} is incomplete or from another recipe; it is built again")
    (out / FFMPEG_STAMP).unlink(missing_ok=True)

    rmtree(work)
    work.mkdir(parents=True)
    sources = {}
    for key, _title, _var in FFMPEG_SOURCE_PARTS:
        if key in recipe:
            archive = fetch_source(recipe[key], work / "downloads")
            sources[key] = extract_source(archive, work / f"src-{key}")

    # meson, ninja, and CMake in a virtual environment of their own: pip
    # must not change the packages of the Python that the runner shares
    # between jobs.
    venv = work / "tools-venv"
    run([sys.executable, "-m", "venv", venv])
    bindir = venv / ("Scripts" if IS_WINDOWS else "bin")
    suffix = ".exe" if IS_WINDOWS else ""
    run([bindir / f"python{suffix}", "-m", "pip", "install", "--quiet",
         f"meson=={recipe['meson']}", f"ninja=={recipe['ninja']}",
         f"cmake=={recipe['cmake']}"])

    target = recipe["target"]
    if target == "android":
        # The Android kits use a Windows host, but the FFmpeg job runs on
        # Linux, so the kit environment does not apply.
        env = dict(os.environ)
        env["ANDROID_NDK"] = str(ensure_ndk(android_sdk(), recipe["ndk"]))
        env["ANDROID_ABI"] = recipe["android_abi"]
        env["ANDROID_API"] = str(recipe["android_api"])
    else:
        if target == "mingw":
            setup_mingw(state["deps"], work)
        # The FFmpeg build does not use the Qt prefix, but the placeholders
        # of kit_environment ask for its root, which a self-hosted runner
        # must set. A directory in the work directory stands in for it.
        os.environ.setdefault("QTCI_PREFIX_ROOT", str(work / "no-prefix"))
        env = kit_environment(cfg, kit)
    # The kit flags are for Qt; they must not reach the builds of the parts
    # (meson and CMake read them). FFmpeg's configure script does not read them.
    for key in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
        env.pop(key, None)

    def path(p: Path) -> str:
        return str(p).replace("\\", "/")

    env.update({
        "FF_TARGET": target,
        "FF_WORK": path(work / "build"),
        "FF_OUT": path(out),
        "FF_CONFIGURE": "\n".join(recipe["configure"]),
        "FF_JOBS": str(os.cpu_count() or 4),
        "TOOLS_BIN": path(bindir),
    })
    for key, _title, var in FFMPEG_SOURCE_PARTS:
        if key in sources:
            env[var] = path(sources[key])
    script = ROOT / "tools" / "build-ffmpeg.sh"
    if IS_WINDOWS:
        # The FFmpeg configure script needs a POSIX shell: MSYS2, as in the
        # FFmpeg build of Qt. MINGW64 makes uname report a MinGW host.
        msys = Path(os.environ.get("QTCI_MSYS2_ROOT", r"C:\msys64"))
        bash = msys / "usr" / "bin" / "bash.exe"
        if not bash.is_file():
            die(f"MSYS2 bash not found at {bash}; set QTCI_MSYS2_ROOT to the MSYS2 directory")
        env["MSYSTEM"] = "MINGW64" if target == "mingw" else "MSYS"
        env["MSYS2_PATH_TYPE"] = "inherit"
        env["CHERE_INVOKING"] = "1"
        # MSYS2 tools before the Windows ones (find, sort), but the MinGW
        # compiler before an MSYS2 gcc. build-ffmpeg.sh puts cl.exe first.
        first = [str(msys / "usr" / "bin")]
        if target == "mingw":
            first.insert(0, str(Path(os.environ["QTCI_MINGW_DIR"]) / "bin"))
        env["PATH"] = os.pathsep.join(first + [env["PATH"]])
        cmd = [bash, "--noprofile", "--norc", path(script)]
    else:
        cmd = ["bash", script]
    run(cmd, env=env)

    check_ffmpeg_license(out)
    copy_ffmpeg_licenses(recipe, sources, out)
    count = write_ffmpeg_notices(work / "build" / "ffmpeg-build", sources["ffmpeg"], out)
    log(f"{count} compiled FFmpeg files carry notices; {out / FFMPEG_NOTICES} has them")
    (out / FFMPEG_SOURCE).write_text(ffmpeg_source_text(recipe), encoding="utf-8")
    check_ffmpeg_programs(recipe, out)
    write_ffmpeg_stamp(out, stamp)
    log(f"FFmpeg {recipe['ffmpeg']['version']} ({FFMPEG_LICENSE}) is in {out}")


def cmd_ffmpeg_status(args) -> None:
    """Set the output "complete": true if --out has the complete FFmpeg of
    the plan. A cache hit can restore an incomplete tree; then the build
    tools must be installed for the rebuild."""
    recipe = load_state(Path(args.state))["deps"]["ffmpeg"]
    complete = ffmpeg_complete(Path(args.out).resolve(), json.dumps(recipe, sort_keys=True))
    set_output("complete", "true" if complete else "false")
    log(f"The FFmpeg in {args.out} is {'complete' if complete else 'missing or incomplete'}")


def cmd_ffmpeg_source(args) -> None:
    """Write the source archive of the FFmpeg of a plan (the ffmpeg job of kit.yml)."""
    state = load_state(Path(args.state))
    recipe = state["deps"]["ffmpeg"]
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    write_ffmpeg_source_bundle(
        recipe, out_dir / f"ffmpeg-{recipe['ffmpeg']['version']}-{state['kit']}-source.tar")


def cmd_package_ffmpeg(args) -> None:
    """Make the release archive of a built FFmpeg, with its source bundle."""
    state = load_state(Path(args.state))
    cfg = load_version(state["qt_version"])
    recipe = state["deps"]["ffmpeg"]
    platform = cfg["toolchains"]["ffmpeg"]["release"].get(state["kit"]) or \
        die(f"kit {state['kit']} has no FFmpeg release (toolchains.ffmpeg.release)")
    src = Path(args.dir).resolve()
    if not ffmpeg_complete(src, json.dumps(recipe, sort_keys=True)):
        die(f"{src} is not the complete FFmpeg of this plan; run build-ffmpeg first")
    check_ffmpeg_license(src)
    if not (src / FFMPEG_CHECKS).is_file():
        die(f"{src / FFMPEG_CHECKS} not found; run build-ffmpeg first")
    if not (src / FFMPEG_CHECKS).read_text(encoding="utf-8").startswith(
            f"FFmpeg checks for {recipe['target']}: PASSED"):
        die(f"{src / FFMPEG_CHECKS} does not report passed checks")

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"ffmpeg-{recipe['ffmpeg']['version']}-lgpl-{platform}"
    archive = out_dir / (name + (".zip" if platform.startswith("win") else ".tar.xz"))
    archive.unlink(missing_ok=True)
    entries = sorted(p for p in src.rglob("*")
                     if p.relative_to(src).as_posix() not in (FFMPEG_STAMP, FFMPEG_MANIFEST))
    if archive.suffix == ".zip":
        import zipfile
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for p in entries:
                if p.is_file():
                    z.write(p, f"{name}/{p.relative_to(src).as_posix()}")
    else:
        # tarfile keeps the symbolic links of the shared libraries.
        with tarfile.open(archive, "w:xz") as tar:
            for p in entries:
                tar.add(p, f"{name}/{p.relative_to(src).as_posix()}", recursive=False)
    digest = file_digest(archive, "sha256")
    # LF on every host: "sha256sum -c" reads a CR as part of the file name.
    (out_dir / f"{archive.name}.sha256").write_text(f"{digest}  {archive.name}\n",
                                                    newline="\n")
    shutil.copy2(src / FFMPEG_CHECKS, out_dir / f"{name}-checks.txt")
    write_ffmpeg_source_bundle(recipe, out_dir / f"{name}-source.tar")
    log(f"{archive.name}: SHA-256 {digest}")
    gh_append("GITHUB_STEP_SUMMARY",
              f"### {archive.name}\n\n- SHA-256: `{digest}`\n- Checks: passed\n")


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------

def clone_module(cfg: dict, module: str, dest: Path) -> None:
    info = cfg["modules"].get(module) or cfg["extra_modules"][module]
    url = info.get("url") or f"{cfg['git_base']}/{module}.git"
    rmtree(dest)
    dest.mkdir(parents=True)
    run(["git", "init", "--quiet"], cwd=dest)
    run(["git", "remote", "add", "origin", url], cwd=dest)
    for attempt in range(1, 4):
        if run(["git", "fetch", "--quiet", "--depth", "1", "origin", info["sha"]],
               cwd=dest, check=False) == 0:
            break
        if attempt == 3:
            raise RuntimeError(f"git fetch failed for {module}")
        time.sleep(attempt * 15)
    run(["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=dest)
    if (dest / ".gitmodules").is_file():
        run(["git", "submodule", "update", "--init", "--recursive", "--depth", "1",
             "--jobs", "4"], cwd=dest)

    patch_dir = ROOT / "patches" / cfg["qt_version"] / module
    for patch in sorted(patch_dir.glob("*.patch")) if patch_dir.is_dir() else []:
        log(f"Applying {patch.relative_to(ROOT)}")
        run(["git", "apply", "--whitespace=nowarn", patch], cwd=dest)


def configure_module(cfg: dict, kit: dict, module: str, src: Path, build: Path,
                     env: dict) -> None:
    values = placeholders(cfg, kit)
    prefix = values["prefix"]
    common = expand(kit.get("cmake_args", []), values)
    rmtree(build)
    build.mkdir(parents=True)
    if module == "qtbase":
        qb = kit["qtbase"]
        qtbase_args = expand(qb.get("args", []), values)
        qtbase_cmake = expand(qb.get("cmake_args", []), values)
        if qb["mode"] == "configure":
            script = src / ("configure.bat" if IS_WINDOWS else "configure")
            cmd = [script, "-prefix", prefix] + qtbase_args + ["--"] + common + qtbase_cmake
        else:
            configs = kit["configs"]
            build_type = (f"-DCMAKE_CONFIGURATION_TYPES={';'.join(configs)}"
                          if len(configs) > 1 else f"-DCMAKE_BUILD_TYPE={configs[0]}")
            cmd = (["cmake", "-S", src, "-B", build, "-G", kit["generator"],
                    f"-DCMAKE_INSTALL_PREFIX={prefix}", build_type]
                   + qtbase_args + common + qtbase_cmake)
    else:
        qcm = Path(prefix) / "bin" / exe("qt-configure-module")
        info = cfg["modules"].get(module) or cfg.get("extra_modules", {}).get(module) or {}
        extra = (expand(info.get("cmake_args", []), values)
                 + expand(kit.get("module_cmake_args", {}).get(module, []), values))
        cmd = ([qcm, src] + kit.get("module_configure_args", []) + ["--"]
               + common + extra)
    run(cmd, cwd=build, env=env, log_path=build / CONFIGURE_LOG)


def build_with_retry(cmd: list, build: Path, env: dict) -> None:
    returncode = run(cmd, cwd=build, env=env, check=False)
    if returncode == 0:
        return
    # Close to the deadline there is no time for the retry. Stop as at the
    # deadline: the build directory is kept, and the next job retries.
    if COMMAND_DEADLINE is not None and COMMAND_DEADLINE - time.time() < 10 * 60:
        log(f"Build failed ({returncode}) less than 10 minutes before the job deadline; "
            "the next job retries it.")
        raise FailedNearDeadline(f"the build failed with exit code {returncode} and no "
                               f"time was left to retry it: {cmd[0]}")
    # A clean Ninja build can schedule a compile before a generated header is
    # written. A second, incremental run only retries the failed edges.
    log("Build failed; retrying once to rule out a generated-header race")
    try:
        run(cmd, cwd=build, env=env)
    except DeadlineExceeded:
        # The retry was making progress when the deadline stopped it, so this
        # is a deadline stop: the build directory is kept and continued.
        log(f"The first build attempt failed ({returncode}); the retry reached the "
            "job deadline.")
        raise


def jobs_for(kit: dict, module: str) -> int:
    jobs = int(os.environ.get("QTCI_JOBS") or os.cpu_count() or 4)
    limit = kit.get("module_jobs", {}).get(module)
    return min(jobs, limit) if limit else jobs


def install_module(kit: dict, build: Path, env: dict) -> None:
    if kit["generator"] == "Ninja Multi-Config":
        for config in kit["configs"]:
            run(["cmake", "--install", ".", "--config", config], cwd=build, env=env)
    else:
        run(["cmake", "--install", "."], cwd=build, env=env)


def has_docs_target(build: Path) -> bool:
    rx = re.compile(r"^build docs(:|\s)", re.MULTILINE)
    for ninja in build.glob("*.ninja"):
        if rx.search(ninja.read_text(encoding="utf-8", errors="replace")):
            return True
    return False


RESUME_MARKER = ".qtci-resume.json"
CONFIGURE_LOG = ".qtci-configure.log"


def qt_skipped_build(build: Path) -> bool:
    """Return True if the configure step said that Qt skips this module here.

    Qt modules that a platform does not support (for example qtwayland on
    Windows and Android) end their configure step with this standard message,
    and then build and install nothing.
    """
    try:
        text = (build / CONFIGURE_LOG).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "Skipping the build as the condition" in text

# Inputs of the current stage that a configure step depends on, besides the
# files in this repository: the kit, the pinned dependencies, the modules in
# the prefix, and the compiler. cmd_stage sets it before each item.
RESUME_CONTEXT: dict = {}

# True while run_build_item continues a kept build directory.
RESUMED_BUILD = False


def toolchain_id(kit: dict, env: dict) -> str:
    """Return a string that changes when the compiler of the kit changes."""
    if kit["toolchain"] == "msvc":
        return f"msvc {env.get('VCTOOLSVERSION', '')} sdk {env.get('WINDOWSSDKVERSION', '')}"
    if kit["toolchain"] == "android":
        return "android"  # the NDK version is in the pinned dependencies
    compiler = env.get("CXX") or ("g++" if kit["toolchain"] == "mingw" else "c++")
    path = shutil.which(compiler, path=env.get("PATH"))
    if not path:
        return f"{compiler} not found"
    try:
        out = subprocess.run([path, "--version"], env=env, capture_output=True,
                             text=True, timeout=60).stdout
        ident = out.splitlines()[0] if out else f"{compiler} unknown"
        if sys.platform == "darwin":
            sdk = subprocess.run(["xcrun", "--show-sdk-version"], env=env,
                                 capture_output=True, text=True, timeout=60).stdout
            ident += f" sdk {sdk.strip()}"
    except (OSError, subprocess.SubprocessError):
        return f"{compiler} unknown"
    return ident


HOST_ID_FILE = ".qtci-host-id"


def host_qt_id() -> str:
    """Return the identity of the host Qt (Android kits), written by 'restore'."""
    host = os.environ.get("QTCI_HOST_QT")
    if not host:
        return ""
    try:
        return (Path(host) / HOST_ID_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def resume_key(cfg: dict, module: str, prefix: Path) -> dict:
    """Identify a build directory that a later job can continue.

    The key covers everything that the configure step used: the module
    commit, the patches, the configuration file, the prefix path, and
    RESUME_CONTEXT (kit, dependencies, finished modules, and compiler).
    """
    info = cfg["modules"].get(module) or cfg["extra_modules"][module]
    digest = hashlib.sha256()
    # All patches of the Qt version (other modules are in the prefix), the
    # version file, and this driver.
    patch_root = ROOT / "patches" / cfg["qt_version"]
    for patch in sorted(patch_root.rglob("*.patch")) if patch_root.is_dir() else []:
        digest.update(patch.relative_to(patch_root).as_posix().encode())
        digest.update(patch.read_bytes())
    digest.update((ROOT / "versions" / f"{cfg['qt_version']}.json").read_bytes())
    digest.update(Path(__file__).read_bytes())
    digest.update(json.dumps(RESUME_CONTEXT, sort_keys=True).encode())
    return {"module": module, "sha": info["sha"], "inputs": digest.hexdigest(),
            "prefix": str(prefix)}


def can_resume(cfg: dict, module: str, prefix: Path) -> bool:
    work = work_dir()
    marker = work / "b" / module / RESUME_MARKER
    try:
        saved = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return saved == resume_key(cfg, module, prefix) and (work / "s" / module).is_dir()


def remove_work(module: str) -> None:
    """Delete the work directories of a module, the resume marker first.

    If a locked file stops the deletion, the directory is no longer
    resumable, so a later job cannot continue a half-deleted build.
    """
    work = work_dir()
    try:
        (work / "b" / module / RESUME_MARKER).unlink(missing_ok=True)
    except OSError as exc:
        log(f"::warning::could not delete the resume marker of {module}: {exc}")
    for path in (work / "b" / module, work / "b" / f"{module}-docs", work / "s" / module):
        try_rmtree(path)


def run_build_item(cfg: dict, kit: dict, module: str, env: dict,
                   keep_work: bool = False, before: set[str] | None = None) -> bool:
    """Build and install a module. Return True if Qt skipped it on this platform.

    'before' is the list of prefix paths before the install. Qt skipped the
    module only if its configure output has the skip message and the
    install added no module file: an example or a subdirectory can print
    the same message.
    """
    global RESUMED_BUILD
    work = work_dir()
    src, build = work / "s" / module, work / "b" / module
    values = placeholders(cfg, kit)
    prefix = Path(values["prefix"])
    RESUMED_BUILD = can_resume(cfg, module, prefix)
    if RESUMED_BUILD:
        # An earlier job on this runner stopped or failed this build after
        # its configure step. Ninja continues it; outputs of killed commands
        # are rebuilt.
        log(f"Continuing the build of {module} in {build}")
    else:
        # A marker that is left without its build (a job cancelled during
        # the clone) must not make a later job continue a partial source.
        (build / RESUME_MARKER).unlink(missing_ok=True)
        clone_module(cfg, module, src)
        configure_module(cfg, kit, module, src, build, env)
        (build / RESUME_MARKER).write_text(
            json.dumps(resume_key(cfg, module, prefix)), encoding="utf-8")
    # With Ninja Multi-Config, Qt sets CMAKE_DEFAULT_CONFIGS to "all"
    # (qt_auto_detect_cmake_config), so this builds every configuration that
    # install_module installs.
    build_with_retry(["cmake", "--build", ".", "--parallel", jobs_for(kit, module)],
                     build, env)
    install_module(kit, build, env)
    added = prefix_paths(prefix) - (before or set())
    skipped = (qt_skipped_build(build)
               and not any(is_module_file(prefix, p.replace(os.sep, "/")) for p in added))
    if skipped:
        log(f"::notice::Qt does not build {module} on this platform")

    for src_pattern, dest_rel in kit.get("post_install", {}).get(module, []):
        expanded = expand([src_pattern], values)
        paths = []
        for item in expanded:
            if any(c in Path(item).name for c in "*?["):
                # A glob in the file name, for example the license files of
                # FFmpeg, which differ per target. It must match a file.
                paths += sorted(p for p in Path(item).parent.glob(Path(item).name)
                                if p.is_file()) or [Path(item)]
            else:
                paths.append(Path(item))
        for path in paths:
            if path.is_file():
                dest = prefix / dest_rel
                dest.mkdir(parents=True, exist_ok=True)
                # Copy the file content, so that a soname symlink such as
                # libavfilter.so.10 becomes a real file in the prefix.
                shutil.copy2(path, dest / path.name)
                log(f"Copied {path} to {dest}")
            else:
                raise InstallCheckFailed(f"{path} not found; the post-install copy of {module} "
                                   "needs it")

    if not skipped:
        copy_qt_attributions(module, src, prefix)
        info = cfg["modules"].get(module) or cfg["extra_modules"].get(module, {})
        if info.get("third_party_licenses"):
            count = copy_third_party_licenses(module, src, info["third_party_licenses"], prefix)
            log(f"Copied {count} third-party license files of {module}")

    if kit.get("copy_example_sources") and not skipped and (src / "examples").is_dir():
        # Same as the local scripts: merge every module's examples/ directory
        # into <prefix>/examples, as the official installer does.
        shutil.copytree(src / "examples", prefix / "examples", dirs_exist_ok=True)
    if not keep_work:
        remove_work(module)
    return skipped


def copy_qt_attributions(module: str, src: Path, prefix: Path) -> int:
    """Copy the license files of the third-party code of a Qt module.

    Each qt_attribution.json names third-party code in the module and its
    license file. The docs show them too, but a kit can be built without
    docs. Test code is left out.
    """
    dest = prefix / "licenses" / "qt" / module
    count = 0
    # The license texts of the module itself: the LGPL and GPL for the code,
    # the GFDL for the docs, BSD-3-Clause for the examples, and the Qt GPL
    # exception for the tools.
    if (src / "LICENSES").is_dir():
        for text in sorted((src / "LICENSES").glob("*.txt")):
            (dest / "LICENSES").mkdir(parents=True, exist_ok=True)
            shutil.copy2(text, dest / "LICENSES" / text.name)
            count += 1
    copyrights = []
    for attribution in sorted(src.rglob("qt_attribution.json")):
        rel = attribution.relative_to(src).as_posix()
        if "/tests/" in f"/{rel}":
            continue
        try:
            # strict=False: some Qt files have a raw line break inside a
            # string (qtbase src/3rdparty/forkfd).
            data = json.loads(attribution.read_text(encoding="utf-8"), strict=False)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"{module}: cannot read {rel}: {exc}") from exc
        for entry in data if isinstance(data, list) else [data]:
            files = entry.get("LicenseFiles") or (
                [entry["LicenseFile"]] if entry.get("LicenseFile") else [])
            for name in files:
                path = attribution.parent / name
                if not path.is_file():
                    log(f"::warning::{module}: {rel} names {name}, which does not exist")
                    continue
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest / f"{entry.get('Id', 'unknown')}-{path.name}")
                count += 1
            # A generic license text has no copyright line; the attribution has.
            copyrights.append(f"{entry.get('Name', entry.get('Id', 'unknown'))}: "
                              f"{entry.get('License', entry.get('LicenseId', ''))}\n"
                              f"{entry.get('Copyright', '')}".rstrip() + "\n")
    if copyrights:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "ATTRIBUTIONS.txt").write_text("\n".join(copyrights), encoding="utf-8")
    return count


LICENSE_FILE = re.compile(r"(LICENSE|LICENCE|COPYING|NOTICE)([._-].*)?", re.IGNORECASE)


def copy_third_party_licenses(module: str, src: Path, dirs: list[str], prefix: Path) -> int:
    """Copy every license file under the given directories of a module.

    Chromium in Qt WebEngine records its third-party code in README.chromium
    and LICENSE files, not in qt_attribution.json. The paths are kept, so
    each license stays next to the name of its code.
    """
    count = 0
    for rel_dir in dirs:
        root = src / rel_dir
        if not root.is_dir():
            raise RuntimeError(f"{module}: {rel_dir} (third_party_licenses) does not exist")
        for path in sorted(root.rglob("*")):
            if path.is_file() and LICENSE_FILE.fullmatch(path.name):
                dest = prefix / "licenses" / "qt" / module / "third_party" / \
                    path.relative_to(src)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
                count += 1
    return count


def has_doc_sources(src: Path) -> bool:
    """Return True if the module source has a qdoc configuration (.qdocconf)."""
    for root, dirs, files in os.walk(src):
        # Third-party code (Chromium in QtWebEngine) is large and has no
        # Qt documentation.
        dirs[:] = [d for d in dirs if d not in ("3rdparty", ".git")]
        if any(f.endswith(".qdocconf") for f in files):
            return True
    return False


def run_docs_item(cfg: dict, kit: dict, module: str, env: dict,
                  qt_skipped: bool = False) -> None:
    """Build and install the docs of a module.

    qt_skipped is True if the build item found that Qt skips the module on
    this platform; such a module has no docs target.
    """
    work = work_dir()
    src, build = work / "s" / module, work / "b" / f"{module}-docs"
    # A build directory that this job kept (webengine pool) needs its source
    # directory. The source is the same checkout, so the docs use it as is.
    kept_source = (work / "b" / module / RESUME_MARKER).is_file() and src.is_dir()
    if not kept_source:
        clone_module(cfg, module, src)
    configure_module(cfg, kit, module, src, build, env)
    if has_docs_target(build):
        config = ["--config", kit["configs"][0]] \
            if kit["generator"] == "Ninja Multi-Config" else []
        # qdoc loads all headers through libclang and needs several GB of
        # memory per process, so docs use few parallel jobs.
        jobs = min(int(os.environ.get("QTCI_DOC_JOBS", "2")), jobs_for(kit, module))
        run(["cmake", "--build", ".", "--target", "docs", "--parallel", jobs] + config,
            cwd=build, env=env)
        run(["cmake", "--build", ".", "--target", "install_docs"] + config,
            cwd=build, env=env)
    elif qt_skipped:
        # Qt does not build this module on this platform (see
        # run_build_item), so it has no docs target either.
        log(f"::warning::Qt does not build {module} on this platform, so it has no docs")
    elif has_doc_sources(src):
        # The module has documentation, so the docs target is missing because
        # qdoc or libclang is missing. Without this error, the package would
        # silently have no docs.
        raise RuntimeError(f"{module} has .qdocconf files but no docs target; check "
                           "LLVM_INSTALL_DIR and qdoc")
    else:
        log(f"{module} has no documentation")
    try_rmtree(build)
    if not kept_source:
        try_rmtree(src)


# --------------------------------------------------------------------------
# stage
# --------------------------------------------------------------------------

def prefix_paths(prefix: Path) -> set[str]:
    """Return the relative paths of all files, links, and directories in the prefix."""
    paths = set()
    for root, dirs, files in os.walk(prefix):
        for name in dirs + files:
            paths.add(os.path.relpath(os.path.join(root, name), prefix))
    return paths


# Files that the Qt build system installs for every repository, also when
# the configure step disabled all of its targets, and the examples that this
# tool copies. They do not show that the module was built.
INFRASTRUCTURE_PREFIXES = ("examples/", "lib/cmake/Qt6BuildInternals/", "mkspecs/",
                           "metatypes/", "sbom/", "lib/metatypes/", "lib/sbom/",
                           # Written by this tool, not by the install of a module:
                           # they must not make an empty install pass the check.
                           "licenses/", "vivace-patches/", "vivace-build/",
                           "BUILDINFO-vivace.txt")


def is_module_file(prefix: Path, rel: str) -> bool:
    """Return True if an installed path is a file of the module itself."""
    if not ((prefix / rel).is_file() or (prefix / rel).is_symlink()):
        return False
    if rel.startswith(INFRASTRUCTURE_PREFIXES):
        return False
    # Find modules of the repository (lib/cmake/Qt6/FindWrap*.cmake and similar).
    return not fnmatch.fnmatchcase(rel, "lib/cmake/Qt6/Find*.cmake")


def check_installed(cfg: dict, kit: dict, module: str, prefix: Path, before: set[str],
                    done: list, qt_skipped: bool = False) -> None:
    """Fail if the install of a module did not add what it must add.

    A configure step can disable a whole module (for example QtWebEngine
    without one of its tools) and still succeed. Two checks catch this:
    - The version file lists path patterns ("installs", "installs_if" for
      another built module, and "module_installs" of the kit).
    - A module that installs nothing (examples aside) fails, unless Qt said
      that it skips the module on this platform (qt_skipped), or the version
      file marks the module with "installs_nothing" (for all kits) or lists
      it in the "installs_nothing" list of the kit.
    """
    info = cfg["modules"].get(module) or cfg.get("extra_modules", {}).get(module) or {}
    added = [p.replace(os.sep, "/") for p in prefix_paths(prefix) - before]
    patterns = list(info.get("installs", []))
    patterns += kit.get("module_installs", {}).get(module, [])
    for other, extra in info.get("installs_if", {}).items():
        if f"build:{other}" in done:
            patterns += extra
    for pattern in patterns:
        if not any(fnmatch.fnmatchcase(p, pattern) for p in added):
            raise InstallCheckFailed(
                f"the install of {module} added nothing that matches {pattern}. The "
                "configure step probably left out a part of the module; read its output")
    if not any(is_module_file(prefix, p) for p in added):
        if (qt_skipped or info.get("installs_nothing")
                or module in kit.get("installs_nothing", [])):
            log(f"::warning::{module} installed nothing on this platform")
        else:
            raise InstallCheckFailed(
                f"the install of {module} added nothing. Its configure step left the "
                "module out, probably because a dependency is missing; read its output")


def remove_new_paths(prefix: Path, before: set[str]) -> int:
    """Delete what was added to the prefix after 'before' was taken. Return the file count."""
    return remove_paths(prefix, prefix_paths(prefix) - before)


def remove_paths(prefix: Path, paths) -> int:
    """Delete the given relative paths in the prefix. Return the file count."""
    count = 0
    # Deepest paths first, so that a new directory is empty when it is removed.
    rels = {p.replace(os.sep, "/") for p in paths}
    for rel in sorted(rels, key=lambda p: p.count("/"), reverse=True):
        path = prefix / rel
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_dir() and not path.is_symlink():
            rmtree(path)
        else:
            path.unlink(missing_ok=True)
            count += 1
    return count


# True after a signal raised SystemExit. The process is then stopping.
SIGNAL_RAISED = False


def raise_system_exit(signum, _frame) -> None:
    """Turn the first signal into SystemExit; ignore later signals.

    A second signal (the runner sends SIGINT, then SIGTERM) can come before
    the cleanup of the first one starts. Raised again, it would skip that
    cleanup (kill_tree in run()), and the compilers would keep running.
    """
    global SIGNAL_RAISED
    if SIGNAL_RAISED:
        return
    SIGNAL_RAISED = True
    raise SystemExit(128 + signum)


def cmd_stage(args) -> None:
    global COMMAND_DEADLINE, RESUME_CONTEXT
    # Check the directory settings before any work (a self-hosted runner
    # must set them).
    work_dir()
    default_prefix_root()
    state_dir = Path(args.state)
    state = load_state(state_dir)
    cfg = load_version(state["qt_version"])
    kit = cfg["kits"][state["kit"]]
    prefix = kit_prefix(cfg, kit)
    deadline = float(args.deadline)
    pool = args.pool
    if pool != MAIN_POOL:
        left_main = remaining_items(state, MAIN_POOL)
        if left_main:
            die(f"{len(left_main)} items of the stage jobs are not built (first: "
                f"{item_id(left_main[0])}). The {pool} job starts only after them.")

    if not restore_prefix(state_dir, prefix):
        if state["done"]:
            die(f"{state_dir} lists {len(state['done'])} finished items but has no saved "
                "prefix. The state artifact is incomplete; start a new run.")
        log("No saved prefix: this is the first stage")
    env = kit_environment(cfg, kit)
    RESUME_CONTEXT = {"kit": state["kit"], "deps": state["deps"],
                      "toolchain": toolchain_id(kit, env), "host_qt": host_qt_id()}

    def update_resume_context() -> None:
        RESUME_CONTEXT["done"] = sorted(d for d in state["done"] if d.startswith("build:"))

    # Sources and build directories that an earlier job left on a self-hosted
    # runner can be large (QtWebEngine: about 100 GB). Keep only a build that
    # the first item of this job can continue; delete everything else.
    update_resume_context()
    first = next(iter(remaining_items(state, pool)), None)
    keep = (first["module"] if first and first["kind"] == "build"
            and can_resume(cfg, first["module"], prefix) else None)
    # The cleanup deletes every child of <work>/b and <work>/s, so it runs
    # only in a work directory that this tool created.
    work = work_dir()
    if (work.is_dir() and not (work / WORK_MARKER).is_file()
            and any(work.iterdir())):
        die(f"{work} exists and was not made by this tool. The tool does not delete "
            "files in it. Set QTCI_WORK to a different directory.")
    work.mkdir(parents=True, exist_ok=True)
    (work / WORK_MARKER).write_text("Created by vivace-qt-build tools/qtci.py\n")
    for sub in ("b", "s"):
        root = work_dir() / sub
        for path in (root.iterdir() if root.is_dir() else []):
            if path.name != keep:
                try:
                    (path / RESUME_MARKER).unlink(missing_ok=True)
                except OSError:
                    pass
                try_rmtree(path)

    # Estimates are multiplied by a calibration factor that rises when items
    # take longer than estimated, so that later stages stop earlier and do not
    # reach the job timeout (which would lose the work of the whole stage).
    # Each pool runs on its own kind of runner, so each has its own factor.
    calibration_key = "calibration" if pool == MAIN_POOL else f"calibration_{pool}"
    calibration = float(state.get(calibration_key, 1.0))
    # The stage time is measured from the job start (QTCI_STAGE_START), so a
    # slow setup does not make a normal first item look too long.
    stage_start = float(os.environ.get("QTCI_STAGE_START") or time.time())
    budget = deadline - stage_start
    todo = remaining_items(state, pool)
    ran: list[tuple[str, float]] = []
    leftover: list[str] = []
    failed = None
    for item in todo:
        iid = item_id(item)
        base = estimate_minutes(cfg, kit, item["kind"], item["module"]) * 60
        estimate = base * calibration
        left = deadline - time.time()
        if ran and estimate > left:
            log(f"Stopping: {iid} needs about {estimate / 60:.0f} min, "
                f"{left / 60:.0f} min left in this job")
            break
        # The fail-fast check uses the uncalibrated estimate: a high
        # calibration must not make a buildable item fail on every re-run.
        # Estimates are for a 4-core GitHub-hosted runner, so the check is
        # for the main pool and for any GitHub-hosted runner. A self-hosted
        # runner of the webengine pool can be much faster.
        hosted = os.environ.get("QTCI_GITHUB_HOSTED") == "1"
        if not ran and (pool == MAIN_POOL or hosted) and base > budget:
            die(f"{iid} needs about {base / 60:.0f} min, but a stage has only "
                f"{budget / 60:.0f} min. Set the QTCI_STAGE_MINUTES variable to a larger "
                "value and use a runner without the 6-hour limit.")
        if not ran and estimate > budget:
            log(f"::warning::{iid} needs about {estimate / 60:.0f} min, but a stage has "
                f"only {budget / 60:.0f} min. The job can reach its timeout.")
        group(f"{iid} (estimate {estimate / 60:.0f} min)")
        start = time.time()
        before = prefix_paths(prefix)
        update_resume_context()
        COMMAND_DEADLINE = deadline
        try:
            if item["kind"] == "build":
                # The webengine pool runs one long job on a self-hosted runner.
                # A finished build directory is kept (with its resume marker)
                # until the next job starts. If the state upload of this job
                # fails, the re-run continues it: Ninja has nothing to build,
                # so only the install runs again.
                qt_skipped = run_build_item(cfg, kit, item["module"], env,
                                            keep_work=pool != MAIN_POOL, before=before)
                check_installed(cfg, kit, item["module"], prefix, before, state["done"],
                                qt_skipped=qt_skipped)
                if qt_skipped:
                    state.setdefault("qt_skipped", []).append(item["module"])
            else:
                run_docs_item(cfg, kit, item["module"], env,
                              qt_skipped=item["module"] in state.get("qt_skipped", []))
        except Exception as exc:  # noqa: BLE001 - report and save progress
            COMMAND_DEADLINE = None
            elapsed = time.time() - start
            stopped = isinstance(exc, DeadlineExceeded)
            # The saved prefix must not contain a partial install of the
            # failed item, because the re-run installs it again on top. A
            # cleanup error only warns: the items that finished before this
            # one must still be saved.
            try:
                removed = remove_new_paths(prefix, before)
                if removed:
                    log(f"Removed {removed} files that the failed item installed")
            except OSError as cleanup_exc:
                # The saved prefix leaves these paths out, so the re-run
                # installs the item into a prefix without them.
                leftover = sorted(prefix_paths(prefix) - before)
                log(f"::warning::could not remove the partial install of {iid}: "
                    f"{cleanup_exc}. The saved prefix leaves out the "
                    f"{len(leftover)} paths that are left.")
            # A configured build directory is kept, so that a later job on the
            # same (self-hosted) runner continues it (see run_build_item).
            # Exception: a continued build that failed for another reason than
            # the deadline is deleted, so that the next attempt starts clean.
            resumable = (item["kind"] == "build"
                         and not isinstance(exc, InstallCheckFailed)
                         and can_resume(cfg, item["module"], prefix)
                         and (stopped or not RESUMED_BUILD))
            if resumable:
                try_rmtree(work_dir() / "b" / f"{item['module']}-docs")
            elif (item["kind"] == "docs"
                  and (work_dir() / "b" / item["module"] / RESUME_MARKER).is_file()):
                # A failed docs item keeps the build directory that this job
                # kept for the module (webengine pool), and its source.
                try_rmtree(work_dir() / "b" / f"{item['module']}-docs")
            else:
                remove_work(item["module"])
            endgroup()
            if stopped:
                # The re-run starts with this item, with the whole job time.
                # A higher calibration makes later stages stop earlier.
                if base >= 15 * 60 and not isinstance(exc, FailedNearDeadline):
                    calibration = max(calibration, min(max(elapsed / base, 1.0), 3.0))
                    state[calibration_key] = round(calibration, 3)
                if ran:
                    # Like a normal stop: the next job starts with this item,
                    # with the whole job time. The last job of a pool still
                    # fails through --fail-if-remaining.
                    log(f"::warning::Stopped {iid} at the deadline of this job after "
                        f"{elapsed / 60:.0f} min ({exc}); the next job builds it again.")
                elif isinstance(exc, FailedNearDeadline):
                    failed = (f"{iid}: {exc}. The build failed less than 10 minutes before "
                              "the deadline, so it was not retried in this job. Re-run the "
                              "failed job; if it fails again, read the build output.")
                elif resumable and not hosted:
                    failed = (f"{iid} did not finish before the deadline of this job "
                              f"({elapsed / 60:.0f} min; {exc}). The build directory is kept on "
                              "this runner. Re-run the failed job: on the same "
                              "self-hosted runner, the build continues where it stopped.")
                else:
                    failed = (f"{iid} did not finish before the deadline of this job "
                              f"({elapsed / 60:.0f} min; {exc}). It was the first item of this "
                              "job, so it needs more job time: set QTCI_STAGE_MINUTES or "
                              "QTCI_WEBENGINE_MINUTES to a larger value and start a new run.")
            else:
                kept = (" The build directory is kept on this runner; a re-run on the "
                        "same self-hosted runner continues it."
                        if resumable and not hosted else "")
                failed = f"{iid}: {exc}.{kept}"
            break
        COMMAND_DEADLINE = None
        endgroup()
        elapsed = time.time() - start
        state["done"].append(iid)
        state["durations"][iid] = round(elapsed)
        # Only long items give a reliable ratio; short ones are mostly clone
        # time. A running average (between 1 and 3) lets one slow item raise
        # the estimates without keeping them high for the rest of the build.
        if base >= 15 * 60:
            ratio = min(max(elapsed / base, 1.0), 3.0)
            calibration = max(1.0, (calibration + ratio) / 2)
            state[calibration_key] = round(calibration, 3)
        ran.append((iid, elapsed))
        save_state(state_dir, state)
        log(f"Finished {iid} in {elapsed / 60:.1f} min")

    # Save only when an item finished. Otherwise the saved prefix from the
    # previous stage is still the correct state.
    if ran:
        # The saved prefix is uploaded as an artifact: like a kit archive, it
        # must carry the directions to the source of its Qt libraries.
        # package writes the file again, so an error here must not lose
        # the items of this stage.
        try:
            write_buildinfo(cfg, state, prefix)
        except OSError as e:
            log(f"::warning::could not write BUILDINFO-vivace.txt into the saved prefix: {e}")
        pack_prefix(prefix, state_dir, exclude=leftover)
    save_state(state_dir, state)
    left_items = remaining_items(state, pool)
    set_output("remaining", str(len(left_items)))
    set_output("saved", "true" if ran else "false")

    rows = "".join(f"| {iid} | {sec / 60:.1f} |\n" for iid, sec in ran)
    gh_append("GITHUB_STEP_SUMMARY",
              f"### Stage results for {state['kit']}\n\n| Item | Minutes |\n|---|---|\n{rows}\n"
              f"Items left: {len(left_items)}\n")
    if failed:
        saved_note = ("The items before it are saved." if ran
                      else "This job finished no item, so nothing new is saved.")
        die(f"{failed.rstrip('.')}. {saved_note} Re-run the failed job to retry "
            "from this item.")
    # The last job of a pool has no next job to continue. It fails, so that
    # "Re-run failed jobs" runs it again and it continues from the saved state.
    if args.fail_if_remaining and left_items:
        die(f"{len(left_items)} items are left (first: {item_id(left_items[0])}) and no "
            "later job builds them. Progress is saved; re-run the failed job to continue.")


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------

def cmd_restore(args) -> None:
    state_dir = Path(args.state)
    state = load_state(state_dir)
    cfg = load_version(state["qt_version"])
    kit = cfg["kits"][state["kit"]]
    if remaining_items(state):
        die(f"the {state['kit']} build in {state_dir} is not complete")
    if args.qt_version and state["qt_version"] != args.qt_version:
        die(f"the {state['kit']} state in {state_dir} is Qt {state['qt_version']}, "
            f"not Qt {args.qt_version}. Use a host_run_id of a run that built Qt "
            f"{args.qt_version}.")
    prefix = kit_prefix(cfg, kit)
    host_id = file_digest(state_dir / "state.json", "sha256")
    if not restore_prefix(state_dir, prefix):
        die(f"no saved prefix in {state_dir}")
    # Identifies this build of the host Qt; part of the resume key of the
    # kits that use it (see host_qt_id).
    (prefix / HOST_ID_FILE).write_text(host_id + "\n", encoding="utf-8")
    set_output("prefix", str(prefix))
    if args.env:
        set_env(args.env, str(prefix))


# --------------------------------------------------------------------------
# package
# --------------------------------------------------------------------------

def write_buildinfo(cfg: dict, state: dict, prefix: Path) -> None:
    lines = [
        f"Qt {cfg['qt_version']} ({state['kit']}), built by vivace-qt-build",
        f"Built: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
    ]
    server, repo, run_id = (os.environ.get(k, "") for k in
                            ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    if run_id:
        lines.append(f"Workflow run: {server}/{repo}/actions/runs/{run_id}")
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        lines.append(f"vivace-qt-build commit: {sha}")
    lines += ["", "Source code: each module is the git repository below at the commit",
              "below, with the patches in vivace-patches/ applied. vivace-build/ has the",
              "scripts and the configuration that built the kit.",
              "", "Modules (repository and git commit):"]
    for item in state["items"]:
        if item["kind"] == "build":
            m = item["module"]
            info = cfg["modules"].get(m) or cfg["extra_modules"][m]
            url = info.get("url") or f"{cfg['git_base']}/{m}.git"
            lines.append(f"  {m} {url} {info['sha']}")
    lines += ["", "Patches (SHA-256), also in vivace-patches/:"]
    patch_root = ROOT / "patches" / cfg["qt_version"]
    for patch in sorted(patch_root.rglob("*.patch")):
        lines.append(f"  {patch.relative_to(patch_root).as_posix()} "
                     f"{file_digest(patch, 'sha256')}")
    lines += ["", "Dependencies:", json.dumps(state["deps"], indent=2)]
    (prefix / "BUILDINFO-vivace.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    dest = prefix / "vivace-patches"
    rmtree(dest)
    if patch_root.is_dir():  # a Qt version can have no patches
        shutil.copytree(patch_root, dest)
        # The patches' own license (patches/LICENSE + patches/LICENSES/)
        # sits above the per-version directory, so copy it in as well.
        shutil.copy2(ROOT / "patches" / "LICENSE", dest / "LICENSE")
        shutil.copytree(ROOT / "patches" / "LICENSES", dest / "LICENSES")

    # The scripts that control the build are part of the source of the Qt
    # libraries (LGPL v3 / GPL v3, "Corresponding Source"). They are under
    # the GPL v3 or later (LICENSE).
    build = prefix / "vivace-build"
    rmtree(build)
    (build / "tools").mkdir(parents=True)
    (build / "versions").mkdir()
    for rel in ("tools/qtci.py", "tools/build-ffmpeg.sh", "tools/gen-kit-workflow.py",
                "LICENSE"):
        shutil.copy2(ROOT / rel, build / rel)
    # The workflows and actions set up the build environment.
    if (ROOT / ".github").is_dir():
        shutil.copytree(ROOT / ".github", build / ".github")
    shutil.copy2(ROOT / "versions" / f"{cfg['qt_version']}.json",
                 build / "versions" / f"{cfg['qt_version']}.json")


def write_ffmpeg_source_bundle(recipe: dict, bundle: Path) -> None:
    """Write the FFmpeg sources of the package into one tar file.

    The LGPL asks that the source is offered from the same place as the
    libraries; the publish job uploads this file next to the archive.
    """
    tmp = bundle.with_name(bundle.name + ".d")
    rmtree(tmp)
    tmp.mkdir(parents=True)
    archives = [fetch_source(recipe[key], tmp) for key, _title, _var in FFMPEG_SOURCE_PARTS
                if key in recipe]
    (tmp / "FFMPEG-SOURCE.txt").write_text(ffmpeg_source_text(recipe), encoding="utf-8")
    # The scripts that control the build are part of the source (LGPL 2.1,
    # section 0). The recipe has every input of the build.
    (tmp / "recipe.json").write_text(json.dumps(recipe, indent=2) + "\n", encoding="utf-8")
    server, repo, run_id, sha = (os.environ.get(k, "") for k in (
        "GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_SHA"))
    info = ["vivace-qt-build built FFmpeg with the scripts in vivace-qt-build/ of this",
            "archive. recipe.json lists their SHA-256 values; a cached FFmpeg is",
            "reused only when they are the same. The scripts are under the GPL v3 or",
            "later (vivace-qt-build/LICENSE).", ""]
    if sha:
        info.append(f"Packaged from vivace-qt-build commit: {sha}")
    if run_id:
        info.append(f"Packaged by workflow run: {server}/{repo}/actions/runs/{run_id}")
    (tmp / "BUILD-INFO.txt").write_text("\n".join(info) + "\n", encoding="utf-8")
    scripts = [ROOT / rel for rel in FFMPEG_SCRIPTS] + [ROOT / "LICENSE"]
    bundle.unlink(missing_ok=True)
    with tarfile.open(bundle, "w") as tar:
        for path in [tmp / "FFMPEG-SOURCE.txt", tmp / "BUILD-INFO.txt",
                     tmp / "recipe.json"] + archives:
            tar.add(path, arcname=path.name)
        for path in scripts:
            tar.add(path, arcname=f"vivace-qt-build/{path.relative_to(ROOT).as_posix()}")
    rmtree(tmp)
    log(f"{bundle.name}: the FFmpeg sources ({', '.join(a.name for a in archives)})")


def split_file(path: Path, part_mib: int) -> list[Path]:
    size = part_mib * 2**20
    parts = []
    with open(path, "rb") as f:
        index = 1
        while True:
            part = path.with_name(f"{path.name}.{index:03d}")
            written = 0
            with open(part, "wb") as out:
                while written < size:
                    chunk = f.read(min(1 << 24, size - written))
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
            if written == 0:
                part.unlink()
                break
            parts.append(part)
            index += 1
    path.unlink()
    return parts


def cmd_package(args) -> None:
    state_dir = Path(args.state)
    state = load_state(state_dir)
    cfg = load_version(state["qt_version"])
    kit = cfg["kits"][state["kit"]]
    left = remaining_items(state)
    if left:
        if left[0].get("pool", MAIN_POOL) == MAIN_POOL:
            hint = "All stage jobs ran out. Use a faster runner or add stage jobs."
        else:
            hint = ("The webengine job stopped before its time limit. Set the "
                    "QTCI_WEBENGINE_MINUTES variable to a larger value, or re-run that job.")
        die(f"{len(left)} items were not built (first: {item_id(left[0])}). {hint}")
    prefix = kit_prefix(cfg, kit)
    # --keep-state keeps the saved archive, so that the workflow can publish
    # the finished state for the kits that use this kit as their host Qt.
    if not restore_prefix(state_dir, prefix, keep_archive=args.keep_state):
        die(f"no saved prefix in {state_dir}")
    write_buildinfo(cfg, state, prefix)

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    name = kit["package"].format(qt_version=cfg["qt_version"])
    archive = out_dir / name
    archive.unlink(missing_ok=True)
    top = prefix.parent.parent  # <root>/<qt_version>/<kit dir>
    rel = prefix.relative_to(top)
    # The prefix keeps its marker (a later run on a self-hosted runner must be
    # able to replace it), but the archive leaves the marker out.
    if name.endswith(".zip"):
        seven = shutil.which("7z") or die("7z is required to make a zip archive")
        run([seven, "a", "-tzip", "-mx=5", "-mmt=on", "-bd", f"-xr!{PREFIX_MARKER}",
             archive, str(rel)], cwd=top)
    elif name.endswith(".tar.xz"):
        env = dict(os.environ, XZ_OPT="-6 -T0")
        run(["tar", f"--exclude={PREFIX_MARKER}", "-cJf", archive, str(rel.as_posix())],
            cwd=top, env=env)
    else:
        die(f"unsupported archive type: {name}")
    digest = file_digest(archive, "sha256")
    # LF on every host: "sha256sum -c" reads a CR as part of the file name.
    (out_dir / f"{name}.sha256").write_text(f"{digest}  {name}\n", newline="\n")
    size = archive.stat().st_size
    log(f"{name}: {size / 2**30:.2f} GiB, SHA-256 {digest}")

    files = [archive]
    if args.split_mib and size > args.split_mib * 2**20:
        files = split_file(archive, args.split_mib)
        log(f"Split into {len(files)} parts of at most {args.split_mib} MiB")
    stem = re.sub(r"\.(zip|tar\.xz)$", "", name)
    shutil.copy2(prefix / "BUILDINFO-vivace.txt", out_dir / f"{stem}-BUILDINFO.txt")
    if "ffmpeg" in state["deps"]:
        bundle = out_dir / f"{stem}-ffmpeg-source.tar"
        recipe = state["deps"]["ffmpeg"]
        if getattr(args, "ffmpeg_source", ""):
            # The source archive that the ffmpeg job uploaded with the FFmpeg:
            # no second download, so an upstream outage cannot fail package.
            made = Path(args.ffmpeg_source) / \
                f"ffmpeg-{recipe['ffmpeg']['version']}-{state['kit']}-source.tar"
            if not made.is_file():
                die(f"{made} not found (the ffmpeg-source artifact of the ffmpeg job)")
            with tarfile.open(made) as tar:
                try:
                    member = tar.extractfile("recipe.json")
                except KeyError:
                    die(f"{made} has no recipe.json; it is not a source archive of build-ffmpeg")
                made_recipe = json.load(member) if member else None
                if made_recipe != recipe:
                    die(f"{made} is the source of another FFmpeg recipe")
                # A partial download must not reach a release: every source
                # archive must be complete.
                for key, _title, _var in FFMPEG_SOURCE_PARTS:
                    if key not in recipe:
                        continue
                    try:
                        data = tar.extractfile(recipe[key]["file"])
                    except KeyError:
                        data = None
                    if data is None or hashlib.sha256(data.read()).hexdigest() \
                            != recipe[key]["sha256"]:
                        die(f"{made}: {recipe[key]['file']} is missing or incomplete")
                # The build scripts are the last members: a truncated archive
                # loses them first. They must be the scripts of the recipe.
                expected = {f"vivace-qt-build/{rel}": digest
                            for rel, digest in recipe["scripts"].items()}
                for member in ("FFMPEG-SOURCE.txt", "BUILD-INFO.txt", "vivace-qt-build/LICENSE"):
                    expected.setdefault(member, None)
                for member, want in expected.items():
                    try:
                        data = tar.extractfile(member)
                    except KeyError:
                        data = None
                    if data is None or (want is not None and
                                        hashlib.sha256(data.read()).hexdigest() != want):
                        die(f"{made}: {member} is missing or is not the file of the recipe")
            shutil.copy2(made, bundle)
        else:
            write_ffmpeg_source_bundle(recipe, bundle)
        files.append(bundle)
    gh_append("GITHUB_STEP_SUMMARY",
              f"### Package {name}\n\n- Size: {size / 2**30:.2f} GiB\n"
              f"- SHA-256: `{digest}`\n- Files: {', '.join(f.name for f in files)}\n")


# --------------------------------------------------------------------------
# check-patches
# --------------------------------------------------------------------------

def cmd_check_patches(args) -> None:
    cfg = load_version(args.qt_version)
    patch_root = ROOT / "patches" / cfg["qt_version"]
    modules = sorted(p.name for p in patch_root.iterdir() if p.is_dir()) \
        if patch_root.is_dir() else []
    if not modules:
        log(f"No patches for Qt {cfg['qt_version']}")
        return
    work = Path(args.work)
    for module in modules:
        if module not in cfg["modules"] and module not in cfg.get("extra_modules", {}):
            die(f"patches/{cfg['qt_version']}/{module} is not a module of Qt {cfg['qt_version']}")
        group(f"Apply patches to {module}")
        clone_module(cfg, module, work / module)  # applies the patches
        endgroup()
        rmtree(work / module)
    log(f"All patches apply to Qt {cfg['qt_version']}: {', '.join(modules)}")


# --------------------------------------------------------------------------
# import-qt5
# --------------------------------------------------------------------------

def cmd_import_qt5(args) -> None:
    qt5 = Path(args.qt5)
    text = (qt5 / ".gitmodules").read_text(encoding="utf-8")
    modules: dict = {}
    current = None
    for line in text.splitlines():
        header = re.match(r'\s*\[submodule "([^"]+)"\]', line)
        if header:
            current = {}
            modules[header.group(1)] = current
            continue
        kv = re.match(r"\s*(\w+)\s*=\s*(.*)$", line)
        if kv and current is not None:
            current[kv.group(1)] = kv.group(2).strip()
    shas = {}
    for line in capture(["git", "-C", qt5, "ls-tree", "HEAD"]).splitlines():
        meta, path = line.split("\t", 1)
        _mode, kind, sha = meta.split()
        if kind == "commit":
            shas[path] = sha
    default_status = {"essential", "addon", "preview", "deprecated"}
    result = {}
    for name, info in modules.items():
        if info.get("project") == "-" or info.get("status") not in default_status:
            continue
        result[name] = {
            "sha": shas[info["path"]],
            "status": info["status"],
            "depends": info.get("depends", "").split(),
            "recommends": info.get("recommends", "").split(),
        }
    # Keys that qt5.git does not have (install checks, CMake options, and
    # their comments) come from the modules of an existing version file.
    if args.base:
        base = json.loads(Path(args.base).read_text(encoding="utf-8")).get("modules", {})
        for name, entry in result.items():
            for key, value in base.get(name, {}).items():
                entry.setdefault(key, value)
        for name in sorted(set(base) - set(result)):
            # stderr: stdout is the JSON output.
            print(f"warning: {name} is in {args.base} but not in qt5.git; its keys are "
                  "dropped", file=sys.stderr)
    print(json.dumps(result, indent=2))


# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan")
    p.add_argument("--qt-version", required=True)
    p.add_argument("--kit", required=True)
    p.add_argument("--docs", choices=["kit-default", "on", "off"], default="kit-default")
    p.add_argument("--webengine", action="store_true")
    p.add_argument("--offload-webengine", action="store_true",
                   help="build QtWebEngine and the modules that use it in the webengine pool")
    p.add_argument("--stage-minutes", type=int, default=0,
                   help="timeout of a stage job; fail if an item cannot fit (0: no check)")
    p.add_argument("--reserve-minutes", type=int, default=30,
                   help="minutes that a stage job keeps free to save its state")
    p.add_argument("--skip", default="", help="comma-separated modules to leave out")
    p.add_argument("--state", help="directory for state.json (required without --check-only)")
    p.add_argument("--check-only", action="store_true",
                   help="check the inputs and the fit only; no network access, no state")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("setup-deps")
    p.add_argument("--state", required=True)
    p.add_argument("--deps-dir", required=True)
    p.add_argument("--ffmpeg-dir", default="", help="FFmpeg that build-ffmpeg made")
    p.add_argument("--host-qt", default="")
    p.set_defaults(func=cmd_setup_deps)

    p = sub.add_parser("build-ffmpeg")
    p.add_argument("--state", required=True)
    p.add_argument("--work", required=True, help="scratch directory; its content is deleted")
    p.add_argument("--out", required=True,
                   help="install directory of FFmpeg; the build deletes its old content")
    p.set_defaults(func=cmd_build_ffmpeg)

    p = sub.add_parser("ffmpeg-status")
    p.add_argument("--state", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_ffmpeg_status)

    p = sub.add_parser("ffmpeg-source")
    p.add_argument("--state", required=True)
    p.add_argument("--out", required=True, help="directory for the source archive")
    p.set_defaults(func=cmd_ffmpeg_source)

    p = sub.add_parser("package-ffmpeg")
    p.add_argument("--state", required=True)
    p.add_argument("--dir", required=True, help="FFmpeg that build-ffmpeg made")
    p.add_argument("--out", required=True, help="directory for the release files")
    p.set_defaults(func=cmd_package_ffmpeg)

    p = sub.add_parser("stage")
    p.add_argument("--state", required=True)
    p.add_argument("--deadline", required=True, help="Unix time to stop starting items")
    p.add_argument("--pool", choices=[MAIN_POOL, WEBENGINE_POOL], default=MAIN_POOL,
                   help="build only the items of this pool")
    p.add_argument("--fail-if-remaining", action="store_true",
                   help="exit non-zero if items of the pool are left (last job of a pool)")
    p.set_defaults(func=cmd_stage)

    p = sub.add_parser("restore")
    p.add_argument("--state", required=True)
    p.add_argument("--env", default="", help="also export the prefix in this variable")
    p.add_argument("--qt-version", default="", help="fail if the state is for another Qt version")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("package")
    p.add_argument("--state", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--split-mib", type=int, default=0)
    p.add_argument("--keep-state", action="store_true",
                   help="keep the saved prefix archive in the state directory")
    p.add_argument("--ffmpeg-source", default="",
                   help="directory with the FFmpeg source archive of the ffmpeg job")
    p.set_defaults(func=cmd_package)

    p = sub.add_parser("check-patches")
    p.add_argument("--qt-version", required=True)
    p.add_argument("--work", required=True, help="scratch directory for the clones")
    p.set_defaults(func=cmd_check_patches)

    p = sub.add_parser("import-qt5")
    p.add_argument("qt5", help="path to a qt5.git checkout at the release tag")
    p.add_argument("--base", help="version file whose module keys that qt5.git does not "
                   "have (installs, installs_if, installs_nothing, cmake_args) are kept")
    p.set_defaults(func=cmd_import_qt5)

    args = parser.parse_args()
    if sys.version_info < (3, 12):
        die("Python 3.12 or later is required")
    # The runner cancels a job with SIGINT (Ctrl+C on Windows), and then
    # SIGTERM or Ctrl+Break. Turn the first one into an exception in every
    # command, so that run() stops the child processes (they run in their own
    # session on POSIX and would otherwise keep running).
    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), raise_system_exit)
    args.func(args)


if __name__ == "__main__":
    main()
