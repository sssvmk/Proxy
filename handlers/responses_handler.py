# BEFORE MODIFYING THIS FILE: Read RULES.md in the project root and follow all rules. No exceptions.
"""
Module: handlers.responses_handler
Purpose: Handles the OpenAI Responses API surface (POST /v1/responses and
         supporting CRUD/compaction endpoints), translating to/from the
         Gemini generateContent schema via the GSK gateway.
Layer Architecture: Layers 2, 3, 4, 5, 6, 7 (same as openai_handler, plus
                     a synthetic response-store layer for GET/DELETE).
Contracts Served: Contract 1 (OpenAI), Responses API surface specifically.
Dependencies: json, time, uuid, requests, config, transforms.path,
              translate.request, translate.response, session.

GSK GATEWAY NOTE: The Responses API fields store, previous_response_id,
background, input are stripped by translate.request.openai_to_gemini()
before forwarding — GSK does not support them (confirmed by testing:
"Unknown name 'store'", "Unknown name 'input'"). This handler synthesises
the storage and ID-chaining behaviour the client expects, entirely
proxy-side, since GSK has no native Responses API or Interactions API
storage layer (confirmed: GET/DELETE on /interactions/{id} return 404).

STORAGE STRATEGY (spec section 4.10, Option A): completed responses are
cached in-memory in SessionStore._response_cache, keyed by a synthesized
resp_{uuid} ID. This cache is lost on proxy restart -- documented limitation.

SPEC-CODE SYNC: proxy_v2_spec.md v3.0 section 4.10, section 9 WORK-08 WORK-09 WORK-10
"""
import json
import time
import uuid
import requests
from flask import Response
import config
from transforms.path import normalize_model, build_target_url, build_headers
from translate.request import openai_to_gemini, TranslationError
from translate.response import gemini_to_openai
from session import SessionStore


def _error_response(status: int, message: str, error_status: str = "INVALID_ARGUMENT") -> Response:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Builds a structured JSON error Response matching the Gemini/Responses
    API error shape used throughout this codebase.

    Parameters: status (int): HTTP status code. message (str): Error detail.
                error_status (str): Gemini-style status enum string.
    Returns: Response — Flask Response object with JSON error body.
    """
    body = json.dumps({"error": {"code": status, "message": message, "status": error_status}})
    return Response(body, status=status, content_type='application/json')


def _synthesize_response_id() -> str:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Generates an OpenAI Responses API style response ID.

    Returns: str — ID in the form "resp_{32 hex chars}".
    """
    return f"resp_{uuid.uuid4().hex}"


def _gemini_response_to_responses_api(
    gemini_json: dict,
    response_id: str,
    model: str,
    created_at: int
) -> dict:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Converts a Gemini generateContent response body into the OpenAI
    Responses API response shape (spec section 4.10).

    Parameters:
      gemini_json (dict): Parsed Gemini generateContent response.
      response_id (str): Synthesized response ID (resp_{uuid}).
      model (str): Model string echoed from the request.
      created_at (int): Unix timestamp of response creation.

    Returns: dict — Responses API shaped response object.

    Output item union type per spec section 3.4.7: only 'message' and
    'function_call' item types are populated here; other types
    (reasoning, mcp_call, image_generation_call, etc.) are not produced
    by the GSK gateway and are out of scope for this translation.
    """
    candidates = gemini_json.get('candidates', [])
    output = []

    if candidates:
        cand  = candidates[0]
        parts = cand.get('content', {}).get('parts', [])

        content_blocks = []
        for part in parts:
            if 'text' in part:
                content_blocks.append({"type": "output_text", "text": part['text']})
            elif 'functionCall' in part:
                fc = part['functionCall']
                output.append({
                    "type": "function_call",
                    "name": fc.get('name', ''),
                    "arguments": json.dumps(fc.get('args', {})),
                    "call_id": f"call_{uuid.uuid4().hex[:24]}"
                })
            elif 'inlineData' in part:
                mime = part['inlineData'].get('mimeType', 'image/png')
                data = part['inlineData'].get('data', '')
                content_blocks.append({
                    "type": "output_image",
                    "image_url": f"data:{mime};base64,{data}"
                })

        if content_blocks:
            output.insert(0, {
                "type":    "message",
                "role":    "assistant",
                "content": content_blocks
            })

    usage_meta = gemini_json.get('usageMetadata', {})
    usage = {
        "input_tokens":  usage_meta.get('promptTokenCount', 0),
        "output_tokens": usage_meta.get('candidatesTokenCount', 0),
        "total_tokens":  usage_meta.get('totalTokenCount', 0)
    }

    status = "completed"
    if candidates and candidates[0].get('finishReason') == 'MAX_TOKENS':
        status = "completed"  # Responses API treats MAX_TOKENS as a completed (truncated) response

    return {
        "id":          response_id,
        "object":      "response",
        "created_at":  created_at,
        "status":      status,
        "model":       model,
        "output":      output,
        "usage":       usage,
        "output_text": "".join(b['text'] for b in
                               (output[0]['content'] if output and output[0].get('type') == 'message' else [])
                               if b.get('type') == 'output_text')
    }


def handle(raw_body: bytes, token_manager, session_store: SessionStore) -> Response:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Handles POST /v1/responses — create a response (sync or streaming).

    Parameters:
      raw_body (bytes): Raw request body (R11 — read once upstream).
      token_manager (TokenManager): Shared token manager instance.
      session_store (SessionStore): Shared session store instance.

    Returns: Response — JSON Responses API shaped response, or SSE stream.
    Enforces R1, R14, R18 (transitively via openai_to_gemini).

    Translation notes (spec section 4.10):
      input (str|array) -> contents[]
      instructions       -> systemInstruction
      text.format         -> generationConfig.responseMimeType + responseSchema
      max_output_tokens   -> generationConfig.maxOutputTokens
      store, background, metadata -> stripped (GSK rejects)

    previous_response_id chaining (fix, was previously a no-op): the prior
    turn's messages are retrieved from SessionStore.get_response_messages()
    and prepended to this turn's messages, so context actually carries
    forward the way Responses API clients expect when they send only the
    new turn's input. Known limitation: chained history currently carries
    text content only (tool-call turns are flattened to their text output
    when re-chained) — full tool-call round-trip chaining is not yet
    implemented.
    """
    # R14: token fetch is first operation
    token = token_manager.get_token()

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return _error_response(400, "Invalid JSON in request body.")

    previous_response_id = payload.get('previous_response_id')
    prior_messages = []
    if previous_response_id:
        cached_messages = session_store.get_response_messages(previous_response_id)
        if cached_messages:
            # Strip system/developer messages from prior turns — each
            # request supplies its own 'instructions' below, which should
            # govern this call rather than accumulate turn over turn.
            prior_messages = [m for m in cached_messages if m.get('role') not in ('system', 'developer')]
        else:
            print(f"  ⚠️  previous_response_id={previous_response_id} not found in chaining cache "
                  f"(expired or proxy restarted) — continuing without prior context")

    session_id = previous_response_id or f"resp_session_{uuid.uuid4().hex[:16]}"

    # ── Translate Responses API request fields to OpenAI-compatible shape ─────
    # so we can reuse the existing, well-tested openai_to_gemini() translator.
    this_turn_messages = []

    instructions = payload.get('instructions')
    if instructions:
        this_turn_messages.append({"role": "system", "content": instructions})

    input_val = payload.get('input', '')
    if isinstance(input_val, str):
        this_turn_messages.append({"role": "user", "content": input_val})
    elif isinstance(input_val, list):
        # Responses API input array — convert each item to a message
        content_items = []
        for item in input_val:
            item_type = item.get('type')
            if item_type == 'input_text' or item_type == 'text':
                content_items.append({"type": "text", "text": item.get('text', '')})
            elif item_type == 'input_image':
                content_items.append({"type": "image_url", "image_url": {
                    "url": item.get('image_url', '') or item.get('file_id', '')
                }})
            elif item_type == 'input_file':
                content_items.append({"type": "file", "file": {
                    "file_data": item.get('file_data'),
                    "file_url":  item.get('file_url'),
                    "file_type": item.get('mime_type', 'application/pdf')
                }})
        if content_items:
            this_turn_messages.append({"role": "user", "content": content_items})

    openai_shaped = {
        "model":    payload.get('model', config.ALLOWED_MODEL),
        "messages": prior_messages + this_turn_messages,
        "stream":   payload.get('stream', False)
    }

    if 'max_output_tokens' in payload:
        openai_shaped['max_tokens'] = payload['max_output_tokens']
    if 'temperature' in payload:
        openai_shaped['temperature'] = payload['temperature']
    if 'tools' in payload:
        openai_shaped['tools'] = payload['tools']
    if 'tool_choice' in payload:
        openai_shaped['tool_choice'] = payload['tool_choice']

    text_format = payload.get('text', {}).get('format')
    if text_format:
        openai_shaped.setdefault('response_format_internal', text_format)

    try:
        gemini_body, target_suffix = openai_to_gemini(openai_shaped, session_id, session_store)
    except TranslationError as e:
        print(f"  ❌ Responses API translation error: {e.message}")
        return _error_response(400, e.message, e.status)

    if text_format:
        gemini_body.setdefault('generationConfig', {})['responseMimeType'] = 'application/json'
        if text_format.get('schema'):
            gemini_body['generationConfig']['responseSchema'] = text_format['schema']

    is_streaming = payload.get('stream', False)
    target_suffix = (f"models/{openai_shaped['model']}:streamGenerateContent"
                      if is_streaming else f"models/{openai_shaped['model']}:generateContent")
    target_path = normalize_model(target_suffix)
    query_string = 'alt=sse' if is_streaming else ''
    target_url = build_target_url(target_path, query_string)
    headers = build_headers(token, {})

    response_id = _synthesize_response_id()
    created_at  = int(time.time())

    # Chain history to persist alongside this turn's answer, for the NEXT
    # response in the chain (if any) to pick up via previous_response_id.
    chain_messages = prior_messages + this_turn_messages

    if is_streaming:
        return _stream(gemini_body, target_url, headers, response_id,
                       openai_shaped['model'], created_at, session_store, chain_messages)
    return _send(gemini_body, target_url, headers, response_id,
                openai_shaped['model'], created_at, session_store, chain_messages)



def _send(gemini_body: dict, target_url: str, headers: dict,
          response_id: str, model: str, created_at: int,
          session_store: SessionStore, chain_messages: list) -> Response:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Non-streaming dispatch for the Responses API. Forwards to GSK,
    translates the response, stores it in the response cache (spec section 4.10
    Option A), and returns it to the client.

    Parameters: gemini_body (dict): Translated Gemini request body.
                target_url (str): Full upstream URL.
                headers (dict): Upstream request headers.
                response_id (str): Synthesized response ID.
                model (str): Model string for the response.
                created_at (int): Unix timestamp.
                session_store (SessionStore): Shared session store.
                chain_messages (list): This turn's messages (prior history +
                                        this turn's own input), to be extended
                                        with the assistant's reply and stored
                                        for previous_response_id chaining.
    Returns: Response — JSON Responses API shaped response.
    """
    try:
        upstream = requests.post(
            target_url, headers=headers, json=gemini_body,
            timeout=(config.UPSTREAM_CONNECT_TIMEOUT, config.UPSTREAM_READ_TIMEOUT)
        )
    except requests.exceptions.RequestException as e:
        return _error_response(504, f"Upstream request failed: {str(e)}", "DEADLINE_EXCEEDED")

    if upstream.status_code != 200:
        # Passthrough upstream error, wrapped minimally
        return Response(upstream.content, status=upstream.status_code,
                        content_type='application/json')

    gemini_json = upstream.json()
    resp_obj = _gemini_response_to_responses_api(gemini_json, response_id, model, created_at)

    # Spec section 4.10 Option A: store in SessionStore response cache for GET/DELETE
    session_store.store_response_cache(response_id, resp_obj)

    # previous_response_id chaining fix: persist this turn's history + reply
    # (text only — see known limitation on tool-call turns in handle()'s
    # docstring) so a future request chaining off THIS response_id has
    # something to prepend.
    assistant_text = resp_obj.get('output_text', '')
    if assistant_text:
        session_store.store_response_messages(
            response_id, chain_messages + [{"role": "assistant", "content": assistant_text}]
        )
    else:
        session_store.store_response_messages(response_id, chain_messages)

    return Response(json.dumps(resp_obj), status=200, content_type='application/json')


def _stream(gemini_body: dict, target_url: str, headers: dict,
           response_id: str, model: str, created_at: int,
           session_store: SessionStore, chain_messages: list) -> Response:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Streaming dispatch for the Responses API. Emits the Responses API SSE
    event taxonomy (spec section 4.10): response.created, response.output_item.added,
    response.output_text.delta, response.function_call_arguments.delta/done,
    response.output_item.done, response.completed.

    Parameters: same as _send(), plus chain_messages (list) — see _send().
    Returns: Response — text/event-stream SSE response.
    Enforces R1 (stream=True on upstream call).

    Fix (was a bug): terminal event was previously named 'response.done',
    which does not exist in the real Responses API event taxonomy — real
    SDKs listen for 'response.completed'/'response.failed'/'response.incomplete'
    and would never observe the stream finishing. Renamed to match.

    Fix (was a bug): only 'text' parts were handled here — a functionCall
    part appearing in a streaming chunk was silently dropped, even though
    the non-streaming _send() path already handled tool calls correctly.
    Tool calls now produce output_item.added / function_call_arguments.delta
    / function_call_arguments.done / output_item.done, same as a real
    Responses API tool-call stream (Gemini returns args as one complete
    JSON object per chunk rather than token-by-token, so the delta event
    carries the full arguments string in one piece rather than incrementally).
    """
    try:
        upstream = requests.post(
            target_url, headers=headers, json=gemini_body, stream=True,  # R1
            timeout=(config.UPSTREAM_CONNECT_TIMEOUT, config.UPSTREAM_READ_TIMEOUT)
        )
    except requests.exceptions.RequestException as e:
        return _error_response(504, f"Upstream request failed: {str(e)}", "DEADLINE_EXCEEDED")

    if upstream.status_code != 200:
        return Response(upstream.content, status=upstream.status_code,
                        content_type='application/json')

    def generate():
        # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
        accumulated_text = ""
        item_added = False
        function_call_items = []
        final_usage = None

        created_event = {"type": "response.created", "response": {
            "id": response_id, "object": "response", "created_at": created_at,
            "status": "in_progress", "model": model
        }}
        yield f"event: response.created\ndata: {json.dumps(created_event)}\n\n"

        for raw_line in upstream.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode('utf-8') if isinstance(raw_line, bytes) else raw_line
            if not line.startswith('data: '):
                continue
            data_str = line[6:].strip()
            if not data_str or data_str == '[DONE]':
                continue
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            if 'usageMetadata' in chunk:
                um = chunk['usageMetadata']
                final_usage = {
                    "input_tokens":  um.get('promptTokenCount', 0),
                    "output_tokens": um.get('candidatesTokenCount', 0),
                    "total_tokens":  um.get('totalTokenCount', 0)
                }

            for cand in chunk.get('candidates', []):
                for part in cand.get('content', {}).get('parts', []):
                    if 'text' in part:
                        if not item_added:
                            added_event = {"type": "response.output_item.added",
                                          "item": {"type": "message", "role": "assistant"}}
                            yield f"event: response.output_item.added\ndata: {json.dumps(added_event)}\n\n"
                            item_added = True
                        accumulated_text += part['text']
                        delta_event = {"type": "response.output_text.delta", "delta": part['text']}
                        yield f"event: response.output_text.delta\ndata: {json.dumps(delta_event)}\n\n"

                    elif 'functionCall' in part:
                        fc = part['functionCall']
                        call_id = f"call_{uuid.uuid4().hex[:24]}"
                        arguments_str = json.dumps(fc.get('args', {}))
                        fc_item = {
                            "type": "function_call",
                            "name": fc.get('name', ''),
                            "arguments": arguments_str,
                            "call_id": call_id
                        }
                        fc_added_event = {"type": "response.output_item.added",
                                          "item": {"type": "function_call",
                                                  "name": fc_item['name'], "call_id": call_id}}
                        yield f"event: response.output_item.added\ndata: {json.dumps(fc_added_event)}\n\n"

                        # Gemini returns args whole, not token-by-token — one
                        # delta carrying the complete string, then done.
                        args_delta_event = {"type": "response.function_call_arguments.delta",
                                            "call_id": call_id, "delta": arguments_str}
                        yield f"event: response.function_call_arguments.delta\ndata: {json.dumps(args_delta_event)}\n\n"
                        args_done_event = {"type": "response.function_call_arguments.done",
                                           "call_id": call_id, "arguments": arguments_str}
                        yield f"event: response.function_call_arguments.done\ndata: {json.dumps(args_done_event)}\n\n"

                        fc_done_event = {"type": "response.output_item.done", "item": fc_item}
                        yield f"event: response.output_item.done\ndata: {json.dumps(fc_done_event)}\n\n"
                        function_call_items.append(fc_item)

        if item_added:
            done_item_event = {"type": "response.output_item.done",
                               "item": {"type": "message", "role": "assistant",
                                       "content": [{"type": "output_text", "text": accumulated_text}]}}
            yield f"event: response.output_item.done\ndata: {json.dumps(done_item_event)}\n\n"

        output_items = []
        if accumulated_text:
            output_items.append({"type": "message", "role": "assistant",
                                 "content": [{"type": "output_text", "text": accumulated_text}]})
        output_items.extend(function_call_items)

        final_resp = {
            "id": response_id, "object": "response", "created_at": created_at,
            "status": "completed", "model": model,
            "output": output_items,
            "output_text": accumulated_text
        }
        if final_usage:
            final_resp["usage"] = final_usage
        session_store.store_response_cache(response_id, final_resp)

        # previous_response_id chaining fix — see _send(). Tool-call turns
        # are chained via their text output only, same documented limitation.
        if accumulated_text:
            session_store.store_response_messages(
                response_id, chain_messages + [{"role": "assistant", "content": accumulated_text}]
            )
        else:
            session_store.store_response_messages(response_id, chain_messages)

        # Fix: was 'response.done', which is not a real Responses API event —
        # real clients listen for 'response.completed' and would never see
        # the stream as finished. See docstring.
        completed_event = {"type": "response.completed", "response": final_resp}
        yield f"event: response.completed\ndata: {json.dumps(completed_event)}\n\n"

    return Response(generate(), content_type='text/event-stream',
                    headers={'X-Response-ID': response_id})



def handle_get(response_id: str, session_store: SessionStore) -> Response:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Handles GET /v1/responses/{id} — retrieve a stored or background response.

    Parameters: response_id (str): The response ID to retrieve.
                session_store (SessionStore): Shared session store.
    Returns: Response — JSON response object, or 404 if not found.

    GSK does not expose this natively (confirmed: GET /interactions/{id}
    returns 404). This implements spec section 4.10 Option A: in-memory cache,
    populated by _send()/_stream() on every successful completion. Lost on
    proxy restart -- documented limitation.
    """
    cached = session_store.get_response_cache(response_id)
    if cached is None:
        return _error_response(404, f"Response '{response_id}' not found. "
                                    f"It may have expired or never existed "
                                    f"(in-memory cache is lost on proxy restart).",
                               "NOT_FOUND")
    return Response(json.dumps(cached), status=200, content_type='application/json')


def handle_delete(response_id: str, session_store: SessionStore) -> Response:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Handles DELETE /v1/responses/{id} — remove a stored response from cache.

    Parameters: response_id (str): The response ID to delete.
                session_store (SessionStore): Shared session store.
    Returns: Response — JSON {deleted: true} on success, 404 if not found.
    """
    existed = session_store.delete_response_cache(response_id)
    if not existed:
        return _error_response(404, f"Response '{response_id}' not found.", "NOT_FOUND")
    return Response(json.dumps({"id": response_id, "object": "response", "deleted": True}),
                    status=200, content_type='application/json')


def handle_input_items(response_id: str, session_store: SessionStore) -> Response:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Handles GET /v1/responses/{id}/input_items — list input items for a
    stored response.

    Parameters: response_id (str): The response ID.
                session_store (SessionStore): Shared session store.
    Returns: Response — JSON paginated list, or 404 if response not found.

    NOTE: Current implementation reconstructs only output items, not the
    original input items, since input is not separately cached in the
    response object (spec section 4.10 stores only the translated output).
    Returns an empty list with has_more=false as a safe minimal implementation.
    """
    cached = session_store.get_response_cache(response_id)
    if cached is None:
        return _error_response(404, f"Response '{response_id}' not found.", "NOT_FOUND")
    return Response(json.dumps({"object": "list", "data": [], "has_more": False}),
                    status=200, content_type='application/json')


def handle_compact(raw_body: bytes, token_manager, session_store: SessionStore) -> Response:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Handles POST /v1/responses/compact — context compaction for long
    multi-turn conversations.

    Parameters: raw_body (bytes): Raw request body containing the input array.
                token_manager (TokenManager): Shared token manager.
                session_store (SessionStore): Shared session store.
    Returns: Response — JSON with a condensed conversation history.

    Compaction strategy (spec section 4.10): if the supplied history has fewer
    than 10 turns, return it unchanged. Otherwise summarise via a Gemini
    generateContent call with a summarisation prompt and return the
    condensed history as a single text turn.
    """
    token = token_manager.get_token()

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return _error_response(400, "Invalid JSON in request body.")

    input_val = payload.get('input', [])
    if not isinstance(input_val, list):
        return _error_response(400, "compact requires 'input' to be an array of turns.")

    if len(input_val) < 10:
        # Short history — no compaction needed
        return Response(json.dumps({"input": input_val, "compacted": False}),
                        status=200, content_type='application/json')

    # Build a summarisation request
    history_text = "\n".join(
        f"{item.get('role', 'user')}: {item.get('content', '')}"
        for item in input_val if isinstance(item.get('content'), str)
    )
    summarise_body = {
        "contents": [{"role": "user", "parts": [{
            "text": ("Summarise the following conversation history concisely, "
                     "preserving all facts, decisions, and context needed to "
                     "continue the conversation:\n\n" + history_text)
        }]}],
        "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.0}
    }

    target_path  = normalize_model(f"models/{config.ALLOWED_MODEL}:generateContent")
    target_url   = build_target_url(target_path, '')
    headers      = build_headers(token, {})

    try:
        upstream = requests.post(
            target_url, headers=headers, json=summarise_body,
            timeout=(config.UPSTREAM_CONNECT_TIMEOUT, config.UPSTREAM_READ_TIMEOUT)
        )
    except requests.exceptions.RequestException as e:
        return _error_response(504, f"Upstream request failed: {str(e)}", "DEADLINE_EXCEEDED")

    if upstream.status_code != 200:
        return Response(upstream.content, status=upstream.status_code,
                        content_type='application/json')

    gemini_json = upstream.json()
    summary_text = ""
    for cand in gemini_json.get('candidates', []):
        for part in cand.get('content', {}).get('parts', []):
            if 'text' in part:
                summary_text += part['text']

    compacted_input = [{"role": "user", "content": summary_text}]
    return Response(json.dumps({"input": compacted_input, "compacted": True,
                                "original_turn_count": len(input_val)}),
                    status=200, content_type='application/json')
