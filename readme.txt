# FlowLink Forcepoint Syslog Formatter SOP

- 文件位置: `/opt/flowlink/`
- Repo 位置: `/mnt/d/OneDrive/RD/illumio_flowlink_/`
- 最後更新: `2026-04-19`
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

- [syslog-formatter.py](/mnt/d/OneDrive/RD/illumio_flowlink_/syslog-formatter.py:1)
- [syslog-formatter.ini](/mnt/d/OneDrive/RD/illumio_flowlink_/syslog-formatter.ini:1)
- [config-syslog.yml](/mnt/d/OneDrive/RD/illumio_flowlink_/config-syslog.yml:1)
- [systemd/syslog-formatter.service](/mnt/d/OneDrive/RD/illumio_flowlink_/systemd/syslog-formatter.service:1)
- [systemd/flowlink-healthcheck.service](/mnt/d/OneDrive/RD/illumio_flowlink_/systemd/flowlink-healthcheck.service:1)
- [systemd/flowlink-healthcheck.timer](/mnt/d/OneDrive/RD/illumio_flowlink_/systemd/flowlink-healthcheck.timer:1)

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
- `workers`: relay worker 數量

### `[healthcheck]`

- `flowlink_cmd`: FlowLink 控制指令路徑
- `flowlink_config`: FlowLink 啟動時使用的 config 路徑
- `flowlink_log`: FlowLink log 路徑
- `relay_service`: Linux service 名稱
- `backup_dir`: crash / journal 備份目錄
- `restart_wait_seconds`: 重啟後等待再次檢查的秒數

## 8. systemd 建議部署方式

目前 repo 已提供 systemd 模板，建議用 systemd 管理 relay 自動重啟與定期健康檢查，不要再混用 shell wrapper。

### 8.1 提供的 unit 檔

- `/opt/flowlink/systemd/syslog-formatter.service`
- `/opt/flowlink/systemd/flowlink-healthcheck.service`
- `/opt/flowlink/systemd/flowlink-healthcheck.timer`

其角色如下：

- `syslog-formatter.service`
  - 啟動 `syslog-formatter.py run`
  - 使用 `Restart=always`
  - relay 異常退出時自動重啟
- `flowlink-healthcheck.service`
  - 單次執行 `syslog-formatter.py healthcheck`
- `flowlink-healthcheck.timer`
  - 每 30 分鐘執行一次 `flowlink-healthcheck.service`

### 8.2 安裝步驟

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

## 9. 日常健康檢查

建議依序檢查：

### 9.1 執行 formatter healthcheck

```bash
python3 /opt/flowlink/syslog-formatter.py --config /opt/flowlink/syslog-formatter.ini healthcheck
```

正常輸出應包含：

```text
[OK] FlowLink is running.
[OK] Syslog Formatter is running.
```

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

## 11. 定期維護建議

若現場仍需額外排程，可保留以下工作，但不要與 systemd timer 重複部署同一個 healthcheck：

- 每天 `03:00` 清理過舊 crash 備份
- 每天 `03:00` 視情況輪替或截斷 FlowLink log
- 視環境需求做 journald / logrotate 管理

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

## 13. 驗證紀錄

本 repo 內已完成的靜態驗證：

- `python3 -m py_compile syslog-formatter.py`
- `python3 syslog-formatter.py --help`
- 以 sample raw CEF 驗證 formatter 的最小欄位輸出

未在此 repo 內完成的項目：

- 實際主機上的 `systemctl daemon-reload`
- 實際 service 啟停驗證
- 實際 FlowLink ingest 行為驗證
