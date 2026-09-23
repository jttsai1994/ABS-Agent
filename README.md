# ABS Agent

FIT 內部知識問答網站。使用者透過 Web 介面選擇已發佈的 Microsoft Foundry Agent，由 FastAPI 後端以受控身分呼叫 Foundry Responses API，並以 SSE 串流顯示回答。

**部署方式：** 本專案使用 Docker Compose 容器化部署，適用開發與正式環境。快速開始見 [docs/DOCKER_QUICKSTART.md](docs/DOCKER_QUICKSTART.md)；完整部署說明見 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 功能

- 多 Agent 選擇：後端白名單管理可用的 Foundry Agents。
- SSE 串流：回答逐段顯示，等待時顯示「Agent 思考中」動畫。
- 對話管理：SQLite 保存會話、訊息、標題與回饋。
- Agent 綁定：一個會話固定使用同一 Agent；切換 Agent 會建立新會話。
- 安全設定：Foundry Project endpoint 與 Azure 認證僅保留於伺服器端。
- 認證相容：開發環境可用 Azure CLI；內網正式環境建議使用 Service Principal 憑證。

## 架構

```text
瀏覽器
  │ HTTPS / HTTP
  ▼
FastAPI + Web UI（chatbot/app.py）
  ├─ SQLite（chatbot/chat.db：對話與回饋）
  ├─ agents.json（Agent 白名單；不提供給前端）
  └─ DefaultAzureCredential
          │ HTTPS
          ▼
Microsoft Entra ID ──> Microsoft Foundry Project ──> Published Agent
```

網站使用者不會直接取得 Azure Token 或 Foundry Project endpoint。網站本身的帳號／權限機制與後端連到 Foundry 的 Service Principal 是兩個獨立的責任。

## 專案結構

```text
ABS-Agent/
├── chatbot/
│   ├── app.py                 # FastAPI、SSE、會話與 Foundry Agent 代理
│   ├── agents.json            # 可使用 Agent 的伺服器端白名單
│   ├── .env.example           # 開發用環境設定範本
│   ├── requirements.txt
│   └── static/index.html      # 單頁聊天介面
├── docs/
│   ├── DEPLOY.md              # 給架構人員的 Docker 設計與安全要求
│   ├── DEPLOYMENT.md          # 給部署與維運人員的完整操作手冊
│   ├── DOCKER_QUICKSTART.md   # 開發與生產快速啟動指南
│   └── TESTING.md             # 測試指南
├── Dockerfile                 # Python 3.11-slim，非 root appuser
├── docker-compose.yml         # FastAPI + Nginx 反向代理服務定義
├── nginx.conf                 # HTTPS 反向代理與安全標頭
├── .env.prod.example          # 正式環境環境設定範本（不提交 .env.prod）
└── README.md
```

## 快速開始

### 推薦方式：Docker Compose（3 分鐘內啟動）

```bash
# 複製環境檔
cp .env.prod.example .env.prod

# 編輯 .env.prod，填入 Azure Service Principal 認證
# （開發時可先留著預設值測試）

# 構建與啟動
docker compose build
docker compose up -d

# 檢查日誌
docker compose logs -f abs-agent

# 開啟瀏覽器
# http://127.0.0.1:8080
```

詳見 [docs/DOCKER_QUICKSTART.md](docs/DOCKER_QUICKSTART.md)。

### 備選方式：本機 Python venv（傳統開發）

若不使用 Docker，可直接用 Python 虛擬環境：

```powershell
# 建立虛擬環境
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r chatbot/requirements.txt

# 設定環境
Copy-Item chatbot/.env.example chatbot/.env
```

編輯 `chatbot/.env`：

```dotenv
CHAT_PROVIDER=foundry_agent
FOUNDRY_AGENTS_FILE=agents.json
FOUNDRY_DEFAULT_AGENT_ID=sharepoint
CHAT_DB_PATH=chatbot/chat.db
```

設定 Azure 認證（二選一）：

- **開發者 Azure CLI**：`az login --tenant <tenant-id>`（暫時開發用）
- **Service Principal 憑證**：見 [docs/DEPLOY.md](docs/DEPLOY.md) 第 3 節

```dotenv
AZURE_TENANT_ID=<tenant-id>
AZURE_CLIENT_ID=<app-registration-client-id>
AZURE_CLIENT_CERTIFICATE_PATH=<.pfx 或 .pem 路徑>
AZURE_CLIENT_CERTIFICATE_PASSWORD=<私鑰密碼>
```

啟動應用程式：

```powershell
.\.venv\Scripts\python.exe -m uvicorn chatbot.app:app --host 127.0.0.1 --port 8080
```

開啟 <http://127.0.0.1:8080> 並測試：

```powershell
Invoke-RestMethod http://127.0.0.1:8080/api/health
Invoke-RestMethod http://127.0.0.1:8080/api/agents
```

## Agent 設定

`chatbot/agents.json` 範例：

```json
{
  "agents": [
    {
      "id": "sharepoint",
      "label": "SharePoint AI Search",
      "project_endpoint": "https://<resource>.services.ai.azure.com/api/projects/<project>",
      "agent_name": "<published-agent-name>",
      "version": "<published-version>",
      "enabled": true
    }
  ]
}
```

- `id`：前後端交換用識別碼，必須唯一。
- `label`：前端顯示名稱。
- `project_endpoint`、`agent_name`、`version`：從 Foundry Agent 的 **Call agent → Python** 取得。
- `enabled`：設為 `false` 即可停用，而無須刪除設定。

變更 Agent 設定後請重啟後端。不要把 `agents.json` 當成秘密檔；其中 endpoint 雖不會回傳給瀏覽器，但仍應限制 Git 儲存庫的存取權。

## API 概要

| 端點                                      | 用途                        |
| ----------------------------------------- | --------------------------- |
| `GET /`                                   | 聊天介面                    |
| `GET /api/health`                         | 服務與 Provider 狀態        |
| `GET /api/agents`                         | 前端可顯示的 Agent metadata |
| `POST /api/chat`                          | 非串流問答                  |
| `POST /api/chat/stream`                   | SSE 串流問答                |
| `GET /api/sessions`                       | 會話清單                    |
| `GET`／`DELETE /api/history/{session_id}` | 讀取／刪除會話歷程          |

## 安全注意事項

- ❌ **不得**提交至 Git：`.env.prod`、`certs/*.pfx`、`.env` 內含密碼、Client Secret。
- ✅ **可在 Git 中**：`.env.example`、`.env.prod.example`（作為配置範本）。
- 正式環境必須使用 Service Principal 憑證，**不可**依賴個人 Azure CLI 登入。
- Docker 部署時，`.pfx` 檔案只能透過 **bind mount**（唯讀）傳遞給容器，不可打進 image。
- 已提供本地帳密登入、Argon2 密碼雜湊、HttpOnly session cookie、CSRF 驗證、`admin`／`user` 角色、群組 Agent 授權、會話擁有者隔離與稽核紀錄。
- 部署時透過 `BOOTSTRAP_ADMIN_USERNAME` 與 `BOOTSTRAP_ADMIN_PASSWORD` 建立第一位管理員；這兩個值只能放在受保護的主機祕密設定中。
- 對內網正式開放時必須使用 HTTPS、COOKIE_SECURE=true，並設置 Nginx 反向代理（見 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)）。
- Foundry／SharePoint 資料會以後端 Service Principal 的權限存取；應在應用程式或 Foundry Project RBAC 層另行限制資料存取範圍。

## 文件

- [快速開始 — Docker](docs/DOCKER_QUICKSTART.md)（推薦閱讀）
- [部署設計與安全要求](docs/DEPLOY.md)（給架構人員）
- [部署、啟動與維運操作](docs/DEPLOYMENT.md)（給維運人員）
- [測試指南](docs/TESTING.md)

## 授權

限 FIT 內部使用。
