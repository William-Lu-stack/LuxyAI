# Contributing

Thanks for helping improve `flawless`.

## Development Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cd frontend/modern
npm install
npm run build
cd ../..
```

## Before Opening a Pull Request

Run:

```bash
python -m compileall -q backend agents mcp_servers cmdb cloud a2a openwebui
python -m pytest tests
cd frontend/modern && npm run build
```

## Design Rules

- Keep execution paths auditable.
- Do not introduce browser-side shell or arbitrary command execution.
- Put credentials in environment variables, Kubernetes Secrets, or a secret manager.
- Keep custom scoring or company-specific algorithms outside the public repository.
- Add tests for any change that touches remediation, release gates, RBAC, or runtime state.

## Skills

Operational skills should be portable. A good skill contains symptoms, required evidence, allowed targets, allowed actions, recovery criteria, and rollback guidance.

## Security

If you find a vulnerability, do not open a public issue with exploit details. Follow `SECURITY.md`.
