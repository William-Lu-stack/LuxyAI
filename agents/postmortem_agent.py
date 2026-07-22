import uuid
from fastapi import FastAPI

from a2a.protocol import A2AMessage, A2AResponse

app = FastAPI(title="Post-Mortem Agent")


@app.get("/")
async def root():
    return {"status": "ok", "component": "postmortem-agent", "health": "/health"}


@app.get("/health")
async def health():
    return {"status": "ok", "component": "postmortem-agent"}


@app.post("/a2a/tasks")
async def handle_task(message: A2AMessage):
    payload = message.payload

    incident = payload.get("incident", {})
    diagnosis = payload.get("diagnosis", {})
    remediation = payload.get("remediation", {})

    report = f"""
# Post-Mortem Report

## Incident
- ID: {incident.get("incident_id")}
- Title: {incident.get("title")}
- Severity: {incident.get("severity")}
- Namespace: {incident.get("namespace")}
- Service: {incident.get("service")}

## Summary
{incident.get("summary")}

## Root Cause
{diagnosis.get("root_cause", "Unknown")}

## Impact
{diagnosis.get("impact", "Unknown")}

## Timeline
- Alert received
- SRE Agent started investigation
- Diagnosis completed
- Healing action proposed or executed
- Incident updated

## Remediation
{remediation}

## Follow-up Actions
1. Add more precise alerting rules.
2. Add resource requests and limits if missing.
3. Improve readiness and liveness probes.
4. Review deployment rollout policy.
5. Add runbook for similar incidents.
"""

    return A2AResponse(
        id=str(uuid.uuid4()),
        source_agent="postmortem-agent",
        target_agent=message.source_agent,
        status="completed",
        result={
            "format": "markdown",
            "report": report,
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8103)
