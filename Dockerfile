FROM python:3.11-slim

WORKDIR /app

# 安裝依賴
COPY chatbot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製應用程式
COPY chatbot/ ./chatbot/

# 建立資料目錄（SQLite 會在此寫入）
RUN mkdir -p /data && chmod 777 /data

# 非 root 執行
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# 健康檢查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import requests; requests.get('http://localhost:8080/api/health', timeout=5)" || exit 1

# 啟動應用程式
CMD ["python", "-m", "uvicorn", "chatbot.app:app", "--host", "0.0.0.0", "--port", "8080"]
