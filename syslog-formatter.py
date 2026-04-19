#!/usr/bin/env python3

import argparse
import configparser
import multiprocessing
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "syslog-formatter.ini"
DEFAULT_LISTEN_IP = "0.0.0.0"
DEFAULT_LISTEN_PORT = 514
DEFAULT_TARGET_IP = "127.0.0.1"
DEFAULT_TARGET_PORT = 5514
DEFAULT_WORKER_COUNT = multiprocessing.cpu_count()
RECV_BUFFER_SIZE = 4 * 1024 * 1024
MAX_DATAGRAM_SIZE = 65535
SUPERVISOR_POLL_SECONDS = 2
TIMESTAMP_PATTERN = re.compile(r"(start|end|rt|art)=(\d{10})\d{3}")
REQUIRED_FLOW_FIELDS = ("start", "src", "dst", "dpt")
FIELD_PATTERNS = {
    field: re.compile(rf"(^|\s){field}=([^\s]+)")
    for field in REQUIRED_FLOW_FIELDS
}

DEFAULT_FLOWLINK_CMD = "/usr/local/bin/illumio-flowlink-ctl"
DEFAULT_FLOWLINK_CONFIG = "/opt/flowlink/config-netflow.yml"
DEFAULT_FLOWLINK_LOG = "/opt/flowlink/data/flowlink.log"
DEFAULT_RELAY_SERVICE = "syslog-formatter"
DEFAULT_BACKUP_DIR = "/var/log/flowlink_crash_backups"
DEFAULT_RESTART_WAIT_SECONDS = 5

CONFIG_SCHEMA = {
    "relay": {
        "listen_ip": str,
        "listen_port": int,
        "target_ip": str,
        "target_port": int,
        "workers": int,
    },
    "healthcheck": {
        "flowlink_cmd": str,
        "flowlink_config": str,
        "flowlink_log": str,
        "relay_service": str,
        "backup_dir": str,
        "restart_wait_seconds": int,
    },
}

stop_requested = False


def log(message, level="INFO"):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}", file=sys.stderr, flush=True)


def handle_stop(signum, frame):
    del frame
    global stop_requested
    stop_requested = True
    log(f"Received signal {signum}, shutting down.", level="WARN")


def get_header():
    ts = time.strftime("%b %d %H:%M:%S")
    return f"<14>{ts} Forcepoint-Relay "


def extract_config_option(argv):
    config_path = os.environ.get("SYSLOG_FORMATTER_CONFIG")
    filtered_argv = []
    index = 0

    while index < len(argv):
        current = argv[index]
        if current == "--config":
            if index + 1 >= len(argv):
                raise ValueError("--config requires a path")
            config_path = argv[index + 1]
            index += 2
            continue
        if current.startswith("--config="):
            config_path = current.split("=", 1)[1]
            index += 1
            continue

        filtered_argv.append(current)
        index += 1

    return config_path, filtered_argv


def resolve_config_path(config_path):
    if config_path:
        return Path(config_path).expanduser()
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    return None


def load_config_defaults(config_path):
    resolved_path = resolve_config_path(config_path)
    if resolved_path is None:
        return {}, None
    if not resolved_path.exists():
        raise ValueError(f"Config file not found: {resolved_path}")

    parser = configparser.ConfigParser()
    parser.read(resolved_path, encoding="utf-8")

    defaults = {}
    for section, options in CONFIG_SCHEMA.items():
        if not parser.has_section(section):
            continue

        for option, caster in options.items():
            if not parser.has_option(section, option):
                continue

            raw_value = parser.get(section, option)
            try:
                defaults[option] = caster(raw_value)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid value for [{section}] {option}: {raw_value}"
                ) from exc

    return defaults, resolved_path


def normalize_command_args(argv):
    if argv and argv[0] not in {"run", "healthcheck", "-h", "--help"}:
        return ["run", *argv]
    return argv


def create_listen_socket(listen_ip, listen_port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUFFER_SIZE)
    sock.bind((listen_ip, listen_port))
    return sock


def process_packet(data):
    text_data = data.decode("utf-8", "ignore")
    text_data = TIMESTAMP_PATTERN.sub(r"\1=\2", text_data)
    if text_data.startswith("CEF:"):
        text_data = get_header() + text_data
    prefix, separator, extension = text_data.partition("CEF:")
    if not separator:
        raise ValueError("Missing CEF payload")

    cef_parts = extension.split("|", 7)
    if len(cef_parts) != 8:
        raise ValueError("Invalid CEF header")

    selected_fields = []
    for field in REQUIRED_FLOW_FIELDS:
        match = FIELD_PATTERNS[field].search(cef_parts[7])
        if match is None:
            raise ValueError(f"Missing required field: {field}")
        selected_fields.append(f"{field}={match.group(2)}")

    minimal_extension = " " + " ".join(selected_fields)
    minimal_cef = "|".join([*cef_parts[:7], minimal_extension])
    return f"{prefix}CEF:{minimal_cef}".encode("ascii")


def run_worker(listen_ip, listen_port, target_ip, target_port):
    error_count = 0
    sock = None
    sender = None

    try:
        sock = create_listen_socket(listen_ip, listen_port)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        log(
            f"Worker {os.getpid()} listening on UDP {listen_ip}:{listen_port} -> "
            f"{target_ip}:{target_port}"
        )

        while True:
            try:
                data, _, flags, _ = sock.recvmsg(MAX_DATAGRAM_SIZE)
                if flags & getattr(socket, "MSG_TRUNC", 0):
                    error_count += 1
                    log(
                        f"Worker {os.getpid()} dropped a truncated UDP datagram.",
                        level="ERROR",
                    )
                    continue

                clean_data = process_packet(data)
                sender.sendto(clean_data, (target_ip, target_port))
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                error_count += 1
                log(
                    f"Worker {os.getpid()} packet processing failed "
                    f"(count={error_count}): {exc}",
                    level="ERROR",
                )
    except KeyboardInterrupt:
        return
    except Exception as exc:
        log(f"Worker {os.getpid()} exited with fatal error: {exc}", level="ERROR")
        raise
    finally:
        if sender is not None:
            sender.close()
        if sock is not None:
            sock.close()


def start_worker(slot, listen_ip, listen_port, target_ip, target_port):
    process = multiprocessing.Process(
        target=run_worker,
        args=(listen_ip, listen_port, target_ip, target_port),
        name=f"syslog-formatter-{slot}",
    )
    process.start()
    return process


def start_server(args):
    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    print("==================================================")
    print("   Forcepoint Syslog Formatter")
    print("==================================================")
    print(f"Workers   : {args.workers}")
    print(f"Listen    : {args.listen_ip}:{args.listen_port}")
    print(f"Forward   : {args.target_ip}:{args.target_port}")
    if args.config_path:
        print(f"Config    : {args.config_path}")
    print("Features  : Header fix + timestamp normalization + minimal flow projection")
    print("==================================================")

    processes = {}
    for slot in range(args.workers):
        processes[slot] = start_worker(
            slot,
            args.listen_ip,
            args.listen_port,
            args.target_ip,
            args.target_port,
        )

    try:
        while not stop_requested:
            for slot, process in list(processes.items()):
                if process.is_alive():
                    continue

                exit_code = process.exitcode
                process.join(timeout=0.1)
                if stop_requested:
                    continue

                log(
                    f"Worker slot {slot} exited with code {exit_code}; restarting.",
                    level="ERROR",
                )
                processes[slot] = start_worker(
                    slot,
                    args.listen_ip,
                    args.listen_port,
                    args.target_ip,
                    args.target_port,
                )

            time.sleep(SUPERVISOR_POLL_SECONDS)
    finally:
        for process in processes.values():
            if process.is_alive():
                process.terminate()
        for process in processes.values():
            process.join(timeout=5)


def run_command(command):
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return None, "", str(exc)

    output = "\n".join(
        part for part in (completed.stdout.strip(), completed.stderr.strip()) if part
    )
    return completed.returncode, output, None


def parse_flowlink_status(return_code, output):
    normalized = output.upper()

    if return_code is None:
        return "command_error"
    if "NOT RUNNING" in normalized:
        return "stopped"
    if return_code != 0 and "RUNNING" not in normalized:
        return "command_error"
    if "RUNNING" in normalized:
        return "running"
    return "unknown"


def backup_file(source_path, backup_dir, prefix, suffix):
    source = Path(source_path)
    if not source.exists():
        return None

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    destination = backup_dir / f"{prefix}_{stamp}.{suffix}"
    shutil.copy2(source, destination)
    return destination


def backup_journal(service_name, backup_dir):
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    destination = backup_dir / f"relay_crash_{stamp}.txt"
    result = subprocess.run(
        ["journalctl", "-u", service_name, "-n", "50", "--no-pager"],
        capture_output=True,
        text=True,
        check=False,
    )
    destination.write_text(result.stdout + result.stderr, encoding="utf-8")
    return destination


def healthcheck(args):
    backup_dir = Path(args.backup_dir)
    print(f"=== Starting Health Check: {time.strftime('%Y%m%d_%H%M%S')} ===")
    if args.config_path:
        print(f"Config: {args.config_path}")

    flowlink_code, flowlink_output, flowlink_error = run_command(
        [args.flowlink_cmd, "status"]
    )
    flowlink_state = parse_flowlink_status(flowlink_code, flowlink_output)

    if flowlink_error is not None or flowlink_state in {"command_error", "unknown"}:
        print("[CRITICAL] FlowLink status check failed!")
        detail = flowlink_error or flowlink_output or "Unknown status response."
        print(f"   -> Detail: {detail}")
    elif flowlink_state == "stopped":
        print("[CRITICAL] FlowLink is DOWN!")

        backup_path = backup_file(
            args.flowlink_log,
            backup_dir,
            "flowlink_crash",
            "log",
        )
        if backup_path is not None:
            print(f"   -> Log backed up to: {backup_path}")

        print("   -> Attempting restart...")
        restart_code, restart_output, restart_error = run_command(
            [args.flowlink_cmd, "start", "--config", args.flowlink_config]
        )
        if restart_error is not None:
            print(f"   -> Restart command failed: {restart_error}")
        elif restart_code != 0:
            print(f"   -> Restart command failed: {restart_output}")

        time.sleep(args.restart_wait_seconds)
        verify_code, verify_output, verify_error = run_command(
            [args.flowlink_cmd, "status"]
        )
        verify_state = parse_flowlink_status(verify_code, verify_output)
        if verify_error is None and verify_state == "running":
            print("   -> Restart SUCCESS.")
        else:
            detail = verify_error or verify_output or "Unknown status response."
            print(f"   -> Restart FAILED. Detail: {detail}")
    else:
        print("[OK] FlowLink is running.")

    relay_status = subprocess.run(
        ["systemctl", "is-active", "--quiet", args.relay_service],
        check=False,
    )
    if relay_status.returncode != 0:
        print("[CRITICAL] Syslog Formatter is DOWN!")
        journal_path = backup_journal(args.relay_service, backup_dir)
        print(f"   -> Journal logs backed up to: {journal_path}")

        print("   -> Attempting restart...")
        subprocess.run(["systemctl", "reset-failed", args.relay_service], check=False)
        subprocess.run(["systemctl", "restart", args.relay_service], check=False)

        verify_relay = subprocess.run(
            ["systemctl", "is-active", "--quiet", args.relay_service],
            check=False,
        )
        if verify_relay.returncode == 0:
            print("   -> Restart SUCCESS.")
        else:
            print("   -> Restart FAILED.")
    else:
        print("[OK] Syslog Formatter is running.")

    print("=== Check Finished ===")
    print("")


def build_parser(config_defaults):
    parser = argparse.ArgumentParser(
        description="Forcepoint syslog formatter and FlowLink health checker."
    )
    parser.add_argument(
        "--config",
        help=(
            "Path to INI config file. Also supports SYSLOG_FORMATTER_CONFIG; "
            f"default file: {DEFAULT_CONFIG_PATH}"
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run the UDP relay.")
    run_parser.add_argument(
        "--listen-ip",
        default=config_defaults.get("listen_ip", DEFAULT_LISTEN_IP),
    )
    run_parser.add_argument(
        "--listen-port",
        type=int,
        default=config_defaults.get("listen_port", DEFAULT_LISTEN_PORT),
    )
    run_parser.add_argument(
        "--target-ip",
        default=config_defaults.get("target_ip", DEFAULT_TARGET_IP),
    )
    run_parser.add_argument(
        "--target-port",
        type=int,
        default=config_defaults.get("target_port", DEFAULT_TARGET_PORT),
    )
    run_parser.add_argument(
        "--workers",
        type=int,
        default=config_defaults.get("workers", DEFAULT_WORKER_COUNT),
    )
    run_parser.set_defaults(handler=start_server)

    health_parser = subparsers.add_parser(
        "healthcheck",
        help="Run FlowLink and relay health checks.",
    )
    health_parser.add_argument(
        "--flowlink-cmd",
        default=config_defaults.get("flowlink_cmd", DEFAULT_FLOWLINK_CMD),
    )
    health_parser.add_argument(
        "--flowlink-config",
        default=config_defaults.get("flowlink_config", DEFAULT_FLOWLINK_CONFIG),
    )
    health_parser.add_argument(
        "--flowlink-log",
        default=config_defaults.get("flowlink_log", DEFAULT_FLOWLINK_LOG),
    )
    health_parser.add_argument(
        "--relay-service",
        default=config_defaults.get("relay_service", DEFAULT_RELAY_SERVICE),
    )
    health_parser.add_argument(
        "--backup-dir",
        default=config_defaults.get("backup_dir", DEFAULT_BACKUP_DIR),
    )
    health_parser.add_argument(
        "--restart-wait-seconds",
        type=int,
        default=config_defaults.get(
            "restart_wait_seconds",
            DEFAULT_RESTART_WAIT_SECONDS,
        ),
    )
    health_parser.set_defaults(handler=healthcheck)

    parser.set_defaults(command="run", handler=start_server)
    return parser


def main():
    raw_argv = sys.argv[1:]
    try:
        config_arg, argv = extract_config_option(raw_argv)
        config_defaults, resolved_config_path = load_config_defaults(config_arg)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    parser = build_parser(config_defaults)
    argv = normalize_command_args(argv)
    args = parser.parse_args(argv)
    args.config_path = (
        str(resolved_config_path) if resolved_config_path is not None else None
    )
    if getattr(args, "workers", 1) < 1:
        parser.error("--workers must be at least 1")

    args.handler(args)


if __name__ == "__main__":
    main()
