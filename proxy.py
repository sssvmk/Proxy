# BEFORE MODIFYING THIS FILE: Read RULES.md in the project root and follow all rules. No exceptions.
"""
Module: proxy.py
Purpose: Flask application entry point, route registration, and startup sequence.
Layer Architecture: Layer 0 (read_body_once) and Layer 1 (Route Dispatch).
Contracts Served: Contract 1, 2, and 3.
Dependencies: time, flask, config, auth, session, handlers.

SPEC-CODE SYNC: proxy_v2_spec.md v3.0 §4.1, §9 WORK-07 to WORK-10
"""
import time
import json
from flask import Flask, request, Response, g
import config
from auth import TokenManager
from session import SessionStore
import handlers.openai_handler as openai_handler
import handlers.gemini_handler as gemini_handler
import handlers.responses_handler as responses_handler
import handlers.images_handler as images_handler

app = Flask(__name__)

token_manager = TokenManager()         # reads from config internally
session_store = SessionStore()         # initialises empty store + TTL cleanup timer
token_manager.warm_up()                # eager token fetch — fails fast if credentials wrong

print("══════════════════════════════════════════════")
print("  Gemini Proxy v2")
print(f"  OAuth URL    : {config.OAUTH_URL}")
print(f"   Base URL : {config._BASE_URL}")
print(f"  Allowed Model: {config.ALLOWED_MODEL}")
print(f"  Port         : {config.PROXY_PORT}")
print(f"  Session TTL  : {config.SESSION_TTL_SECS}s")
print("══════════════════════════════════════════════")

@app.before_request
def read_body_once():
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Hook to read the stream payload fully before dispatching to handlers.

    Parameters: None
    Returns: None
    Enforces R11.
    """
    # R11: Body read via flask.g.raw_body only — never request.data
    g.raw_body = request.get_data()
    print(f"\n[{time.strftime('%H:%M:%S')}] ── Incoming ──────────────────────")
    print(f"  METHOD : {request.method}")
    print(f"  PATH   : {request.path}")
    print(f"  QUERY  : {request.query_string.decode('utf-8')}")
    try:
        log_body = g.raw_body.decode('utf-8')[:config.LOG_BODY_CHARS]
    except Exception:
        log_body = repr(g.raw_body[:config.LOG_BODY_CHARS])
    print(f"  BODY   : {log_body}")

# ── Discovery endpoint intercepts ────────────────────────────────────────────
# These are capability-discovery requests fired by OpenAI-compatible clients
# (Hermes, LangChain, etc.) during initialisation.  gateway has no such
# endpoints — the proxy answers them locally with synthetic responses.
# This prevents 400 error storms and wasteful session creation.

_MODELS_RESPONSE = json.dumps({
    "object": "list",
    "data": [{
        "id": config.ALLOWED_MODEL,
        "object": "model",
        "created": 1700000000,
        "owned_by": "google",
        "permission": [],
        "root": config.ALLOWED_MODEL,
        "parent": None
    }]
})

_MODEL_DETAIL_RESPONSE = json.dumps({
    "id": config.ALLOWED_MODEL,
    "object": "model",
    "created": 1700000000,
    "owned_by": "google"
})

_VERSION_RESPONSE = json.dumps({"version": "proxy-v2"})

_TAGS_RESPONSE = json.dumps({
    "models": [{
        "name": config.ALLOWED_MODEL,
        "modified_at": "2025-01-01T00:00:00Z",
        "size": 0,
        "digest": "proxy-v2",
        "details": {}
    }]
})

_PROPS_RESPONSE = json.dumps({
    "total_duration": 0,
    "load_duration": 0,
    "prompt_eval_count": 0,
    "eval_count": 0
})


@app.route('/v1/models', methods=['GET', 'OPTIONS'])
@app.route('/models', methods=['GET', 'OPTIONS'])
@app.route('/api/v1/models', methods=['GET', 'OPTIONS'])
def discovery_models():
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Intercepts model list discovery requests and returns a synthetic response locally.
    Prevents forwarding to  which has no /models endpoint.
    """
    if request.method == 'OPTIONS':
        return Response(status=200)
    print(f"  🗂️  Discovery: model list — answered locally")
    return Response(_MODELS_RESPONSE, status=200, content_type='application/json')


@app.route('/v1/models/<path:model_id>', methods=['GET', 'OPTIONS'])
def discovery_model_detail(model_id):
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Intercepts single model detail requests and returns a synthetic response locally.
    """
    if request.method == 'OPTIONS':
        return Response(status=200)
    print(f"  🗂️  Discovery: model detail ({model_id}) — answered locally")
    return Response(_MODEL_DETAIL_RESPONSE, status=200, content_type='application/json')


@app.route('/api/tags', methods=['GET', 'OPTIONS'])
def discovery_tags():
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Intercepts Ollama-style /api/tags discovery requests locally.
    """
    if request.method == 'OPTIONS':
        return Response(status=200)
    print(f"  🗂️  Discovery: api/tags — answered locally")
    return Response(_TAGS_RESPONSE, status=200, content_type='application/json')


@app.route('/api/show', methods=['POST', 'OPTIONS'])
def discovery_show():
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Intercepts Ollama-style /api/show model info requests locally.
    """
    if request.method == 'OPTIONS':
        return Response(status=200)
    print(f"  🗂️  Discovery: api/show — answered locally")
    return Response(_MODEL_DETAIL_RESPONSE, status=200, content_type='application/json')


@app.route('/version', methods=['GET', 'OPTIONS'])
def discovery_version():
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Intercepts version check requests locally.
    """
    if request.method == 'OPTIONS':
        return Response(status=200)
    print(f"  🗂️  Discovery: version — answered locally")
    return Response(_VERSION_RESPONSE, status=200, content_type='application/json')


@app.route('/v1/props', methods=['GET', 'OPTIONS'])
@app.route('/props', methods=['GET', 'OPTIONS'])
def discovery_props():
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Intercepts props discovery requests locally.
    """
    if request.method == 'OPTIONS':
        return Response(status=200)
    print(f"  🗂️  Discovery: props — answered locally")
    return Response(_PROPS_RESPONSE, status=200, content_type='application/json')


# ── Contract 1 — OpenAI (both versioned and unversioned paths) ────────────────
@app.route('/v1/chat/completions', methods=['POST', 'OPTIONS'])
@app.route('/chat/completions', methods=['POST', 'OPTIONS'])
def openai_route():
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Route for OpenAI contract.
    Returns: Response object from the handler.
    """
    if request.method == 'OPTIONS':
        return Response(status=200)
    return openai_handler.handle(g.raw_body, token_manager, session_store)


# ── 501 stubs —  has no storage layer (GET/DELETE confirmed 404 by testing) ──
# WORK-07 / TODO-10: These must be registered BEFORE the catch-all so Flask
# matches them before /<path:path>. Returning 501 instead of forwarding to 
# which returns a confusing upstream 404. (R11, spec section 8)
_NOT_IMPLEMENTED_BODY = json.dumps({
    "error": {
        "code":    501,
        "message": (" endpoint has no storage layer. "
                    "GET and DELETE operations are not available. "
                    "Use POST to generate content."),
        "status":  "NOT_IMPLEMENTED"
    }
})


@app.route('/interactions/<path:iid>', methods=['GET', 'DELETE', 'OPTIONS'])
@app.route('/files', methods=['GET', 'OPTIONS'])
@app.route('/files/<path:fid>', methods=['GET', 'DELETE', 'OPTIONS'])
@app.route('/cachedContents', methods=['GET', 'OPTIONS'])
@app.route('/cachedContents/<path:cid>', methods=['GET', 'DELETE', 'OPTIONS'])
@app.route('/batches', methods=['GET', 'OPTIONS'])
@app.route('/batches/<path:bid>', methods=['GET', 'DELETE', 'OPTIONS'])
def _not_implemented(**kwargs):
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Returns 501 for GET/DELETE on  aliased paths that have no storage layer.
    Confirmed by H11 endpoint testing: all return 404 from .
    Proxy intercepts here so clients receive a clear 501 instead of a
    forwarded 404. Enforces spec section 8.
    """
    if request.method == 'OPTIONS':
        return Response(status=200)
    print(f"  WARNING 501 stub: {request.method} {request.path} --  has no storage layer")
    return Response(_NOT_IMPLEMENTED_BODY, status=501, content_type='application/json')


# -- Contract 1 -- OpenAI Responses API ----------------------------------------
# WORK-08 / TODO-01,02: POST /v1/responses (sync + streaming)
# WORK-09 / TODO-03,04,05: GET/DELETE /v1/responses/<id> + input_items
# WORK-10 / TODO-06: POST /v1/responses/compact
# NOTE: /v1/responses/compact must be registered BEFORE /v1/responses/<response_id>
# to prevent Flask matching "compact" as a response_id.

@app.route('/v1/responses/compact', methods=['POST', 'OPTIONS'])
def responses_compact_route():
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Route for OpenAI Responses API context compaction endpoint.
    Returns: Response object from responses_handler.
    """
    if request.method == 'OPTIONS':
        return Response(status=200)
    return responses_handler.handle_compact(g.raw_body, token_manager, session_store)


@app.route('/v1/responses', methods=['POST', 'OPTIONS'])
def responses_create_route():
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Route for OpenAI Responses API create endpoint (sync + streaming).
    Returns: Response object from responses_handler.
    """
    if request.method == 'OPTIONS':
        return Response(status=200)
    return responses_handler.handle(g.raw_body, token_manager, session_store)


@app.route('/v1/responses/<response_id>/input_items', methods=['GET', 'OPTIONS'])
def responses_input_items_route(response_id):
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Route for listing input items of a stored Responses API response.
    Returns: Response object from responses_handler.
    """
    if request.method == 'OPTIONS':
        return Response(status=200)
    return responses_handler.handle_input_items(response_id, session_store)


@app.route('/v1/responses/<response_id>', methods=['GET', 'DELETE', 'OPTIONS'])
def responses_crud_route(response_id):
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Route for GET (retrieve/poll) and DELETE (cancel) of a stored Responses API response.
    Returns: Response object from responses_handler.
    """
    if request.method == 'OPTIONS':
        return Response(status=200)
    if request.method == 'GET':
        return responses_handler.handle_get(response_id, session_store)
    return responses_handler.handle_delete(response_id, session_store)


# -- Images API — generations only (see images_handler.py module docstring
#    for scope/assumptions: gemini_3.1-pro-preview image-output support is
#    assumed per explicit direction, not independently verified) ────────────
@app.route('/v1/images/generations', methods=['POST', 'OPTIONS'])
@app.route('/images/generations', methods=['POST', 'OPTIONS'])
def images_generations_route():
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Route for OpenAI Images API create endpoint (generations only —
    /images/edits and /images/variations are not yet implemented, see
    images_handler.py module docstring).
    Returns: Response object from images_handler.
    """
    if request.method == 'OPTIONS':
        return Response(status=200)
    return images_handler.handle(g.raw_body, token_manager)


# ── Contract 2 / 3 — Native Gemini (catch-all) ───────────────────────────────
@app.route('/<path:path>', methods=['GET', 'POST', 'OPTIONS'])
def gemini_route(path):
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Catch-all route for Native Gemini payload requests.
    Returns: Response object from the handler.
    """
    if request.method == 'OPTIONS':
        return Response(status=200)
    return gemini_handler.handle(path, g.raw_body, token_manager, session_store)


if __name__ == "__main__":
    app.run(port=config.PROXY_PORT, threaded=True, debug=False)
