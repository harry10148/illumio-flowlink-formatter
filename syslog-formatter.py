#!/usr/bin/env python3

import argparse
import configparser
import multiprocessing
import os
import re
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
STATS_INTERVAL_SECONDS = 60
TIMESTAMP_PATTERN = re.compile(r"\b(start|end|rt|art)=(\d{10})\d{3}")
REQUIRED_FLOW_FIELDS = ("start", "src", "dst", "dpt")
FIELD_PATTERNS = {
    field: re.compile(rf"(^|\s){field}=([^\s]+)")
    for field in REQUIRED_FLOW_FIELDS
}
# Forcepoint proxy transactions carry no protocol tag, but FlowLink's text
# parser requires one (`protocol: proto` in the consumer config) and discards
# any record whose value it cannot parse — "Discarding flow due to invalid
# protocol". Proxy traffic is TCP, so the relay synthesises proto=6.
DEFAULT_PROTOCOL_VALUE = "6"

# Since FlowLink 2.0.0 the daemon is a Linux service owned by the RPM, so the
# healthcheck only observes it — restarting is systemd's job, not ours.
DEFAULT_FLOWLINK_SERVICE = "illumio-flowlink"
DEFAULT_RELAY_SERVICE = "syslog-formatter"

CONFIG_SCHEMA = {
    "relay": {
        "listen_ip": str,
        "listen_port": int,
        "target_ip": str,
        "target_port": int,
        "workers": int,
        "protocol_value": str,
    },
    "healthcheck": {
        "flowlink_service": str,
        "relay_service": str,
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


def supports_reuseport():
    if not hasattr(socket, "SO_REUSEPORT"):
        return False

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def create_listen_socket(listen_ip, listen_port, use_reuseport):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if use_reuseport:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUFFER_SIZE)
    sock.bind((listen_ip, listen_port))
    return sock


def process_packet(data, protocol_value=DEFAULT_PROTOCOL_VALUE):
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

    if protocol_value:
        selected_fields.append(f"proto={protocol_value}")

    minimal_extension = " " + " ".join(selected_fields)
    minimal_cef = "|".join([*cef_parts[:7], minimal_extension])
    return f"{prefix}CEF:{minimal_cef}".encode("utf-8")


def run_worker(listen_ip, listen_port, target_ip, target_port, use_reuseport,
               protocol_value=DEFAULT_PROTOCOL_VALUE):
    relayed_count = 0
    error_count = 0
    truncated_count = 0
    window_start = time.monotonic()
    sock = None
    sender = None

    def flush_stats(now):
        nonlocal relayed_count, error_count, truncated_count, window_start
        log(
            f"Worker {os.getpid()} stats: relayed={relayed_count} "
            f"errors={error_count} truncated={truncated_count} "
            f"window={int(now - window_start)}s"
        )
        relayed_count = error_count = truncated_count = 0
        window_start = now

    try:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        sock = create_listen_socket(listen_ip, listen_port, use_reuseport)
        # Timeout doubles as the idle heartbeat: stats are emitted even
        # when no traffic arrives, proving the worker is alive.
        sock.settimeout(STATS_INTERVAL_SECONDS)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        log(
            f"Worker {os.getpid()} listening on UDP {listen_ip}:{listen_port} -> "
            f"{target_ip}:{target_port}"
        )

        while True:
            now = time.monotonic()
            if now - window_start >= STATS_INTERVAL_SECONDS:
                flush_stats(now)

            try:
                data, _, flags, _ = sock.recvmsg(MAX_DATAGRAM_SIZE)
                if flags & getattr(socket, "MSG_TRUNC", 0):
                    truncated_count += 1
                    if truncated_count == 1:
                        log(
                            f"Worker {os.getpid()} dropped a truncated UDP "
                            "datagram (further ones this window are counted "
                            "in stats).",
                            level="ERROR",
                        )
                    continue

                clean_data = process_packet(data, protocol_value)
                sender.sendto(clean_data, (target_ip, target_port))
                relayed_count += 1
            except socket.timeout:
                continue
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                # Log only the first error per stats window to avoid
                # journald flooding under malformed traffic; totals are
                # reported in the periodic stats line.
                error_count += 1
                if error_count == 1:
                    log(
                        f"Worker {os.getpid()} packet processing failed: {exc} "
                        "(further errors this window are counted in stats)",
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


def start_worker(slot, listen_ip, listen_port, target_ip, target_port, use_reuseport,
                 protocol_value=DEFAULT_PROTOCOL_VALUE):
    process = multiprocessing.Process(
        target=run_worker,
        args=(listen_ip, listen_port, target_ip, target_port, use_reuseport,
              protocol_value),
        name=f"syslog-formatter-{slot}",
    )
    process.start()
    return process


def start_server(args):
    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    workers = args.workers
    use_reuseport = supports_reuseport()
    if workers > 1 and not use_reuseport:
        log(
            "SO_REUSEPORT is unavailable; falling back to a single worker.",
            level="WARN",
        )
        workers = 1

    print("==================================================")
    print("   Forcepoint Syslog Formatter")
    print("==================================================")
    print(f"Workers   : {workers}")
    print(f"Listen    : {args.listen_ip}:{args.listen_port}")
    print(f"Forward   : {args.target_ip}:{args.target_port}")
    if args.config_path:
        print(f"Config    : {args.config_path}")
    if args.protocol_value:
        print(f"Protocol  : proto={args.protocol_value} (synthesised)")
    else:
        print("Protocol  : not injected")
    print("Features  : Header fix + timestamp normalization + minimal flow projection")
    print("==================================================")

    processes = {}
    for slot in range(workers):
        processes[slot] = start_worker(
            slot,
            args.listen_ip,
            args.listen_port,
            args.target_ip,
            args.target_port,
            use_reuseport,
            args.protocol_value,
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
                    use_reuseport,
                    args.protocol_value,
                )

            time.sleep(SUPERVISOR_POLL_SECONDS)
    finally:
        for process in processes.values():
            if process.is_alive():
                process.terminate()
        for process in processes.values():
            process.join(timeout=5)


def udp_port_bound(port):
    """Return True if any local socket is bound to the given UDP port."""
    needle = f":{port:04X}"
    for proc_path in ("/proc/net/udp", "/proc/net/udp6"):
        try:
            with open(proc_path, encoding="ascii") as handle:
                next(handle, None)  # skip header
                for line in handle:
                    fields = line.split()
                    if len(fields) > 1 and fields[1].endswith(needle):
                        return True
        except OSError:
            continue
    return False


def service_active(service_name):
    """Return True if systemd reports the unit as active."""
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", service_name],
        check=False,
    )
    return result.returncode == 0


def healthcheck(args):
    """Report on the data path; never repair it.

    FlowLink and the relay are both systemd services now, so restarting is
    systemd's job. What systemd cannot see is the "process is active but
    nothing is bound" blind spot — a FlowLink running a config without our
    consumer, or a relay whose workers die in a bind loop while the
    supervisor stays up. That is what the port checks are for.
    """
    critical = False
    print(f"=== Starting Health Check: {time.strftime('%Y%m%d_%H%M%S')} ===")
    if args.config_path:
        print(f"Config: {args.config_path}")

    if service_active(args.flowlink_service):
        print(f"[OK] FlowLink ({args.flowlink_service}) is running.")
    else:
        critical = True
        print(f"[CRITICAL] FlowLink ({args.flowlink_service}) is DOWN!")
        print(f"   -> Inspect: systemctl status {args.flowlink_service}")

    if udp_port_bound(args.flowlink_port):
        print(f"[OK] UDP {args.flowlink_port} is bound (FlowLink consumer).")
    else:
        critical = True
        print(
            f"[CRITICAL] UDP {args.flowlink_port} is NOT bound; "
            "FlowLink is not consuming flows!"
        )
        print("   -> Check the consumer block in /etc/illumio-flowlink/config.yaml")

    if service_active(args.relay_service):
        print(f"[OK] Syslog Formatter ({args.relay_service}) is running.")
    else:
        critical = True
        print(f"[CRITICAL] Syslog Formatter ({args.relay_service}) is DOWN!")
        print(f"   -> Inspect: journalctl -u {args.relay_service} -n 50")

    if udp_port_bound(args.relay_port):
        print(f"[OK] UDP {args.relay_port} is bound (relay listener).")
    else:
        critical = True
        print(
            f"[CRITICAL] UDP {args.relay_port} is NOT bound; "
            "relay workers are not listening!"
        )

    print("=== Check Finished ===")
    print("")
    if critical:
        sys.exit(1)


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
        "--protocol-value",
        default=config_defaults.get("protocol_value", DEFAULT_PROTOCOL_VALUE),
        help=(
            "Value injected as the proto= tag FlowLink parses as protocol "
            "(default 6 = TCP). Empty string disables injection."
        ),
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
        "--flowlink-service",
        default=config_defaults.get("flowlink_service", DEFAULT_FLOWLINK_SERVICE),
        help="systemd unit of the FlowLink daemon (installed by the RPM).",
    )
    health_parser.add_argument(
        "--relay-service",
        default=config_defaults.get("relay_service", DEFAULT_RELAY_SERVICE),
    )
    health_parser.add_argument(
        "--relay-port",
        type=int,
        default=config_defaults.get("listen_port", DEFAULT_LISTEN_PORT),
        help="UDP port the relay must have bound.",
    )
    health_parser.add_argument(
        "--flowlink-port",
        type=int,
        default=config_defaults.get("target_port", DEFAULT_TARGET_PORT),
        help="UDP port FlowLink must have bound.",
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
