# Security Policy

`flawless` touches production operations workflows. Treat every integration and remediation path as security-sensitive.

## Supported Versions

Only the `main` branch is supported before the first stable release.

## Reporting a Vulnerability

Please report vulnerabilities privately to the project maintainer or the owning security team. Do not publish secrets, tokens, kubeconfigs, exploit payloads, or production screenshots in public issues.

## Security Boundaries

- Do not commit `.env`, kubeconfig files, model keys, OAuth secrets, Rancher tokens, Langfuse keys, or custom algorithms.
- The public repository includes a baseline algorithm module only. Private scoring modules should be supplied at runtime through `FLAWLESS_CUSTOM_ALGORITHM_PATH`.
- High-risk remediation must stay behind explicit operator confirmation and audit logging.
- Production deployments should use TLS, identity-aware ingress, network policy, and least-privilege RBAC.

## Recommended Production Controls

- Disable public NodePort exposure.
- Use an enterprise secret manager or Kubernetes Secrets.
- Keep `AUTO_HEALING_ENABLED=false` until RBAC, namespaces, action catalog, and rollback paths are approved.
- Scope write permissions to approved namespaces and resource types.
- Run image scanning and dependency auditing in CI.
