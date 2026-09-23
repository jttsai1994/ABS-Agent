# ABS Agent 部署設計 — Docker 容器化

本文件提供架構師、Azure／Entra 管理員與維運人員部署目前 **Foundry Agent 版** ABS Agent（Docker 容器化）的設計依據。

## 1. 目標架構

```text
公司內網使用者
      │
      │ HTTPS
      ▼
反向代理（Nginx / IIS）
      │ localhost HTTP
      ▼
┌─────────────────────────────────┐
│  Docker 容器（ABS Agent）        │
│  ├─ FastAPI (Uvicorn 8080)      │
│  ├─ SQLite: /data/chat.db       │
│  ├─ agents.json                 │
│  └─ /cert/abs-agent.pfx (mount) │
└─────────────────────────────────┘
      │
      │ HTTPS 443
      ▼
Microsoft Entra ID / Foundry Project
      │
      ▼
Published Agents
```

## 2. 主機與容器需求

| 項目      | 建議                                                     |
| --------- | -------------------------------------------------------- |
| 作業系統  | Linux (Ubuntu 22.04+) 或 Windows Server / MacOS (開發用) |
| Docker    | Docker Engine 20.10+ 與 Docker Compose 2.0+              |
| CPU / RAM | 至少 2 vCPU / 4 GB RAM；建議 4 vCPU / 8 GB RAM           |
| 網路輸出  | TCP 443 至 Entra ID 與 `*.services.ai.azure.com`         |
| 網路輸入  | 僅內網反向代理的 HTTPS (port 443)；容器內部 port 8080    |
| 儲存空間  | 10 GB 以上；SQLite 與日誌 volume 需求視使用量成長        |

不需要部署 Qdrant、向量索引、RAG API 或 MCP server。

## 3. Azure Service Principal 與憑證管理

### 3.1 建立 App Registration 與憑證

為每個環境（開發、正式）建立獨立的 App Registration，避免環境間的認證洩露影響：

| 環境       | 建議名稱         | 說明                           |
| ---------- | ---------------- | ------------------------------ |
| 開發／測試 | `abs-agent-dev`  | 開發機與容器測試使用           |
| 正式環境   | `abs-agent-prod` | 正式 Docker 容器與內網站台使用 |

**步驟 1：建立 App Registration（Azure Portal）**

1. 登入 [Azure Portal](https://portal.azure.com)
2. 搜尋 **應用程式註冊 (App registrations)**
3. 選擇 **新註冊**
4. 設定：
   - **名稱**：`abs-agent-prod`
   - **支援的帳戶類型**：僅此組織目錄中的帳戶
   - **重新導向 URI**：留空（服務帳號不需要）
5. 建立後，記下：
   - **Application (client) ID**
   - **Directory (tenant) ID**

**步驟 2：生成憑證（本機開發機）**

使用 OpenSSL 生成自簽憑證（有效期 3 年）：

```bash
# 生成私鑰
openssl genrsa -out abs-agent-prod.key 4096

# 生成自簽憑證（.cer）
openssl req -new -x509 -key abs-agent-prod.key -out abs-agent-prod.cer \
  -days 1095 \
  -subj "/CN=ABS Agent Prod/O=Company/C=TW"

# 生成 .pfx（包含私鑰與公開憑證，含密碼保護）
openssl pkcs12 -export -in abs-agent-prod.cer -inkey abs-agent-prod.key \
  -out abs-agent-prod.pfx -name "ABS Agent Prod Certificate" \
  -passout pass:<pfx-password>
```

保管好三個檔案：

- `abs-agent-prod.key`：私鑰（**絕不可洩露**）
- `abs-agent-prod.cer`：公開憑證（上傳至 Azure）
- `abs-agent-prod.pfx`：組合檔（放進 Docker volume）

**步驟 3：上傳 .cer 至 App Registration（Azure Portal）**

1. 開啟 App Registration `abs-agent-prod`
2. 進入 **憑證與祕密 (Certificates & secrets)**
3. 選擇 **上傳憑證**
4. 選擇 `abs-agent-prod.cer` 檔案
5. 填寫 **描述**（例如：`Prod Certificate 2025`）
6. **上傳**

憑證會在 Azure 上註冊，公開金鑰用來驗證容器發送的簽名。

**或使用 Azure CLI**（速度更快）：

```bash
az ad app credential create \
  --id <app-id> \
  --cert @abs-agent-prod.cer
```

### 3.2 在 Foundry Project 中授予 Service Principal 角色

1. 登入 Azure Portal，進入目標 **Foundry Project**（或 Azure AI Hub）
2. 選擇 **存取控制 (IAM)**
3. **新增角色指派 (Add role assignment)**
4. 選擇角色：`Azure AI User`（最低權限開始）
5. 成員：搜尋並選擇 App Registration `abs-agent-prod`
6. 指派

若後續 Agent 呼叫失敗並回傳 `403`，可根據錯誤訊息升級至 `Azure AI Developer` 或其他角色。

### 3.3 在容器內安全使用憑證

`.pfx` 檔案以唯讀 bind mount 的方式掛載至容器 `/cert/abs-agent.pfx`：

- 容器內 `AZURE_CLIENT_CERTIFICATE_PATH=/cert/abs-agent.pfx`
- 主機端 `.pfx` 檔案權限 `600`（僅 owner 可讀）
- 密碼通過環境變數 `AZURE_CLIENT_CERTIFICATE_PASSWORD`（不硬編碼）
- 不將 `.pfx` 打進 Docker image；使用 runtime volume mount

## 4. 應用程式安全設計

### 4.1 使用者驗證與 Foundry 認證完全分離

- **網站端**：本地帳密認證（Argon2 雜湊、HttpOnly session cookie、CSRF token）
  - 使用者只需公司內部帳號，**無需 O365 或 Azure CLI**
  - 支援 admin/user 角色、群組與 Agent 級別授權控制
- **後端呼叫**：固定使用 Service Principal 身分
  - 呼叫 Foundry Agent 時採用 `.pfx` 憑證認證
  - 使用者永不接觸 Azure credentials；所有 Agent 呼叫皆以 Service Principal 執行
- **初始化**：第一位管理員由環境變數建立
  - `BOOTSTRAP_ADMIN_USERNAME` 與 `BOOTSTRAP_ADMIN_PASSWORD` 在容器首次啟動時自動建立
  - 其他帳號依 `ALLOW_SELF_REGISTRATION` 決定是否開放自行註冊

### 4.2 授權與資料範圍

Foundry Agent 以 Service Principal 身分存取資源，**不會自動套用網站使用者個別的 SharePoint 權限**。因此：

- 只將所有登入者都可存取的資料提供給 Agent，或
- 在後端依使用者角色選擇不同 Agent／知識來源並驗證授權
- **前端隱藏選項不能取代後端授權檢查**

### 4.3 網路與 TLS

- Docker 容器內部 Uvicorn 綁定 `127.0.0.1:8080`，不直接對外公開
- 使用 Nginx 反向代理對外提供 HTTPS，並限制為公司內網／VPN
- 對反向代理啟用 HTTPS、適當的請求大小限制與存取日誌
- SQLite 資料庫透過 Docker volume 持久化，並建立加密備份與保留政策

## 5. 配置與檔案管理

### 5.1 Git 版控策略

| 檔案／位置             | 用途                   | 是否提交 Git   | 容器內位置                 |
| ---------------------- | ---------------------- | -------------- | -------------------------- |
| `chatbot/agents.json`  | Agent 白名單與版本     | 是（權限控制） | `/app/chatbot/agents.json` |
| `chatbot/.env.example` | 非機密環境變數範本     | 是             | （參考用）                 |
| `chatbot/.env`         | 本機開發設定           | **否**         | 不含在 image               |
| `docker-compose.yml`   | 容器編排配置           | 是             | （主機使用）               |
| `.env.prod`            | 正式環境機密設定       | **否**         | 由 docker-compose 提供     |
| `abs-agent-prod.pfx`   | Service Principal 私鑰 | **否**         | /cert/ (bind mount)        |
| `chat.db`              | 對話紀錄資料庫         | **否**         | /data/chat.db (volume)     |

### 5.2 Docker 相關檔案

```
.
├── Dockerfile                 # 容器映像定義
├── docker-compose.yml         # 容器編排配置（Git 提交）
├── .env.example               # 環境變數範本（Git 提交）
├── .env.prod                  # 正式環境機密（主機保管，不提交）
├── certs/
│   └── abs-agent-prod.pfx    # 私鑰檔（主機保管，不提交）
├── data/
│   └── chat.db                # SQLite（容器 volume，自動生成）
└── chatbot/
    ├── agents.json            # Agent 白名單（Git 提交）
    ├── requirements.txt        # Python 依賴（Git 提交）
    └── app.py                 # 應用程式碼（Git 提交）
```

## 6. 監控與維運

### 6.1 容器監控清單

- Docker 容器狀態（running / exited / restarting）
- 應用程式日誌（Docker logs）
- Foundry API 呼叫失敗率、延遲與狀態碼（401 / 403）
- SQLite 檔案大小與可用磁碟空間
- Service Principal 憑證到期日（建議到期前 60、30、7 天通知）
- Docker volume 與 bind mount 掛載狀態

### 6.2 備份與恢復

- **SQLite 資料庫**：透過 Docker volume 持久化在主機
  - 建議每日或每小時備份一次
  - 備份必須加密存放（含使用者對話內容）
- **容器設定**：版本化 `docker-compose.yml` 與 `.env.prod`
  - 存放於加密受管祕密服務（例如 HashiCorp Vault）
  - 不可進 Git，建議與 SQLite 備份同樣加密保護

部署及日常操作步驟見 [DEPLOYMENT.md](DEPLOYMENT.md)。
