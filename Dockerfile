FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 POSTEDFRESH_DB=/data/postedfresh.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

VOLUME /data
EXPOSE 8000
# One worker: the poller runs inside the app process and pushes to its SSE clients.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
