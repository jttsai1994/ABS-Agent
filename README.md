# ABS Agent

FIT 內部知識問答網站。使用者透過 Web 介面選擇已發佈的 Microsoft Foundry Agent，由 FastAPI 後端以受控身分呼叫 Foundry Responses API，並以 SSE 串流顯示回答。

> 本專案目前的核心是 **Microsoft Foundry Agent**，不是舊版 Qdrant／BM25 RAG 架構。根目錄的 `docker-compose.yml` 是從舊專案保留的檔案，**不會啟動目前的 Chatbot，也不應用於此版本部署**。

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
│   ├── .env.example           # 非機密設定範本
│   ├── requirements.txt
│   └── static/index.html      # 單頁聊天介面
├── docs/
│   ├── DEPLOY.md              # 給維運／架構人員的部署設計
│   └── DEPLOYMENT.md          # 給部署與日常維護人員的操作手冊
├── docker-compose.yml         # 舊版 RAG 遺留檔案；目前不使用
└── README.md
```

## 快速開始：本機開發

### 1. 建立 Python 環境並安裝套件

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r chatbot/requirements.txt
```

### 2. 建立設定檔

```powershell
Copy-Item chatbot/.env.example chatbot/.env
```

設定 `chatbot/.env`：

```dotenv
CHAT_PROVIDER=foundry_agent
FOUNDRY_AGENTS_FILE=agents.json
FOUNDRY_DEFAULT_AGENT_ID=sharepoint
```

`chatbot/agents.json` 是 Agent 白名單。每個 Agent 必須已在 Foundry **Publish**，且需填入正確的 Project endpoint、Agent name 與 version。

### 3. 設定 Azure 認證

可選其中一種方式：

- **開發者 Azure CLI**：執行 `az login --tenant <tenant-id>`。適合暫時的本機開發。
- **Service Principal 憑證**：建議用於本機整合測試與正式內網主機。設定以下環境變數後，程式會自動使用 `EnvironmentCredential`：

```dotenv
AZURE_TENANT_ID=<tenant-id>
AZURE_CLIENT_ID=<app-registration-client-id>
AZURE_CLIENT_CERTIFICATE_PATH=<僅伺服器可讀取的 .pfx 或 .pem 路徑>
AZURE_CLIENT_CERTIFICATE_PASSWORD=<私鑰密碼>
```

Service Principal 必須在目標 Foundry Project 具有最低必要權限，通常由 `Azure AI User` 開始授權。詳細憑證與權限做法請見 [docs/DEPLOY.md](docs/DEPLOY.md)。

### 4. 啟動網站

```powershell
.\.venv\Scripts\python.exe -m uvicorn chatbot.app:app --host 127.0.0.1 --port 8080
```

開啟 <http://127.0.0.1:8080>，並可用下列端點確認狀態：

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

- `.env`、`.pfx`、`.pem`、密碼與 Client Secret 不得提交至 Git。
- 正式環境不要使用個人 Azure CLI 登入；請使用獨立的 Service Principal 憑證。
- PFX 私鑰僅授權給執行 Uvicorn 的服務帳號讀取。
- 已提供本地帳密登入、Argon2 密碼雜湊、HttpOnly session cookie、CSRF 驗證、`admin`／`user` 角色、群組 Agent 授權、會話擁有者隔離與稽核紀錄。
- 部署時透過 `BOOTSTRAP_ADMIN_USERNAME` 與 `BOOTSTRAP_ADMIN_PASSWORD` 建立第一位管理員；這兩個值只能放在受保護的主機祕密設定中。
- 對內網正式開放時仍必須使用 HTTPS 反向代理，並將 `COOKIE_SECURE=true`。
- Foundry／SharePoint 資料會以後端 Service Principal 的權限存取；應在網站後端另行實作部門、廠區或角色的資料授權規則。

## 文件

- [部署設計與安全要求](docs/DEPLOY.md)
- [部署、啟動與維運操作](docs/DEPLOYMENT.md)

## 授權

限 FIT 內部使用。
