FROM python:3.11-slim

# 日本語フォント (Noto Sans CJK JP) と curl のインストール
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-noto-cjk \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run 標準の PORT 環境変数に対応
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD exec uvicorn app:app --host 0.0.0.0 --port ${PORT}
