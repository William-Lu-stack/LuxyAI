#!/usr/bin/env python3
"""Run the complete Flawless local service group under one supervisor."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass


ROOT_DIR = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Service:
    name: str
    module: str
    port: int


def service_plan(api_port: int) -> list[Service]:
    services = [
        Service("observability", "agents.observability_agent:app", 8100),
        Service("healing", "agents.healing_agent:app", 8101),
        Service("incident", "agents.incident_agent:app", 8102),
        Service("postmortem", "agents.postmortem_agent:app", 8103),
        Service("mcp", "mcp_servers.mcp_http_server:app", 8105),
        Service("adapter", "openwebui.openwebui_adapter:app", 8200),
        Service("cmdb", "cmdb.local_cmdb:app", 8300),
        Service("api", "backend.app.main:app", api_port),
    ]
    ports = [service.port for service in services]
    if len(ports) != len(set(ports)):
        raise ValueError("local service ports must be unique")
    return services


def stop_processes(processes: dict[str, subprocess.Popen[bytes]]) -> None:
    for process in processes.values():
        if process.poll() is None:
            process.terminate()

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and any(process.poll() is None for process in processes.values()):
        time.sleep(0.1)

    for process in processes.values():
        if process.poll() is None:
            process.kill()
    for process in processes.values():
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def run(host: str, api_port: int) -> int:
    processes: dict[str, subprocess.Popen[bytes]] = {}
    stopping = False

    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT_DIR / ".env", override=False)
    except ImportError:
        pass

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    environment = os.environ.copy()
    environment.setdefault("PYTHONUNBUFFERED", "1")

    try:
        for service in service_plan(api_port):
            command = [
                sys.executable,
                "-m",
                "uvicorn",
                service.module,
                "--host",
                host,
                "--port",
                str(service.port),
            ]
            print(
                f"[flawless] starting {service.name:<13} {service.module} on {host}:{service.port}",
                flush=True,
            )
            processes[service.name] = subprocess.Popen(
                command,
                cwd=ROOT_DIR,
                env=environment,
            )

        while not stopping:
            for name, process in processes.items():
                return_code = process.poll()
                if return_code is not None:
                    print(f"[flawless] {name} exited unexpectedly with code {return_code}", file=sys.stderr, flush=True)
                    return return_code or 1
            time.sleep(0.25)
        return 0
    finally:
        stop_processes(processes)


def start_detached(host: str, api_port: int, pid_file: Path, log_file: Path) -> int:
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--host",
        host,
        "--api-port",
        str(api_port),
    ]
    with log_file.open("ab", buffering=0) as output:
        process = subprocess.Popen(
            command,
            cwd=ROOT_DIR,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    temporary_pid_file = pid_file.with_suffix(f"{pid_file.suffix}.tmp")
    temporary_pid_file.write_text(f"{process.pid}\n", encoding="ascii")
    temporary_pid_file.replace(pid_file)
    return process.pid


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="bind address for every local service")
    parser.add_argument("--api-port", type=int, default=8080, help="public control-plane port")
    parser.add_argument("--check", action="store_true", help="print the service plan without starting processes")
    parser.add_argument("--daemon", action="store_true", help="start a detached supervisor process")
    parser.add_argument("--pid-file", type=Path, help="write the detached supervisor PID to this file")
    parser.add_argument("--log-file", type=Path, help="append detached supervisor output to this file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.api_port <= 65535:
        raise SystemExit("--api-port must be between 1 and 65535")
    if args.check:
        print(json.dumps([asdict(service) for service in service_plan(args.api_port)], indent=2))
        return 0
    if args.daemon:
        if args.pid_file is None or args.log_file is None:
            raise SystemExit("--daemon requires --pid-file and --log-file")
        print(start_detached(args.host, args.api_port, args.pid_file, args.log_file))
        return 0
    return run(args.host, args.api_port)


if __name__ == "__main__":
    raise SystemExit(main())
