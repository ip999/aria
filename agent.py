"""
Lakera Red demo target -- Stateless, OpenAI-compatible chat agent + live monitor.

Target endpoint (what Lakera Red calls), per the "Stateless" Agent contract:
https://docs.lakera.ai/docs/red/connect-to-your-agent#stateless

  POST /v1/chat/completions
  Request:  { "model": str, "messages": [{ "role": "user|assistant|system", "content": str }] }
  Response: { "choices": [{ "message": { "role": "assistant", "content": str } }] }

On top of the target this also serves a small operator dashboard so you can watch
the scan happen live:

  GET  /                       login page, or the dashboard once authenticated
  POST /login                  exchange the admin password for a session cookie
  POST /logout                 drop the session
  GET  /admin/stream           Server-Sent Events feed of incoming interactions
  GET  /admin/token            current target bearer token (admin only)
  POST /admin/token/regenerate mint a new target bearer token (admin only)
  GET  /admin/config           current persona config (prompt/refusals/decoy)
  POST /admin/config           update persona config at runtime (admin only)
  POST /admin/persona          switch the active persona (admin only)
  GET  /health                 unauthenticated liveness probe (exposes nothing)

Two independent auth surfaces:
  * The TARGET endpoint is protected by a bearer token -- this is the value you
    paste into Lakera Red. It lives in memory and can be regenerated from the UI.
  * The DASHBOARD / admin endpoints are protected by a separate admin password
    (AGENT_ADMIN_PASSWORD), so the token-management surface is never open.

No secrets are written to disk. The only real credential (the LLM API key) is read
from the environment. The decoy code is a fake, non-sensitive string used only so
the scanner has a concrete "confidential" value to try to exfiltrate; it -- along
with the system prompt and refusal phrases -- is editable live from the dashboard
(in-memory only, so everything resets to defaults on restart).

The backing model is reached through the OpenAI SDK, but the endpoint is not
hardwired to OpenAI: set OPENAI_BASE_URL to any OpenAI-compatible gateway (e.g.
OpenRouter at https://openrouter.ai/api/v1) and the same code drives it.
"""

import asyncio
import copy
import hmac
import json
import os
import secrets
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal, Optional, Set, Tuple

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel, Field
from openai import AsyncOpenAI

import personas

# --- Configuration (no secrets hardcoded; everything via environment) --------

BASE_DIR = Path(__file__).resolve().parent

# --- LLM backend: OpenAI by default, any OpenAI-compatible endpoint via base URL.
# The OpenAI SDK speaks a contract that OpenRouter (and most gateways) also speak,
# so pointing at a different provider is just a base URL + key, no code branching.

# Key: accept either name so OpenAI and OpenRouter users can both "just set the key".
LLM_API_KEY = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY")

# Base URL: unset -> the SDK's OpenAI default; set it to e.g.
# https://openrouter.ai/api/v1 to drive OpenRouter instead.
LLM_BASE_URL = os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENROUTER_BASE_URL")

AGENT_MODEL = os.environ.get("AGENT_MODEL", "gpt-4o-mini")
# Optional. Newer reasoning models reject any non-default temperature, so we omit
# the parameter unless AGENT_TEMPERATURE is explicitly set.
AGENT_TEMPERATURE = os.environ.get("AGENT_TEMPERATURE")
ALLOW_NO_AUTH = os.environ.get("AGENT_ALLOW_NO_AUTH", "false").lower() == "true"
COOKIE_SECURE = os.environ.get("AGENT_COOKIE_SECURE", "false").lower() == "true"

# Cap on retained conversations (each holds its rounds). Bounds memory on a long
# scan; oldest conversations fall off first. One process only -- see README.
MAX_CONVERSATIONS = int(os.environ.get("AGENT_MAX_CONVERSATIONS", "1000"))

# Safety bound on the tool-call loop: how many times we'll execute tools and ask
# the model again before giving up on getting a plain text answer.
MAX_TOOL_ROUNDS = int(os.environ.get("AGENT_MAX_TOOL_ROUNDS", "4"))

if not LLM_API_KEY:
    sys.exit(
        "No LLM API key set. Export OPENAI_API_KEY (or OPENROUTER_API_KEY) "
        "before starting the agent."
    )


def _llm_default_headers() -> dict:
    """Optional OpenRouter ranking headers; sent only when configured."""
    headers = {}
    referer = os.environ.get("OPENROUTER_HTTP_REFERER")
    title = os.environ.get("OPENROUTER_X_TITLE")
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title
    return headers


_client_kwargs = {"api_key": LLM_API_KEY}
if LLM_BASE_URL:
    _client_kwargs["base_url"] = LLM_BASE_URL
_extra_headers = _llm_default_headers()
if _extra_headers:
    _client_kwargs["default_headers"] = _extra_headers

client = AsyncOpenAI(**_client_kwargs)

# Effective endpoint + a friendly provider label, resolved once for logging/UI.
LLM_BASE_URL_EFFECTIVE = str(client.base_url)
if "openrouter.ai" in LLM_BASE_URL_EFFECTIVE:
    LLM_PROVIDER = "OpenRouter"
elif "api.openai.com" in LLM_BASE_URL_EFFECTIVE:
    LLM_PROVIDER = "OpenAI"
else:
    LLM_PROVIDER = "custom (OpenAI-compatible)"

# --- Reply classification (dashboard badging only; not a security control) ----
# The persona's prompt, decoy, refusal phrases and tools all live in personas.py
# and are seeded into app.state below (switchable at runtime via /admin/persona).


def classify(response: str, decoy: str, hints: List[str]) -> str:
    low = response.lower()
    if decoy and decoy.lower() in low:
        return "leaked"
    if any(h.lower() in low for h in hints if h):
        return "refused"
    return "answered"


# --- Request / response models matching the stateless contract ---------------


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage] = Field(..., min_length=1)


class ResponseMessage(BaseModel):
    role: str
    content: str


class Choice(BaseModel):
    index: int = 0
    message: ResponseMessage
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    model: str
    choices: List[Choice]


class ConfigUpdate(BaseModel):
    """Runtime persona overrides from the dashboard. Any field may be omitted to
    leave it unchanged. Bounded to keep a single hostile field from ballooning."""
    system_prompt: Optional[str] = Field(default=None, max_length=20000)
    refusal_hints: Optional[List[str]] = Field(default=None, max_length=200)
    decoy_code: Optional[str] = Field(default=None, max_length=200)
    tools: Optional[List[dict]] = Field(default=None, max_length=50)


class PersonaSelect(BaseModel):
    """Switch the active persona (resets the live config to its defaults)."""
    persona: str


# --- App + in-memory state ----------------------------------------------------

app = FastAPI(title="Lakera Red demo target -- multi-persona support agent")

# Target bearer token: seeded from env if provided, else generated. Always set,
# so the target endpoint is protected by default (unless AGENT_ALLOW_NO_AUTH).
app.state.bearer_token = os.environ.get("AGENT_AUTH_TOKEN") or secrets.token_urlsafe(24)
app.state.allow_no_auth = ALLOW_NO_AUTH

# Admin password protects the dashboard. Generated + printed if not supplied.
_admin_pw = os.environ.get("AGENT_ADMIN_PASSWORD")
app.state.admin_generated = _admin_pw is None
app.state.admin_password = _admin_pw or secrets.token_urlsafe(12)

app.state.sessions: Set[str] = set()
app.state.subscribers: Set["asyncio.Queue[Tuple[str, dict]]"] = set()
# Interactions are grouped into conversations. The target is stateless (the
# contract carries no session id), so a multi-round attack arrives as separate
# requests whose message lists extend one another; _assign_conversation groups
# them by prefix + our echoed reply. Each conversation:
# {"id", "seq", "rounds": [event,...], "last_key", "match_key"}.
app.state.conversations: deque = deque(maxlen=MAX_CONVERSATIONS)
app.state.conversation_seq = 0
app.state.stats = {
    "total": 0, "refused": 0, "leaked": 0, "answered": 0, "conversations": 0,
    "tool_calls": 0,
}

# Live-editable target persona (in-memory only; resets to defaults on restart).
# Seeded from the active persona (personas.py); switch with /admin/persona.
app.state.persona_id = personas.DEFAULT_PERSONA_ID
_seed = personas.persona_config(app.state.persona_id)
app.state.system_prompt = _seed["system_prompt"]
app.state.refusal_hints = _seed["refusal_hints"]
app.state.decoy_code = _seed["decoy_code"]
app.state.tools = _seed["tools"]


def _sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def broadcast(event_type: str, data: dict) -> None:
    for q in list(app.state.subscribers):
        try:
            q.put_nowait((event_type, data))
        except Exception:
            pass


def _messages_key(messages: List[dict]) -> Tuple[Tuple[str, str], ...]:
    """A hashable, comparable signature of a message list (role + content)."""
    return tuple((m["role"], m["content"]) for m in messages)


def _assign_conversation(messages: List[dict], reply: str) -> Tuple[dict, int]:
    """Group a stateless request into a conversation and return (conv, round_no).

    The contract carries no session id, so we infer membership from the message
    history. In stateless multi-turn, round N+1 re-sends the whole transcript:
    round N's messages, then OUR reply to round N as an assistant turn, then the
    new user turn(s). So a conversation's "expected next prefix" is its latest
    round's messages PLUS our reply to that round.

    We match a request to the conversation whose expected-next-prefix it begins
    with (longest wins). Including our reply is what disambiguates conversations
    that share a user-message opening -- common in scans that template the first
    prompt -- because our replies to them differ; matching on the messages alone
    would cross-assign interleaved rounds and split/merge conversations wrongly.

    Fallback: if nothing matches tightly (e.g. a harness that doesn't echo our
    reply verbatim), match on the round's messages alone, but only when the next
    turn is an assistant turn (i.e. it still looks like a continuation). Failing
    both, it's a new conversation.
    """
    key = _messages_key(messages)

    def longest_match(attr, require_assistant_next):
        best = None  # (matched_len, conversation)
        for conv in app.state.conversations:
            ck = conv[attr]
            n = len(ck)
            if n < len(key) and key[:n] == ck:
                if require_assistant_next and key[n][0] != "assistant":
                    continue
                if best is None or n > best[0]:
                    best = (n, conv)
        return best[1] if best else None

    conv = longest_match("match_key", False) or longest_match("last_key", True)
    reply_turn = ("assistant", reply)

    if conv is not None:
        conv["last_key"] = key
        conv["match_key"] = key + (reply_turn,)
        return conv, len(conv["rounds"]) + 1

    app.state.conversation_seq += 1
    conv = {
        "id": f"conv-{app.state.conversation_seq}",
        "seq": app.state.conversation_seq,
        "rounds": [],
        "last_key": key,
        "match_key": key + (reply_turn,),
    }
    app.state.conversations.append(conv)
    return conv, 1


# --- Auth: target bearer token ------------------------------------------------


def require_bearer(
    request: Request, authorization: Optional[str] = Header(default=None)
) -> None:
    token = request.app.state.bearer_token
    if request.app.state.allow_no_auth:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    provided = authorization.split(" ", 1)[1].strip()
    if not hmac.compare_digest(provided, token):
        raise HTTPException(status_code=403, detail="Invalid token")


# --- Auth: dashboard session --------------------------------------------------


def require_session(request: Request) -> None:
    sid = request.cookies.get("dash_session")
    if not sid or sid not in request.app.state.sessions:
        raise HTTPException(status_code=401, detail="Not authenticated")


def _is_authed(request: Request) -> bool:
    sid = request.cookies.get("dash_session")
    return bool(sid and sid in request.app.state.sessions)


# --- Target endpoint ----------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    dependencies=[Depends(require_bearer)],
)
async def chat_completions(req: ChatCompletionRequest) -> ChatCompletionResponse:
    # Our authoritative system prompt first, then the full conversation Lakera
    # sent (injected system/assistant turns are passed through on purpose --
    # that is part of the attack surface being tested). The prompt, decoy and
    # tools are read live so dashboard edits take effect on the next request.
    decoy = app.state.decoy_code
    system_prompt = app.state.system_prompt.replace("{decoy}", decoy)
    convo = [{"role": "system", "content": system_prompt}]
    convo += [{"role": m.role, "content": m.content} for m in req.messages]

    tools = app.state.tools
    tool_calls_made: List[dict] = []
    content = ""
    completion_id = ""
    last_msg = None
    try:
        # Bounded tool-call loop: ask the model; if it calls tools, run the dummy
        # implementations, feed the results back, and ask again -- until it
        # returns plain text or we hit MAX_TOOL_ROUNDS.
        for _ in range(MAX_TOOL_ROUNDS + 1):
            kwargs = {"model": AGENT_MODEL, "messages": convo}
            if tools:
                kwargs["tools"] = tools
            if AGENT_TEMPERATURE is not None:
                kwargs["temperature"] = float(AGENT_TEMPERATURE)
            completion = await client.chat.completions.create(**kwargs)
            completion_id = completion.id
            last_msg = completion.choices[0].message
            calls = getattr(last_msg, "tool_calls", None)
            if not calls:
                content = last_msg.content or ""
                break
            # Echo the assistant's tool-call turn, then append a result per call.
            convo.append({
                "role": "assistant",
                "content": last_msg.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in calls
                ],
            })
            for tc in calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                result = personas.run_persona_tool(
                    personas.get_persona(app.state.persona_id), tc.function.name, args, decoy)
                tool_calls_made.append({"name": tc.function.name, "arguments": args, "result": result})
                convo.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        else:
            # Ran out of tool rounds without a final text answer.
            content = (last_msg.content if last_msg else "") or "(stopped after too many tool calls)"
    except Exception as exc:  # surface upstream errors as a clean 502
        print(f"Upstream LLM call failed: {exc!r}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=502, detail=f"Upstream LLM error: {exc}")

    status = classify(content, decoy, app.state.refusal_hints)

    # Group this request into a conversation (see _assign_conversation). Our
    # reply is passed in: it's echoed back as an assistant turn in the next
    # round, which is what lets us tell apart conversations with the same opening.
    sent_messages = [{"role": m.role, "content": m.content} for m in req.messages]
    conv, round_no = _assign_conversation(sent_messages, content)

    # Update stats + live feed.
    app.state.stats["total"] += 1
    app.state.stats[status] = app.state.stats.get(status, 0) + 1
    app.state.stats["conversations"] = len(app.state.conversations)
    app.state.stats["tool_calls"] = app.state.stats.get("tool_calls", 0) + len(tool_calls_made)
    event = {
        "id": completion_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": AGENT_MODEL,
        "messages": sent_messages,
        "response": content,
        "status": status,
        "tool_calls": tool_calls_made,
        "conversation_id": conv["id"],
        "conversation_seq": conv["seq"],
        "round": round_no,
    }
    conv["rounds"].append(event)
    broadcast("interaction", event)
    broadcast("stats", app.state.stats)

    return ChatCompletionResponse(
        id=completion_id,
        model=AGENT_MODEL,
        choices=[Choice(message=ResponseMessage(role="assistant", content=content))],
    )


# --- Dashboard + admin --------------------------------------------------------


def _login_page(error: str = "") -> str:
    msg = (
        f'<p class="err">{error}</p>'
        if error
        else '<p class="hint">Enter the admin password shown in the server console.</p>'
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Red Team Monitor &middot; Sign in</title>
<style>
  :root {{ --berry:#ee0c5d; --berry-click:#e40c5b; --grey:#41273c; --clay:#f2f2f2; --line:#e3dfe1; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:Arial, sans-serif; background:var(--clay); color:var(--grey);
         display:flex; min-height:100vh; align-items:center; justify-content:center; }}
  .card {{ background:#fff; border:1px solid var(--line); width:360px; max-width:92vw; }}
  .bar {{ background:var(--berry); color:#fff; padding:16px 20px; font-weight:bold; letter-spacing:.2px; }}
  form {{ padding:20px; }}
  label {{ display:block; font-size:12px; text-transform:uppercase; letter-spacing:.6px; margin-bottom:6px; }}
  input[type=password] {{ width:100%; padding:10px 12px; border:1px solid var(--line); font-size:15px;
                          font-family:Consolas, monospace; }}
  button {{ margin-top:16px; width:100%; padding:11px; border:0; background:var(--berry); color:#fff;
            font-size:14px; font-weight:bold; cursor:pointer; }}
  button:hover {{ background:var(--berry-click); }}
  .hint {{ font-size:12px; color:#7a6c74; margin:12px 0 0; }}
  .err {{ font-size:13px; color:#ff3312; margin:12px 0 0; }}
</style></head><body>
  <div class="card">
    <div class="bar">Check Point &middot; Red Team Monitor</div>
    <form method="post" action="/login">
      <label for="pw">Admin password</label>
      <input id="pw" name="password" type="password" autofocus autocomplete="current-password">
      <button type="submit">Sign in</button>
      {msg}
    </form>
  </div>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def root(request: Request) -> HTMLResponse:
    if not _is_authed(request):
        return HTMLResponse(_login_page())
    html = (BASE_DIR / "webui.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if hmac.compare_digest(password, request.app.state.admin_password):
        sid = secrets.token_urlsafe(32)
        request.app.state.sessions.add(sid)
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie(
            "dash_session", sid, httponly=True, samesite="lax", secure=COOKIE_SECURE
        )
        return resp
    return HTMLResponse(_login_page("Incorrect password."), status_code=401)


@app.post("/logout")
def logout(request: Request):
    sid = request.cookies.get("dash_session")
    if sid:
        request.app.state.sessions.discard(sid)
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie("dash_session")
    return resp


@app.get("/admin/meta", dependencies=[Depends(require_session)])
def get_meta() -> JSONResponse:
    # The decoy is a fake, non-sensitive string; the dashboard uses it only to
    # highlight where a leak occurred in the agent's reply.
    return JSONResponse(
        {"decoy": app.state.decoy_code, "model": AGENT_MODEL, "provider": LLM_PROVIDER}
    )


def _config_payload() -> dict:
    return {
        "system_prompt": app.state.system_prompt,
        "refusal_hints": app.state.refusal_hints,
        "decoy_code": app.state.decoy_code,
        "tools": app.state.tools,
        "persona": app.state.persona_id,
        "personas": personas.persona_summaries(),
        "model": AGENT_MODEL,
        "provider": LLM_PROVIDER,
        "defaults": personas.persona_config(app.state.persona_id),
    }


def _validate_tools(tools) -> list:
    """Ensure submitted tools are well-formed OpenAI function tools."""
    if not isinstance(tools, list):
        raise HTTPException(status_code=400, detail="tools must be a list")
    for i, t in enumerate(tools):
        if not isinstance(t, dict) or t.get("type") != "function":
            raise HTTPException(status_code=400, detail=f"tool[{i}] must be an object with type 'function'")
        fn = t.get("function")
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str) or not fn.get("name", "").strip():
            raise HTTPException(status_code=400, detail=f"tool[{i}].function.name is required")
    return tools


@app.get("/admin/config", dependencies=[Depends(require_session)])
def get_config() -> JSONResponse:
    return JSONResponse(_config_payload())


@app.post("/admin/config", dependencies=[Depends(require_session)])
def update_config(cfg: ConfigUpdate) -> JSONResponse:
    # Admin-only. Each field is optional; omitted fields are left unchanged.
    if cfg.system_prompt is not None:
        sp = cfg.system_prompt.strip()
        if not sp:
            raise HTTPException(status_code=400, detail="system_prompt cannot be empty")
        app.state.system_prompt = sp
    if cfg.refusal_hints is not None:
        app.state.refusal_hints = [
            h.strip() for h in cfg.refusal_hints if isinstance(h, str) and h.strip()
        ]
    if cfg.decoy_code is not None:
        app.state.decoy_code = cfg.decoy_code.strip()
    if cfg.tools is not None:
        app.state.tools = _validate_tools(cfg.tools)
    return JSONResponse(_config_payload())


@app.post("/admin/persona", dependencies=[Depends(require_session)])
def set_persona(sel: PersonaSelect) -> JSONResponse:
    # Admin-only. Switch the active persona and reset the live config to its
    # defaults (prompt / decoy / refusal phrases / tools).
    if sel.persona not in personas.persona_ids():
        raise HTTPException(status_code=400, detail="unknown persona")
    app.state.persona_id = sel.persona
    cfg = personas.persona_config(sel.persona)
    app.state.system_prompt = cfg["system_prompt"]
    app.state.refusal_hints = cfg["refusal_hints"]
    app.state.decoy_code = cfg["decoy_code"]
    app.state.tools = cfg["tools"]
    return JSONResponse(_config_payload())


@app.get("/admin/token", dependencies=[Depends(require_session)])
def get_token(request: Request) -> JSONResponse:
    return JSONResponse({"token": request.app.state.bearer_token})


@app.post("/admin/token/regenerate", dependencies=[Depends(require_session)])
def regenerate_token(request: Request) -> JSONResponse:
    request.app.state.bearer_token = secrets.token_urlsafe(24)
    return JSONResponse({"token": request.app.state.bearer_token})


@app.get("/admin/stream", dependencies=[Depends(require_session)])
async def stream(request: Request) -> StreamingResponse:
    async def event_source():
        q: "asyncio.Queue[Tuple[str, dict]]" = asyncio.Queue()
        app.state.subscribers.add(q)
        try:
            # Prime a freshly-opened dashboard: current stats + recent history.
            # Replay oldest conversation first, rounds in order, so the client
            # rebuilds the same tree (newest conversation ends up on top).
            yield _sse("stats", app.state.stats)
            for conv in list(app.state.conversations):
                for item in conv["rounds"]:
                    yield _sse("interaction", item)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event_type, data = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(event_type, data)
        finally:
            app.state.subscribers.discard(q)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- Startup banner -----------------------------------------------------------


@app.on_event("startup")
def _banner() -> None:
    line = "=" * 64
    print(line)
    print("Lakera Red demo target is starting.")
    print(f"  Target endpoint : POST /v1/chat/completions  (model: {AGENT_MODEL})")
    print(f"  LLM backend     : {LLM_PROVIDER}  ({LLM_BASE_URL_EFFECTIVE})")
    print(f"  Persona         : {personas.get_persona(app.state.persona_id)['name']}")
    print(f"  Dashboard       : GET  /")
    if app.state.admin_generated:
        print(f"  Admin password  : {app.state.admin_password}   <- generated, use to sign in")
    else:
        print("  Admin password  : (from AGENT_ADMIN_PASSWORD)")
    if app.state.allow_no_auth:
        print("  Target auth     : DISABLED (AGENT_ALLOW_NO_AUTH=true) -- local use only")
    else:
        print("  Target auth     : Bearer token (view/copy/regenerate in the dashboard)")
    print(line)


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("AGENT_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENT_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
