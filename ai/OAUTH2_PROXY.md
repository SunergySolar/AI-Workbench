# oauth2-proxy — Google group gating in front of Open WebUI

Sits between Cloudflare and Open WebUI. Every request to `chat.zeoenergy.com` must pass a Google sign-in AND belong to a specific Google Workspace group before it can reach Open WebUI. Access is controlled from **Google Admin console → Directory → Groups** — add or remove a user from the group to grant or revoke.

## Traffic flow

```
Browser  →  Cloudflare (TLS)  →  cloudflared tunnel  →  oauth2-proxy:4180  →  openwebui:8080
                                                             ↓
                                                       Google OAuth
                                                             +
                                                    Google Directory API
                                                    (group membership check)
```

### Quick start

```bash
make up-oauth2-proxy
```

Or explicitly:

```bash
docker compose -f ai/docker-compose.oauth2-proxy.yml --env-file .env -p ai-oauth2-proxy up -d
```

| Container | Port | Purpose |
|---|---|---|
| `oauth2-proxy` | `4180` (internal only) | Google login + group membership check; proxies to `openwebui:8080` on the `ai_shared` network |

The container is **not published to the host**. Cloudflare's tunnel reaches it by Docker service name over the `ai_shared` network.

---

## Access control

- **Grant access:** add the user to `zeoai.access@zeoenergy.com` in Google Admin → Directory → Groups.
- **Revoke access:** remove them. The change propagates on the next cookie refresh (default: every 15 minutes; hard expiry at 1 hour). To shorten, edit `OAUTH2_PROXY_COOKIE_REFRESH` in the compose file.

Suspending the user's entire Google account in Admin console also blocks them (Google OAuth refuses to issue a token), but breaks all their Google access, not just Open WebUI.

---

## One-time Google Cloud setup

### 1. OAuth 2.0 client

The existing OAuth client used by Open WebUI is reused. It needs one additional authorized redirect URI:

```
https://chat.zeoenergy.com/oauth2/callback
```

Add it at: [Google Cloud Console → APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials) → click the OAuth 2.0 Client ID → **Authorized redirect URIs** → **+ Add URI** → paste → **Save**.

The existing OpenWebUI redirect URI (`.../oauth/google/callback`) stays; you are only appending.

### 2. Service account (for group membership lookups)

oauth2-proxy queries Google's Directory API to check whether the signed-in user is in the allowed group. That call is authenticated by a service account with **domain-wide delegation**.

1. Google Cloud Console → **IAM & Admin → Service Accounts** → **Create service account**.
   - Name: `oauth2-proxy-groups` (or similar).
   - Skip the "grant access to project" step.
2. On the created service account → **Keys** tab → **Add key → Create new key → JSON**. The JSON downloads to your machine.
3. Rename the file to `sa-key.json` and place it on the docker host at:
   ```
   ai/oauth2-proxy/sa-key.json
   ```
   The compose file mounts this path into the container. **Do not commit this file** — it is a credential.
4. On the same service account → **Details** tab → copy the **Unique ID** (a long numeric string). You need it for domain-wide delegation, below.

---

## One-time Google Admin console setup

Everything below requires super-admin on the Google Workspace tenant that contains `zeoenergy.com`. Both `gosunergy.com` and `zeoenergy.com` are on the same tenant, so `admin@gosunergy.com` is authoritative for both domains.

### 1. Domain-wide delegation

1. [Google Admin console](https://admin.google.com/) → **Security → Access and data control → API controls**.
2. Scroll to the bottom → **Manage Domain-wide Delegation**.
3. **Add new**:
   - **Client ID:** the numeric Unique ID from the service account.
   - **OAuth scopes:** `https://www.googleapis.com/auth/admin.directory.group.readonly`
4. **Authorize**.

This grants the service account read-only access to group membership across the Workspace, on behalf of the admin user it impersonates (see `OAUTH2_PROXY_GOOGLE_ADMIN_EMAIL` below).

### 2. Access group

1. Google Admin console → **Directory → Groups** → confirm `zeoai.access@zeoenergy.com` exists.
2. If creating fresh: **Access type** should be at least **Team** so the service account can enumerate members.
3. Add the users who should have access to Open WebUI.

---

## Configuration reference

Every value is sourced from the root `.env` — edit there, never in the compose file. The `PORT_OAUTH2_PROXY` slot in the port registry reserves `4180` even though it is not published to the host (cloudflared reaches oauth2-proxy over the `ai_shared` Docker network by service name).

| Compose env var | `.env` key | Default | Notes |
|---|---|---|---|
| `OAUTH2_PROXY_PROVIDER` | `OAUTH2_PROXY_PROVIDER` | `google` | OAuth provider — only google is exercised here |
| `OAUTH2_PROXY_HTTP_ADDRESS` | `OAUTH2_PROXY_HTTP_ADDRESS` | `0.0.0.0:4180` | Listener inside the container. Port must equal `PORT_OAUTH2_PROXY` |
| `OAUTH2_PROXY_REVERSE_PROXY` | `OAUTH2_PROXY_REVERSE_PROXY` | `true` | Trusts `X-Forwarded-*` headers from cloudflared |
| `OAUTH2_PROXY_PASS_USER_HEADERS` | `OAUTH2_PROXY_PASS_USER_HEADERS` | `true` | Forwards `X-Forwarded-Email` / `X-Forwarded-User` to Open WebUI |
| `OAUTH2_PROXY_SET_XAUTHREQUEST` | `OAUTH2_PROXY_SET_XAUTHREQUEST` | `true` | Also emits `X-Auth-Request-*` headers |
| `OAUTH2_PROXY_CLIENT_ID` | `OPENWEBUI_GOOGLE_CLIENT_ID` | _(reused)_ | Reused from Open WebUI's Google OAuth 2.0 client |
| `OAUTH2_PROXY_CLIENT_SECRET` | `OPENWEBUI_GOOGLE_CLIENT_SECRET` | _(reused)_ | Same client's secret |
| `OAUTH2_PROXY_COOKIE_SECRET` | `OAUTH2_PROXY_COOKIE_SECRET` | _(generate)_ | 32-byte URL-safe base64. Signs session cookies. See generator snippet in `.env.example` |
| `OAUTH2_PROXY_COOKIE_DOMAINS` | `OAUTH2_PROXY_COOKIE_DOMAINS` | `chat.zeoenergy.com` | Cookie is only sent to this hostname |
| `OAUTH2_PROXY_COOKIE_EXPIRE` | `OAUTH2_PROXY_COOKIE_EXPIRE` | `1h` | Hard session lifetime |
| `OAUTH2_PROXY_COOKIE_REFRESH` | `OAUTH2_PROXY_COOKIE_REFRESH` | `15m` | Re-check group membership at this interval within a session |
| `OAUTH2_PROXY_UPSTREAMS` | `OAUTH2_PROXY_UPSTREAMS` | `http://openwebui:8080` | Where to proxy authenticated requests. Docker service DNS, not localhost |
| `OAUTH2_PROXY_REDIRECT_URL` | `OAUTH2_PROXY_REDIRECT_URL` | `https://chat.zeoenergy.com/oauth2/callback` | Must match the URI added to the Google OAuth client |
| `OAUTH2_PROXY_WHITELIST_DOMAINS` | `OAUTH2_PROXY_WHITELIST_DOMAINS` | `chat.zeoenergy.com` | Allowed post-login redirect targets |
| `OAUTH2_PROXY_EMAIL_DOMAINS` | `OAUTH2_PROXY_EMAIL_DOMAINS` | `zeoenergy.com` | Rejects any sign-in outside this domain before the group check |
| `OAUTH2_PROXY_GOOGLE_GROUPS` | `OAUTH2_PROXY_GOOGLE_GROUPS` | `zeoai.access@zeoenergy.com` | User must belong to this group |
| `OAUTH2_PROXY_GOOGLE_ADMIN_EMAIL` | `OAUTH2_PROXY_GOOGLE_ADMIN_EMAIL` | `admin@gosunergy.com` | Workspace admin the service account impersonates to read group members |
| `OAUTH2_PROXY_GOOGLE_SERVICE_ACCOUNT_JSON` | `OAUTH2_PROXY_GOOGLE_SERVICE_ACCOUNT_JSON` | `/etc/oauth2-proxy/sa-key.json` | Path **inside the container**; host file is bind-mounted from `ai/oauth2-proxy/sa-key.json` |

---

## Cloudflare tunnel routing

The tunnel is token-based (`CLOUDFLARE_TUNNEL_TOKEN`), so its ingress rules are managed in the Cloudflare Zero Trust dashboard, not in a local config file. Until this step is done, traffic bypasses oauth2-proxy entirely — Cloudflare will keep routing straight to Open WebUI and no group check runs.

1. [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com/) → **Networks → Tunnels**.
2. Click the tunnel that routes `chat.zeoenergy.com`.
3. Click **Edit** → **Public Hostnames** tab.
4. Find the row for `chat.zeoenergy.com` → click **Edit** on that row.
5. Change the **Service** URL:
   - From: `http://openwebui:8080`
   - To: `http://oauth2-proxy:4180`
6. **Save**.

Traffic reroutes within ~30 seconds. Verify in a fresh incognito window: `chat.zeoenergy.com` should now show oauth2-proxy's Google sign-in flow first (not Open WebUI's login screen), and accounts outside `zeoai.access@zeoenergy.com` should be rejected before reaching Open WebUI.

---

## Double-login note

Open WebUI still has its own Google OAuth login enabled. After passing the oauth2-proxy gate at Cloudflare's edge, the user then sees Open WebUI's own Google login screen and signs in a second time with the same account. Same identity, one extra click.

If a single-sign-on experience is desired, Open WebUI can be switched to trusted-header auth (`WEBUI_AUTH_TRUSTED_EMAIL_HEADER=X-Forwarded-Email` etc.) so it accepts the identity that oauth2-proxy has already verified. That change is deliberately not made here — it removes Open WebUI's independent auth layer, and requires guaranteeing that Open WebUI is unreachable except through the proxy.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `oauth2-proxy` container exits immediately | `OAUTH2_PROXY_COOKIE_SECRET` is missing or not 32 bytes of URL-safe base64. Regenerate |
| Redirect to Google, then `redirect_uri_mismatch` error | The Google OAuth client is missing `https://chat.zeoenergy.com/oauth2/callback` in Authorized redirect URIs |
| Sign-in succeeds but 403 "You do not have access" from oauth2-proxy | The user is not in `zeoai.access@zeoenergy.com`, OR domain-wide delegation is misconfigured, OR the impersonated admin email doesn't exist on the tenant |
| oauth2-proxy logs `googleapi: Error 403: Not Authorized` | Domain-wide delegation missing the `admin.directory.group.readonly` scope, or admin email is not a Workspace admin |
| oauth2-proxy logs `unable to read service account key` | `sa-key.json` is missing from `ai/oauth2-proxy/` on the host, or the JSON is malformed |
| Users still hit Open WebUI directly, bypassing the proxy | Cloudflare tunnel is still routing `chat.zeoenergy.com` to `openwebui:8080` — update the tunnel's public hostname service URL |
| Revocation not taking effect | `OAUTH2_PROXY_COOKIE_REFRESH` interval hasn't elapsed. Reduce it, or have the user clear cookies for `chat.zeoenergy.com` |

### Useful commands

```bash
# Follow oauth2-proxy logs
make logs-oauth2-proxy

# Restart after config change
make clean-oauth2-proxy && make up-oauth2-proxy

# Verify the container can reach Open WebUI over the shared network
docker exec oauth2-proxy wget -qO- http://openwebui:8080/health
```

---

## What NOT to commit

- `ai/oauth2-proxy/sa-key.json` — service account private key
- `.env` values for `OAUTH2_PROXY_COOKIE_SECRET`, `OPENWEBUI_GOOGLE_CLIENT_SECRET`

Both should be in `.gitignore` already.
