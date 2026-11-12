# PACT — Policy and Authorization Control Tower

A financial governance runtime for autonomous AI agents. PACT provides deterministic, centralized and auditable control over financial actions initiated by AI agents.

```
AI Agent → MCP → PACT Policy Engine → Shared Budget → Human Approval → Restricted Grant → PayPal Sandbox → Audit
```

> Status: **in development** for the PayPal AI Hackathon. See the [project specification](specification/PROJECT_SPEC.md) and the GitHub issues for the implementation plan.

## Quick start (development)

```bash
uv sync
cp .env.example .env   # then fill in PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET (Sandbox)
```

## Branching

- `main`: released, demo-ready code
- `dev`: integration branch
- `feat/*`: feature branches, merged into `dev` through pull requests

## License

MIT
