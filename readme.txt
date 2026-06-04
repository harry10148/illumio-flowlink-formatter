# FlowLink Forcepoint Syslog Formatter SOP

- 文件位置: `/opt/flowlink/`
- Repo 位置: `/home/harry/rd/illumio-flowlink-formatter/`
- 最後更新: `2026-05-07`
- 適用對象: 系統管理員 / 運維人員
- 核心功能: 接收 Forcepoint CEF Syslog，轉成 FlowLink 可接受的最小流量資料，再由 FlowLink 上傳到 SaaS / PCE

## 1. 專案目的

這個專案不是 FlowLink 主程式本身，而是部署在 FlowLink 前面的格式化與維運層。

它負責兩件事：

1. 接收 Forcepoint Web Proxy 送出的 CEF / Syslog。
2. 將資料整理成 FlowLink `text` consumer 需要的最小欄位，再轉送到本機 FlowLink。

FlowLink 的用途是做基本流量正規化與聚合，提供 PCE / Illumination 顯示流量關聯，不是保存完整 proxy 事件內容。基於這個用途，formatter 現在只轉送最小必要欄位，而不再轉送大量文字欄位。

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
4. 保留 CEF 基本頭，將整理後的結果轉送到 FlowLink。

範例輸出：

```text
<14>Apr 19 16:41:59 Forcepoint-Relay CEF:0|Forcepoint|Security|8.5.4|227|Transaction permitted|Low| start=1763015021 src=192.168.72.35 dst=162.247.243.29 dpt=443
```

這樣做的原因：

- FlowLink 目前設定只會使用 `src` / `dst` / `dpt` / `start`
- 原始 CEF 中的 `suser`、`cs1`、`request` 等欄位可能包含中文與長字串
- 這些欄位對目前的 FlowLink 用途沒有直接價值，卻會增加格式相容性與封包長度風險


驗證結論：

- 原始事件是裸 `CEF:` 記錄，沒有 syslog header，因此補 header 是必要的
- 原始事件的 `start` / `end` / `rt` / `art` 確實是 13 位毫秒時間戳
- 原始事件中 `suser` 和 `cs1` 含有中文
- 經過最小欄位投影後，單筆訊息大小從約 `2 KB` 降到約 `160 bytes`

## 5. 關鍵檔案

部署目錄預期如下：

- `api_info`: FlowLink 連線到 PCE / SaaS 的 API key 檔案
- `config-syslog.yml`: FlowLink 主程式設定檔
- `syslog-formatter.py`: relay 與 healthcheck 主程式
- `syslog-formatter.ini`: formatter 執行設定檔
- `systemd/`: 建議部署的 systemd unit / timer 模板
- `data/`: FlowLink 資料與 log 目錄

Repo 中主要檔案如下：

- [syslog-formatter.py](syslog-formatter.py)
- [syslog-formatter.ini](syslog-formatter.ini)
- [config-syslog.yml](config-syslog.yml)
- [systemd/syslog-formatter.service](systemd/syslog-formatter.service)
- [systemd/flowlink-healthcheck.service](systemd/flowlink-healthcheck.service)
- [systemd/flowlink-healthcheck.timer](systemd/flowlink-healthcheck.timer)

## 6. 執行方式

### 6.1 手動啟動 relay

```bash
python3 /opt/flowlink/syslog-formatter.py --config /opt/flowlink/syslog-formatter.ini run
```

### 6.2 手動執行健康檢查

```bash
python3 /opt/flowlink/syslog-formatter.py --config /opt/flowlink/syslog-formatter.ini healthcheck
```

### 6.3 設定檔搜尋順序

若未指定 `--config`，程式會依序使用：

1. 環境變數 `SYSLOG_FORMATTER_CONFIG`
2. 與程式同目錄的 `syslog-formatter.ini`

## 7. syslog-formatter.ini 說明

目前預設內容如下：

```ini
[relay]
listen_ip = 0.0.0.0
listen_port = 514
target_ip = 127.0.0.1
target_port = 5514
workers = 4

[healthcheck]
flowlink_cmd = /usr/local/bin/illumio-flowlink-ctl
flowlink_config = /opt/flowlink/config-syslog.yml
flowlink_log = /opt/flowlink/data/flowlink.log
relay_service = syslog-formatter
backup_dir = /var/log/flowlink_crash_backups
restart_wait_seconds = 5
```

欄位說明：

### `[relay]`

- `listen_ip`: 接收 Forcepoint syslog 的位址
- `listen_port`: 接收 Forcepoint syslog 的 UDP port
- `target_ip`: 轉送到 FlowLink 的目標 IP
- `target_port`: 轉送到 FlowLink 的目標 port
- `workers`: relay worker 數量；若作業系統不支援 `SO_REUSEPORT`，會自動退回單一 worker 並在啟動 log 印出警告

### `[healthcheck]`

- `flowlink_cmd`: FlowLink 控制指令路徑
- `flowlink_config`: FlowLink 啟動時使用的 config 路徑
- `flowlink_log`: FlowLink log 路徑
- `relay_service`: Linux service 名稱
- `backup_dir`: crash / journal 備份目錄
- `backup_retain`: 備份目錄只保留最新 N 個檔案，超過自動清除（0 = 不清除）
- `restart_wait_seconds`: 重啟後等待再次檢查的秒數

healthcheck 另外會檢查 UDP port 綁定（relay 用 `[relay] listen_port`、FlowLink 用 `[relay] target_port`），可用 `--relay-port` / `--flowlink-port` 覆寫。

## 8. systemd 建議部署方式

目前 repo 已提供 systemd 模板，建議用 systemd 管理 relay 自動重啟與定期健康檢查，不要再混用 shell wrapper。

### 8.1 提供的 unit 檔

- `/opt/flowlink/systemd/syslog-formatter.service`
- `/opt/flowlink/systemd/flowlink-start.service`
- `/opt/flowlink/systemd/flowlink-healthcheck.service`
- `/opt/flowlink/systemd/flowlink-healthcheck.timer`

其角色如下：

- `syslog-formatter.service`
  - 啟動 `syslog-formatter.py run`
  - 使用 `Restart=always`
  - relay 異常退出時自動重啟
  - 以專用使用者 `flowlink` 執行（deploy.sh 會建立），靠 `CAP_NET_BIND_SERVICE` 綁 UDP 514，不以 root 跑
- `flowlink-start.service`
  - 開機時啟動 FlowLink 本體（FlowLink 不是 systemd 服務，沒有這個 unit 的話，開機後要等 healthcheck timer 才會被拉起來，期間 relay 轉送的封包會無聲消失）
- `flowlink-healthcheck.service`
  - 單次執行 `syslog-formatter.py healthcheck`
  - healthcheck 出現任何 `[CRITICAL]` 時以非零值結束；可在 unit 加 `OnFailure=` 接告警（unit 檔內有註解範例）
- `flowlink-healthcheck.timer`
  - 開機 1 分鐘後執行第一次，之後每 30 分鐘一次

### 8.2 安裝步驟

建議直接使用一鍵部署腳本（含前置檢查、api_key 注入、systemd 安裝與部署後驗證）：

```bash
sudo /home/harry/rd/illumio-flowlink-formatter/deploy.sh            # 部署 + 驗證
sudo /home/harry/rd/illumio-flowlink-formatter/deploy.sh --dry-run  # 只列出動作
```

前置條件：`/opt/flowlink/api_info` 必須已放入實際 API key，且 FlowLink 本體（`illumio-flowlink-ctl`）已安裝；缺一即硬停報錯。

以下為等效的手動步驟。部署前，先把 `config-syslog.yml` 中的 `api_key: REPLACE_ME` 換成實際的 API key（FlowLink 不會做 shell 替換，必須是字面值）。可手動編輯，或用：

```bash
API_KEY="$(cat /opt/flowlink/api_info)"
sed -i "s|^api_key:.*|api_key: ${API_KEY}|" /opt/flowlink/config-syslog.yml
```

接著安裝 systemd unit：

```bash
cp /opt/flowlink/systemd/*.service /etc/systemd/system/
cp /opt/flowlink/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now syslog-formatter.service
systemctl enable --now flowlink-healthcheck.timer
```

### 8.3 驗證指令

```bash
systemctl status syslog-formatter.service
systemctl status flowlink-healthcheck.timer
systemctl list-timers | grep flowlink-healthcheck
```

### 8.4 從舊版部署升級（syslog-relay + monitor_services.sh）

舊版環境的特徵：service 名稱是 `syslog-relay`、cron 跑 `monitor_services.sh`、
FlowLink 設定檔可能是 `config-netflow.yml`。升級步驟如下（建議在低流量時段執行，
切換期間 UDP 514 會有數秒中斷）：

```bash
# 0. 盤點現況
systemctl status syslog-relay
crontab -l | grep -E 'monitor_services|truncate'
ps aux | grep -i flowlink          # 確認 FlowLink 目前用哪個 config 在跑
/usr/local/bin/illumio-flowlink-ctl status

# 1. 移除舊 cron（monitor_services.sh 巡檢與 03:00 truncate）
crontab -e

# 2. 停用舊 relay（舊版佔著 UDP 514，deploy.sh preflight 會擋）
systemctl disable --now syslog-relay
mv /opt/flowlink/monitor_services.sh /opt/flowlink/monitor_services.sh.disabled

# 3. 確認 API key 已就位
test -s /opt/flowlink/api_info && echo OK

# 4. 部署新版（先 dry-run 看一遍動作）
cd <repo 目錄>
sudo ./deploy.sh --dry-run
sudo ./deploy.sh

# 5. 清掉舊 unit 檔（確認新版運作正常後）
rm -f /etc/systemd/system/syslog-relay.service
systemctl daemon-reload
```

注意：若步驟 0 發現 FlowLink 正以 `config-netflow.yml` 執行，請先確認現場是否
仍需要 netflow consumer。新版 healthcheck / flowlink-start 重啟 FlowLink 時一律
使用 `config-syslog.yml`；若兩種 consumer 都要，請把 netflow consumer 合併進
`config-syslog.yml` 再部署。

## 9. 日常健康檢查

建議依序檢查：

### 9.1 執行 formatter healthcheck

```bash
python3 /opt/flowlink/syslog-formatter.py --config /opt/flowlink/syslog-formatter.ini healthcheck
```

正常輸出應包含：

```text
[OK] FlowLink is running.
[OK] UDP 5514 is bound (FlowLink consumer).
[OK] Syslog Formatter is running.
[OK] UDP 514 is bound (relay listener).
```

任何一項 `[CRITICAL]` 都會讓 healthcheck 以 exit code 1 結束（即使自動重啟成功），
方便 systemd `OnFailure=` 或外部排程接告警。port 檢查的目的是補捉
「process 活著但沒在聽」的盲區（例如 worker bind 失敗反覆重啟、FlowLink
consumer 掛掉但主進程還在）。

relay 每 60 秒會輸出一行統計到 journal（含閒置心跳）：

```text
Worker 12345 stats: relayed=1234 errors=0 truncated=0 window=60s
```

異常封包不會逐包寫 log（避免 journald 洪水），每個統計視窗只記第一筆的詳細原因，
其餘計入 `errors=`。

### 9.2 檢查 Port 監聽

```bash
netstat -uapnl | grep -E '514|5514'
```

檢查重點：

- `UDP 514` 由 Python relay 佔用
- `UDP 5514` 由 FlowLink 佔用

### 9.3 檢查 FlowLink log

```bash
tail -f /opt/flowlink/data/flowlink.log
```

重點是確認 FlowLink 有持續接收並寫出 flow 資料。

## 10. 故障處理

### 10.1 修改 FlowLink 設定後重啟

```bash
illumio-flowlink-ctl stop
illumio-flowlink-ctl start --config /opt/flowlink/config-syslog.yml
```

### 10.2 重啟 relay service

```bash
systemctl restart syslog-formatter.service
systemctl status syslog-formatter.service
```

worker 子進程已正確響應 SIGTERM，`systemctl restart` / `stop` 應在數秒內完成；若觀察到 systemd 走 `SIGKILL` 強制終止，代表 worker 實際卡在處理上而非 signal 處理錯誤。

### 10.3 查看 timer 與巡檢狀態

```bash
systemctl status flowlink-healthcheck.timer
systemctl status flowlink-healthcheck.service
```

### 10.4 查看備份與巡檢輸出

```bash
ls -l /var/log/flowlink_crash_backups
```

若 healthcheck 發現 FlowLink 或 relay 有問題，會將相關資訊備份到 `backup_dir`。
備份目錄由 healthcheck 自動清理，只保留最新 `backup_retain`（預設 20）個檔案。

## 11. 定期維護建議

舊版 shell 巡檢腳本（如 `/opt/flowlink/monitor_services.sh`）已被 `syslog-formatter.py healthcheck` 完整取代，且其內容引用舊 service 名稱 `syslog-relay` 與舊設定檔 `config-netflow.yml`，繼續執行會以錯誤設定重啟 FlowLink。請從 cron 移除並停用該腳本（升級步驟見 §8.4）。

磁碟管理已由 deploy.sh 部署，不需要再掛 cron：

- FlowLink log 輪替：`/etc/logrotate.d/flowlink`（daily / maxsize 100M / 保留 7 份 / copytruncate），取代舊的 `03:00 truncate` cron
- crash 備份清理：healthcheck 每次執行時自動保留最新 `backup_retain` 個檔案

## 12. 目前已知注意事項

### 12.1 FlowLink config 路徑需依現場確認

目前 repo 預設已對齊使用 `config-syslog.yml`：

- 文件與 repo 中的 FlowLink 設定檔是 `config-syslog.yml`
- `syslog-formatter.ini` 預設值為 `/opt/flowlink/config-syslog.yml`

若現場仍使用其他檔名（例如 `config-netflow.yml`），請同步修改 `syslog-formatter.ini` 的 `flowlink_config`。

### 12.2 `protocol: tcp` 仍維持現況

目前沒有改動 `config-syslog.yml` 的 `protocol: tcp` 寫法。  
本次改善只針對 formatter 與部署方式，沒有調整 FlowLink parser 的欄位映射策略。

### 12.3 FlowLink 本身仍需外部監控

FlowLink 官方設計並非高可用，relay 雖已有 `Restart=always`，但 FlowLink 本體與整體資料鏈路仍建議保留定期 healthcheck。

### 12.4 CEF 跳脫 pipe 為已知限制

CEF header 欄位若含跳脫的 `\|`，`split("|", 7)` 會切錯位導致該封包被丟棄並計入
errors。Forcepoint 的固定欄位（vendor/product/version/signature/name/severity）
實務上不含 pipe，故僅列為已知限制不特別處理。

### 12.5 建議以防火牆限制 514 來源

relay 監聽 `0.0.0.0:514` 且不驗證來源，任何能路由到本機的設備都能注入假流量到
PCE Illumination。建議用主機防火牆（firewalld / nftables）只放行 Forcepoint
來源 IP 的 UDP 514。UDP 本身可偽造來源，此措施屬縱深防禦。

### 12.6 `api_key` 必須是字面值

`config-syslog.yml` 中的 `api_key` 不會被 FlowLink 做 shell 替換，必須是真正的 key 字串。repo 預設值為 `REPLACE_ME`，部署時務必依 §8.2 手動換成 `/opt/flowlink/api_info` 內容，否則 FlowLink 對 PCE / SaaS 的認證會直接失敗。

## 13. 驗證紀錄

本 repo 內已完成的靜態驗證：

- `python3 -m py_compile syslog-formatter.py`
- `python3 syslog-formatter.py --help`
- 以 sample raw CEF 驗證 formatter 的最小欄位輸出
- 端到端 relay 測試（loopback 高埠）：轉送正確性、壞封包 log rate-limit、
  60 秒統計輸出、SIGTERM 正常退出
- healthcheck port 盲區與 exit code 測試（stub flowlink-ctl / systemctl）
- `process_packet` 單核效能基準：~35k pkts/s（2KB 真實樣本；4 workers 約
  140k pkts/s，遠高於 Forcepoint 實際事件量）
- deploy.sh：dry-run 全流程、api_key 注入、重跑冪等備份

未在此 repo 內完成的項目：

- 實際主機上的 `systemctl daemon-reload`
- 實際 service 啟停驗證
- 實際 FlowLink ingest 行為驗證
