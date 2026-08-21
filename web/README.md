# aios web desktop

The interactive React/TypeScript control surface for the aios REST/WebSocket
control plane (Phase 4 — docs/10-ui.md §3, docs/11-roadmap.md §6).

## Panels

| Panel | Source | Notes |
|---|---|---|
| Processes | `GET /v1/agents` + WS feed | spawn (spec JSON), suspend / resume / kill, attach console |
| Scheduler | `GET /v1/scheduler` + WS feed | queue depths, utilization, dispatch/preempt, per-agent wait stats |
| Audit | `GET /v1/audit` + WS feed | hash-chain `verify`, live entries |
| Approvals | `GET /v1/approvals` | approve / deny tickets (operator role) |
| Files | `POST /v1/fs/search` | semantic artifact search for a chosen agent |
| LLM | `GET /v1/llm` | provider failover health |
| Console (drawer) | WS `/v1/agents/{pid}/ws/console` | live log tail + operator chat via mailbox |

## Auth

The desktop exchanges an operator API key for a short-lived JWT
(`POST /v1/auth/token`), which then authenticates every REST call
(`Authorization: Bearer`) and WebSocket handshake (`?token=`).

With no `AIOS_API_KEYS` set, `aios serve` enables the dev key `dev-key`
(operator role) — sign in with it in the browser.

### Single sign-on (OIDC + PKCE, Slice 5.3)

When the control plane is configured with `AIOS_OIDC_ISSUER` +
`AIOS_OIDC_CLIENT_ID`, the login card shows a **Sign in with SSO** button. The
desktop asks the control plane for the provider's authorization URL, the human
completes the IdP consent, and the control plane's callback sets a one-time
HttpOnly grant cookie (path-scoped to `/v1/auth/oidc`) before redirecting back
to the web shell. On mount the desktop auto-exchanges that grant for a normal
JWT exactly once per page load (`POST /v1/auth/oidc/session`).

Dev tip: with `npm run dev` the IdP must be configured with
`AIOS_OIDC_REDIRECT_URI=http://localhost:5173/v1/auth/oidc/callback` (the Vite
proxy does not rewrite `Host`).

## Development

```sh
npm install
npm run dev            # http://localhost:5173 — proxies /v1 to 127.0.0.1:8000
```

Start the control plane in another terminal:

```sh
aios serve --host 127.0.0.1 --port 8000
```

## Production build

```sh
npm run build          # emits web/dist
```

`aios serve` mounts `web/dist` automatically when present, so
http://127.0.0.1:8000/ serves the desktop same-origin with the API. The
desktop paths get a narrowed CSP (`default-src 'none'` stays on every API
route; the app shell gets `script-src 'self'` + `connect-src 'self' ws:`).

Set `AIOS_WEB_DIST` to override where `aios serve` looks for the build.