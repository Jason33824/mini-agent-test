FROM python:3.12-slim

WORKDIR /app

COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The worker owns the brain: orchestrator + agents. It never touches HTTP.
COPY orchestrator.py ./
COPY agents/ ./agents/
COPY worker/ ./worker/

# DATABASE_URL / REDIS_URL / OPENAI_* arrive from the compose environment.
CMD ["arq", "worker.main.WorkerSettings"]
