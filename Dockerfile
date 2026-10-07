# BotBowl Bench - LLM benchmark server (web UI + tournament scheduler).
# Built for Railway, but runs anywhere:  docker build -t botbowlbench . && docker run -p 8080:8080 -e OPENROUTER_API_KEY=... botbowlbench
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DATA_DIR=/data \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-bench.txt .
RUN pip install --no-cache-dir -r requirements-bench.txt

COPY . .
# compile the C++ pathfinding (falls back to pure python if this ever fails)
RUN cythonize -i -3 botbowl/core/pathfinding/cython_pathfinding.pyx || echo "cython build failed - using python pathfinding"

RUN mkdir -p /data
EXPOSE 8080
CMD ["python", "-m", "bench", "serve"]
