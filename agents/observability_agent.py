import traceback

from fastapi import FastAPI, Request
from agents.sre_graph import build_graph

app = FastAPI(title="Observability Agent")

sre_graph = build_graph()


@app.get("/health")
async def health():
    return {"status": "ok", "component": "observability-agent"}


@app.post("/alertmanager/webhook")
async def alertmanager_webhook(request: Request):
    body = await request.json()
    auto_healing_enabled = bool(body.get("auto_healing_enabled", False))

    results = []

    for alert in body.get("alerts", []):
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        label_auto_healing = str(labels.get("auto_healing_enabled", "")).lower() == "true"

        sre_alert = {
            "alert_name": labels.get("alertname"),
            "namespace": labels.get("namespace", "default"),
            "deployment": labels.get("deployment"),
            "workload_name": labels.get("workload_name"),
            "workload_type": labels.get("workload_type", "Deployment"),
            "pod": labels.get("pod"),
            "service": labels.get("service"),
            "severity": labels.get("severity", "P2"),
            "priority": labels.get("priority", "high"),
            "summary": annotations.get("summary"),
            "description": annotations.get("description"),
            "auto_healing_enabled": auto_healing_enabled or label_auto_healing,
        }

        try:
            result = await sre_graph.ainvoke({"alert": sre_alert})
            results.append(
                {
                    "alert": sre_alert,
                    "status": "processed",
                    "result": result.get("final_answer"),
                    "raw": result,
                }
            )
        except Exception as e:
            results.append(
                {
                    "alert": sre_alert,
                    "status": "failed",
                    "error": f"{type(e).__name__}: {e}",
                    "trace": traceback.format_exc(limit=6),
                }
            )

    return {
        "status": "processed" if all(r["status"] == "processed" for r in results) else "partial_failed",
        "results": results,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8100)
