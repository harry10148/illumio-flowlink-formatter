# 把舊環境遷移到 FlowLink 2.0.0

> FlowLink 2.0.0 起自己就是 Linux service，我們過去為了補「FlowLink 不是服務」而自建的
> systemd unit、logrotate 規則與 cron 巡檢全部要退場。
>
> 依 2026-08-11 在 `lab-flowlink` 實機走完一遍的結果整理，每一步都附驗證點。

| | |
|---|---|
| 適用 | FlowLink 1.x → 2.0.0-7 |
| 驗證環境 | Rocky Linux 9.5 |
| 預估停機 | 5–15 分鐘 |
| 權限 | root |

---

## 開始之前：先盤點，決定走哪條路

同樣是「舊環境」，主機可能停在兩個很不一樣的狀態。先跑這段，再決定從哪一步開始。

```bash
# FlowLink 版本與服務
rpm -qa | grep -i flowlink
systemctl cat illumio-flowlink >/dev/null 2>&1 && echo "2.0.0 服務存在" || echo "沒有官方服務（1.x）"

# 我們自建的東西還在不在
ls /etc/systemd/system/ | grep -E 'flowlink|syslog-(relay|formatter)'
ls /etc/logrotate.d/ | grep -i flow
crontab -l 2>/dev/null | grep -Ei 'flowlink|monitor_services'

# 目前的資料路徑（決定等一下要備份什麼）
ss -ulnp | grep -E ':514|:5514|:2055'
ps aux | grep -i '[f]lowlink'
```

| 路線 | 判斷依據 | 從哪開始 |
|---|---|---|
| **A · 尚未升級** | 沒有 `illumio-flowlink` 服務，FlowLink 仍以 `illumio-flowlink-ctl` 手動啟動 | 步驟 01，完整走完 |
| **B · 已升級但沒收尾** | 服務已存在，但自建 unit／logrotate 還在、或 syslog 資料斷了 | 跳到步驟 04 |

> **先確認這件事**
>
> 升級會把 `config.yaml` 換成 RPM 的預設值，**你原本的 consumer 定義不會保留**。
> 動手前一定要先抄下現行設定裡所有 consumer（netflow、ipfix、syslog…）的 port 與
> parser 對應，步驟 04 要原樣併回去。

---

## 01 · 備份現況

**路線 A 與 B 都要做，不可省略。** 備份放在 `/root/` 而不是 `/opt/flowlink/`，因為後者最後要清空。

```bash
B=/root/flowlink-migration-$(date +%Y%m%d_%H%M%S) && mkdir -p $B && echo $B

# 設定、認證、自建 unit、cron（路徑依現場調整）
cp -a /opt/flowlink/*.yml /opt/flowlink/api_info $B/ 2>/dev/null
cp -a /etc/systemd/system/flowlink-*.service /etc/systemd/system/flowlink-*.timer $B/ 2>/dev/null
cp -a /etc/systemd/system/syslog-*.service /etc/logrotate.d/flowlink $B/ 2>/dev/null
crontab -l > $B/crontab.root.txt 2>/dev/null

# 記下目前的 RPM 版本，回滾時要用
rpm -qa | grep -i flowlink | tee $B/rpm-version.txt
chmod 600 $B/api_info 2>/dev/null; ls -la $B/
```

**回滾素材：** 另外確認手上有**舊版 RPM 檔**。`rpm -e` 之後要退回去，只能靠原始安裝檔，
PCE 端沒有備份。

---

## 02 · 停掉舊服務與自建元件

**路線 A。** 先停 relay 再停 FlowLink，避免 relay 對著已消失的 5514 空轉；順序反過來也不會壞，
只是 journal 會多一批錯誤。

```bash
systemctl disable --now syslog-formatter.service
systemctl disable --now flowlink-healthcheck.timer  # 自建，2.0.0 後退場
systemctl disable --now flowlink-start.service      # 自建，2.0.0 後退場
systemctl disable --now syslog-relay.service        # 更舊的版本才有

# 舊 cron 巡檢（deploy.sh 之後也會自動清，先停避免干擾）
crontab -l | grep -Ev 'monitor_services|flowlink' | crontab -

/usr/local/bin/illumio-flowlink-ctl stop
ss -ulnp | grep -E ':514|:5514'   # ← 應該完全沒有輸出
```

---

## 03 · 移除 1.x、安裝 2.0.0

**路線 A。** 官方升級流程就是先移除再安裝，沒有 in-place upgrade。

```bash
rpm -e illumio-flowlink
rpm -ivh illumio-flowlink-2.0.0-7.x86_64.rpm

# 確認新佈局長出來了
rpm -qa | grep flowlink
id ilo-flowlink
ls -la /etc/illumio-flowlink/ /var/lib/illumio-flowlink/ /var/log/illumio-flowlink/
systemctl cat illumio-flowlink | head -5
```

### 路徑對照

| 項目 | 1.x（自建佈局） | 2.0.0（RPM 擁有） |
|---|---|---|
| 設定檔 | `/opt/flowlink/config-syslog.yml` | `/etc/illumio-flowlink/config.yaml` |
| 認證檔 | `/opt/flowlink/api_info` | `/etc/illumio-flowlink/api_info` |
| 資料目錄 | `/opt/flowlink/data/` | `/var/lib/illumio-flowlink/data/` |
| Log | `/opt/flowlink/data/flowlink.log` | `/var/log/illumio-flowlink/flowlink.log` |
| 開機啟動 | `flowlink-start.service`（自建） | `illumio-flowlink.service` |
| Log 輪替 | `/etc/logrotate.d/flowlink`（自建） | `flowlink-logrotate.conf` |
| 執行帳號 | root | `ilo-flowlink` |

---

## 04 · 把認證與 consumer 設定搬回去

**路線 A 與 B 都要做。整個遷移最容易出事的一步。**

先放認證檔。格式是 `api_<key_id> <secret>` 一行、空白分隔。

```bash
cp $B/api_info /etc/illumio-flowlink/api_info
chown ilo-flowlink:ilo-flowlink /etc/illumio-flowlink/api_info
chmod 640 /etc/illumio-flowlink/api_info
```

再編輯設定檔，把備份裡的 consumer 原樣併回來。

```bash
cp -p /etc/illumio-flowlink/config.yaml /etc/illumio-flowlink/config.yaml.rpm-default
vi /etc/illumio-flowlink/config.yaml
```

```yaml
pce_addr: pce.example.com:8443
org_id: 1
api_key: /etc/illumio-flowlink/api_info
data_directory: /var/lib/illumio-flowlink/data
aggregation_minutes: 10
consumers:
  # 現場原本就有的，一個都不能漏
  - name: netflow
    parser:
      type: netflow
    connectors:
      - type: udp
        properties:
          ports: "2055"

  # Forcepoint syslog：relay 轉進來的 port
  - name: Forcepoint_CEF_log
    parser:
      type: text
      properties:
        src_ip: src
        dst_ip: dst
        dst_port: dpt
        protocol: proto      # ← 讀 relay 注入的 proto=6
        timestamp: start
        timestamp_format: unix
    connectors:
      - type: udp
        properties:
          ports: "5514"
```

> **別照抄舊設定的 protocol**
>
> 舊設定寫 `protocol: tcp`，意思是「讀 `tcp=` 這個 tag」——但 Forcepoint 的 proxy 事件裡
> 根本沒有這個欄位，永遠讀不到。`protocol` 是 text parser 的**必填**屬性，而且 FlowLink
> 會驗證值，解析不出來就整筆丟掉。新版 relay 會注入 `proto=6`，所以這裡要寫
> `protocol: proto`。

啟動服務：

```bash
systemctl enable --now illumio-flowlink
systemctl status illumio-flowlink --no-pager
ss -ulnp | grep -E ':2055|:5514'   # ← consumer 有幾個就該看到幾個 port
```

**通過條件：** 服務 active，而且**每一個 consumer 的 port 都真的 bound**。服務 active 但
port 沒開，代表設定被吃掉了——去 `/var/log/illumio-flowlink/flowlink.log` 看啟動訊息。

---

## 05 · 部署新版 relay

**路線 A 與 B 都要做。** `deploy.sh` 只部署 relay，順手清掉本專案舊版留下的所有東西，
不會碰 RPM 擁有的檔案。乾淨主機上重跑是 no-op。

```bash
# 把 repo 放到主機上（rsync 或 git clone 都行）
rsync -a --exclude='.git' <repo>/ root@<host>:/root/flowlink-repo/

bash /root/flowlink-repo/deploy.sh --dry-run   # ← 先看它要做什麼
bash /root/flowlink-repo/deploy.sh
```

它會做這些事：移除 `flowlink-start.service`、`flowlink-healthcheck.service/.timer`、
`/etc/logrotate.d/flowlink`、legacy `syslog-relay.service` 與 cron；安裝 relay 到
`/opt/flowlink/`、建立無特權帳號 `flowlink`、套用 UDP buffer sysctl、啟用
`syslog-formatter.service`；最後跑 healthcheck，四項不全綠就以非零值結束。

**通過條件：** 結尾出現 `Deployment OK: relay + FlowLink healthy, UDP 514/5514 listening.`
且 exit code 為 0。

---

## 06 · 分層驗證

**路線 A 與 B 都要做。** 一層一層往上打，壞在哪一層一眼就看得出來。前三層完全不需要
Forcepoint 真的在送資料。

### L1 — 服務層

```bash
python3 /opt/flowlink/syslog-formatter.py --config /opt/flowlink/syslog-formatter.ini healthcheck
# [OK] FlowLink (illumio-flowlink) is running.
# [OK] UDP 5514 is bound (FlowLink consumer).
# [OK] Syslog Formatter (syslog-formatter) is running.
# [OK] UDP 514 is bound (relay listener).

systemctl --failed --no-pager   # ← 應該是 0 loaded units
```

### L2 + L3 — relay 與 FlowLink 解析

灌一筆合成的 CEF 進 UDP 514，看它有沒有一路走到 FlowLink 的計數器。

```bash
python3 -c '
import socket, time
now = int(time.time() * 1000)
msg = ("<134>CEF:0|Forcepoint|Security|8.5.4|227|Transaction permitted|Low| "
       f"start={now} src=192.0.2.35 dst=203.0.113.10 dpt=443 "
       "suser=OU\\=測試中文欄位/00764675")
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for _ in range(10): s.sendto(msg.encode(), ("127.0.0.1", 514))
print("sent 10")'

# L2：relay 有沒有收下並轉出（統計每 60 秒印一次，等一輪）
journalctl -u syslog-formatter --since -3min | grep -E 'relayed=[1-9]'
# Worker 1476210 stats: relayed=10 errors=0 truncated=0 window=60s

# L3：FlowLink 有沒有解析（count 應該 +10，且沒有 Discarding）
grep -a -A2 'meter Forcepoint_CEF_log' /var/log/illumio-flowlink/flowlink.log | tail -3
grep -ac 'Discarding flow due to invalid protocol' /var/log/illumio-flowlink/flowlink.log
```

怎麼判讀：

- `relayed=0` → 封包沒進 relay，查防火牆與來源位址
- `relayed=10` 但 `meter count` 不動 → FlowLink 沒在聽 5514，回步驟 04
- 出現 `Discarding` → `protocol` 對應錯了，回步驟 04 檢查 `protocol: proto`

### L4 — PCE

FlowLink 依 `aggregation_minutes`（預設 10 分鐘）批次上報，所以這層要等一輪。

```bash
grep -a 'Reporting Flows\|Flow count\|Response Code' /var/log/illumio-flowlink/flowlink.log | tail -4
# Reporting Flows / Flow count: 753 / Response Code 204
```

要更硬的證據，直接用 PCE API 把 flow 撈回來對（`POST /api/v2/orgs/1/traffic_flows/async_queries`
建立查詢、輪詢到 `completed`、再 GET `/download`，記得帶 `Accept: application/json`），
查詢條件用剛才那筆的目的 IP。

**在 PCE 畫面上找不到流量時：** 資料進了 PCE 不代表 Illumination map 上看得到。map 依
network 分群，而 PCE 對**兩端都是已定義 workload** 的 flow、與**有一端只是 IP List** 的
flow 會歸到不同 network。先用 Explorer／Traffic 頁面查（不做 network 分群），確認資料在，
再處理 map 的檢視條件。

---

## 07 · 清掉舊佈局殘留

**驗證全過之後才做。** 先確認沒有行程還抓著舊路徑，再刪。實測一台主機清出 262 MB。

```bash
lsof +D /opt/flowlink/data 2>/dev/null        # ← 應該沒有輸出
grep -n 'opt/flowlink' /etc/illumio-flowlink/config.yaml   # ← 應該沒有輸出
cmp /opt/flowlink/api_info /etc/illumio-flowlink/api_info && echo "認證檔一致，舊的可刪"

rm -f /opt/flowlink/config-*.yml /opt/flowlink/api_info
rm -f /opt/flowlink/monitor_services.sh.disabled.*
rm -rf /opt/flowlink/data

# 確認 logrotate 不再每天失敗（舊規則指向已停用的 log）
systemctl reset-failed logrotate.service
systemctl start logrotate.service && systemctl status logrotate.service --no-pager | head -6
```

**收尾檢查：** `/opt/flowlink/` 只剩 `syslog-formatter.py`、`syslog-formatter.ini`、
`systemd/`；`systemctl --failed` 零筆；healthcheck 四項全綠。

---

## 回滾

只有兩種情況需要回滾：2.0.0 起不來，或流量在步驟 06 怎麼調都通不了，而現場不能繼續斷。

```bash
systemctl disable --now illumio-flowlink syslog-formatter
rpm -e illumio-flowlink
rpm -ivh <舊版 RPM>                       # 版本見 $B/rpm-version.txt
cp -a $B/*.yml $B/api_info /opt/flowlink/
cp -a $B/flowlink-*.service $B/flowlink-*.timer $B/syslog-*.service /etc/systemd/system/
systemctl daemon-reload
/usr/local/bin/illumio-flowlink-ctl start --config /opt/flowlink/config-syslog.yml
systemctl enable --now syslog-formatter.service flowlink-healthcheck.timer
```

注意：回滾後 `/var/lib/illumio-flowlink/data/` 裡尚未上報的 flow 會留在原地不再被處理。
量通常很小（一個 aggregation 週期），但若在意就先確認該目錄已清空再回滾。

---

## 實機踩到的坑

都是 2026-08-11 在 `lab-flowlink` 真的發生過、而且花了時間才找出來的。

### 升級會清掉 syslog consumer，但 relay 什麼都不會說

RPM 把 `config.yaml` 換成只有 netflow 的預設值，UDP 5514 沒人監聽。relay 照常運作、
統計照常印，轉出去的封包卻被系統直接丟棄——整條鏈路無聲斷掉。

這就是 healthcheck 要檢查 port 而不只是檢查行程的原因。

### 自建的 logrotate 規則會拖垮整台主機的 logrotate

舊規則指向升級後已停止寫入的 `/opt/flowlink/data/flowlink.log`，`logrotate.service`
因此每天失敗——連帶其他所有 log 都不輪替。RPM 自帶 `flowlink-logrotate.conf`，
我們的那份必須刪。

### protocol 是必填，而且 FlowLink 真的會驗證值

實測：`proto=6` / `proto=17` 正常成為 flow；`proto=xyz` 會被
`Discarding flow due to invalid protocol` 整筆丟棄；完全沒有這個欄位則不丟棄，
但協定資訊缺失。

而 Forcepoint 的 proxy 事件從來就沒有協定欄位——這個洞之所以多年沒爆，是因為
Forcepoint 端根本沒開始送資料。

### illumio-flowlink-ctl 要用服務帳號跑

2.0.0 仍保留這個工具，但正常操作應該用 `systemctl`；排錯時用
`sudo -u ilo-flowlink /usr/local/bin/illumio-flowlink-ctl status`，直接以 root 跑
可能寫出屬主不對的檔案。

### api_key 兩種寫法都合法

依 RPM 內的 `flowlink_config_schema.json`，這個欄位是「檔案**或**指令」：值以 `$` 開頭
視為指令、由 STDOUT 提供內容，否則當檔案路徑開啟。`/etc/illumio-flowlink/api_info` 和
`$cat /etc/illumio-flowlink/api_info` 都對，不必為了官方範例改寫。

### 判斷版本要看 systemd unit，不能看 ctl 在不在

`illumio-flowlink-ctl` 在 2.0.0 仍然存在，拿它判斷版本會誤判。用
`systemctl cat illumio-flowlink` 的回傳值判斷才準——2.0.0 的 unit 由
`/etc/init.d/illumio-flowlink` 產生，一樣認得出來。

---

指令中的 `<host>`、`pce.example.com`、RPM 檔名請依現場替換。
