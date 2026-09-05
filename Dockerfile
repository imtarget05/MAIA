# MAIA embedding worker image (PROJECT 2) - runs against a real Kafka broker.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
COPY requirements-kafka.txt .
RUN pip install --no-cache-dir -r requirements.txt -r requirements-kafka.txt \
    && rm -rf /root/.cache/pip

COPY src ./src
ENV PYTHONPATH=/app/src

# STREAM_TRANSPORT=kafka selects the confluent-kafka transport (see compose env).
CMD ["python", "-m", "maia.cli", "stream", "worker", "4"]