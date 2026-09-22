# ABS Agent 部署與維運手冊

本文件說明如何在公司內網 Linux 主機部署目前的 Foundry Agent Chatbot。架構與安全前提請先閱讀 [DEPLOY.md](DEPLOY.md)。

## 1. 部署前檢查

- 已取得目標 Foundry Project 的 Published Agent 名稱、版本與 Project endpoint。
- 已建立正式用 Service Principal，並已授與目標 Foundry Project 必要角色。
- 已將私鑰 `.pfx`／`.pem` 安全放在主機上。
- 主機可透過 TCP 443 連線至 Entra ID 與 Foundry endpoint。
- 已準備內網 DNS、TLS 憑證與反向代理設定。
- 已確認對話資料保存與備份政策。

## 2. 建立服務帳號與目錄

以下命令由具 `sudo` 權限的維運人員執行：

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin absagent
sudo install -d -o absagent -g absagent -m 750 /opt/abs-agent
sudo install -d -o absagent -g absagent -m 700 /etc/abs-agent/certs
sudo install -d -o absagent -g absagent -m 750 /var/lib/abs-agent
```

將專案程式部署至 `/opt/abs-agent`，並確保服務帳號可讀取程式與寫入 `/var/lib/abs-agent`。

```bash
sudo chown -R absagent:absagent /opt/abs-agent /var/lib/abs-agent
```

## 3. 安裝 Python 套件

```bash
cd /opt/abs-agent
sudo -u absagent python3 -m venv .venv
sudo -u absagent .venv/bin/python -m pip install --upgrade pip
sudo -u absagent .venv/bin/python -m pip install -r chatbot/requirements.txt
```

## 4. 安裝 Service Principal 私鑰

將正式 `.pfx`／`.pem` 安全傳送至主機，再調整檔案權限：

```bash
sudo install -o absagent -g absagent -m 600 \
  /安全的暫存位置/abs-agent-prod.pfx \
  /etc/abs-agent/certs/abs-agent-prod.pfx
```

傳送成功後，立即從暫存位置移除私鑰副本。私鑰不可放入專案目錄、Git、家目錄、備份未加密區或 Docker image。

## 5. 設定 Agent 與機密環境變數

### 5.1 Agent 白名單

編輯 `/opt/abs-agent/chatbot/agents.json`，填入已發佈的 Agent。範例：

```json
{
  "agents": [
    {
      "id": "sharepoint",
      "label": "SharePoint AI Search",
      "project_endpoint": "https://<resource>.services.ai.azure.com/api/projects/<project>",
      "agent_name": "<agent-name>",
      "version": "<published-version>",
      "enabled": true
    }
  ]
}
```

更新版本時，只變更 `version` 並重啟服務。Agent 必須先在 Foundry Publish。

### 5.2 正式主機環境檔

建立 `/etc/abs-agent/abs-agent.env`：

```dotenv
CHAT_PROVIDER=foundry_agent
FOUNDRY_AGENTS_FILE=agents.json
FOUNDRY_DEFAULT_AGENT_ID=sharepoint
CHAT_DB_PATH=/var/lib/abs-agent/chat.db

AZURE_TENANT_ID=<tenant-id>
AZURE_CLIENT_ID=<app-registration-client-id>
AZURE_CLIENT_CERTIFICATE_PATH=/etc/abs-agent/certs/abs-agent-prod.pfx
AZURE_CLIENT_CERTIFICATE_PASSWORD=<pfx-password>

# 選用：限制直接存取 Uvicorn 的內部 API key。
# 設定後，Nginx 必須注入相同的 X-Chatbot-Key 標頭。
CHATBOT_API_KEY=<random-secret>
```

保護檔案：

```bash
sudo chown root:absagent /etc/abs-agent/abs-agent.env
sudo chmod 640 /etc/abs-agent/abs-agent.env
```

> `CHATBOT_API_KEY` 僅限制直接存取 Uvicorn；它不是完整的使用者登入機制，也不會識別使用者。若要提供本地帳密登入，需另外實作安全的帳號、密碼雜湊、session、CSRF 防護與角色授權。

## 6. 建立 systemd 服務

建立 `/etc/systemd/system/abs-agent.service`：

```ini
[Unit]
Description=ABS Agent FastAPI service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=absagent
Group=absagent
WorkingDirectory=/opt/abs-agent
EnvironmentFile=/etc/abs-agent/abs-agent.env
ExecStart=/opt/abs-agent/.venv/bin/python -m uvicorn chatbot.app:app --host 127.0.0.1 --port 8080 --proxy-headers
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/var/lib/abs-agent

[Install]
WantedBy=multi-user.target
```

啟動並查看狀態：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now abs-agent
sudo systemctl status abs-agent
sudo journalctl -u abs-agent -f
```

## 7. 驗證 Service Principal 與網站

在主機上測試服務狀態：

```bash
curl http://127.0.0.1:8080/api/health
curl http://127.0.0.1:8080/api/agents
```

若需要驗證後端服務帳號取得 Token，使用環境檔啟動一次性測試；不要使用個人 `az login`：

```bash
sudo -u absagent env $(grep -v '^#' /etc/abs-agent/abs-agent.env | xargs) \
  /opt/abs-agent/.venv/bin/python -c \
  "from azure.identity import DefaultAzureCredential; print(DefaultAzureCredential().get_token('https://ai.azure.com/.default').expires_on)"
```

取得 token 不代表一定具備呼叫 Agent 的權限；請再從 Web UI 送出一則測試問題，確認 Foundry Project RBAC、Agent 名稱與版本正確。

## 8. Nginx 反向代理範例

以下範例只供內網 HTTPS 使用。將 `<internal-hostname>` 和 TLS 憑證路徑替換為實際值。

```nginx
server {
    listen 443 ssl http2;
    server_name <internal-hostname>;

    ssl_certificate     /etc/nginx/tls/abs-agent.crt;
    ssl_certificate_key /etc/nginx/tls/abs-agent.key;

    client_max_body_size 1m;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        # 僅在設定 CHATBOT_API_KEY 時啟用；值必須與環境檔一致。
        proxy_set_header X-Chatbot-Key <random-secret>;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_buffering off;
    }
}
```

SSE 串流需要 `proxy_buffering off` 與較長的 `proxy_read_timeout`。設定後執行：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## 9. 日常維運

### 更新程式碼

```bash
sudo -u absagent git -C /opt/abs-agent pull
sudo -u absagent /opt/abs-agent/.venv/bin/python -m pip install -r /opt/abs-agent/chatbot/requirements.txt
sudo systemctl restart abs-agent
sudo systemctl status abs-agent
```

### 查看日誌

```bash
sudo journalctl -u abs-agent -n 100 --no-pager
sudo journalctl -u abs-agent -f
```

### 備份與還原對話資料

先停止服務或使用 SQLite 在線備份方式，再備份資料庫：

```bash
sudo systemctl stop abs-agent
sudo cp /var/lib/abs-agent/chat.db /secure-backup/abs-agent-chat-$(date +%F).db
sudo chown absagent:absagent /var/lib/abs-agent/chat.db
sudo systemctl start abs-agent
```

備份檔含使用者對話內容，必須依公司資料分類與保留規範加密及控管存取。

### 更新 Agent 版本

1. 在 Foundry Publish 新版本。
2. 更新 `chatbot/agents.json` 的 `version`。
3. 驗證 JSON 格式與 Agent ID。
4. `sudo systemctl restart abs-agent`。
5. 從 UI 新建對話並測試。

## 10. 故障排除

| 現象                          | 檢查方式                                                                                           |
| ----------------------------- | -------------------------------------------------------------------------------------------------- |
| 服務無法啟動                  | `sudo journalctl -u abs-agent -n 100 --no-pager`；檢查 `.env`、`agents.json` 與 Python 套件。      |
| `DefaultAzureCredential` 失敗 | 確認 `AZURE_TENANT_ID`、`AZURE_CLIENT_ID`、憑證路徑、密碼與檔案權限；正式環境不應依賴 `az login`。 |
| `401` 或 `403`                | 確認 App Registration 的 Service Principal 在正確 Foundry Project 具有必要角色，並等待 RBAC 傳播。 |
| 找不到 Agent／版本            | Agent 必須 Publish；確認 `agents.json` 的 endpoint、name、version。                                |
| UI 等待但沒有回覆             | 查 `journalctl` 的 Foundry 呼叫錯誤，並確認 Nginx 已設定 `proxy_buffering off`。                   |
| SQLite locked                 | 確認只執行一個應用程式實例；多副本部署需改用共用的資料庫與會話設計。                               |

## 11. 不再適用的舊版項目

請勿依照舊文件啟動下列元件：

- `docker compose up qdrant`
- `rag/ingest.py`
- RAG API 的 `/query`、`/query/stream`
- MCP server
- `FOUNDRY_API_KEY` 舊版設定

目前版本直接呼叫已發佈的 Microsoft Foundry Agent。
