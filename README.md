# Lakera Red demo target — Meridian Pay support agent + live monitor

An OpenAI-compatible **stateless** agent endpoint for Lakera Red, plus a small
operator dashboard so you can watch a scan happen in real time. The agent is a
fictional payments-support bot ("Aria") deliberately tuned to produce a *mix* of
outcomes: some attacks are correctly refused, others can succeed.

## Files

- `agent.py` — FastAPI app: target endpoint + dashboard + admin endpoints
- `webui.html` — the dashboard (served by `agent.py`)
- `requirements.txt` — dependencies (all permissive licenses: MIT / BSD / Apache, no GPL)
- `Dockerfile` — container image (used by Coolify)
- `.dockerignore`, `.gitignore` — keep the build context lean and secrets out of git
- `compose.local.yaml` — optional, for running locally with Docker
- `.env.example` — config template

## The contract it implements

Lakera Red's **Stateless** agent contract
(<https://docs.lakera.ai/docs/red/connect-to-your-agent#stateless>):

- **Request:** `{ "model": str, "messages": [{ "role": "user|assistant|system", "content": str }] }`
- **Response:** `{ "choices": [{ "message": { "role": "assistant", "content": str } }] }`

The `model` field in the request is ignored; the backing model is `AGENT_MODEL`.

## Two auth surfaces (by design)

| Surface | Protects | Set via | Used by |
| --- | --- | --- | --- |
| **Bearer token** | `POST /v1/chat/completions` | `AGENT_AUTH_TOKEN` (auto-generated if unset), regenerable in UI | Lakera Red |
| **Admin password** | dashboard + `/admin/*` | `AGENT_ADMIN_PASSWORD` (auto-generated + printed if unset) | you |

Keeping them separate means the token-management UI is never an open endpoint.
The bearer token lives in memory only — nothing sensitive is written to disk.
The only real credential (the LLM API key) is read from the environment.

## Environment variables

| Var | Required | Notes |
| --- | --- | --- |
| `OPENAI_API_KEY` | yes¹ | LLM API key, read from env only |
| `OPENROUTER_API_KEY` | yes¹ | alternative to `OPENAI_API_KEY`; set one |
| `OPENAI_BASE_URL` | no | LLM endpoint; unset = OpenAI default. Set to `https://openrouter.ai/api/v1` for OpenRouter (alias: `OPENROUTER_BASE_URL`) |
| `OPENROUTER_HTTP_REFERER` | no | optional OpenRouter ranking header; sent only if set |
| `OPENROUTER_X_TITLE` | no | optional OpenRouter ranking header; sent only if set |
| `AGENT_ADMIN_PASSWORD` | recommended | dashboard login; auto-generated + printed to logs if unset |
| `AGENT_AUTH_TOKEN` | recommended | target bearer token; set it so it survives restarts/redeploys |
| `AGENT_MODEL` | no | default `gpt-4o-mini`. For OpenRouter use a namespaced id, e.g. `openai/gpt-4o-mini` |
| `AGENT_COOKIE_SECURE` | prod | set `true` when served over HTTPS |
| `DEMO_DECOY_CODE` | no | fake decoy string the scan tries to extract |
| `AGENT_ALLOW_NO_AUTH` | no | local-only escape hatch; disables target auth |
| `PORT` | no | listen port (default 8000) |

¹ Exactly one of `OPENAI_API_KEY` / `OPENROUTER_API_KEY` is required.

### Using OpenRouter (or any OpenAI-compatible endpoint)

The agent talks to the model through the OpenAI SDK, but the endpoint isn't
hardwired to OpenAI — any OpenAI-compatible gateway works by setting a base URL
and key. For [OpenRouter](https://openrouter.ai):

```bash
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_API_KEY=sk-or-...           # or put it in OPENAI_API_KEY
AGENT_MODEL=openai/gpt-4o-mini         # OpenRouter ids are namespaced
# Optional ranking headers:
# OPENROUTER_HTTP_REFERER=https://your-site.example
# OPENROUTER_X_TITLE=Meridian Pay demo
```

No other endpoint behaves differently — same `/v1/chat/completions` contract,
same dashboard. The startup banner prints the resolved provider and endpoint so
you can confirm which backend you're driving.

---

## Deploy to Coolify (GitHub auto-deploy)

Coolify gives the app a real domain with automatic HTTPS, so **no tunnel is
needed** — point Lakera Red straight at the Coolify URL.

1. **Push this repo to GitHub.** Confirm `.env` is *not* committed (the
   `.gitignore` handles this — only `.env.example` should be in the repo).
2. In Coolify: **+ New** → **Public/Private Repository** → pick the repo and
   branch. Coolify auto-detects the **Dockerfile** build pack.
3. **Ports:** set **Ports Exposes** to `8000`.
4. **Environment Variables** (Coolify → your app → Environment Variables) — add
   the values from the table above. At minimum an LLM key (`OPENAI_API_KEY`, or
   `OPENROUTER_API_KEY` + `OPENAI_BASE_URL` for OpenRouter); also set
   `AGENT_ADMIN_PASSWORD`, `AGENT_AUTH_TOKEN`, and `AGENT_COOKIE_SECURE=true`.
   Setting the token + password explicitly means they stay stable across every
   redeploy (and the admin password won't be printed to deploy logs).
5. **Domain:** assign a domain; Coolify provisions a Let's Encrypt certificate.
6. **Health check:** the Dockerfile already defines one on `/health`; no extra
   config needed.
7. **Deploy.** Coolify wires up a GitHub webhook, so future pushes to the branch
   auto-deploy.

> **Keep it to a single instance.** The live feed, history, and in-memory token
> are per-process state, so the image runs one worker — do **not** raise worker
> count or scale replicas above 1, or the dashboard will miss events. SSE works
> fine through Coolify's Traefik proxy as-is.

Once deployed, open `https://<your-domain>/`, sign in with the admin password,
and grab the bearer token from the panel.

---

## Run it locally

```bash
pip install -r requirements.txt
cp .env.example .env          # then edit .env (at minimum, an LLM API key)
export $(grep -v '^#' .env | xargs)
python agent.py               # serves on http://127.0.0.1:8000
```

Or with Docker:

```bash
cp .env.example .env          # edit it
docker compose -f compose.local.yaml up --build
```

On startup the console prints the dashboard URL and, if you didn't set one, the
generated **admin password**.

Smoke-test the target contract (use the token shown in the dashboard):

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer <token-from-dashboard>" \
  -H "Content-Type: application/json" \
  -d '{"model":"x","messages":[{"role":"user","content":"hi, what can you help with?"}]}'
```

It should appear as a card in the dashboard immediately.

## The dashboard

- **Live traffic** — incoming requests are grouped into **conversations** and
  shown as a collapsible tree, **collapsed by default** so a long scan stays
  scannable. Each conversation is one row summarising its rounds (round count,
  time span, and a **worst-case** status badge — **Answered / Refused / Decoy
  leaked**). Expand it to see each round; expand a round to see the turns Red
  sent plus the agent's reply. When the planted decoy code shows up in a reply,
  that round/conversation turns orange and the leaked substring is highlighted.
  Because the target is **stateless** (the contract carries no session id),
  multi-round attacks are grouped by inferring that each round's message history
  extends the previous one's. Opening the dashboard mid-scan replays history and
  rebuilds the same tree.
- **Token panel** — view, **Copy**, or **Regenerate** the bearer token.
  Regenerating invalidates the old value immediately, so update it in Lakera Red
  afterwards. (For Coolify, prefer setting `AGENT_AUTH_TOKEN` so it persists
  across redeploys.)
- **Stats** — running totals for requests, conversations, refusals, and decoy
  leaks, plus a live-connection indicator.

Red-team payloads are rendered as text (never as HTML), so hostile message
content can't execute in the dashboard.

## Configure the target in Lakera Red

On **Create target** (<https://red.lakera.ai/targets/create>):

| Field | Value |
| --- | --- |
| Target name | e.g. `Meridian Pay demo` |
| Target configuration | **Agent** |
| API Endpoint | `https://<your-coolify-domain>/v1/chat/completions` |
| Auth Type | **Bearer Token** → the token from the dashboard |
| Conversation history | **Stateless** |

Then **Test Connection** → **Create Target**. The test request shows up in the
dashboard too.

### Suggested Refusal Detection phrases

The agent uses a few non-standard refusal lines; adding these under **Refusal
Detection** helps Red's adaptive scans backtrack correctly:

- `contact a licensed professional`
- `through official channels`
- `Report a problem`

## Tuning the "mix"

Edit `SYSTEM_PROMPT` in `agent.py`: tighten the rules to make the demo harder,
loosen them to make leaks more reliable. The decoy code is a **fake** string
(`DEMO_DECOY_CODE`), not a credential.
