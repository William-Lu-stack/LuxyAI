import uuid
from datetime import datetime

from fastapi import FastAPI

from a2a.protocol import A2AMessage, A2AResponse

app = FastAPI(title="Incident Management Agent")


@app.get("/health")
async def health():
    return {"status": "ok", "component": "incident-agent"}


INCIDENTS = {}


@app.post("/a2a/tasks")
async def handle_task(message: A2AMessage):
    payload = message.payload

    incident_id = payload.get("incident_id") or f"INC-{uuid.uuid4().hex[:8]}"

    INCIDENTS[incident_id] = {
        "incident_id": incident_id,
        "title": payload.get("title", "Kubernetes Incident"),
        "severity": payload.get("severity", "P2"),
        "namespace": payload.get("namespace"),
        "service": payload.get("service"),
        "summary": payload.get("summary"),
        "status": payload.get("status", "open"),
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }

    return A2AResponse(
        id=str(uuid.uuid4()),
        source_agent="incident-agent",
        target_agent=message.source_agent,
        status="completed",
        result=INCIDENTS[incident_id],
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8102)
