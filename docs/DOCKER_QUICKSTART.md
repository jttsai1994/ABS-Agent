# Docker 容器化快速開始指南

本指南提供最小化步驟在本地或生產環境快速啟動 ABS Agent Docker 容器。

## 前置條件

1. **Docker 與 Docker Compose**：已安裝 Docker 20.10+ 和 Compose 2.0+
2. **Azure 認證**：完成 [DEPLOY.md](docs/DEPLOY.md) 第 3 節的憑證準備
3. **Foundry Project**：已取得 Agent endpoint、名稱與版本

## 快速步驟（開發環境）

### 1️⃣ 環境檔案

```bash
# 複製範本
cp .env.prod.example .env.prod

# 編輯（填入 Azure 認證與管理員帳密）
nano .env.prod
```

### 2️⃣ Agent 白名單

編輯 `chatbot/agents.json`：

```json
{
  "agents": [
    {
      "id": "sharepoint",
      "label": "SharePoint AI Search",
      "project_endpoint": "https://<resource>.services.ai.azure.com/api/projects/<id>",
      "agent_name": "SharePoint",
      "version": "2025-01-15",
      "enabled": true
    }
  ]
}
```

### 3️⃣ 啟動容器

```bash
# 構建 & 啟動
docker compose build
docker compose up -d

# 檢查狀態
docker compose ps
docker compose logs -f abs-agent

# 測試
curl http://127.0.0.1:8080/api/health
```

### 4️⃣ 登入

瀏覽 `http://localhost:8080`，使用 `.env.prod` 中的管理員帳密登入。

---

## 生產環境部署

### 前置：憑證與主機設定

```bash
# 1. 在主機建立目錄
mkdir -p /opt/abs-agent/certs

# 2. 安全複製 .pfx（來自開發機）
scp abs-agent-prod.pfx user@production-host:/opt/abs-agent/certs/
chmod 600 /opt/abs-agent/certs/abs-agent-prod.pfx

# 3. TLS 憑證（用於 Nginx）
mkdir -p /etc/nginx/tls
# 將 HTTPS TLS 憑證放在此（不是 .pfx，是服務器憑證）
```

### 部署命令

```bash
cd /opt/abs-agent

# 複製環境檔案（編輯填入機密）
cp .env.prod.example .env.prod
chmod 600 .env.prod

# 構建並啟動
docker compose build
docker compose up -d

# 驗證
docker compose ps
docker compose logs abs-agent
```

---

## 常用命令

```bash
# 即時日誌
docker compose logs -f abs-agent

# 重啟容器
docker compose restart abs-agent

# 停止
docker compose stop

# 備份資料庫
docker run --rm -v abs-agent-data:/data -v $(pwd):/backup \
  alpine cp /data/chat.db /backup/chat.db.backup

# 進入容器終端（除錯用）
docker compose exec abs-agent bash
```

---

## 下一步

- 詳細部署步驟：[DEPLOYMENT.md](docs/DEPLOYMENT.md)
- 架構與安全設計：[DEPLOY.md](docs/DEPLOY.md)
- 發現問題？查看故障排除章節

---

**⚠️ 重要提醒**：

- `.env.prod` 包含機密，**永不提交 Git**
- `.pfx` 私鑰**只能透過 bind mount 傳遞**，不可打進 image
- 生產環境必須啟用 HTTPS（設定 `COOKIE_SECURE=true`）
