#!/usr/bin/env bash
# One-click deployment for the FlowLink Forcepoint Syslog Formatter relay.
#
# Scope since FlowLink 2.0.0: this script deploys ONLY the relay. FlowLink
# itself is a Linux service owned by the RPM (illumio-flowlink.service,
# /etc/illumio-flowlink/config.yaml) — we never install units for it, never
# start it with illumio-flowlink-ctl, and never rewrite its config.
#
# Usage:
#   sudo ./deploy.sh            # deploy + verify
#   sudo ./deploy.sh --dry-run  # print actions without changing anything
#
# Preconditions (checked in preflight):
#   - run as root (UDP 514 + systemd)
#   - python3 installed
#   - illumio-flowlink RPM installed and its service present
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/opt/flowlink
FLOWLINK_SERVICE=illumio-flowlink
FLOWLINK_CONFIG=/etc/illumio-flowlink/config.yaml
SYSCTL_FILE=/etc/sysctl.d/99-flowlink-udp.conf
RELAY_PORT=514
CONSUMER_PORT=5514
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1
ts="$(date +%Y%m%d_%H%M%S)"

log()  { echo "[deploy] $*"; }
fail() { echo "[deploy] ERROR: $*" >&2; exit 1; }
run()  { if (( DRY_RUN )); then echo "[dry-run] $*"; else "$@"; fi; }

# --- 1. Preflight -----------------------------------------------------------
log "Preflight checks..."
if (( ! DRY_RUN )) && [[ ${EUID} -ne 0 ]]; then
    fail "must run as root (UDP 514 is a privileged port; systemd install required)."
fi
command -v python3 >/dev/null || fail "python3 not found."
systemctl cat "${FLOWLINK_SERVICE}" >/dev/null 2>&1 \
    || fail "${FLOWLINK_SERVICE} service not found; install the FlowLink 2.0.0 RPM first."
[[ -r "${FLOWLINK_CONFIG}" ]] || fail "${FLOWLINK_CONFIG} not readable; FlowLink is not configured."

# UDP 514 must be free, unless it is our own relay from a previous deploy.
if ss -uln "sport = :${RELAY_PORT}" | grep -q ":${RELAY_PORT}" \
   && ! systemctl is-active --quiet syslog-formatter.service; then
    fail "UDP ${RELAY_PORT} already in use: $(ss -ulpn "sport = :${RELAY_PORT}" | tail -n +2)"
fi
log "Preflight OK."

# --- 2. Remove artefacts this project used to install ------------------------
# Pre-2.0.0 we shipped units and a logrotate rule for FlowLink itself. The RPM
# now owns all of that; leaving ours behind actively breaks things (our
# logrotate rule targets the abandoned /opt/flowlink/data/flowlink.log and
# fails logrotate.service daily). Every step is a no-op on a clean host.
for unit in flowlink-start.service flowlink-healthcheck.service flowlink-healthcheck.timer; do
    if [[ -f "/etc/systemd/system/${unit}" ]]; then
        log "Removing obsolete ${unit} (superseded by ${FLOWLINK_SERVICE})..."
        run systemctl disable --now "${unit}" || true
        run rm -f "/etc/systemd/system/${unit}"
    fi
    # systemd keeps a failed entry around after the unit file disappears.
    run systemctl reset-failed "${unit}" 2>/dev/null || true
    # ...and the template we used to ship alongside it.
    [[ -f "${DEST}/systemd/${unit}" ]] && run rm -f "${DEST}/systemd/${unit}"
done

if [[ -f /etc/logrotate.d/flowlink ]]; then
    log "Removing obsolete /etc/logrotate.d/flowlink (RPM ships flowlink-logrotate.conf)..."
    run rm -f /etc/logrotate.d/flowlink
fi

if [[ -f /etc/systemd/system/syslog-relay.service ]]; then
    log "Legacy syslog-relay.service found; disabling and removing..."
    run systemctl disable --now syslog-relay.service || true
    run rm -f /etc/systemd/system/syslog-relay.service
fi

if [[ -f "${DEST}/monitor_services.sh" ]]; then
    log "Legacy monitor_services.sh found; disabling..."
    run mv "${DEST}/monitor_services.sh" "${DEST}/monitor_services.sh.disabled.${ts}"
fi

# Root crontab: drop legacy flowlink jobs (30-min monitor loop, 03:00 truncate
# + crash-backup cleanup, weekly monitor-log rotation). Non-flowlink jobs kept.
LEGACY_CRON_RE='monitor_services\.sh|flowlink_crash_backups|/opt/flowlink/data/flowlink\.log|flowlink_monitor\.log'
if crontab -l 2>/dev/null | grep -Eq "${LEGACY_CRON_RE}"; then
    log "Legacy flowlink cron jobs found in root crontab; removing..."
    if (( DRY_RUN )); then
        echo "[dry-run] back up root crontab to ${DEST}/crontab.root.bak.${ts}, then strip legacy entries"
    else
        mkdir -p "${DEST}"
        crontab -l > "${DEST}/crontab.root.bak.${ts}"
        remaining="$(crontab -l | grep -Ev "${LEGACY_CRON_RE}" || true)"
        if echo "${remaining}" | grep -Eq '^[^#[:space:]]'; then
            echo "${remaining}" | crontab -
            log "Kept non-flowlink cron jobs; backup: crontab.root.bak.${ts}"
        else
            crontab -r
            log "No other active cron jobs; root crontab cleared. Backup: crontab.root.bak.${ts}"
        fi
    fi
fi

run systemctl daemon-reload

# --- 3. Install relay files --------------------------------------------------
log "Installing relay files to ${DEST} ..."
run mkdir -p "${DEST}/systemd"

# Dedicated unprivileged user for the relay (syslog-formatter.service binds
# UDP 514 via CAP_NET_BIND_SERVICE instead of running as root).
if ! id -u flowlink >/dev/null 2>&1; then
    run useradd --system --no-create-home --shell /usr/sbin/nologin flowlink
fi

# Back up a site-edited ini before overwriting.
if [[ -f "${DEST}/syslog-formatter.ini" ]] \
   && ! cmp -s "${REPO_DIR}/syslog-formatter.ini" "${DEST}/syslog-formatter.ini"; then
    run cp -p "${DEST}/syslog-formatter.ini" "${DEST}/syslog-formatter.ini.bak.${ts}"
    log "Existing syslog-formatter.ini differs from repo; backed up to syslog-formatter.ini.bak.${ts}"
fi

run cp "${REPO_DIR}/syslog-formatter.py" "${REPO_DIR}/syslog-formatter.ini" "${DEST}/"
run cp "${REPO_DIR}/systemd/syslog-formatter.service" "${DEST}/systemd/"

# --- 4. UDP buffer sysctl (persistent; see system-setting.txt) --------------
log "Applying UDP buffer sysctl settings..."
if (( DRY_RUN )); then
    echo "[dry-run] write ${SYSCTL_FILE} and apply with sysctl -p"
else
    cat > "${SYSCTL_FILE}" <<'EOF'
# FlowLink syslog relay: enlarge UDP receive buffers (see system-setting.txt)
net.core.rmem_max = 26214400
net.core.rmem_default = 26214400
net.ipv4.udp_mem = 8388608 12582912 16777216
EOF
    sysctl -p "${SYSCTL_FILE}" >/dev/null
fi

# --- 5. systemd unit (relay only) -------------------------------------------
log "Installing syslog-formatter.service..."
run cp "${REPO_DIR}/systemd/syslog-formatter.service" /etc/systemd/system/
run systemctl daemon-reload
run systemctl enable syslog-formatter.service
run systemctl restart syslog-formatter.service

# --- 6. FlowLink consumer check (we report, the operator edits) --------------
# Merging a consumer into the RPM-owned config.yaml is deliberately manual:
# that file also carries the site's netflow/ipfix consumers and PCE
# credentials, and a bad merge takes down flow collection entirely.
if ! grep -q "'${CONSUMER_PORT}'\|\"${CONSUMER_PORT}\"" "${FLOWLINK_CONFIG}"; then
    log "NOTE: ${FLOWLINK_CONFIG} has no consumer on UDP ${CONSUMER_PORT}."
    log "      Merge the block from ${REPO_DIR}/config-syslog.yml into it, then:"
    log "        sudo systemctl restart ${FLOWLINK_SERVICE}"
fi

if (( DRY_RUN )); then
    log "Dry-run complete. No changes were made."
    exit 0
fi

# --- 7. Verify (deployment is successful only if this passes) ---------------
log "Verifying deployment..."
sleep 2
systemctl is-active --quiet syslog-formatter.service || fail "syslog-formatter.service is not active."
systemctl is-active --quiet "${FLOWLINK_SERVICE}" || fail "${FLOWLINK_SERVICE} is not active."

python3 "${DEST}/syslog-formatter.py" --config "${DEST}/syslog-formatter.ini" healthcheck \
    || fail "healthcheck reported failures; see output above."

log "Deployment OK: relay + FlowLink healthy, UDP ${RELAY_PORT}/${CONSUMER_PORT} listening."
