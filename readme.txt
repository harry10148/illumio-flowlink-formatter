# FlowLink Forcepoint Syslog Formatter SOP

- 文件位置: `/opt/flowlink/`
- Repo 位置: `/home/harry/rd/illumio-flowlink-formatter/`
- 最後更新: `2026-08-11`
- 適用對象: 系統管理員 / 運維人員
- 核心功能: 接收 Forcepoint CEF Syslog，轉成 FlowLink 可接受的最小流量資料，再由 FlowLink 上傳到 SaaS / PCE
- 適用 FlowLink 版本: **2.0.0 以上**（FlowLink 已是 Linux service）

## 1. 專案目的

這個專案不是 FlowLink 主程式本身，而是部署在 FlowLink 前面的格式化層。

它負責兩件事：

1. 接收 Forcepoint Web Proxy 送出的 CEF / Syslog。
2. 將資料整理成 FlowLink `text` consumer 需要的最小欄位，再轉送到本機 FlowLink。

FlowLink 的用途是做基本流量正規化與聚合，提供 PCE / Illumination 顯示流量關聯，不是保存完整 proxy 事件內容。基於這個用途，formatter 只轉送最小必要欄位，而不再轉送大量文字欄位。

### 1.1 職責邊界（FlowLink 2.0.0 之後）

FlowLink 2.0.0 起由 RPM 安裝成 Linux service，行程存活、開機自啟、log 輪替全部由官方負責。本專案**只**管 relay：

| 項目 | 負責者 |
|---|---|
| FlowLink 行程、開機自啟 | RPM 的 `illumio-flowlink.service` |
| FlowLink 設定 | `/etc/illumio-flowlink/config.yaml`（RPM 擁有） |
| FlowLink log 輪替 | `/etc/logrotate.d/flowlink-logrotate.conf`（RPM 擁有） |
| syslog relay（本專案） | `syslog-formatter.service` |
| 資料路徑巡檢 | `syslog-formatter.py healthcheck`（手動執行） |

**不要**再自建 `flowlink-start.service`、`flowlink-healthcheck.timer` 或指向
`/opt/flowlink/data/flowlink.log` 的 logrotate 規則。舊部署留下的這些檔案會與
RPM 衝突（例如舊 logrotate 規則指向已停用的 log 路徑，會讓 `logrotate.service`
每天失敗），`deploy.sh` 會自動移除。

## 2. 資料流與 Port

資料流如下：

`Forcepoint -> syslog-formatter.py -> FlowLink -> SaaS / PCE`

Port 對應如下：

- Forcepoint -> formatter: `UDP 514`
- formatter -> FlowLink: `UDP 5514`
- FlowLink -> SaaS / PCE: `TCP 443`

## 3. 目前轉送策略

Formatter 目前會做以下處理：

1. 若原始訊息直接以 `CEF:` 開頭，補上標準 syslog header。
2. 將 `start` / `end` / `rt` / `art` 的 13 位毫秒時間戳轉成 10 位秒級時間戳。
3. 從 CEF extension 中只保留 FlowLink 目前真正需要的欄位：
   - `start`
   - `src`
   - `dst`
   - `dpt`
4. 補上 `proto=6`（TCP）。原始 Forcepoint proxy 事件沒有任何協定欄位，但
   FlowLink 的 text parser 一定要有 protocol 來源（見 §3.1）。
5. 保留 CEF 基本頭，將整理後的結果轉送到 FlowLink。

範例輸出：

```text
<14>Aug 11 15:41:11 Forcepoint-Relay CEF:0|Forcepoint|Security|8.5.4|227|Transaction permitted|Low| start=1763015021 src=192.168.72.35 dst=162.247.243.29 dpt=443 proto=6
```

這樣做的原因：

- FlowLink 目前設定只會使用 `src` / `dst` / `dpt` / `start` / `proto`
- 原始 CEF 中的 `suser`、`cs1`、`request` 等欄位可能包含中文與長字串
- 這些欄位對目前的 FlowLink 用途沒有直接價值，卻會增加格式相容性與封包長度風險

驗證結論（2026-08-11 於 ilo-flow 實機重測）：

- 原始事件是裸 `CEF:` 記錄，沒有 syslog header，因此補 header 是必要的
- 原始事件的 `start` / `end` / `rt` / `art` 確實是 13 位毫秒時間戳
- 原始事件中 `suser` 和 `cs1` 含有中文
- 經過最小欄位投影後，單筆真實事件從 `1497 bytes` 降到 `169 bytes`，
  且輸出中的非 ASCII 位元組數為 `0`

### 3.1 為什麼要自己補 `proto=6`

`protocol` 是 FlowLink text parser 的**必填**設定（見 RPM 內的
`/usr/local/illumio/flowlink_config_schema.json`），而 Forcepoint 的 proxy
transaction 事件裡根本沒有協定欄位。實機驗證行為如下：

| 送進 FlowLink 的內容 | 結果 |
|---|---|
| 完全沒有 `proto=` 欄位 | 不會被丟棄，但協定資訊缺失 |
| `proto=6` / `proto=17` | 正常解析成 flow |
| `proto=xyz`（無法解析） | `Discarding flow due to invalid protocol: xyz`，**整筆丟棄** |

因為 proxy transaction 一律是 TCP，relay 直接補 `proto=6` 最精確。此值可用
`syslog-formatter.ini` 的 `[relay] protocol_value` 調整，留空則不注入。

## 4. 關鍵檔案

本專案部署到 `/opt/flowlink/`：

- `syslog-formatter.py`: relay 主程式（另含 `healthcheck` 子命令）
- `syslog-formatter.ini`: relay 執行設定檔
- `systemd/syslog-formatter.service`: relay 的 unit 模板

FlowLink 本體（RPM 擁有，**不要**由本專案覆寫）：

- `/usr/local/bin/flowlink`、`/usr/local/bin/illumio-flowlink-ctl`
- `/etc/illumio-flowlink/config.yaml`: FlowLink 設定檔
- `/etc/illumio-flowlink/api_info`: PCE API 認證檔
- `/var/lib/illumio-flowlink/data/`: 資料目錄
- `/var/log/illumio-flowlink/flowlink.log`: FlowLink log
- 服務帳號: `ilo-flowlink`

Repo 中主要檔案如下：

- [syslog-formatter.py](syslog-formatter.py)
- [syslog-formatter.ini](syslog-formatter.ini)
- [config-syslog.yml](config-syslog.yml)（要**併入** `/etc/illumio-flowlink/config.yaml` 的 consumer 片段）
- [systemd/syslog-formatter.service](systemd/syslog-formatter.service)
- [deploy.sh](deploy.sh)

## 5. 執行方式

### 5.1 手動啟動 relay

```bash
python3 /opt/flowlink/syslog-formatter.py --config /opt/flowlink/syslog-formatter.ini run
```

### 5.2 手動執行健康檢查

```bash
python3 /opt/flowlink/syslog-formatter.py --config /opt/flowlink/syslog-formatter.ini healthcheck
```

### 5.3 設定檔搜尋順序

若未指定 `--config`，程式會依序使用：

1. 環境變數 `SYSLOG_FORMATTER_CONFIG`
2. 與程式同目錄的 `syslog-formatter.ini`

## 6. syslog-formatter.ini 說明

目前預設內容如下：

```ini
[relay]
listen_ip = 0.0.0.0
listen_port = 514
target_ip = 127.0.0.1
target_port = 5514
workers = 4
protocol_value = 6

[healthcheck]
flowlink_service = illumio-flowlink
relay_service = syslog-formatter
```

### `[relay]`

- `listen_ip`: 接收 Forcepoint syslog 的位址
- `listen_port`: 接收 Forcepoint syslog 的 UDP port
- `target_ip`: 轉送到 FlowLink 的目標 IP
- `target_port`: 轉送到 FlowLink 的目標 port
- `workers`: relay worker 數量；若作業系統不支援 `SO_REUSEPORT`，會自動退回單一 worker 並在啟動 log 印出警告
- `protocol_value`: 注入的 `proto=` 值（`6` = TCP，見 §3.1）；留空則不注入

### `[healthcheck]`

- `flowlink_service`: FlowLink 的 systemd unit 名稱（RPM 安裝，預設 `illumio-flowlink`）
- `relay_service`: relay 的 systemd unit 名稱

healthcheck 另外會檢查 UDP port 綁定（relay 用 `[relay] listen_port`、FlowLink 用
`[relay] target_port`），可用 `--relay-port` / `--flowlink-port` 覆寫。

**healthcheck 只回報、不修復**：FlowLink 與 relay 都是 systemd 服務，重啟是
systemd 的職責。healthcheck 的價值在於補 systemd 看不到的盲區——「行程 active
但沒有 bind port」（例如 FlowLink 跑在沒有 5514 consumer 的設定上，或 relay 的
worker 反覆 bind 失敗而 supervisor 仍活著）。

## 7. 部署方式

### 7.1 一鍵部署

```bash
sudo ./deploy.sh            # 部署 + 驗證
sudo ./deploy.sh --dry-run  # 只列出動作
```

前置條件：FlowLink 2.0.0 RPM 已安裝且 `illumio-flowlink` service 存在、
`/etc/illumio-flowlink/config.yaml` 可讀、python3 已安裝。缺一即硬停報錯。

deploy.sh 的動作：

1. **清除本專案舊版留下的檔案**：`flowlink-start.service`、
   `flowlink-healthcheck.service` / `.timer`、`/etc/logrotate.d/flowlink`，
   以及更舊的 `syslog-relay.service`、`monitor_services.sh`、legacy cron。
   乾淨主機上為 no-op。
2. 安裝 relay 檔案到 `/opt/flowlink/`，建立無特權帳號 `flowlink`
3. 套用 UDP buffer sysctl（`/etc/sysctl.d/99-flowlink-udp.conf`）
4. 安裝並啟用 `syslog-formatter.service`
5. 檢查 `/etc/illumio-flowlink/config.yaml` 是否有 UDP 5514 consumer；
   沒有就印出指引（**不會**自動改官方設定檔，見 §7.2）
6. 驗證：relay 與 FlowLink 都 active、UDP 514/5514 都 bound

### 7.2 FlowLink consumer 設定（手動）

`/etc/illumio-flowlink/config.yaml` 同時放著站點的 netflow/ipfix consumer 與 PCE
認證，合併失誤會整個流量收集停擺，所以這步刻意不自動化。把
[config-syslog.yml](config-syslog.yml) 的 `consumers:` 片段併進去後重啟：

```bash
sudo cp -p /etc/illumio-flowlink/config.yaml /etc/illumio-flowlink/config.yaml.bak.$(date +%F)
sudo vi /etc/illumio-flowlink/config.yaml
sudo systemctl restart illumio-flowlink
```

合併後的完整範例見 `config-syslog.yml` 檔尾註解。

### 7.3 驗證指令

```bash
systemctl status syslog-formatter.service
systemctl status illumio-flowlink
ss -ulnp | grep -E ':514|:5514'
python3 /opt/flowlink/syslog-formatter.py --config /opt/flowlink/syslog-formatter.ini healthcheck
```

### 7.4 從 FlowLink 1.x 部署升級到 2.0.0

官方升級流程是 `rpm -e illumio-flowlink` 後重裝新版 RPM。升級後路徑全部改變，
舊部署的殘留必須清掉：

| 舊路徑（1.x / 本專案自建） | 新路徑（2.0.0 RPM） |
|---|---|
| `/opt/flowlink/config-syslog.yml` | `/etc/illumio-flowlink/config.yaml` |
| `/opt/flowlink/data/` | `/var/lib/illumio-flowlink/data/` |
| `/opt/flowlink/data/flowlink.log` | `/var/log/illumio-flowlink/flowlink.log` |
| `flowlink-start.service`（自建） | `illumio-flowlink.service`（RPM） |
| `flowlink-healthcheck.timer`（自建） | 已移除，改為手動 healthcheck |
| `/etc/logrotate.d/flowlink`（自建） | `/etc/logrotate.d/flowlink-logrotate.conf`（RPM） |

升級後的收尾：

```bash
# 1. 確認 RPM 版本與服務
rpm -qa | grep illumio-flowlink
systemctl status illumio-flowlink

# 2. 把 syslog consumer 併進官方設定檔（§7.2），重啟 FlowLink

# 3. 跑 deploy.sh 清掉舊檔並重新部署 relay
sudo ./deploy.sh --dry-run
sudo ./deploy.sh

# 4. 舊資料目錄確認沒用到之後再刪（升級後不再寫入）
ls -la /opt/flowlink/data/
```

注意：升級時 `/etc/illumio-flowlink/config.yaml` 會是 RPM 的預設值（只有
netflow consumer），**syslog consumer 不會被保留**，必須自己併回去，否則
relay 轉出的封包沒有人接收，資料會無聲消失。

## 8. 日常健康檢查

### 8.1 執行 formatter healthcheck

```bash
python3 /opt/flowlink/syslog-formatter.py --config /opt/flowlink/syslog-formatter.ini healthcheck
```

正常輸出應包含：

```text
[OK] FlowLink (illumio-flowlink) is running.
[OK] UDP 5514 is bound (FlowLink consumer).
[OK] Syslog Formatter (syslog-formatter) is running.
[OK] UDP 514 is bound (relay listener).
```

任何一項 `[CRITICAL]` 都會讓 healthcheck 以 exit code 1 結束，方便外部排程接告警。

relay 每 60 秒會輸出一行統計到 journal（含閒置心跳）：

```text
Worker 12345 stats: relayed=1234 errors=0 truncated=0 window=60s
```

異常封包不會逐包寫 log（避免 journald 洪水），每個統計視窗只記第一筆的詳細原因，
其餘計入 `errors=`。

### 8.2 檢查 Port 監聽

```bash
ss -ulnp | grep -E ':514|:5514'
```

檢查重點：

- `UDP 514` 由 Python relay 佔用
- `UDP 5514` 由 FlowLink 佔用

### 8.3 檢查 FlowLink log

```bash
tail -f /var/log/illumio-flowlink/flowlink.log
```

重點訊息：

- `meter Forcepoint_CEF_log` 的 `count` 應隨流量增加（0 代表沒收到資料）
- 每 `aggregation_minutes` 一次的 `Reporting Flows` + `Flow count` + `Response Code 204`
- `Discarding flow due to invalid protocol:` 代表 protocol 欄位有問題（見 §3.1）

## 9. 故障處理

### 9.1 修改 FlowLink 設定後重啟

```bash
sudo vi /etc/illumio-flowlink/config.yaml
sudo systemctl restart illumio-flowlink
```

排錯時可用官方控制工具（要以服務帳號執行）：

```bash
sudo -u ilo-flowlink /usr/local/bin/illumio-flowlink-ctl status
sudo -u ilo-flowlink /usr/local/bin/illumio-flowlink-ctl stop
sudo -u ilo-flowlink /usr/local/bin/illumio-flowlink-ctl start -v
```

### 9.2 重啟 relay service

```bash
systemctl restart syslog-formatter.service
systemctl status syslog-formatter.service
```

worker 子進程已正確響應 SIGTERM，`systemctl restart` / `stop` 應在數秒內完成；
若觀察到 systemd 走 `SIGKILL` 強制終止，代表 worker 實際卡在處理上而非 signal
處理錯誤。

### 9.3 資料沒有進 PCE 時的排查順序

1. `journalctl -u syslog-formatter -n 20` → `relayed=` 是否在增加（0 表示 Forcepoint 沒送）
2. `ss -ulnp | grep 5514` → FlowLink 是否在聽（沒有代表 config.yaml 缺 consumer）
3. `grep -a "meter Forcepoint_CEF_log" -A2 /var/log/illumio-flowlink/flowlink.log | tail` → count 是否增加
4. `grep -a "Discarding\|Response Code" /var/log/illumio-flowlink/flowlink.log | tail` → 解析與上傳結果

## 10. 定期維護

- FlowLink log 輪替：由 RPM 的 `/etc/logrotate.d/flowlink-logrotate.conf` 處理
  （200M / 保留 7 份 / copytruncate / 歸檔到 `/var/log/illumio-flowlink/archive`），
  **不要**再自建規則
- relay log 進 journald，由 systemd journal 的既有輪替策略處理
- 舊版的 cron 巡檢（`monitor_services.sh`、03:00 truncate）已全部退場，
  `deploy.sh` 會自動清除

## 11. 目前已知注意事項

### 11.1 沒有自動巡檢

`flowlink-healthcheck.timer` 已移除，healthcheck 只在 `deploy.sh` 部署時與人工
執行時跑。若需要自動巡檢，可自行掛 cron 或 systemd timer 執行 §8.1 的指令並接
告警（exit code 1 代表有 `[CRITICAL]`）。

### 11.2 升級 FlowLink 會清掉 syslog consumer

見 §7.4。這是 2026-08-11 實際踩到的狀況：升級後 `config.yaml` 只剩 netflow
consumer，UDP 5514 無人監聽，relay 照常運作但轉出的封包全部落空。

### 11.3 CEF 跳脫 pipe 為已知限制

CEF header 欄位若含跳脫的 `\|`，`split("|", 7)` 會切錯位導致該封包被丟棄並計入
errors。Forcepoint 的固定欄位（vendor/product/version/signature/name/severity）
實務上不含 pipe，故僅列為已知限制不特別處理。

### 11.4 建議以防火牆限制 514 來源

relay 監聽 `0.0.0.0:514` 且不驗證來源，任何能路由到本機的設備都能注入假流量到
PCE Illumination。建議用主機防火牆（firewalld / nftables）只放行 Forcepoint
來源 IP 的 UDP 514。UDP 本身可偽造來源，此措施屬縱深防禦。

### 11.5 `api_key` 可以是檔案路徑或指令

依 RPM 內 `flowlink_config_schema.json` 的定義，`api_key` 是「提供認證的檔案
**或**腳本」：值以 `$` 開頭時視為指令，由其 STDOUT 提供內容；否則當成檔案路徑
直接開啟。因此以下兩種寫法都正確：

```yaml
api_key: /etc/illumio-flowlink/api_info        # 檔案路徑
api_key: $cat /etc/illumio-flowlink/api_info   # 指令（官方範例寫法）
```

檔案內容為 `api_<key_id> <secret>`（空白分隔一行）。相對路徑以啟動時的 CWD
解析，務必寫絕對路徑。認證檔權限應為 `640` 且屬 `ilo-flowlink`（RPM 預設）。

## 12. 驗證紀錄

### 12.1 2026-08-11（FlowLink 2.0.0-7 / Rocky Linux 9.5 / ilo-flow）

- 真實 Forcepoint CEF（含中文 `suser` / `cs1`）經 relay：`1497 bytes` → `169 bytes`，
  非 ASCII 位元組數 `0`
- 端到端：UDP 514 → relay → UDP 5514 → FlowLink → PCE，`Response Code 204`
- protocol 行為實測：無效值 `proto=xyz` 被 `Discarding flow due to invalid
  protocol` 整筆丟棄；`proto=6` / `proto=17` 正常成為 flow
- `deploy.sh` dry-run 與實跑：舊 `/etc/logrotate.d/flowlink` 清除、relay 部署、
  healthcheck 四項全綠、exit 0
- `process_packet` 單元驗證：中文剝除、`proto=6` 注入、`protocol_value` 留空時
  不注入、缺必要欄位丟 `ValueError`

### 12.2 靜態驗證

- `python3 -m py_compile syslog-formatter.py`
- `python3 syslog-formatter.py --help`
- `process_packet` 單核效能基準：~35k pkts/s（2KB 真實樣本；4 workers 約
  140k pkts/s，遠高於 Forcepoint 實際事件量）
