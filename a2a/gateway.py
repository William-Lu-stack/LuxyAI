import httpx
from typing import Optional

from a2a.protocol import A2AMessage, A2AResponse


class A2AGateway:
    """A2A Gateway: routes messages between agents."""

    def __init__(self):
        self._agent_registry: dict[str, str] = {
            "healing-agent": "http://localhost:8101/a2a/tasks",
            "incident-agent": "http://localhost:8102/a2a/tasks",
            "postmortem-agent": "http://localhost:8103/a2a/tasks",
        }

    def register_agent(self, name: str, endpoint: str):
        self._agent_registry[name] = endpoint

    def resolve_endpoint(self, target_agent: str) -> Optional[str]:
        return self._agent_registry.get(target_agent)

    async def send(self, message: A2AMessage) -> A2AResponse:
        endpoint = self.resolve_endpoint(message.target_agent)

        if endpoint is None:
            return A2AResponse(
                id="error",
                source_agent="a2a-gateway",
                target_agent=message.source_agent,
                status="failed",
                error=f"Unknown target agent: {message.target_agent}",
            )

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(endpoint, json=message.model_dump())
            resp.raise_for_status()
            data = resp.json()

        return A2AResponse(**data)


gateway = A2AGateway()
