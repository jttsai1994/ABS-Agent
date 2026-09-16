# RAG API 測試指南

本文件說明如何測試 VM 上部署的 RAG API，並提供一組 20 題的測試資料集，作為評斷檢索與回答品質的依據。

---

## 一、測試前置

確保下列服務已部署並運作正常（部署步驟見 [docs/DEPLOY.md](DEPLOY.md)）：

```bash
curl http://localhost:8000/health
# 預期回應：
# {"status":"ok","collection":"fit_docs","points_count":<>0,"bm25_ready":true}
```

`points_count` 必須 > 0 且 `bm25_ready` 為 true，否則先執行 `python ingest.py`。

---

## 二、測試方式概述

| 測試類型        | 工具                                        | 說明                          |
| --------------- | ------------------------------------------- | ----------------------------- |
| 1. 手動煙霧測試 | `curl` / Postman                            | 確認 API 可正常回應           |
| 2. 自動化評測   | [tests/run_tests.py](../tests/run_tests.py) | 用測試資料集量化評分          |
| 3. 延遲量測     | `run_tests.py` 結果                         | 每題 latency_ms，觀察平均/p95 |

---

## 三、手動煙霧測試

### 健康檢查

```bash
curl http://localhost:8000/health
```

### 中文問題

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{
    "question": "FIT Cable BU 在 2022 上半年的 Recovery Plan 內容？",
    "top_k": 5
  }'
```

### 英文問題

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What is FIT'\''s strategy regarding Belkin Connected Home?",
    "top_k": 5
  }'
```

### Metadata 過濾（按分類）

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{
    "question": "PSS 良率改善進度",
    "top_k": 5,
    "category_filter": "COVID-19"
  }'
```

### 啟用 API Key 時

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your-api-key>" \
  -d '{"question":"...","top_k":5}'
```

### PowerShell 版本

```powershell
Invoke-RestMethod -Uri http://localhost:8000/query -Method POST `
  -ContentType "application/json" `
  -Body (@{
    question = "FIT Cable BU 2022 Recovery Plan"
    top_k    = 5
  } | ConvertTo-Json)
```

---

## 四、自動化評測（推薦）

### 4.1 執行步驟

```bash
# 確保已啟用虛擬環境並安裝依賴
source rag/.venv/bin/activate    # Windows: rag\.venv\Scripts\Activate.ps1
pip install requests

# 執行測試（VM 本機）
python tests/run_tests.py --base-url http://localhost:8000

# 從遠端機器測試 VM
python tests/run_tests.py --base-url http://<vm-ip>:8000

# 若 API 啟用 X-API-Key
python tests/run_tests.py --base-url http://localhost:8000 --api-key <key>
```

### 4.2 輸出範例

```
[health] {'status': 'ok', 'points_count': 11607, 'bm25_ready': True}
[# 1] PASS  (412ms, 5 sources)  Foxconn 與 Homodeus 在 COVID-19 ...
[# 2] PASS  (388ms, 5 sources)  PSS 專案 BK Ramp Up 的產能目標進度 ...
[# 3] FAIL  (450ms, 5 sources)  FIT 在 USB Power Delivery EPR ...
        reason: matched 1/5 topics (need >= 3)
...
Summary: {'total': 20, 'passed': 17, 'failed': 3, 'pass_rate': 0.85}
Saved details to tests/results.json
```

詳細結果寫入 [tests/results.json](../tests/results.json)，可用於後續分析。

---

## 五、測試資料集設計

題庫位於 [tests/testset.json](../tests/testset.json)，共 **20 題**，分為 5 種類型：

| 類型                                    | 題數 | 評分標準                                        |
| --------------------------------------- | ---- | ----------------------------------------------- |
| `single_doc_lookup`                     | 10   | 答案/來源中需命中 ≥ 3 個關鍵字（或題目指定數）  |
| `topic_summary`                         | 4    | 跨多份文件主題彙整，命中 ≥ 2 個關鍵字           |
| `filter_by_time` / `filter_by_category` | 2    | 驗證 metadata 過濾與時間範圍判斷                |
| `negative_test`                         | 2    | **預期模型應拒答**，若回答內容則視為幻覺 = FAIL |
| `language_test`                         | 2    | 英文問題，驗證跨語言檢索與回答                  |

### 題目欄位說明

```json
{
  "id": 1,
  "category": "single_doc_lookup",
  "question": "問題文字",
  "expected_topics": ["關鍵字1", "關鍵字2"],
  "expected_doc_category_contains": "FIT > Lab",
  "min_recall_keywords": 3,
  "expected_no_answer": false,
  "notes": "說明備註"
}
```

| 欄位                             | 用途                                            |
| -------------------------------- | ----------------------------------------------- |
| `expected_topics`                | 答案或來源 metadata 應出現的關鍵字（小寫比對）  |
| `expected_doc_category_contains` | 期望命中文件的 category 包含此字串（可為 null） |
| `min_recall_keywords`            | 至少需命中幾個 `expected_topics` 才算 PASS      |
| `expected_no_answer`             | 設為 true 時，反過來：模型必須拒答才 PASS       |

---

## 六、評分指標

### 6.1 主要指標

- **Pass Rate**：整體通過率，**目標 ≥ 80%**
- **Negative Test Pass Rate**：拒答測試通過率，**目標 100%**（避免幻覺）
- **平均延遲**：建議 < 1500ms（含 Foundry embedding + LLM 呼叫）
- **p95 延遲**：建議 < 3000ms

### 6.2 進階分析

針對失敗題目應檢查：

1. **Sources 是否相關？** 若相關但 LLM 沒用上，是 prompt 問題
2. **Sources 完全錯誤？** 是檢索（embedding / BM25 / RRF）的問題
3. **回答出現幻覺？** 加強 system prompt 中的「無資料就拒答」要求

### 6.3 比較不同策略

可透過修改 `rag/.env` 比較：

- `VECTOR_TOP_K` / `BM25_TOP_K` 對召回率的影響
- `FINAL_TOP_K`（送進 LLM 的數量）對回答品質的影響
- `CHUNK_TOKENS`（重新 ingest 後）對長文回答的影響

---

## 七、新增 / 修改測試題

直接編輯 [tests/testset.json](../tests/testset.json) 即可，無需改 `run_tests.py`。建議：

- 測試題應與資料庫實際內容對應（不確定時可先手動 `curl` 確認）
- 機敏內容（人名、客戶機密細節）只放在 `notes` 不放在 `expected_topics`，避免誤判
- 保留 2~3 題 `negative_test`，這是檢測幻覺最有效的方式

---

## 八、CI / 排程測試（建議）

部署後可設定排程，每日跑一次測試確保系統穩定：

**Linux crontab：**

```cron
0 3 * * * cd /path/to/AI-SUPERNOTES && /path/to/.venv/bin/python tests/run_tests.py --base-url http://localhost:8000 >> /var/log/rag-test.log 2>&1
```

**Windows 工作排程器：** 設定每日執行 `run_tests.py`，並監控 exit code（非 0 代表有失敗）。
