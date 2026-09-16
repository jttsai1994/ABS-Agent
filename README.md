# AI-SUPERNOTES — FIT 內部知識庫智能問答系統

整合 RAG（檢索增強生成）、對話記憶、會話管理的企業知識庫系統。支援 web UI 聊天、流式回應、多輪對話記憶、LLM/MCP 整合。

## 核心功能

| 功能                | 說明                                           |
| ------------------- | ---------------------------------------------- |
| 🤖 **智能問答**     | 混合搜尋（向量 + BM25） + Azure LLM 自動推論   |
| 💬 **對話記憶**     | 最多帶入前 3 輪對話，自動偵測追問並擴展查詢    |
| 📊 **會話管理**     | 編輯標題、刪除舊對話，左側欄直觀檢視           |
| ⚡ **即時流式輸出** | 逐字元推送 SSE，提升 UX                        |
| 🔗 **MCP 伺服器**   | 與 Azure Foundry Agent 整合，提供 3 個工具函數 |
| 📝 **SQLite 歷史**  | 完整對話記錄，支援導出                         |

## 架構概述

```
┌─────────────────────────────────────────────────────┐
│           使用者瀏覽器 (Web UI)                       │
│    chatbot/static/index.html (單頁應用)              │
└─────────────────────────────────────────────────────┘
                      ↓ HTTP
┌─────────────────────────────────────────────────────┐        ┌──────────────────────────────┐
│         Chatbot API (chatbot/app.py:8080)            │◄────► │ SQLite (VM 本機)             │
│  • 會話管理                                           │      │ chatbot/chat.db               │
│  • 歷史記憶 (last 3 turns)                           │       │會話 + 對話紀錄持久化           │
│  • 追問偵測 & 查詢擴展                               │        └──────────────────────────────┘
└─────────────────────────────────────────────────────┘
                      ↓ HTTP
┌─────────────────────────────────────────────────────┐
│        RAG API (rag/main.py:8000)                    │
│  • 混合檢索 (Qdrant + BM25 + RRF)                    │
│  • LLM 推論 (Azure Foundry gpt-5.4-nano)             │
│  • 流式/非流式端點                                    │
└─────────────────────────────────────────────────────┘
                ↙ 向量        ↘ 全文
         ┌──────────┐        ┌──────────┐
         │ Qdrant   │        │ BM25 idx │
         │ (Docker) │        │ (memory) │
         └──────────┘        └──────────┘
```

## 模組結構

```
AI-SUPERNOTES/
├── README.md                         # 本文件
├── DEPLOYMENT.md                     # VM 部署 & 內部連接指南
├── docker-compose.yml
├── .env.example
│
├── chatbot/                          # Web 聊天介面
│   ├── app.py                        # FastAPI 後端（port 8080）
│   ├── chat.db                       # SQLite 對話歷史
│   └── static/
│       └── index.html                # SPA 前端（HTML+JS）
│
├── rag/                              # RAG 核心
│   ├── main.py                       # FastAPI RAG API（port 8000）
│   ├── ingest.py                     # 向量化：JSONL → Qdrant
│   ├── retriever.py                  # 混合檢索
│   ├── config.py                     # 設定管理
│   └── mcp_server/
│       └── server.py                 # MCP 伺服器（port 8001）
│
├── fitwebparsing/                    # 資料爬蟲層
│   ├── README.md
│   ├── run_scraper.py
│   └── output/rag/index.jsonl        # 文件語料庫
│
└── scripts/
    ├── start.sh                      # 啟動所有服務
    ├── install-hooks.sh              # Git hook 安裝
    └── ...
```

## 快速開始（本地開發）

### 直接使用 Microsoft Foundry Agent 作為聊天核心

目前 `chatbot` 支援兩種後端：

- `CHAT_PROVIDER=foundry_agent`：直接呼叫已發佈的 Foundry Agent（建議用於截圖中的 `abs-sharepoint`）。
- `CHAT_PROVIDER=rag`：維持原本的 `RAG_BASE_URL` 呼叫流程。

#### 1. 從 Foundry 複製 Agent 資訊

在 Foundry Agent 頁面選擇 **Call agent → Python**，取得以下三個值：

| 畫面欄位         | 環境變數                   | 此專案範例                                                                         |
| ---------------- | -------------------------- | ---------------------------------------------------------------------------------- |
| Project endpoint | `FOUNDRY_PROJECT_ENDPOINT` | `https://poc-test-foundryiq.services.ai.azure.com/api/projects/poc-test-foundryiq` |
| Agent name       | `FOUNDRY_AGENT_NAME`       | `abs-sharepoint`                                                                   |
| Version          | `FOUNDRY_AGENT_VERSION`    | `7`                                                                                |

> Agent 必須先 **Publish**。版本更新後，也要同步更新 `FOUNDRY_AGENT_VERSION`。

#### 2. 建立本機設定

在 PowerShell 執行：

```powershell
Copy-Item chatbot/.env.example chatbot/.env
```

確認 `chatbot/.env` 至少包含：

```dotenv
CHAT_PROVIDER=foundry_agent
FOUNDRY_PROJECT_ENDPOINT=https://poc-test-foundryiq.services.ai.azure.com/api/projects/poc-test-foundryiq
FOUNDRY_AGENT_NAME=abs-sharepoint
FOUNDRY_AGENT_VERSION=7
```

`.env` 已被 Git 忽略，不要將權杖、Client Secret 或其他秘密提交到版本庫。

#### 3. 安裝套件並登入 Azure

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r chatbot/requirements.txt
az login
```

若帳號位於多租戶環境，使用 `az login --tenant <tenant-id>`，並可在 `chatbot/.env` 設定 `AZURE_TENANT_ID`。登入身分至少需要目標 Foundry Project 的 **Foundry User** 角色。

正式部署到 Azure 時不需執行互動式登入；請啟用 App Service、Container App 或 VM 的 Managed Identity，並將相同的 Foundry Project 角色授予該身分。`DefaultAzureCredential` 會自動選用可用的登入方式。

#### 4. 啟動與測試

```powershell
python -m uvicorn chatbot.app:app --host 127.0.0.1 --port 8080
```

另開 PowerShell，先檢查設定：

```powershell
Invoke-RestMethod http://127.0.0.1:8080/api/health
```

預期 `provider` 為 `foundry_agent`、`status` 為 `ok`，且 `missing` 為空陣列。接著直接測試後端：

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8080/api/chat `
    -Method POST `
    -ContentType 'application/json' `
    -Body (@{ message = '請簡介你可以協助的事項' } | ConvertTo-Json)
```

最後開啟 <http://127.0.0.1:8080>，即可用既有介面測試 Agent。瀏覽器只連 FastAPI，Azure 身分與 Endpoint 均保留在伺服器端。

#### 常見 Foundry 連線錯誤

| 狀態／訊息                      | 處理方式                                                                          |
| ------------------------------- | --------------------------------------------------------------------------------- |
| `configuration_error`           | 檢查 `.env` 三個 `FOUNDRY_*` 值，修改後重啟服務。                                 |
| `DefaultAzureCredential failed` | 執行 `az login`；多租戶時指定 tenant。                                            |
| `401 Unauthorized`              | 登入身分或租戶錯誤，重新登入正確租戶。                                            |
| `403 Forbidden`                 | 對使用者或 Managed Identity 授予 Project 層級的 Foundry User 角色，等待權限生效。 |
| 找不到 Agent／版本              | 確認 Agent 已 Publish，名稱與版本必須和 **Call agent** 畫面完全一致。             |
| Endpoint 錯誤                   | 必須使用完整 Project endpoint，包含 `/api/projects/<project-name>`。              |

### 前置需求

- Python 3.11+
- Docker & Docker Compose
- Azure Foundry API key

### 1. 環境設定

```bash
# 複製環境範本到 rag/
cp rag/.env.example rag/.env

# 編輯 rag/.env，填入必需參數
# FOUNDRY_API_KEY=your-api-key
# FOUNDRY_BASE_URL=https://hub-supernote-dev.services.ai.azure.com/openai/v1

# 可選：調整 port（多服務部署時避免衝突）
# RAG_API_PORT=8000
# CHATBOT_PORT=8080
# MCP_PORT=8001
```

### 2. 啟動服務

```bash
# 啟動 Qdrant 向量資料庫
docker compose up qdrant -d

# 首次運行：向量化語料庫
cd rag && python ingest.py && cd ..

# 啟動完整系統（RAG API + Chatbot + MCP）
# start.sh 會自動讀取 rag/.env 中的 port 配置
bash scripts/start.sh

# 驗證
curl http://localhost:8000/health
curl http://localhost:8080/
curl http://localhost:8001/mcp/tools
```

### 3. 訪問

- **Web UI**：http://localhost:9000（透過 SSH 隧道）
- **RAG API**：http://localhost:8000/docs（Swagger）
- **聊天測試**：http://localhost:8080

詳細部署說明請見 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 關鍵技術選型

| 組件       | 選擇         | 原因                                           |
| ---------- | ------------ | ---------------------------------------------- |
| 向量資料庫 | Qdrant       | 輕量、高性能、metadata filter、Docker友善      |
| 全文搜尋   | BM25         | 純 Python、記憶體內、無外部依賴                |
| LLM 模型   | gpt-5.4-nano | Azure Foundry 部署模型，支援長上下文與穩定推論 |
| 對話歷史   | SQLite       | 無網絡依賴、VM 部署友善、自適應容量            |
| MCP 框架   | FastMCP      | 輕量級、快速集成、Foundry 相容                 |

## 對話流程

```
使用者提問
    ↓
[記憶檢查] → 追問？✓ 展開 query + 帶歷史
    ↓
[檢索] → Qdrant 向量 + BM25 關鍵字
    ↓
[融合] → RRF 排序（top-5 文件）
    ↓
[推論] → LLM（system prompt + history + context）
    ↓
[回應] → 流式推送或完整返回
    ↓
[存檔] → SQLite（原始問題 + 回答 + 來源）
```

## 新功能亮點

### 對話記憶（有界滑動窗口）

- **限制**：最多 3 輪（6 則訊息），≤ 3000 字元
- **自動檢測**：短訊息或參照詞（「那」「詳細」「為何」…）→ 追問
- **擴展查詢**：追問自動加上前一則用戶問題
- **防爆炸**：固定的記憶容量，適合長對話

### 會話管理

- **編輯標題**：點擊 ✎ 編輯會話名稱，即時更新（無重新加載）
- **刪除對話**：點擊 ⊗ 移除會話和全部訊息
- **會話列表**：按更新時間排序，顯示最後互動時間

### 流式回應

- **即時推送**：LLM 逐字符號透過 SSE 推送
- **Fallback**：若 `/query/stream` 不可用自動降級到 `/query`
- **錯誤恢復**：Chunk 處理錯誤不會中斷整個流

## 環境變數

| 變數               | 預設值             | 說明                |
| ------------------ | ------------------ | ------------------- |
| `FOUNDRY_API_KEY`  | （必須）           | Azure Foundry 金鑰  |
| `FOUNDRY_BASE_URL` | （必須）           | Azure Foundry 端點  |
| `RAG_API_PORT`     | 8000               | RAG API 監聽 port   |
| `CHATBOT_PORT`     | 8080               | Chatbot 監聽 port   |
| `MCP_PORT`         | 8001               | MCP 伺服器監聽 port |
| 其他參數           | 見 `rag/config.py` | 通常保持預設值即可  |

## 常見問題

**Q：為什麼記憶容量有限？**  
A：避免 token 數爆炸。Azure LLM 單次最多 128K tokens，留充足空間給檢索結果和上下文。

**Q：如何在公司內部連接？**  
A：見 [DEPLOYMENT.md#內部訪問](DEPLOYMENT.md#內部訪問) 的 SSH tunnel 設定。

**Q：支援多個使用者同時聊天嗎？**  
A：支援。每個會話有獨立 `session_id`，SQLite 自動處理並發。

## 貢獻指南

1. 新功能請建 feature branch：`git checkout -b feat/xxx`
2. 修 bug 請建 bugfix branch：`git checkout -b fix/xxx`
3. Commit 格式：`feat(scope): description` 或 `fix(scope): description`
4. PR 前確保本地測試通過

## 許可證

內部使用。
