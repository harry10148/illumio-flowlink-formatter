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

# UDP 514 must be free, unless it is our own relay from a previous deploy.
if ss -uln 'sport = :514' | grep -q ':514' && ! systemctl is-active --quiet syslog-formatter.service; then
    fail "UDP 514 already in use by another process: $(ss -ulpn 'sport = :514' | tail -n +2)"
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

# Back up site-edited configs before overwriting (config-syslog.yml always
# differs after api_key injection, so a backup is created on every re-deploy).
ts="$(date +%Y%m%d_%H%M%S)"
for f in config-syslog.yml syslog-formatter.ini; do
    if [[ -f "${DEST}/${f}" ]] && ! cmp -s "${REPO_DIR}/${f}" "${DEST}/${f}"; then
        run cp -p "${DEST}/${f}" "${DEST}/${f}.bak.${ts}"
        log "Existing ${f} differs from repo; backed up to ${f}.bak.${ts}"
    fi
done

run cp "${REPO_DIR}/syslog-formatter.py" "${REPO_DIR}/syslog-formatter.ini" "${REPO_DIR}/config-syslog.yml" "${DEST}/"
run cp "${REPO_DIR}"/systemd/*.service "${REPO_DIR}"/systemd/*.timer "${DEST}/systemd/"

# --- 3. Inject api_key (fail-fast: REPLACE_ME must not survive) -------------
log "Injecting api_key from ${API_INFO} ..."
if (( ! DRY_RUN )); then
    API_KEY="$(tr -d '[:space:]' < "${API_INFO}")"
    [[ -n "${API_KEY}" ]] || fail "API key in ${API_INFO} is empty after trimming whitespace."
    sed -i "s|^api_key:.*|api_key: ${API_KEY//&/\\&}|" "${DEST}/config-syslog.yml"
    if grep -q '^api_key:.*REPLACE_ME' "${DEST}/config-syslog.yml"; then
        fail "api_key injection failed; REPLACE_ME still present in ${DEST}/config-syslog.yml."
    fi
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
