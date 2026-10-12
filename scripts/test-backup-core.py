"""Source-only acceptance runner. Establish isolation BEFORE importing the app.

Default: full unittest discovery. --test selects focused tests; --script runs
one existing browser gate under the same pre-import network/credential fence.
No package installation, live service, or product environment is used.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
STRIP = {"PYTHONPATH", "MEMORY_DB_PATH", "LOCAL_KB_PATH", "TRADEWIND_DATA_DIR",
         "TRADEWIND_NO_BROWSER"}
SCRIPTS = {"test-backup-web.py", "test-followup-web.py", "test-followup-regressions.py"}


def isolated_environment(root):
    env = {k: v for k, v in os.environ.items()
           if not k.upper().endswith("_API_KEY") and k.upper() not in STRIP
           and k.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}}
    env.pop("TASK9_MUTANT", None)
    # Neutral test value lets mocked launcher tests exercise their normal branch.
    # Explicit browser/startfile fences below, not this flag, prohibit real opens.
    env.update(TRADEWIND_DATA_DIR=str(root / "app"), TRADEWIND_NO_BROWSER="0",
               PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
               TASK9_FENCE_ROOT=str(root / "fence"), NO_PROXY="127.0.0.1,::1")
    return env


def install_fence():
    """Inherited by Python -c descendants even when their env strips PYTHONPATH.

    Ports become admissible only after a test-owned loopback bind(0), or a
    rebind of that same registered port. DNS is never used for providers.
    """
    import socket
    import types
    if getattr(socket, "_task9_fenced", False):
        return
    socket._task9_fenced = True
    socket._task9_denied = []
    fence = Path(os.environ["TASK9_FENCE_ROOT"])
    fence.mkdir(parents=True, exist_ok=True)
    original_bind = socket.socket.bind

    def allowed(address):
        return (isinstance(address, tuple) and len(address) >= 2
                and address[0] in {"127.0.0.1", "::1"}
                and address[1] != 8101
                and (fence / str(address[1])).is_file())

    def bind(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            if not (address[0] in {"127.0.0.1", "::1"}
                    and (address[1] == 0 or allowed(address))):
                raise PermissionError("test network fence: unowned bind")
        result = original_bind(sock, address)
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            port = sock.getsockname()[1]
            if port == 8101:
                sock.close()
                raise PermissionError("test network fence: reserved live port")
            (fence / str(port)).touch()
        return result

    socket.socket.bind = bind
    # HTTPServer uses getfqdn during bind; avoid even loopback reverse DNS.
    socket.getfqdn = lambda name="": name or "127.0.0.1"

    def audit(event, args):
        if event in {"socket.connect", "socket.sendto"} and not allowed(args[-1]):
            socket._task9_denied.append(event)
            raise PermissionError("test network fence: unowned destination")
        if event == "socket.getaddrinfo" and not allowed((args[0], args[1])):
            socket._task9_denied.append(event)
            raise PermissionError("test network fence: external DNS prohibited")
        if event in {"socket.gethostbyname", "socket.gethostbyname_ex", "socket.gethostbyaddr"}:
            socket._task9_denied.append(event)
            raise PermissionError("test network fence: DNS prohibited")
        if event == "open" and isinstance(args[0], (str, bytes)):
            path = Path(os.fsdecode(args[0])).absolute()
            # Source and installed libraries remain readable; live runtime and
            # the source dotenv never do. All fixture roots are TemporaryDirectory.
            if path == ROOT / ".env" or path.is_relative_to(ROOT / "data"):
                raise PermissionError("test filesystem fence: live data prohibited")

    sys.addaudithook(audit)
    # Do not initialize a platform keyring backend. CredentialStore still gets
    # tested with its explicitly supplied fake backends.
    keyring = types.ModuleType("keyring")
    def no_credentials(*_args, **_kwargs):
        raise AssertionError("Windows credentials prohibited in acceptance")
    for name in ("get_password", "set_password", "delete_password"):
        setattr(keyring, name, no_credentials)
    sys.modules["keyring"] = keyring

    import webbrowser
    def no_real_browser(*_args, **_kwargs):
        raise AssertionError("real browser/startfile opening prohibited in acceptance")
    for name in ("open", "open_new", "open_new_tab"):
        setattr(webbrowser, name, no_real_browser)
    if hasattr(os, "startfile"):
        os.startfile = no_real_browser

    original_popen = subprocess.Popen
    prelude = (f"import runpy; runpy.run_path({str(Path(__file__).resolve())!r})"
               "['install_fence'](); ")
    class FencedPopen(original_popen):
        # asyncio.windows_utils subclasses Popen: preserve that class contract.
        def __init__(self, args, *positional, **kwargs):
            if isinstance(args, (list, tuple)) and args and Path(str(args[0])).name.lower() in {"python", "python.exe", "python3"}:
                args = list(args)
                if "-c" not in args:
                    runner = str(Path(__file__).resolve())
                    if runner not in args:
                        raise PermissionError("test fence: unsupported Python descendant")
                    index = args.index(runner)
                    argv = [runner, *args[index + 1:]]
                    args = [args[0], "-B", "-c", f"import runpy,sys; sys.argv={argv!r}; runpy.run_path({runner!r},run_name='__main__')"]
                index = args.index("-c") + 1
                args[index] = prelude + args[index]
            super().__init__(args, *positional, **kwargs)
    subprocess.Popen = FencedPopen


def child(arguments):
    install_fence()
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    if arguments.probe:
        from app import config
        import webbrowser
        browser_blocked = startfile_blocked = False
        try:
            webbrowser.open("synthetic://must-not-open")
        except AssertionError:
            browser_blocked = True
        if hasattr(os, "startfile"):
            try:
                os.startfile("synthetic://must-not-open")
            except AssertionError:
                startfile_blocked = True
        else:
            startfile_blocked = True
        print(json.dumps({"owned_config": config.ROOT_DIR == Path(os.environ["TRADEWIND_DATA_DIR"]),
                          "keys_absent": not any(k.upper().endswith("_API_KEY") for k in os.environ),
                          "paths_absent": not any(k in os.environ for k in ("PYTHONPATH", "MEMORY_DB_PATH", "LOCAL_KB_PATH")),
                          "no_browser": browser_blocked and startfile_blocked,
                          "neutral_browser_env": os.environ["TRADEWIND_NO_BROWSER"] == "0"}))
        return 0
    if arguments.script:
        import runpy
        sys.argv = [str(ROOT / "scripts" / arguments.script), *arguments.script_args]
        runpy.run_path(sys.argv[0], run_name="__main__")
        return 0
    import unittest
    suite = (unittest.defaultTestLoader.loadTestsFromNames(arguments.test) if arguments.test
             else unittest.defaultTestLoader.discover(str(ROOT / "tests")))
    if arguments.exclude_test:
        def cases(current):
            for item in current:
                if isinstance(item, unittest.TestSuite):
                    yield from cases(item)
                else:
                    yield item
        discovered = list(cases(suite))
        excluded = set(arguments.exclude_test)
        missing = excluded - {test.id() for test in discovered}
        if missing:
            raise ValueError("explicit reused-evidence test IDs not found: " + ", ".join(sorted(missing)))
        selected = [test for test in discovered if test.id() not in excluded]
        print(json.dumps({"selection_discovered": len(discovered), "selection_run": len(selected),
                          "explicit_reused_evidence": sorted(excluded)}), flush=True)
        suite = unittest.TestSuite(selected)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return int(not result.wasSuccessful())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="append", default=[])
    parser.add_argument("--exclude-test", action="append", default=[],
                        help="Explicit loader selection only; default never excludes tests")
    parser.add_argument("--script", choices=sorted(SCRIPTS))
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--mutant", choices=["unknown", "suppression", "download-owner-leak"],
                        help="Controlled test-sanity fault; never product RED")
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    arguments, arguments.script_args = parser.parse_known_args()
    if arguments.script_args and not arguments.script:
        parser.error("extra arguments require --script")
    if arguments._child:
        return child(arguments)
    with tempfile.TemporaryDirectory(prefix="tradewind-core-") as directory:
        root = Path(directory)
        (root / "app" / "data").mkdir(parents=True)
        (root / "app" / ".env").write_text("", encoding="utf-8")
        (root / "app" / "data" / "config.json").write_text("{}", encoding="utf-8")
        command = [sys.executable, "-B", str(Path(__file__).resolve()), "--_child", *sys.argv[1:]]
        env = isolated_environment(root)
        if arguments.mutant:
            env["TASK9_MUTANT"] = arguments.mutant
        return subprocess.run(command, cwd=ROOT, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
