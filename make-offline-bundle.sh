#!/usr/bin/env bash
# Build a self-contained tarball for air-gapped / customer sites.
#
# The bundle carries everything needed to deploy the relay on a host that has
# no network access and no git: the relay, its unit template, deploy.sh, the
# consumer snippet to merge, the SOP and the migration runbook.
#
# NOT included (and cannot be): the FlowLink RPM itself. Download it from the
# Illumio Support Portal and carry it alongside this bundle.
#
# Usage:
#   ./make-offline-bundle.sh            # build into ./dist/
#   ./make-offline-bundle.sh /tmp/out   # build into a chosen directory
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${1:-${REPO_DIR}/dist}"
cd "${REPO_DIR}"

# Files the bundle ships.
FILES=(
    deploy.sh
    syslog-formatter.py
    syslog-formatter.ini
    config-syslog.yml
    systemd/syslog-formatter.service
    readme.txt
    system-setting.txt
    docs/flowlink-2.0.0-migration-runbook.md
)

for f in "${FILES[@]}"; do
    [[ -f "${f}" ]] || { echo "ERROR: missing ${f}" >&2; exit 1; }
done

# Stamp the bundle with the commit it was built from, so a site can report
# exactly what it is running. Falls back gracefully outside a git checkout.
if git rev-parse --git-dir >/dev/null 2>&1; then
    sha="$(git rev-parse --short HEAD)"
    if ! git diff --quiet || ! git diff --cached --quiet; then
        echo "WARNING: working tree has uncommitted changes; bundling them as -dirty." >&2
        sha="${sha}-dirty"
    fi
else
    sha="nogit"
fi

stamp="$(date +%Y%m%d)"
name="flowlink-formatter-${stamp}-${sha}"
staging="$(mktemp -d)"
trap 'rm -rf "${staging}"' EXIT

mkdir -p "${staging}/${name}"
for f in "${FILES[@]}"; do
    mkdir -p "${staging}/${name}/$(dirname "${f}")"
    cp -p "${f}" "${staging}/${name}/${f}"
done
chmod +x "${staging}/${name}/deploy.sh"

cat > "${staging}/${name}/INSTALL.txt" <<EOF
FlowLink Forcepoint Syslog Formatter — 離線部署包
建置時間：$(date '+%Y-%m-%d %H:%M:%S %Z')
版本：${name}

前置條件
--------
1. FlowLink 2.0.0 RPM 已安裝，且 illumio-flowlink service 存在
   （本包不含 RPM，請從 Illumio Support Portal 另外取得）
2. /etc/illumio-flowlink/config.yaml 已設定好 pce_addr 與 api_key
3. python3 已安裝
4. 以 root 執行

快速部署
--------
    tar xzf ${name}.tar.gz
    cd ${name}
    sudo ./deploy.sh --dry-run      # 先看它要做什麼
    sudo ./deploy.sh                # 部署 + 驗證

deploy.sh 只部署 relay，並清除本專案舊版留下的 systemd unit 與 logrotate
規則。它不會改動 RPM 擁有的任何檔案。

還要手動做的一步
----------------
把 config-syslog.yml 裡的 consumers: 片段併入
/etc/illumio-flowlink/config.yaml，然後：

    sudo systemctl restart illumio-flowlink

這步刻意不自動化：該檔同時放著站點既有的 netflow/ipfix consumer 與 PCE
認證，合併失誤會讓整個流量收集停擺。

驗證
----
    python3 /opt/flowlink/syslog-formatter.py \\
        --config /opt/flowlink/syslog-formatter.ini healthcheck

四項全部 [OK] 才算完成。分層驗證與疑難排解見
docs/flowlink-2.0.0-migration-runbook.md 步驟 06。

檔案
----
    INSTALL.txt         本說明
    deploy.sh           一鍵部署
    syslog-formatter.py relay 主程式
    syslog-formatter.ini relay 設定
    config-syslog.yml   要併入 FlowLink 設定的 consumer 片段
    systemd/            relay 的 unit 模板
    readme.txt          完整 SOP
    system-setting.txt  核心參數備忘
    docs/flowlink-2.0.0-migration-runbook.md
                        舊環境遷移到 2.0.0 的逐步程序
EOF

mkdir -p "${OUT_DIR}"
tarball="${OUT_DIR}/${name}.tar.gz"
tar -czf "${tarball}" -C "${staging}" "${name}"
( cd "${OUT_DIR}" && sha256sum "${name}.tar.gz" > "${name}.tar.gz.sha256" )

echo "Bundle : ${tarball}"
echo "Size   : $(du -h "${tarball}" | cut -f1)"
echo "SHA256 : $(cut -d' ' -f1 "${OUT_DIR}/${name}.tar.gz.sha256")"
echo
echo "Contents:"
tar -tzf "${tarball}" | sed 's/^/  /'
