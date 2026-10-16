---
description: "Deploy procedures for Vercel and Docker VPS. Loaded when user invokes /deploy or asks about deployment."
---

# Deploy

1. `git add` + `commit` + `push`
2. Platform:
   - **Vercel**: poll status until "Ready", curl verify URL
   - **Docker (VPS)**: SSH → `git pull && docker compose up -d --build` → `docker ps` + `logs --tail=20` + health check
3. Output: status + evidence (HTTP 200 / health OK / clean logs)
4. [CRITICAL] Deploy failed → error logs, do NOT say "completed"
5. Production availability: run expensive validation (model checks, data verification, warm-ups, migration rehearsals) against the candidate before cutover or behind blue/green/canary routing, once per candidate. Keep only exclusive steps inside any production-closing window, and report planned versus actual closed duration with the exclusive steps it contained. A window longer than its exclusive steps require is an operational defect to record and fix.
