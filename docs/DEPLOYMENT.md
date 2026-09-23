# ABS Agent 部署與維運手冊 — Docker Compose

本文件提供完整的 Docker 容器化部署與日常維運步驟。

**前置條件：** 請先閱讀 [DEPLOY.md](DEPLOY.md)，了解以下設計原則：

- 目標架構（Docker 容器化）與網路需求
- Azure Service Principal 與憑證的生成、上傳、安全管理
- 應用程式安全設計（使用者驗證、授權、資料範圍）
- 監控與備份政策

本手冊假設上述設計已被組織認可，且您已完成憑證準備。

---

## 1. 部署前檢查清單

部署前，確保以下條件已完備：

- ✅ **Docker 與 Docker Compose 已安裝**（Docker 20.10+, Compose 2.0+）
- ✅ **Foundry Project endpoint、Agent 名稱、版本已取得**（例如 `https://<resource>.services.ai.azure.com/api/projects/<id>`）
- ✅ **Azure App Registration 已建立**（應用程式 ID 與租戶 ID）
- ✅ **Service Principal 憑證已生成**（`.cer` / `.pfx` 檔案）
- ✅ **`.cer` 已上傳至 Azure App Registration**（Certificates & secrets）
- ✅ **Service Principal 已在 Foundry Project 取得 `Azure AI User` 角色**
- ✅ **`.pfx` 私鑰已安全儲存在主機**（權限 `600`）
- ✅ **內網 DNS、TLS 憑證（HTTPS）與 Nginx 反向代理已準備**
- ✅ **對話資料保存與備份政策已確認**

---

## 2. 準備 Docker 環境檔案

### 2.1 複製並編輯環境檔

在部署目錄（例如 `/opt/abs-agent/`）準備以下檔案：

**複製範本檔案：**

```bash
cd /opt/abs-agent
cp chatbot/.env.example .env.prod
```

**編輯 `.env.prod`（機密設定，不可提交 Git）：**

```dotenv
# ========== 應用程式設定 ==========
CHAT_PROVIDER=foundry_agent
FOUNDRY_AGENTS_FILE=agents.json
FOUNDRY_DEFAULT_AGENT_ID=sharepoint  # 預設 Agent ID（改成您的 Agent）
CHAT_DB_PATH=/data/chat.db          # 容器內 SQLite 路徑

# ========== Azure Service Principal ==========
AZURE_TENANT_ID=cdb587e9-824c-41b5-b47f-5af9864b075b
AZURE_CLIENT_ID=e8d357a3-9259-4de2-a8ef-dea7b9826870
AZURE_CLIENT_CERTIFICATE_PATH=/cert/abs-agent-prod.pfx
AZURE_CLIENT_CERTIFICATE_PASSWORD=<pfx-password>

# ========== 本地認證與會話 ==========
LOCAL_AUTH_ENABLED=true
ALLOW_SELF_REGISTRATION=false           # 改成 true 以允許自行註冊
AUTH_SESSION_DAYS=14
COOKIE_SECURE=true                      # HTTPS 部署時必須為 true
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=<strong-password-here>

# ========== 安全設定 ==========
CHATBOT_API_KEY=<random-32-char-secret>  # 用於 Nginx 驗證（選用）
```

**檔案權限（主機端）：**

```bash
chmod 600 .env.prod
```

### 2.2 準備 Agent 白名單

確保 `chatbot/agents.json` 已正確填入已發佈的 Agent：

```json
{
  "agents": [
    {
      "id": "sharepoint",
      "label": "SharePoint AI Search",
      "project_endpoint": "https://<resource>.services.ai.azure.com/api/projects/<project-id>",
      "agent_name": "SharePoint",
      "version": "2025-01-15",
      "enabled": true
    }
  ]
}
```

---

## 3. 準備 Service Principal 私鑰

### 3.1 安全放置 `.pfx` 檔案

從開發機將 `abs-agent-prod.pfx` 安全複製到部署主機：

```bash
# 主機上建立憑證目錄
mkdir -p /opt/abs-agent/certs
chmod 700 /opt/abs-agent/certs

# 從開發機複製（使用 scp 或安全傳輸方式）
scp /local/path/abs-agent-prod.pfx user@production-host:/opt/abs-agent/certs/

# 調整檔案權限
chmod 600 /opt/abs-agent/certs/abs-agent-prod.pfx
```

**安全注意事項：**

- ❌ **不可**將 `.pfx` 打進 Docker image
- ❌ **不可**提交 `.pfx` 至 Git
- ✅ **只能**透過主機 bind mount 傳遞給容器
- ✅ 複製完成後立即刪除開發機上的臨時 `.pfx` 副本

---

## 4. 建立 Dockerfile

在專案根目錄建立 `Dockerfile`：

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# 安裝依賴
COPY chatbot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製應用程式
COPY chatbot/ ./chatbot/

# 建立資料目錄
RUN mkdir -p /data

# 非 root 執行
RUN useradd -m -u 1000 appuser
USER appuser

# 健康檢查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import requests; requests.get('http://localhost:8080/api/health', timeout=5)"

# 啟動
CMD ["python", "-m", "uvicorn", "chatbot.app:app", "--host", "0.0.0.0", "--port", "8080"]
```

---

## 5. 建立 docker-compose.yml

在部署目錄（例如 `/opt/abs-agent/`）建立 `docker-compose.yml`：

```yaml
version: "3.9"

services:
  abs-agent:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: abs-agent
    ports:
      - "127.0.0.1:8080:8080" # 僅內部存取
    environment:
      # 動態載入 .env.prod 檔案
      - CHAT_PROVIDER=foundry_agent
      - CHAT_DB_PATH=/data/chat.db
    env_file:
      - .env.prod
    volumes:
      # SQLite 資料庫持久化
      - abs-agent-data:/data
      # Service Principal 憑證（唯讀掛載）
      - ./certs/abs-agent-prod.pfx:/cert/abs-agent.pfx:ro
      # 應用程式碼（若需要即時編輯）
      - ./chatbot/agents.json:/app/chatbot/agents.json:ro
    restart: unless-stopped
    networks:
      - internal
    logging:
      driver: "json-file"
      options:
        max-size: "50m"
        max-file: "10"

  # 選用：Nginx 反向代理（若主機已有 Nginx，可移除此服務）
  nginx:
    image: nginx:latest
    container_name: abs-agent-nginx
    ports:
      - "0.0.0.0:443:443"
      - "0.0.0.0:80:80"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - /etc/nginx/tls/abs-agent.crt:/etc/nginx/tls/abs-agent.crt:ro
      - /etc/nginx/tls/abs-agent.key:/etc/nginx/tls/abs-agent.key:ro
    depends_on:
      - abs-agent
    networks:
      - internal
    restart: unless-stopped

volumes:
  abs-agent-data:
    driver: local

networks:
  internal:
    driver: bridge
```

**注意：** 若主機已安裝獨立 Nginx，可省略 `nginx` 服務，改為外部 Nginx 代理。

---

## 6. Nginx 反向代理配置

如果使用容器內的 Nginx（上述 `docker-compose.yml` 包含），準備 `nginx.conf`：

```nginx
user nginx;
worker_processes auto;
error_log /var/log/nginx/error.log warn;
pid /var/run/nginx.pid;

events {
  worker_connections 1024;
}

http {
  include /etc/nginx/mime.types;
  default_type application/octet-stream;

  log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                  '$status $body_bytes_sent "$http_referer" '
                  '"$http_user_agent" "$http_x_forwarded_for"';

  access_log /var/log/nginx/access.log main;

  sendfile on;
  tcp_nopush on;
  tcp_nodelay on;
  keepalive_timeout 65;
  types_hash_max_size 2048;
  client_max_body_size 1m;

  server {
    listen 80;
    server_name _;
    # 重新導向 HTTPS
    return 301 https://$host$request_uri;
  }

  server {
    listen 443 ssl http2;
    server_name <internal-hostname>;

    ssl_certificate     /etc/nginx/tls/abs-agent.crt;
    ssl_certificate_key /etc/nginx/tls/abs-agent.key;
    ssl_protocols TLSv1.2 TLSv1.3;

    location / {
      proxy_pass http://abs-agent:8080;
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_read_timeout 300s;
      proxy_buffering off;  # SSE 直播需要
    }
  }
}
```

**或，若主機上已有獨立 Nginx，改在主機 Nginx 設定代理：**

```nginx
server {
    listen 443 ssl http2;
    server_name <internal-hostname>;

    ssl_certificate     /etc/nginx/tls/abs-agent.crt;
    ssl_certificate_key /etc/nginx/tls/abs-agent.key;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_buffering off;
    }
}
```

測試 Nginx：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

---

## 7. 啟動容器

### 7.1 建立並啟動容器

```bash
cd /opt/abs-agent

# 構建 Docker image
docker compose build

# 啟動容器（後台執行）
docker compose up -d

# 檢查容器狀態
docker compose ps
docker compose logs -f abs-agent
```

### 7.2 驗證容器健康狀態

```bash
# 檢查容器日誌
docker compose logs abs-agent

# 測試 API 健康檢查
curl http://127.0.0.1:8080/api/health

# 測試 Agent 可用性
curl http://127.0.0.1:8080/api/agents
```

若看到 `{"ok": true}` 回應，表示容器正常運作。

---

## 8. 登入與初始設定

1. 在瀏覽器開啟 `https://<internal-hostname>`
2. 使用 `BOOTSTRAP_ADMIN_USERNAME` 與 `BOOTSTRAP_ADMIN_PASSWORD` 登入
3. 進入【管理】分頁建立額外使用者或設定 Agent 授權

---

## 9. 日常維運

### 9.1 檢查日誌

```bash
# 即時日誌
docker compose logs -f abs-agent

# 查看特定行數
docker compose logs -n 100 abs-agent

# 儲存日誌到檔案
docker compose logs abs-agent > logs.txt
```

### 9.2 更新程式碼

```bash
# 拉取最新程式碼
git pull

# 重建 image
docker compose build

# 重啟容器
docker compose restart abs-agent

# 驗證
docker compose logs -f abs-agent
```

### 9.3 更新 Agent 版本

1. 在 Foundry 發佈新版本
2. 編輯 `chatbot/agents.json`，更新 `version` 欄位
3. 重啟容器：`docker compose restart abs-agent`
4. 從 UI 建立新對話測試

### 9.4 備份 SQLite 資料庫

```bash
# 停止容器
docker compose stop abs-agent

# 備份資料庫
docker run --rm -v abs-agent-data:/data -v $(pwd):/backup \
  alpine cp /data/chat.db /backup/chat.db.$(date +%F-%H%M%S)

# 重啟容器
docker compose start abs-agent
```

### 9.5 恢復資料庫備份

```bash
# 停止容器
docker compose stop abs-agent

# 恢復備份
docker run --rm -v abs-agent-data:/data -v $(pwd):/backup \
  alpine cp /backup/chat.db.2025-01-15-120000 /data/chat.db

# 重啟容器
docker compose start abs-agent
```

---

## 10. 故障排除

| 現象                    | 檢查方式                                                                                                    |
| ----------------------- | ----------------------------------------------------------------------------------------------------------- |
| 容器無法啟動            | `docker compose logs abs-agent` 查看錯誤訊息；檢查 `.env.prod`、`agents.json`、憑證路徑                     |
| `401 Unauthorized` 錯誤 | 檢查 `.pfx` 密碼、`AZURE_CLIENT_CERTIFICATE_PASSWORD`、憑證是否已上傳 Azure                                 |
| `403 Forbidden` 錯誤    | 確認 Service Principal 在 Foundry Project 具有 `Azure AI User` 或更高角色；等待 RBAC 傳播（通常 5-10 分鐘） |
| Agent 找不到或版本錯誤  | 檢查 `agents.json` 的 endpoint、agent_name、version 是否正確發佈                                            |
| SQLite locked 錯誤      | 確保只有一個應用程式實例在執行；若需多實例，改用共用資料庫                                                  |
| Nginx 連線拒絕          | 檢查防火牆規則、TLS 憑證有效期、主機 DNS 解析                                                               |

### 10.1 深度診斷

```bash
# 進入容器終端
docker compose exec abs-agent bash

# 在容器內測試 Service Principal
python -c "from azure.identity import DefaultAzureCredential; \
  c = DefaultAzureCredential(); \
  token = c.get_token('https://ai.azure.com/.default'); \
  print('Token expires:', token.expires_on)"

# 檢查環境變數
env | grep AZURE

# 檢查檔案掛載
ls -la /cert/
ls -la /data/
```

---

## 11. 不再適用的舊版項目

請勿依照舊文件啟動下列元件：

- ❌ Qdrant 向量資料庫
- ❌ RAG API 服務（`/query` 端點）
- ❌ MCP server
- ❌ `FOUNDRY_API_KEY` 舊版設定

目前版本直接呼叫已發佈的 Microsoft Foundry Agent，無需向量搜尋或 RAG 管道。
