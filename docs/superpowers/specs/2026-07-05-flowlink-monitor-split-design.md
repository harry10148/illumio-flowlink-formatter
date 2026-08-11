# FlowLink 監控拆分設計（healthcheck 退場與雙模式 monitor）

日期：2026-07-05
狀態：**已由 2026-08-11 的實作取代（歷史紀錄）**

> 2026-08-11 更新：lab-flowlink 已升級 FlowLink 2.0.0-7，站點決定不保留 legacy
> 支援，因此本設計的「雙模式 monitor」未實作。實際做法更簡單：
> `flowlink-start.service`、`flowlink-healthcheck.service` / `.timer`、
> 自建 logrotate 規則全部刪除，`syslog-formatter.py healthcheck` 改寫成
> service 模式的純監控（只回報、不修復），deploy.sh 只部署 relay。
> 現況以 `readme.txt` 為準，本文件僅保留設計脈絡。

## 背景與動機

FlowLink 2.0.0 起預設安裝為 Linux service（官方 systemd unit：`illumio-flowlink`），行程存活與開機啟動改由 systemd 負責。現有 `syslog-formatter.py healthcheck` 中「用 `illumio-flowlink-ctl` 偵測並重啟 FlowLink」與 `flowlink-start.service`「開機拉起 FlowLink」都是為了補「舊版 FlowLink 不是 service」的洞，新版之後即過時。

但 healthcheck 中的「資料路徑檢查」（UDP 5514 / 514 是否 bound）在新版之後仍然必要：systemd 只能保證行程活著，無法偵測「行程 active 但沒有 bind port、沒在收資料」的盲區（例如 FlowLink 跑到 legacy config）。

因此本設計把「修復邏輯」與「監控邏輯」拆開：修復交還 systemd（新版）或保留 ctl 重啟（舊版），監控獨立成腳本並支援雙模式。

## 目標

1. `syslog-formatter.py` 變回純 relay，不再認識 FlowLink。
2. 新增 `flowlink-monitor.py`，單一腳本支援新舊 FlowLink 雙模式。
3. service 模式為純監控（回報 + exit code），零修復動作。
4. legacy 模式行為與現行 healthcheck 完全一致，舊環境零回歸。
5. deploy.sh 依偵測到的模式決定安裝內容。

## 非目標

- 告警傳輸不在範圍內：維持 `flowlink-healthcheck.service` 中 `OnFailure=` hook 註解，由站點自行接（SMTP / LINE Notify 等）。
- 不改 relay 檔名（`syslog-formatter.py` 沿用，避免動到既有 unit 與文件）。
- 不移除既有 legacy 支援（不採「只往前看」策略）。

## 設計

### 1. 檔案拆分

- **`syslog-formatter.py`**：移除 `healthcheck` 子命令與其專屬函式（`run_command`、`parse_flowlink_status`、`backup_file`、`backup_journal`、`udp_port_bound`、`prune_backups`、`healthcheck` 及對應 argparse 子命令）。`CONFIG_SCHEMA` 只保留 `[relay]` 區段。`normalize_command_args` 同步移除 `healthcheck` 分支。
- **`flowlink-monitor.py`**（新檔）：承接監控職責。讀同一份 `syslog-formatter.ini`；設定區段由 `[healthcheck]` 改名為 `[monitor]`，並新增 `mode` 選項。原 `[healthcheck]` 的選項（`flowlink_cmd`、`flowlink_config`、`flowlink_log`、`relay_service`、`backup_dir`、`backup_retain`、`restart_wait_seconds`）移入 `[monitor]`，另新增：
  - `mode = auto | service | legacy`（預設 `auto`）
  - `flowlink_service = illumio-flowlink`（service 模式檢查的官方 unit 名稱，預留可設定）

  其中 `flowlink_cmd`、`flowlink_config`、`flowlink_log`、`backup_dir`、`backup_retain`、`restart_wait_seconds` 僅在 legacy 模式使用。port 檢查的預設值沿用現行邏輯，取自 `[relay]` 的 `listen_port`（relay 監聽埠）與 `target_port`（FlowLink 消費埠），亦可用 CLI 參數覆寫。

### 2. 模式判定

- `auto`：以 `systemctl cat illumio-flowlink`（回傳 0 即 unit 存在）偵測官方 unit；存在即 service 模式，否則 legacy 模式。判定結果印在輸出開頭，方便排錯。
- `service` / `legacy`：站點強制指定，跳過偵測。
- 注意：**不能**以 `illumio-flowlink-ctl` 是否存在來判斷版本——2.0.0 仍保留該工具（官方以 `sudo -u ilo-flowlink` 執行）。偵測依據只能是 systemd unit。2.0.0 的 unit 由 `/etc/init.d/illumio-flowlink` 產生亦可被 `systemctl cat` 識別。

### 3. 各模式檢查行為

| 檢查項目 | service 模式（新版） | legacy 模式（舊版） |
|---|---|---|
| FlowLink 存活 | `systemctl is-active illumio-flowlink`；只回報，不重啟 | `ctl status`；掛掉時備份 flowlink.log、`ctl start` 重啟、等待後驗證 |
| UDP 5514 bound（FlowLink 消費資料） | 檢查，未 bound 記 critical | 同左 |
| Relay 存活（syslog-formatter.service） | `is-active`；只回報 | `is-active`；掛掉時備份 journal、reset-failed、restart、驗證 |
| UDP 514 bound（relay 監聽） | 檢查，未 bound 記 critical | 同左 |
| 備份修剪（prune_backups） | 不執行（此模式不產生備份） | 維持現狀 |

任一 critical 時 exit 1（供 timer 的 `OnFailure=` 與 deploy.sh 驗證使用），否則 exit 0。

原理說明：relay 的 worker bind-crash-loop 情境下 supervisor 行程仍活著，systemd 回報 active，只有 port 檢查抓得到；且重啟通常救不了 crash loop，所以 service 模式對此僅告警。

### 4. systemd 單元

- `flowlink-healthcheck.service` / `flowlink-healthcheck.timer`：**沿用檔名**（減少遷移面），`ExecStart` 改指向 `flowlink-monitor.py`，Description 更新為 monitor 語意。排程維持每 30 分鐘。
- `flowlink-start.service`：降為 **legacy 專屬**。service 模式下開機啟動由官方 unit 的 `systemctl enable flowlink` 負責。

### 5. deploy.sh 調整

- 偵測 `illumio-flowlink` unit 是否存在（與 monitor 的 auto 邏輯一致）：
  - **存在（service 模式）**：不安裝 `flowlink-start.service`；若主機上已有則 `systemctl disable` 並移除。確認 `systemctl is-enabled illumio-flowlink`，未 enable 則 enable。
  - **不存在（legacy 模式）**：維持現行安裝流程。
- 步驟 3.5「legacy config 切換重啟」需分模式：service 模式下 FlowLink 設定檔位於 `/etc/illumio-flowlink/config.yaml`，consumer 設定（UDP 5514）需併入該檔且以 `systemctl restart illumio-flowlink` 重啟；legacy 模式維持 `ctl start --config /opt/flowlink/config-syslog.yml`。service 模式的 config 合併不自動化（站點環境差異大），deploy.sh 僅檢查 UDP 5514 是否 bound，未 bound 時輸出指引後 fail。
- 驗證步驟改為執行 `flowlink-monitor.py` 並判斷 **exit code**，取代現行 `grep -qE 'FAILED|status check failed'` 的字串比對。
- 部署 `flowlink-monitor.py` 至 `/opt/flowlink/`。

### 6. 設定遷移

- repo 內 `syslog-formatter.ini` 的 `[healthcheck]` 區段改名 `[monitor]` 並加入 `mode = auto`。
- 站點已自訂的舊 ini：deploy.sh 既有的「差異即備份」機制會保留舊檔（`.bak.<ts>`）；readme 補充遷移說明（區段改名對照）。不做程式端的 `[healthcheck]` fallback，維持單純。
- readme 補充 FlowLink 1.x 升級 2.0.0 的官方流程重點（`rpm -e illumio-flowlink` 後重裝）與新路徑對照（config `/etc/illumio-flowlink/config.yaml`、資料 `/var/lib/illumio-flowlink/data`、log `/var/log/illumio-flowlink`、服務帳號 `ilo-flowlink`）。logrotate 設定（針對 `/opt/flowlink/data/flowlink.log`）為 legacy 專屬，service 模式不安裝。

### 7. 測試

新增 `tests/`（pytest），覆蓋兩塊純邏輯：

1. `process_packet()`：CEF 標頭補齊、timestamp 毫秒截斷、必要欄位缺失丟 `ValueError`、欄位投影結果。
2. monitor：`parse_flowlink_status()` 各狀態分支；模式判定（`auto` 偵測、強制 `service`/`legacy`）以 mock subprocess 驗證。

註：`syslog-formatter.py` 檔名帶連字號無法直接 import，測試以 `importlib` 載入或於 `conftest.py` 處理；`flowlink-monitor.py` 同樣處理，維持檔名與現有部署慣例一致。

## 錯誤處理

- `systemctl` 不存在或執行失敗：視同該項檢查 critical，輸出明確錯誤訊息（不噴 traceback）。
- legacy 模式的 ctl 錯誤處理維持現行 `run_command` / `parse_flowlink_status` 行為。

## 驗收標準

1. pytest 全數通過。
2. service 模式下執行 monitor：FlowLink/relay 正常時 exit 0；手動 stop `illumio-flowlink` 後 exit 1 且**不**嘗試重啟。
3. legacy 模式下行為與現行 healthcheck 一致（含重啟、備份、修剪）。
4. `syslog-formatter.py` 僅剩 `run` 功能，`healthcheck` 子命令不存在。
5. deploy.sh 於兩種主機（有 / 無 `illumio-flowlink` unit）dry-run 顯示正確的安裝差異。

## 參考資料

- FlowLink 2.0.0 Configuration and Usage（Illumio Core 25.2 官方文件）：
  https://product-docs-repo.illumio.com/Tech-Docs/Core/25.2/Install-Upgrade-Admin/out/en/flowlink-2-0-0-configuration-and-usage/overview.html
- Deploy Flowlink as a Linux Service（unit 名稱 `illumio-flowlink`、路徑、`illumio-flowlink-ctl` 仍存在、1.x 升級流程）：
  https://product-docs-repo.illumio.com/Tech-Docs/Core/25.2/Install-Upgrade-Admin/out/en/flowlink-2-0-0-configuration-and-usage/flowlink-usage/deploy-flowlink-as-a-linux-service.html
