# CM TECHMAP Local Install Agent (Host Machine + Vercel Frontend)

This package now works as an install agent that prepares and runs the full backend stack on the target host machine, then exposes it for Vercel frontend integration.

Core idea:

- Host machine runs one all-in-one backend container (`cm-techmap-all-in-one`).
- Optional gateway + Cloudflare Tunnel profile gives a stable HTTPS public URL.
- Frontend on Vercel uses rewrites to that stable URL.
- Install agent automates env bootstrap, startup, health checks, and rewrite patching.
- Install agent also auto-resolves free host ports when defaults are occupied.

Stack inside the all-in-one runtime:

- FastAPI API (port 8000)
- PostgreSQL + PostGIS (internal)
- Redis (internal)
- MinIO S3 + Console (9000/9001)
- Keycloak (8080)
- Celery worker + beat
- Flower (5555)

Optional sidecars (enabled with `--with-tunnel`):

- Caddy gateway (`cm-techmap-gateway`, port 8088 on host)
- Cloudflared tunnel (`cm-techmap-cloudflared`)
- Self-heal daemon (`tunnel-self-heal.sh`, host-side process)

## Fastest Path (recommended)

### Zero-Touch On A New Machine (single command)

If you do not want to copy the project manually, run this from the new Linux machine:

```bash
curl -fsSL https://raw.githubusercontent.com/firewaredigital/cm-techmap-frontend/main/applications/deploy/all-in-one/zero-touch-bootstrap.sh | bash -s -- \
	--repo-url https://github.com/firewaredigital/cm-techmap-frontend.git \
	--branch main \
	--frontend-url https://YOUR_FRONTEND.vercel.app
```

This bootstrap script:

- installs required packages (`curl`, `git`, Docker + compose plugin),
- clones/updates repository into `$HOME/cmtechmap-workspace`,
- creates `.env.single`, generates secrets for placeholders,
- runs `install-agent.sh` automatically.

For fixed Cloudflare hostname mode:

```bash
curl -fsSL https://raw.githubusercontent.com/firewaredigital/cm-techmap-frontend/main/applications/deploy/all-in-one/zero-touch-bootstrap.sh | bash -s -- \
	--repo-url https://github.com/firewaredigital/cm-techmap-frontend.git \
	--branch main \
	--frontend-url https://YOUR_FRONTEND.vercel.app \
	--tunnel-token YOUR_CLOUDFLARE_TUNNEL_TOKEN \
	--tunnel-hostname api.your-domain.com
```

Notes:

- default behavior is `--with-tunnel` + quick tunnel;
- use `--without-tunnel` if you only need local/network access;
- if the target machine already has an existing checkout with local changes, use another `--workspace-dir`.

1. Configure environment:

```bash
cd applications/deploy/all-in-one
cp .env.single.example .env.single
# edit secrets (PUBLIC_BACKEND_URL is auto-detected by install-agent)
```

2. Start local stack in one command:

```bash
./install-agent.sh --frontend-url https://YOUR_FRONTEND.vercel.app
```

3. If you want automatic public HTTPS without opening ports directly, set tunnel token and run:

```bash
./install-agent.sh --with-tunnel --frontend-url https://YOUR_FRONTEND.vercel.app
```

By default in tunnel mode, self-heal is enabled automatically.

4. Self-heal is enabled by default, but you can still force it explicitly:

```bash
./install-agent.sh --with-tunnel --enable-self-heal --frontend-url https://YOUR_FRONTEND.vercel.app
```

To disable self-heal for one run:

```bash
./install-agent.sh --with-tunnel --disable-self-heal --frontend-url https://YOUR_FRONTEND.vercel.app
```

4. Commit `applications/cm-techmap-frontend/vercel.json` and redeploy frontend.

`quick-deploy.sh` is now a thin wrapper for `install-agent.sh`.

## Install Agent Behavior (end-to-end)

`install-agent.sh` executes the full flow:

1. Preflight:
- Requires `docker`, `docker compose`, `curl`.
- Ensures `.env.single` exists (creates from template when missing).

2. Host port auto-allocation:
- Checks preferred ports (`8000`, `8080`, `9000`, `9001`, `5555`, `8088`).
- If a port is occupied (for example `8080`), auto-selects next free port.
- Persists selected values in `.env.single` (`HOST_*_PORT`).

3. Environment hydration:
- Resolves `PUBLIC_BACKEND_URL` automatically with priority:
	1) `--public-backend-url` (if provided)
	2) existing `PUBLIC_BACKEND_URL` in `.env.single`
	3) in tunnel mode: `CLOUDFLARE_TUNNEL_PUBLIC_URL`
	4) in tunnel mode: `CLOUDFLARE_TUNNEL_HOSTNAME`
	5) in tunnel mode: auto-discovered `https://*.trycloudflare.com` from cloudflared logs
	6) non-tunnel mode: `http://<public-ip>:8000`
	7) non-tunnel mode: `http://<local-ip>:8000`
	8) final fallback: `http://127.0.0.1:8000`
- Updates `KEYCLOAK_EXTERNAL_URL` based on resolved public URL.
- Accepts `--frontend-url` and updates `APP_CORS_ORIGINS`.

4. Runtime orchestration:
- Starts all-in-one backend via Docker Compose.
- If `--with-tunnel`, starts gateway and cloudflared profile too.

5. Post-start validation:
- Executes smoke checks for API/ready/metrics, Keycloak and MinIO.

6. Frontend auto-connect prep:
- Patches `applications/cm-techmap-frontend/vercel.json` rewrite origin from placeholder to `PUBLIC_BACKEND_URL`.

7. Optional self-healing runtime:
- Starts a host-side daemon that continuously validates tunnel URL and public health.
- If URL drifts, daemon updates `.env.single`, `KEYCLOAK_EXTERNAL_URL`, and frontend rewrites.
- If repeated health failures happen, daemon restarts `cm-techmap-cloudflared` and `cm-techmap-gateway`.

## Tunnel Mode (recommended for local host + Vercel)

Vercel cannot call localhost directly. For automatic connection while backend runs on a local/edge machine, use tunnel mode:

Mode A: token tunnel with fixed hostname (best stability)

1. Create Cloudflare Tunnel and route DNS (example: `cmtechmap-api.example.com`).
2. Set `CLOUDFLARE_TUNNEL_TOKEN` in `.env.single`.
3. Set either:
	- `CLOUDFLARE_TUNNEL_PUBLIC_URL=https://cmtechmap-api.example.com`, or
	- `CLOUDFLARE_TUNNEL_HOSTNAME=cmtechmap-api.example.com`.
4. Run `./install-agent.sh --with-tunnel`.

Mode B: quick tunnel auto-discovery (zero fixed DNS)

1. Set `CLOUDFLARE_TUNNEL_QUICK=true` (default in template).
2. Run `./install-agent.sh --with-tunnel`.
3. Agent reads cloudflared logs, discovers `https://<random>.trycloudflare.com`, persists it as `PUBLIC_BACKEND_URL`, then patches frontend rewrites.

Notes:

- quick tunnel URL changes between runs/restarts and is not suitable for long-lived production URLs.
- fixed hostname with token tunnel is strongly recommended for repeatability.

## Self-Healing Mode (continuous correction)

Scripts:

- `applications/deploy/all-in-one/tunnel-self-heal.sh`
- `applications/deploy/all-in-one/tunnel-self-heal-stop.sh`

What self-heal does each cycle:

1. Reads tunnel URL from fixed vars or cloudflared logs.
2. Compares against `PUBLIC_BACKEND_URL` in `.env.single`.
3. If changed:
	- updates `PUBLIC_BACKEND_URL`
	- updates `KEYCLOAK_EXTERNAL_URL`
	- patches frontend rewrites in `vercel.json`
	- optionally triggers Vercel redeploy command
4. Calls `${PUBLIC_BACKEND_URL}/api/v1/health`.
5. If failure threshold is reached, restarts tunnel components.

Manual control:

```bash
# Start daemon standalone
./tunnel-self-heal.sh --daemon --interval 60 --fail-threshold 3

# Run one cycle only (debug)
./tunnel-self-heal.sh --run-once

# Stop daemon
./tunnel-self-heal-stop.sh
```

Daemon state/log files:

- `applications/deploy/all-in-one/.state/tunnel-self-heal.pid`
- `applications/deploy/all-in-one/.state/tunnel-self-heal.state`
- `applications/deploy/all-in-one/.state/tunnel-self-heal.log`

## CLI Reference

```bash
./install-agent.sh [options]

Options:

- `--with-tunnel`: enables gateway + cloudflared profile.
- `--public-backend-url <url>`: forces URL used by frontend rewrites.
- `--public-backend-port <port>`: fallback auto-detection port (default 8000).
- `--tunnel-timeout <seconds>`: max wait for tunnel URL discovery and public health reachability.
- `--enable-self-heal`: starts tunnel self-heal daemon (requires `--with-tunnel`).
- `--disable-self-heal`: disables tunnel self-heal daemon even in tunnel mode.
- `--self-heal-interval <sec>`: daemon polling interval.
- `--self-heal-fail-threshold <n>`: failures before restart action.
- `--self-heal-auto-vercel-deploy`: redeploy frontend when URL changes.
- `--self-heal-vercel-command <cmd>`: custom redeploy command.
- `--frontend-url <url>`: CORS origin bootstrap.
- `--skip-frontend-patch`: keep `vercel.json` unchanged.
- `--skip-smoke`: skip smoke checks.
```

## Exposed Ports

All-in-one container publishes host-mapped dynamic ports:

- `${HOST_BACKEND_PORT}` -> backend API container `8000`
- `${HOST_KEYCLOAK_PORT}` -> keycloak container `8080`
- `${HOST_MINIO_API_PORT}` -> minio API container `9000`
- `${HOST_MINIO_CONSOLE_PORT}` -> minio console container `9001`
- `${HOST_FLOWER_PORT}` -> flower container `5555`

Tunnel profile publishes gateway on host `8088` and forwards:

- `${HOST_GATEWAY_PORT}` -> gateway container `8088`

- `/api/*`, `/metrics`, `/docs`, `/openapi.json` -> backend:8000
- `/auth/*`, `/realms/*` -> keycloak:8080

## Vercel Integration

Frontend rewrites in `applications/cm-techmap-frontend/vercel.json` must target `PUBLIC_BACKEND_URL`.

The install agent can patch it automatically when `PUBLIC_BACKEND_URL` is set.

Example targets:

- `/api/*` -> `https://<vm-host>/api/*`
- `/auth/*` -> `https://<vm-host>/auth/*`
- `/realms/*` -> `https://<vm-host>/realms/*`

Keep `VITE_API_URL` empty in production for same-origin proxy mode.

## Health Checks

- API liveness: `GET /api/v1/health`
- API readiness: `GET /api/v1/health/ready`
- Metrics: `GET /metrics`

## First-Boot Behavior (inside all-in-one)

Entrypoint performs idempotent bootstrap:

- Initializes PostgreSQL data directory if missing
- Ensures app DB and Keycloak DB exist
- Ensures PostGIS extension
- Ensures MinIO buckets
- Applies Alembic + SQL migrations (safe re-run)

## Operational Notes

- This path is optimized for fast validation and local-host deployment.
- For long-term production, split stateful services (DB/Redis/MinIO/Keycloak) from the app runtime.
- Keep Flower/MinIO console private or protected.
- In tunnel mode, self-heal defaults to enabled unless explicitly disabled.
