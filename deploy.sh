#!/usr/bin/env bash
# One-click deployment for FlowLink Forcepoint Syslog Formatter.
#
# Usage:
#   sudo ./deploy.sh            # deploy + verify
#   sudo ./deploy.sh --dry-run  # print actions without changing anything
#
# Preconditions (checked in preflight):
#   - run as root (UDP 514 + systemd)
#   - python3 and /usr/local/bin/illumio-flowlink-ctl installed
#   - /opt/flowlink/api_info contains the real PCE API key (see readme.txt §8.2)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/opt/flowlink
API_INFO="${DEST}/api_info"
BACKUP_DIR=/var/log/flowlink_crash_backups
SYSCTL_FILE=/etc/sysctl.d/99-flowlink-udp.conf
FLOWLINK_CTL=/usr/local/bin/illumio-flowlink-ctl   # must match flowlink_cmd in syslog-formatter.ini
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
[[ -x "${FLOWLINK_CTL}" ]] || fail "${FLOWLINK_CTL} not found; install FlowLink first (not covered by this script)."
[[ -s "${API_INFO}" ]] || fail "${API_INFO} missing or empty; put the PCE API key there first (readme.txt §8.2)."
run chmod 600 "${API_INFO}"

# --- 1.5 Legacy replacement (readme.txt §8.4) -------------------------------
# Old deployments run syslog-relay.service + monitor_services.sh cron jobs.
# Every step below is a no-op on a clean host.
if [[ -f /etc/systemd/system/syslog-relay.service ]]; then
    log "Legacy syslog-relay.service found; disabling and removing..."
    run systemctl disable --now syslog-relay.service
    run rm -f /etc/systemd/system/syslog-relay.service
    run systemctl daemon-reload
fi

if [[ -f "${DEST}/monitor_services.sh" ]]; then
    log "Legacy monitor_services.sh found; disabling..."
    run mv "${DEST}/monitor_services.sh" "${DEST}/monitor_services.sh.disabled.${ts}"
fi

# Root crontab: drop legacy flowlink jobs (30-min monitor loop, 03:00
# truncate + crash-backup cleanup, weekly monitor-log rotation). The original
# crontab is backed up to BACKUP_DIR first; non-flowlink jobs are kept.
LEGACY_CRON_RE='monitor_services\.sh|flowlink_crash_backups|/opt/flowlink/data/flowlink\.log|flowlink_monitor\.log'
if crontab -l 2>/dev/null | grep -Eq "${LEGACY_CRON_RE}"; then
    log "Legacy flowlink cron jobs found in root crontab; removing..."
    if (( DRY_RUN )); then
        echo "[dry-run] back up root crontab to ${BACKUP_DIR}/crontab.root.bak.${ts}, then strip legacy entries"
    else
        mkdir -p "${BACKUP_DIR}"
        crontab -l > "${BACKUP_DIR}/crontab.root.bak.${ts}"
        remaining="$(crontab -l | grep -Ev "${LEGACY_CRON_RE}" || true)"
        if echo "${remaining}" | grep -Eq '^[^#[:space:]]'; then
            # Other active jobs exist: keep them (stale comments may remain).
            echo "${remaining}" | crontab -
            log "Kept non-flowlink cron jobs; backup: crontab.root.bak.${ts}"
        else
            crontab -r
            log "No other active cron jobs; root crontab cleared. Backup: crontab.root.bak.${ts}"
        fi
    fi
fi

# UDP 514 must be free, unless it is our own relay from a previous deploy.
if ss -uln 'sport = :514' | grep -q ':514' && ! systemctl is-active --quiet syslog-formatter.service; then
    if (( DRY_RUN )) && systemctl is-active --quiet syslog-relay.service; then
        # Real run would have stopped the legacy relay above already.
        log "NOTE: UDP 514 is held by legacy syslog-relay (would be stopped by the step above)."
    else
        fail "UDP 514 already in use by another process: $(ss -ulpn 'sport = :514' | tail -n +2)"
    fi
fi
log "Preflight OK."

# --- 2. Install files -------------------------------------------------------
log "Installing files to ${DEST} ..."
run mkdir -p "${DEST}/data" "${DEST}/systemd" "${BACKUP_DIR}"

# Dedicated unprivileged user for the relay (syslog-formatter.service
# binds UDP 514 via CAP_NET_BIND_SERVICE instead of running as root).
if ! id -u flowlink >/dev/null 2>&1; then
    run useradd --system --no-create-home --shell /usr/sbin/nologin flowlink
fi

# Back up site-edited configs before overwriting.
for f in config-syslog.yml syslog-formatter.ini; do
    if [[ -f "${DEST}/${f}" ]] && ! cmp -s "${REPO_DIR}/${f}" "${DEST}/${f}"; then
        run cp -p "${DEST}/${f}" "${DEST}/${f}.bak.${ts}"
        run chmod 600 "${DEST}/${f}.bak.${ts}"   # old backups may contain a literal api_key
        log "Existing ${f} differs from repo; backed up to ${f}.bak.${ts}"
    fi
done

run cp "${REPO_DIR}/syslog-formatter.py" "${REPO_DIR}/syslog-formatter.ini" "${REPO_DIR}/config-syslog.yml" "${DEST}/"
run cp "${REPO_DIR}"/systemd/*.service "${REPO_DIR}"/systemd/*.timer "${DEST}/systemd/"

# --- 3. API credentials ------------------------------------------------------
# config-syslog.yml ships with `api_key: /opt/flowlink/api_info`; FlowLink
# opens that path itself (file format: "api_<key_id> <secret>"). No injection
# needed — preflight already verified the file exists and chmod'ed it 600.

# --- 3.5 Switch FlowLink off a legacy config (e.g. config-netflow.yml) ------
# A FlowLink that is RUNNING but not bound to UDP 5514 (target_port in
# syslog-formatter.ini) is consuming with a legacy config; restart it with
# config-syslog.yml so the verify step passes. NOTE: if a netflow consumer is
# still required on this host, merge it into config-syslog.yml first (§8.4).
if (( DRY_RUN )); then
    echo "[dry-run] restart FlowLink with ${DEST}/config-syslog.yml if running without UDP 5514 bound"
elif "${FLOWLINK_CTL}" status 2>/dev/null | grep -q 'FlowLink: RUNNING' && \
     ! ss -uln 'sport = :5514' | grep -q ':5514'; then
    log "FlowLink is running but UDP 5514 is not bound (legacy config?); restarting with config-syslog.yml ..."
    "${FLOWLINK_CTL}" stop || true
    sleep 2
    "${FLOWLINK_CTL}" start --config "${DEST}/config-syslog.yml" \
        || fail "FlowLink failed to start with config-syslog.yml."
fi

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

# --- 5. logrotate (FlowLink keeps its log fd open; copytruncate inside) -----
log "Installing logrotate config..."
run cp "${REPO_DIR}/logrotate/flowlink" /etc/logrotate.d/flowlink

# --- 6. systemd units -------------------------------------------------------
log "Installing systemd units..."
run cp "${REPO_DIR}"/systemd/*.service "${REPO_DIR}"/systemd/*.timer /etc/systemd/system/
run systemctl daemon-reload
run systemctl enable syslog-formatter.service
run systemctl restart syslog-formatter.service
run systemctl enable flowlink-start.service
run systemctl enable --now flowlink-healthcheck.timer

if (( DRY_RUN )); then
    log "Dry-run complete. No changes were made."
    exit 0
fi

# --- 7. Verify (deployment is successful only if this passes) ---------------
log "Verifying deployment..."
sleep 2
systemctl is-active --quiet syslog-formatter.service || fail "syslog-formatter.service is not active."

# healthcheck always exits 0; judge by its output. A stopped FlowLink is
# auto-started here ("Restart SUCCESS." counts as healthy).
hc_out="$(python3 "${DEST}/syslog-formatter.py" --config "${DEST}/syslog-formatter.ini" healthcheck 2>&1)" || true
echo "${hc_out}"
if echo "${hc_out}" | grep -qE 'FAILED|status check failed'; then
    fail "healthcheck reported failures; see output above."
fi

for port in 514 5514; do
    ss -uln "sport = :${port}" | grep -q ":${port}" || fail "UDP ${port} is not listening."
done

log "Deployment OK: relay + FlowLink healthy, UDP 514/5514 listening."
