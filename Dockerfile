FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install the package itself
COPY . .
RUN pip install --no-cache-dir -e .

# Railway injects $PORT at runtime
ENV TRADINGAGENTS_WEB_HOST=0.0.0.0

EXPOSE 8000

CMD ["python", "web_server.py"]
