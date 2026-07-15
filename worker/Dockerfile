FROM python:3.12-slim

WORKDIR /app

# System deps: none extra beyond what pip needs.
RUN pip install --no-cache-dir --upgrade pip

# TradingAgents package (installed editable-like via COPY).
COPY pyproject.toml README.md ./
COPY tradingagents ./tradingagents
COPY web ./web
COPY worker ./worker

RUN pip install --no-cache-dir . && \
    pip install --no-cache-dir -r worker/requirements.txt

# Environment overrides pass via docker run -e / compose env_file.
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "worker.daemon"]
