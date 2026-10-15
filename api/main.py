"""API entrypoint. W1 scaffold: health endpoint only; auth and task routes arrive in W2-W3."""
from fastapi import FastAPI

app = FastAPI(title="mini-agent-platform", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "api", "stage": "w1-scaffold"}
