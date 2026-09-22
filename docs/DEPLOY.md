# ABS Agent 部署設計

本文件提供架構師、Azure／Entra 管理員與維運人員部署目前 **Foundry Agent 版** ABS Agent 的設計依據。

> 本文件不適用於舊版 Qdrant、BM25、RAG API 或 MCP 服務。這些元件不在目前網站的請求路徑上。

## 1. 目標架構

```text
公司內網使用者
      │
      │ HTTPS
      ▼
反向代理（Nginx / IIS）
      │ localhost HTTP
      ▼
ABS Agent FastAPI 服務（Uvicorn）
      ├─ SQLite：會話與回饋
      ├─ Agent allowlist：chatbot/agents.json
      └─ Service Principal + 憑證
                │ HTTPS 443
                ▼
       Microsoft Entra ID
                │
                ▼
       Microsoft Foundry Project / Published Agents
```

## 2. 主機與網路需求

| 項目      | 建議                                                 |
| --------- | ---------------------------------------------------- |
| 作業系統  | Ubuntu 22.04 LTS 或更新版本                          |
| CPU / RAM | 至少 2 vCPU / 4 GB RAM；建議 4 vCPU / 8 GB RAM       |
| Python    | Python 3.11 以上                                     |
| 網路輸出  | TCP 443 至 Entra ID 與目標 `*.services.ai.azure.com` |
| 網路輸入  | 僅內網反向代理的 HTTPS；Uvicorn 不直接公開給使用者   |
| 儲存空間  | 10 GB 以上；SQLite 與日誌需求視使用量成長            |
| 執行帳號  | 專屬的低權限 Linux service account，例如 `absagent`  |

不需要部署 Qdrant、Docker、向量索引、RAG API 或 MCP server。

## 3. Azure／Entra 身分設計

### 3.1 使用 Service Principal + 憑證

正式內網主機不應使用開發者的 `az login`。建立兩個分開的 App Registration：

| 環境       | 建議名稱         | 說明                 |
| ---------- | ---------------- | -------------------- |
| 開發／測試 | `abs-agent-dev`  | 開發機與整合測試使用 |
| 正式環境   | `abs-agent-prod` | 僅正式網站主機使用   |

每個 App Registration 使用自己的憑證與到期日，避免開發環境的憑證外洩影響正式環境。

### 3.2 建立與授權流程

1. Entra ID 建立單一租戶 App Registration。
2. 為每個環境建立一張 Client Authentication 憑證。
3. 在 App Registration 的 **Certificates & secrets** 上傳公開憑證（`.cer`／`.pem` 公鑰）。
4. 將私鑰 `.pfx`／`.pem` 只放在對應服務主機。
5. 在目標 Foundry Project 的 IAM，將 Service Principal 指派最低必要角色；通常從 `Azure AI User` 開始。
6. 使用主機的 Service Principal 進行 Agent 呼叫測試。

若 `Azure AI User` 不足以進行目標操作，應依 Foundry 回傳的授權錯誤，由資源管理員評估是否必須增加 `Azure AI Developer`。不要先給 Owner、Contributor 或 Subscription 範圍角色。

### 3.3 服務帳號設定

建議私鑰位置為 `/etc/abs-agent/certs/abs-agent-prod.pfx`，且只允許服務帳號讀取：

```text
擁有者：absagent
群組：absagent
權限：0600
```

正式環境設定值應由 systemd 的 `EnvironmentFile`、受管祕密服務或公司密碼管理系統提供：

```dotenv
AZURE_TENANT_ID=<tenant-id>
AZURE_CLIENT_ID=<application-client-id>
AZURE_CLIENT_CERTIFICATE_PATH=/etc/abs-agent/certs/abs-agent-prod.pfx
AZURE_CLIENT_CERTIFICATE_PASSWORD=<由祕密管理系統提供>
```

不要把私鑰或密碼放進 Git、Docker image、一般使用者家目錄或可被備份／同步的共用資料夾。

## 4. 應用程式安全設計

### 4.1 使用者驗證與 Foundry 認證分離

- 網站使用者可以使用公司本地帳密登入；不需要 O365 帳號。
- 後端固定使用 Service Principal 呼叫 Foundry；使用者永遠不接觸 Azure CLI、device code 或 Azure Token。
- 目前程式尚未實作本地帳密登入、角色或使用者管理。正式開放前必須補上此功能，或在反向代理／既有內網身分系統進行驗證。

### 4.2 授權與資料範圍

Foundry Agent 以 Service Principal 身分存取資源，不會自動套用網站使用者個別的 SharePoint 權限。因此：

- 只將所有登入者都可存取的資料提供給此 Agent，或
- 在後端依使用者角色選擇不同 Agent／知識來源並驗證授權。

前端隱藏選項不能取代後端授權檢查。

### 4.3 網路與 TLS

- 使用 Nginx 或 IIS 對外提供 HTTPS，並限制為公司內網／VPN。
- Uvicorn 只綁定 `127.0.0.1`。
- 對反向代理啟用 HTTPS、適當的請求大小限制與存取日誌。
- 保護 `chatbot/chat.db`，並建立加密備份與保留政策。

## 5. 設定檔責任

| 檔案／位置                     | 用途                   | 是否可提交 Git     |
| ------------------------------ | ---------------------- | ------------------ |
| `chatbot/agents.json`          | Agent 白名單與版本     | 可；限制儲存庫權限 |
| `chatbot/.env.example`         | 非機密範本             | 可                 |
| `chatbot/.env`                 | 本機開發設定           | 不可               |
| `/etc/abs-agent/abs-agent.env` | 正式主機機密設定       | 不可               |
| `/etc/abs-agent/certs/*.pfx`   | Service Principal 私鑰 | 不可               |
| `chatbot/chat.db`              | 對話紀錄               | 不可               |

## 6. 監控與維運

至少監控：

- systemd 服務是否存活與重啟次數。
- Foundry 請求的失敗率、延遲與 `401`／`403` 回應。
- Service Principal 憑證到期日；建議在到期前 60、30、7 天通知。
- SQLite 檔案大小、可用磁碟空間與備份完成狀態。
- 反向代理的 HTTP 5xx、異常流量與 TLS 憑證期限。

部署及日常操作請見 [DEPLOYMENT.md](DEPLOYMENT.md)。
