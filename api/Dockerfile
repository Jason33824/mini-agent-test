FROM python:3.12-slim

WORKDIR /app

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api/ ./api/

EXPOSE 8000
# DATABASE_URL / REDIS_URL arrive from the compose environment at runtime;
# the engine is built in lifespan, so import stays side-effect free.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
