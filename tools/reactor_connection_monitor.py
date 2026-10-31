"""Watch every TCP connection from this Mac to the Reactor server and record which process made it.

    python3 tools/reactor_connection_monitor.py --out runs/test-live/connections.jsonl --enforce

Every ``--interval`` seconds (default 0.5) lsof lists the connections to the host's addresses. Each new one is logged
with the process id, its parent, the program (``reactor/client.py``, ``dashboard/server.py``, ...) and, for the
client, its action, Reactor history and question id; each closed one with its duration. A connection belongs to the
benchmark run when its process is the ``memory_challenge run-reactor`` process or one of its descendants; anything
else is FOREIGN. With ``--enforce`` a foreign process that is a known Reactor tool (client.py, smoke_test.py,
run_benchmark.py, workflow.py, dashboard/server.py, a second run-reactor) is stopped with SIGTERM, and that is logged;
other programs are only reported. Only ids and program names are logged, never command-line values (they hold
question text) or the key. Events also go to stdout, one line each. Connections shorter than the interval can be
missed; the run's own requests are all in ``<run>/requests.jsonl``. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

HOST = "t05.api.reactor-hackathon.rxai.dev"
TOOLS = ("reactor/client.py", "smoke_test.py", "run_benchmark.py", "workflow.py", "dashboard/server.py",
         "memory_challenge run-reactor")


def resolve(host: str) -> set[str]:
    try:
        return {a[4][0] for a in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}
    except OSError:
        return set()


def connections(ips: set[str], port: int | None) -> dict[tuple[int, str], str]:
    """(pid, connection name) → command name, for TCP connections to any of ``ips`` (and ``port``, if given)."""
    out: dict[tuple[int, str], str] = {}
    for ip in ips:
        target = f"-iTCP@{ip}:{port}" if port else f"-iTCP@{'[' + ip + ']' if ':' in ip else ip}"
        res = subprocess.run(["lsof", "-nP", target, "-F", "pcn"], capture_output=True, text=True)
        pid, cmd = None, ""
        for line in res.stdout.splitlines():
            if line.startswith("p"):
                pid = int(line[1:])
            elif line.startswith("c"):
                cmd = line[1:]
            elif line.startswith("n") and pid is not None and "->" in line:
                remote = line[1:].split("->", 1)[1]  # only our side: the remote end is the Reactor address
                rhost, _, rport = remote.rpartition(":")
                if rhost.strip("[]") in ips and (port is None or rport == str(port)):
                    out[(pid, line[1:])] = cmd
    return out


def process_table() -> dict[int, tuple[int, str]]:
    res = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True)
    table = {}
    for line in res.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    return table


def describe(command: str) -> dict:
    """The program and, for Reactor's client, its action and ids; no other argument values."""
    args = command.split()
    program = next((t for t in TOOLS if t in command), None)
    if program is None:
        script = next((a for a in args if a.endswith(".py")), None)
        program = Path(script).name if script else Path(args[0]).name if args else "?"
    info = {"program": program}
    if program == "reactor/client.py":
        info["action"] = "new-history" if "--new-history" in args else "text" if "--text" in args else "question"
        for flag, key in (("--history", "history_id"), ("--question-id", "question_id")):
            if flag in args and args.index(flag) + 1 < len(args):
                info[key] = args[args.index(flag) + 1]
    return info


RUN_ROOT = re.compile(r"^\S*python[\d.]*\s+(?:-\S+\s+)*-m\s+memory_challenge\s+run-reactor\b")


def run_tree(table: dict[int, tuple[int, str]]) -> set[int]:
    """The run-reactor Python process and its descendants (a shell merely mentioning run-reactor doesn't count)."""
    roots = {pid for pid, (_, cmd) in table.items() if RUN_ROOT.match(Path(cmd.split(" ", 1)[0]).name + " " +
                                                                        (cmd.split(" ", 1)[1] if " " in cmd else ""))}
    ours, changed = set(roots), True
    while changed:
        changed = False
        for pid, (ppid, _) in table.items():
            if ppid in ours and pid not in ours:
                ours.add(pid)
                changed = True
    return ours


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, help="JSONL log of connection events")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--ip", action="append", help="watch these addresses instead of resolving --host (tests)")
    ap.add_argument("--port", type=int, help="only connections to this port (tests)")
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--duration", type=float, help="stop after this many seconds (default: run until interrupted)")
    ap.add_argument("--enforce", action="store_true", help="stop foreign processes that are known Reactor tools")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    def emit(record: dict) -> None:
        record = {"ts": round(time.time(), 3), "time": time.strftime("%H:%M:%S"), **record}
        line = json.dumps(record)
        with open(out, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)

    ips = set(args.ip or []) or resolve(args.host)
    resolved_at, started = time.time(), time.time()
    emit({"event": "monitor_start", "host": args.host, "ips": sorted(ips), "enforce": args.enforce,
          "interval_s": args.interval, "pid": os.getpid()})
    open_conns: dict[tuple[int, str], dict] = {}
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    try:
        while not stopping and (args.duration is None or time.time() - started < args.duration):
            if not args.ip and time.time() - resolved_at > 60:
                ips, resolved_at = (resolve(args.host) or ips), time.time()
            now = connections(ips, args.port)
            new = [k for k in now if k not in open_conns]
            table = process_table() if new else {}
            ours = run_tree(table) if new else set()
            for key in new:
                pid, name = key
                ppid, command = table.get(pid, (None, ""))
                rec = {"event": "open", "pid": pid, "ppid": ppid, "command": now[key], "connection": name,
                       "belongs_to_run": pid in ours, **describe(command)}
                if not rec["belongs_to_run"]:
                    rec["event"] = "open_FOREIGN"
                open_conns[key] = {**rec, "opened": time.time()}
                emit(rec)
                if not rec["belongs_to_run"] and args.enforce and rec["program"] in TOOLS:
                    try:
                        os.kill(pid, signal.SIGTERM)
                        emit({"event": "stopped_foreign", "pid": pid, "program": rec["program"]})
                    except OSError as exc:
                        emit({"event": "stop_failed", "pid": pid, "program": rec["program"], "error": str(exc)})
            for key in [k for k in open_conns if k not in now]:
                rec = open_conns.pop(key)
                emit({"event": "close", "pid": key[0], "program": rec["program"],
                      "belongs_to_run": rec["belongs_to_run"], "open_s": round(time.time() - rec["opened"], 1),
                      **{k: rec[k] for k in ("action", "question_id", "history_id") if k in rec}})
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    emit({"event": "monitor_stop", "still_open": len(open_conns)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
