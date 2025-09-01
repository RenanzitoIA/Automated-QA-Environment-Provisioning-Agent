# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Install curl for ngrok download & git & docker-cli
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates docker.io \
    && rm -rf /var/lib/apt/lists/*

# Install ngrok
RUN curl -sSL https://bin.equinox.io/c/4VmDzA7iaHb/ngrok-stable-linux-amd64.tgz | tar -xz -C /usr/local/bin

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Default envs (can be overridden by .env file via docker-compose)
ENV PY_API_PORT=8088 \
    BASE_WORKDIR=/data/qa-envs

EXPOSE 8088
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8088"]
