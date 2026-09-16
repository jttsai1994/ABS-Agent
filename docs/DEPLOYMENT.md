# AI-SUPERNOTES 部署指南

本文件說明如何在 FIT 公司 VM 上啟動聊天服務，以及內部員工如何遠端訪問。

## 📋 目錄

- [VM 上啟動服務](#vm-上啟動服務)
- [內部訪問](#內部訪問)
- [故障排除](#故障排除)
- [常見命令](#常見命令)

---

## VM 上啟動服務

### 系統環境

| 項目     | 配置                        |
| -------- | --------------------------- |
| 作業系統 | Ubuntu 20.04+               |
| Python   | 3.11+                       |
| Docker   | 20.10+（含 Docker Compose） |
| 記憶體   | 建議 8GB+                   |
| 磁碟     | Qdrant 向量索引 ~2-5GB      |

### 前置步驟

#### 1. SSH 登入 VM

```bash
ssh fit-dt@10.56.110.67 -p 11222
# 輸入密碼：6688
```

#### 2. 確認環境

```bash
# 進入專案目錄
cd ~/volume/AI-SUPERNOTES

# 檢查 Python
python --version  # 應為 3.11+

# 檢查 Docker
docker --version
docker compose --version

# 檢查 git
git status
```

#### 3. 環境配置

複製範本並編輯環境變數：

```bash
cp rag/.env.example rag/.env
nano rag/.env  # 或用其他編輯器
```

**必須設定的變數**（放在 `rag/.env`）：

```bash
# Azure Foundry API（必填）
FOUNDRY_API_KEY=your_api_key_here
FOUNDRY_BASE_URL=https://hub-supernote-dev.services.ai.azure.com/openai/v1
```

**可選：調整服務監聽 port（多個部署或有 port 衝突時）**：

```bash
# 修改為不同 port，避免與其他服務衝突
# RAG_API_PORT=9000
# CHATBOT_PORT=9080
# MCP_PORT=9001

# 或可選的 API 金鑰保護
# CHATBOT_API_KEY=your_secret_key
# RAG_API_KEY=your_secret_key
```

### 啟動服務

#### 前置：確認 Qdrant 已運行

Qdrant 向量資料庫應由維運同事在 Docker 中啟動。確認其狀態：

```bash
# 檢查 Qdrant 是否正常
curl http://localhost:6333/health
```

#### 第一次部署

```bash
# 1. 確認環境變數已設定（見步驟 3）
cat rag/.env

# 2. 向量化語料庫（首次執行，需要 30~60 分鐘）
cd rag
python ingest.py
# 或 dry-run 先看筆數
python ingest.py --dry-run
cd ..

# 3. 啟動所有服務（RAG API、Chatbot、MCP）
bash scripts/start.sh
```

#### 日常啟動

```bash
# 一鍵重啟所有服務（自動讀取 rag/.env 中的 port 配置）
bash scripts/start.sh
```

**start.sh 會自動讀取 `rag/.env` 中的 port 設定**（若無設定則使用預設值）。

#### 驗證服務啟動

```bash
# 查看進程
ps aux | grep uvicorn

# 檢查 RAG API health
curl http://localhost:8000/health

# 檢查 Chatbot
curl http://localhost:8080/

# 檢查日誌
tail -50 ~/volume/rag.log
tail -50 ~/volume/chatbot.log

# 查看 Qdrant 狀態
curl http://localhost:6333/health
```

### 服務埠位對應

| 服務        | 預設埠 | 可配置環境變數 | 說明                         |
| ----------- | ------ | -------------- | ---------------------------- |
| Qdrant      | 6333   | —              | 向量資料庫（由維運同事管理） |
| RAG API     | 8000   | `RAG_API_PORT` | 混合檢索 + LLM               |
| Chatbot API | 8080   | `CHATBOT_PORT` | 對話代理 + Web UI            |
| MCP 伺服器  | 8001   | `MCP_PORT`     | Foundry Agent 整合           |

**多個部署時調整 port**：編輯 `rag/.env` 填入 `RAG_API_PORT=9000` 等值，`start.sh` 會自動使用。

---

## 內部訪問

公司內部員工用自己的電腦訪問 VM 上的聊天服務。

### 方案 A：SSH Tunnel（推薦）

適合 **臨時測試** 或 **開發人員**。安全性高，但需手動維護連線。

#### Windows PowerShell

```powershell
# 建立隧道（前台執行，關閉視窗即斷開）
ssh -L 9000:localhost:8080 fit-dt@10.56.110.67 -p 11222

# 或背景執行（需要後續關閉）
Start-Process -NoNewWindow -WindowStyle Hidden `
  -FilePath "ssh" `
  -ArgumentList "-L", "9000:localhost:8080", "fit-dt@10.56.110.67", "-p", "11222", "-N"

# 查看本機監聽埠
netstat -an | Select-String 9000
```

#### macOS / Linux

```bash
# 前台執行
ssh -L 9000:localhost:8080 fit-dt@10.56.110.67 -p 11222

# 背景執行
ssh -L 9000:localhost:8080 fit-dt@10.56.110.67 -p 11222 -N &
```

#### 驗證隧道

```bash
# 測試連線（在本機執行）
curl http://localhost:9000/

# 應看到 HTML 頁面（聊天介面）
```

#### 在瀏覽器訪問

建立隧道後，用瀏覽器開啟：

```
http://localhost:9000
```

### 方案 B：VPN（適合長期使用）

若公司有 VPN 網路，員工可直接連接 VM 的內部 IP（10.56.110.67:8080）。

**優點**：無需 SSH 隧道，直接訪問  
**缺點**：需要 IT 部門配置

### 方案 C：公開代理（慎用）

若要暴露到公司內網（不推薦直接暴露到公網），可設定 nginx reverse proxy：

```nginx
server {
    listen 80;
    server_name supernotes.fit.local;

    location / {
        proxy_pass http://localhost:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## 故障排除

### Qdrant 無法啟動

```bash
# 檢查 Docker daemon
sudo systemctl status docker

# 檢查是否有舊容器佔用埠
docker ps -a | grep qdrant

# 清理舊容器
docker rm ai-supernotes-qdrant

# 重新啟動
docker compose up qdrant -d
```

### RAG 服務返回 503 Retriever not initialised

```bash
# 原因：Qdrant 或 BM25 索引未初始化
# 解決：執行 ingest.py

cd ~/volume/AI-SUPERNOTES/rag
python ingest.py

# 重啟 RAG 服務
ps aux | grep "uvicorn main"
# 殺掉進程，重新啟動
```

### Chatbot 無法連接 RAG

```bash
# 檢查 RAG API 是否在線
curl http://localhost:8000/health

# 檢查 .env 中 RAG_BASE_URL
cat ~/.env | grep RAG_BASE_URL

# 查看 Chatbot 日誌
tail -100 ~/volume/chatbot.log | grep -i error
```

### SSH tunnel 斷開

```bash
# 重新建立隧道
ssh -L 9000:localhost:8080 fit-dt@10.56.110.67 -p 11222
```

---

## 常見命令

### 檢視日誌

```bash
# RAG 服務日誌（最後 100 行）
tail -100 ~/volume/rag.log

# Chatbot 日誌
tail -100 ~/volume/chatbot.log

# 持續監控（類似 tail -f）
tail -f ~/volume/rag.log
```

### 停止服務

```bash
# 停止所有 uvicorn 進程
pkill -f "uvicorn"

# 停止 Qdrant
docker compose down

# 完整清理（含資料）
docker compose down -v
```

### 重啟服務

```bash
# 一鍵重啟
bash scripts/start.sh

# 或手動重啟後重新啟動
pkill -f "uvicorn"
sleep 2
bash scripts/start.sh
```

### 檢查資料庫大小

```bash
# Qdrant 已索引文件數
curl http://localhost:6333/collections/fit_docs | jq '.result.points_count'

# 資料庫檔案大小
du -sh ~/qdrant_data/

# SQLite 對話歷史大小
ls -lh ~/volume/AI-SUPERNOTES/chatbot/chat.db
```

### 備份資料

```bash
# 備份向量索引
cp -r ~/qdrant_data ~/qdrant_backup_$(date +%Y%m%d)

# 備份對話歷史
cp ~/volume/AI-SUPERNOTES/chatbot/chat.db ~/chatbot_backup_$(date +%Y%m%d).db
```

---

## 效能最佳化

### 提高 Qdrant 記憶體

編輯 `docker-compose.yml`：

```yaml
services:
  qdrant:
    environment:
      - QDRANT__STORAGE__SNAPSHOTS_PATH=/qdrant/snapshots
    deploy:
      resources:
        limits:
          memory: 4G # 調整為實際可用
```

### 增加 LLM 快取

RAG API 已自動快取相同查詢 30 秒內的結果。如需更長快取，可編輯 `rag/config.py`。

### 監控資源使用

```bash
# 查看容器 CPU、記憶體
docker stats ai-supernotes-qdrant

# 或用 top 檢視 Python 進程
top -p $(pgrep -f uvicorn | tr '\n' ',')
```

---

## 常見問題

**Q：服務如何自動重啟？**  
A：使用 git post-merge hook。執行 `bash scripts/install-hooks.sh` 後，`git pull` 會自動重啟服務。

**Q：可以訪問舊的對話嗎？**  
A：可以。所有對話存在 SQLite，左側欄「會話列表」顯示最近 50 個會話。

**Q：能導出對話嗎？**  
A：當前 UI 不支援，但可直接查詢 SQLite：

```bash
sqlite3 ~/volume/AI-SUPERNOTES/chatbot/chat.db \
  "SELECT * FROM messages WHERE session_id='xxx' ORDER BY created_at;"
```

**Q：LLM API 配額超限怎麼辦？**  
A：檢查 Azure 配額頁面，或臨時修改 `config.py` 降低 `max_completion_tokens`。

---

## 支援聯絡

- **技術問題**：提交 GitHub issue 或聯絡開發者
- **API 金鑰問題**：聯絡 IT 部門確認 Azure 帳號
- **資料備份**：定期執行上述備份命令

---

**最後更新**：2026-05-06
