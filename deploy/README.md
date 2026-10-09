# Deploying the hosted API + MCP

Target: the secondlandings droplet that already runs inkcheck behind Caddy.
The service is a second compose project that joins inkcheck's `backend`
network; Caddy gets one more site block.

## One-time setup (droplet owner)

1. **DNS** — add an A record `permits-api.secondlandings.ai → 64.23.172.250` at Porkbun.
   `secondlandings.ai` has a wildcard pointing at Porkbun parking (207.207.210.x),
   so the explicit record is required; verify with `dig +short permits-api.secondlandings.ai`.
2. **Checkout** on the droplet:
   ```bash
   git clone https://github.com/chaoz23/king-county-permit-status /opt/king-county-permit-status
   ```
3. **Caddy** — inkcheck's Caddy mounts `<inkcheck checkout>/deploy/Caddyfile`
   read-only, so append our site block there and reload:
   ```bash
   cat /opt/king-county-permit-status/deploy/Caddyfile >> /opt/inkcheck/deploy/Caddyfile
   cd /opt/inkcheck && docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
   ```
   (`/opt/inkcheck` is inkcheck's default `APP_DIR`; adjust if it lives elsewhere.)
   The `permits` upstream resolves because both compose projects share the
   `inkcheck_backend` network (override with `BACKEND_NETWORK=` if yours differs);
   our container also joins its own `egress` network for outbound portal traffic,
   since `inkcheck_backend` is internal.
4. **Start**:
   ```bash
   cd /opt/king-county-permit-status && docker compose up -d --build
   curl -s https://permits-api.secondlandings.ai/healthz
   ```
5. **Redeploys from GitHub** — reuse inkcheck's deploy key. In this repo's
   Settings → Secrets add `DROPLET_SSH_KEY`, `DROPLET_HOST`, `DROPLET_USER`
   (and the variable `APP_DIR=/opt/king-county-permit-status`). Then
   `.github/workflows/deploy.yml` redeploys on manual dispatch or a Release.

## After it's live

- Smoke: `curl 'https://permits-api.secondlandings.ai/api/lookup?q=1817+Morris+Ave+S,+Renton+WA'`
- MCP: point any Streamable-HTTP client at `https://permits-api.secondlandings.ai/mcp`
  (no auth). `server.json` is the MCP Registry manifest — publish with
  `mcp-publisher` once the URL answers; list on Glama / Docker MCP registry the same way.
- Rate limits are per client IP (30/min) and global (300/min); tune via env in `compose.yaml`.
