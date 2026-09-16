# AI-SUPERNOTES RAG 系統部署指南

本文件分為兩個對象：

- **[Part A]** 給架構師：Docker 容器規格需求
- **[Part B]** 給開發者：程式碼部署、資料向量化、日常操作

---

## 架構說明

```
┌─────────────────────────── VM ───────────────────────────┐
│                                                           │
│   [由維運同事建立的 Docker 環境]                           │
│   ┌──────────────────┐   ┌──────────────────────────┐    │
│   │  容器: qdrant    │   │  容器: api (FastAPI)      │    │
│   │  port 6333/6334  │◄──│  port 8000               │    │
│   │  volume 持久化   │   │  POST /query             │    │
│   │                  │   │  GET  /health            │    │
│   └──────────────────┘   └──────────┬───────────────┘    │
│                                     │                     │
└─────────────────────────────────────┼─────────────────────┘
                                      │ HTTPS
                              Azure AI Foundry
                          (embedding + LLM endpoint)
```

- **qdrant**：向量資料庫，資料持久化在 VM 磁碟，容器重啟不遺失資料。
- **api**：FastAPI RAG 服務，收到問題後混合檢索，再呼叫 Foundry LLM 回答。
- 兩個容器需能互相連線（同一 Docker network），重啟策略設為 `unless-stopped`。

---

# Part A｜Docker 容器規格

> **給架構師** 請依照以下規格建立容器環境，完成後告知開發者容器的 IP/hostname 即可。

---

## A-1. VM 規格需求

| 項目 | 最低需求                             | 建議             |
| ---- | ------------------------------------ | ---------------- |
| OS   | Ubuntu 22.04 / RHEL 8+               | Ubuntu 22.04 LTS |
| CPU  | 2 vCPU                               | 4 vCPU           |
| RAM  | 4 GB                                 | 8 GB             |
| 磁碟 | 20 GB 可用空間                       | 50 GB            |
| 網路 | 可對外連線 `*.services.ai.azure.com` | —                |

---

## A-2. 容器一：Qdrant（向量資料庫）

| 項目           | 設定值                                                                 |
| -------------- | ---------------------------------------------------------------------- |
| Image          | `qdrant/qdrant:latest`                                                 |
| Container name | `qdrant`                                                               |
| Port mapping   | `6333:6333`（HTTP REST），`6334:6334`（gRPC）                          |
| Volume         | VM 本機路徑（例如 `/data/qdrant_storage`）掛載至容器 `/qdrant/storage` |
| Restart policy | `unless-stopped`                                                       |
| Network        | 與 `api` 容器同一個 internal network（例如 `rag-net`）                 |
| 環境變數       | 無需設定，使用預設值即可                                               |

**注意事項：**

- Port `6333`、`6334` **不需對外開放**，只供同 VM 上的 `api` 容器內部使用。
- Volume 路徑請確保有讀寫權限，且不會被定期清除。

---

## A-3. 容器二：api（FastAPI RAG 服務）

| 項目           | 設定值                                                     |
| -------------- | ---------------------------------------------------------- |
| 建置方式       | 從 repo 根目錄 `rag/Dockerfile` 建置，或由開發者提供 image |
| Container name | `api`                                                      |
| Port mapping   | `8000:8000`                                                |
| Restart policy | `unless-stopped`                                           |
| Network        | 與 `qdrant` 容器同一個 internal network                    |
| 依賴           | 需等 `qdrant` 容器健康後再啟動                             |

**必要環境變數（請向開發者取得實際值後填入）：**

| 環境變數           | 說明                                             | 範例                                                        | 位置       |
| ------------------ | ------------------------------------------------ | ----------------------------------------------------------- | ---------- |
| `FOUNDRY_API_KEY`  | Azure AI Foundry API Key（機敏，不得寫入程式碼） | `rGeQ...`                                                   | `rag/.env` |
| `FOUNDRY_BASE_URL` | Foundry endpoint                                 | `https://hub-supernote-dev.services.ai.azure.com/openai/v1` | `rag/.env` |

**其他環境變數（維運同事在 Docker 容器中設定，開發者無需修改）：**

| 環境變數          | 說明                                              | 範例                     | 預設位置         |
| ----------------- | ------------------------------------------------- | ------------------------ | ---------------- |
| `EMBEDDING_MODEL` | Embedding 模型名稱                                | `text-embedding-3-large` | rag/config.py    |
| `LLM_DEPLOYMENT`  | LLM 部署名稱                                      | `gpt-5.4-nano`           | rag/config.py    |
| `QDRANT_HOST`     | Qdrant 容器的 hostname（Docker network 內）       | `qdrant`                 | Docker env       |
| `QDRANT_PORT`     | Qdrant port                                       | `6333`                   | Docker env       |
| `COLLECTION_NAME` | Qdrant collection 名稱                            | `fit_docs`               | rag/config.py    |
| `JSONL_PATH`      | 來源 JSONL 路徑（容器內路徑，見下方 Volume 設定） | `/data/index.jsonl`      | Docker env       |
| `API_KEY`         | 保護 `/query` 端點的 API Key（留空 = 不驗證）     | 自訂強密碼或留空         | rag/.env（可選） |

**建議 Volume（供 ingest 時掛載來源資料）：**

| VM 路徑                           | 容器路徑 | 說明                                                   |
| --------------------------------- | -------- | ------------------------------------------------------ |
| `/data/fitwebparsing/output/rag/` | `/data/` | 爬蟲輸出資料夾，`ingest.py` 會讀取其中的 `index.jsonl` |

---

## A-4. 防火牆設定

| Port   | 方向       | 說明                           |
| ------ | ---------- | ------------------------------ |
| `8000` | 對內網開放 | FastAPI API，限內網或 VPN 存取 |
| `6333` | 僅 VM 內部 | Qdrant HTTP，**不對外**        |
| `6334` | 僅 VM 內部 | Qdrant gRPC，**不對外**        |

---

## A-5. 完成確認

維運同事完成後，請確認並回覆開發者以下資訊：

```
✅ qdrant 容器：running，port 6333/6334 可從 api 容器存取
✅ api 容器：running，port 8000 可從 VM 內網存取
✅ /data/ volume 已掛載，可讀寫
✅ 環境變數已設定
✅ VM IP 或 hostname：___________
```

---

# Part B｜給開發者：程式碼部署與日常操作

> **前提**：Part A 的 Docker 環境已由維運同事建立完成。

---

## B-1. 取得程式碼並設定環境變數

```bash
git clone <repo-url> AI-SUPERNOTES
cd AI-SUPERNOTES
```

建立 `rag/.env`（從範本複製）：

```bash
cp rag/.env.example rag/.env
```

編輯 `rag/.env`，填入以下必填值：

```bash
FOUNDRY_API_KEY=<向負責人取得>
FOUNDRY_BASE_URL=https://hub-supernote-dev.services.ai.azure.com/openai/v1
```

其餘參數（`QDRANT_HOST`、`JSONL_PATH` 等）已有預設值或由維運同事在 Docker 環境變數中設定，開發者無需修改。

---

## B-2. 確認容器正常 & 資料已就位

```bash
# 確認 api 容器服務正常（由維運同事操作，此處只驗證）
curl http://<vm-ip>:8000/health
# 預期：{"status":"ok","points_count":0,"bm25_ready":false}
# points_count=0 代表尚未 ingest，屬正常

# 確認 JSONL 資料存在
ls <維運同事掛載路徑>/index.jsonl
```

---

## B-3. 執行資料向量化（首次，或有新資料時）

```bash
# 建立 Python 虛擬環境
python3 -m venv rag/.venv
source rag/.venv/bin/activate      # Windows: rag\.venv\Scripts\Activate.ps1
pip install -r rag/requirements.txt

cd rag

# 先 dry-run 確認筆數
python ingest.py --dry-run

# 正式執行
python ingest.py
```

> **時間估計**：11,607 筆、批次 16，約 **30~60 分鐘**（視 Foundry 回應速度）。
> 中途 `Ctrl+C` 可中斷，重跑會自動跳過已處理文件。

完成後驗證：

```bash
curl http://<vm-ip>:8000/health
# 預期：{"status":"ok","points_count":<>0,"bm25_ready":true}
```

---

## B-4. 驗證查詢功能

```bash
curl -X POST http://<vm-ip>:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question": "FIT AI lab 有哪些專案？", "top_k": 5}'
```

自動化測試（完整 20 題評測）：

```bash
python tests/run_tests.py --base-url http://<vm-ip>:8000
```

詳見 [docs/TESTING.md](TESTING.md)。

---

## B-5. 更新程式碼後重建 api 容器

程式碼修改後，請通知維運同事重建 `api` 容器（或若有 CI/CD 流程，走 pipeline）。

> **注意**：本地開發時無需此步驟。只有維運同事在 VM 上需要執行。

**開發者提供給維運同事的指令：**

```bash
# 在 VM 上執行
cd ~/volume/AI-SUPERNOTES
git pull  # 更新程式碼

docker build -t rag-api ./rag
docker stop api && docker rm api
docker run -d --name api \
  --network rag-net \
  --restart unless-stopped \
  -p 8000:8000 \
  --env-file /path/to/rag.env \
  -v /data:/data \
  rag-api
```

---

## B-6. 增量更新資料（爬蟲有新資料後）

```bash
source rag/.venv/bin/activate
cd rag
python ingest.py          # 只處理新增文件
```

完成後通知維運同事重啟 `api` 容器（重建 BM25 index）：

```bash
docker restart api
```

---

## B-7. 常見問題排查

| 現象                                | 排查步驟                                               |
| ----------------------------------- | ------------------------------------------------------ |
| `/health` 無回應                    | 確認 `api` 容器 running；確認防火牆 port 8000 已開放   |
| `points_count: 0`                   | Ingest 未執行，重跑 `python ingest.py`                 |
| `/query` 回傳 503                   | api 容器剛啟動，BM25 index 建立中（等 30~60 秒後重試） |
| Ingest 失敗：`FOUNDRY_API_KEY` 無效 | 確認 `rag/.env` 的 key 正確，重跑 `python ingest.py`   |
| Ingest 失敗：無法連線 Qdrant        | 確認 `QDRANT_HOST` 設定正確，容器間 network 連通       |
| Ingest 中途中斷                     | 直接重跑，增量模式自動續跑                             |
