# apps/ — Product plane

This plane holds user-facing applications only. No direct imports from the engine.

Structure (target):
- frontend/: React/Vite app (/, /opt, /opt/slice)
- api/: REST/OpenAPI service (`/api/v1/*`)
- websocket/: WS gateway (market, manifold, signals, ledger, system, performance, trades)

Rules:
- Frontend consumes only `apps/api` and `apps/websocket` via HTTP/WS.
- Contracts (OpenAPI + WS schemas) are versioned and append-only.
- Each app has its own `.env.example` and README.
