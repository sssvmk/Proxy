# BEFORE MODIFYING THIS FILE: Read RULES.md in the project root and follow all rules. No exceptions.
"""
Module: handlers.openai_handler
Purpose: Processes requests from OpenAI-compatible clients, performing full translation.
Layer Architecture: Layer 2, 3, 4, 5, 6, 7.
Contracts Served: Contract 1.
Dependencies: json, time, uuid, hashlib, requests, flask, config, transforms.path, translate, session.
"""
import json
import time
import uuid
import hashlib
import requests
from flask import request, Response
import config
from transforms.path import normalize_model, build_target_url, build_headers
from translate.request import openai_to_gemini, TranslationError
from translate.response import gemini_to_openai, translate_response_chunk
from session import SessionStore


def _derive_session_id(openai_payload: dict) -> str:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Derives a stable session ID from the system message content.

    The system message is constant within a single Hermes conversation but
    different across agent instances and configurations. Hashing it gives a
    deterministic, stable session key that survives token refreshes and does
    not require client-side header echoing.

    Falls back to a random UUID if no system message is present.

    Parameters: openai_payload (dict): Parsed OpenAI request body.
    Returns: str — stable session ID for this conversation.
    Side effects: None.
    """
    for msg in openai_payload.get('messages', []):
        if msg.get('role') in ('system', 'developer'):
            content = msg.get('content', '')
            # OA15 fix: content can be a list when system message uses
            # list content blocks (WORK-04). Extract text before hashing.
            if isinstance(content, list):
                content = ' '.join(
                    item.get('text', '') for item in content
                    if isinstance(item, dict) and item.get('type') == 'text'
                )
            if content and isinstance(content, str):
                h = hashlib.sha256(content.encode('utf-8')).hexdigest()[:32]
                return f"sys_{h}"
    # No system message — try X-Session-ID header next
    header_sid = request.headers.get('X-Session-ID')
    if header_sid:
        return header_sid

    # OA9 fix: no system message and no header — derive stable session ID
    # from the first user message so that multi-step tool call conversations
    # (step 1 → step 2 → ...) share the same session and the proxy can
    # reattach thoughtSignatures stored in step 1 when processing step 2.
    # Without this each request gets a random UUID and the thoughtSignature
    # stored in step 1 is never found in step 2 → GSK rejects step 2 (400).
    for msg in openai_payload.get('messages', []):
        if msg.get('role') == 'user':
            content = msg.get('content', '')
            if isinstance(content, list):
                content = ' '.join(
                    item.get('text', '') for item in content
                    if isinstance(item, dict) and item.get('type') == 'text'
                )
            if content and isinstance(content, str):
                h = hashlib.sha256(content.encode('utf-8')).hexdigest()[:32]
                return f'usr_{h}'

    # Final fallback — truly stateless request, no stable anchor available
    return str(uuid.uuid4())


def _send(gemini_payload: dict, target_url: str, headers: dict,
          openai_payload: dict, session_id: str, session_store: SessionStore) -> Response:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Dispatches non-streaming OpenAI translated requests upstream and translates the response back.

    Parameters:
      gemini_payload (dict): Translated body for upstream.
      target_url (str): Destination upstream URL.
      headers (dict): Prepared request headers.
      openai_payload (dict): Original request from the client.
      session_id (str): Associated session identifier.
      session_store (SessionStore): The global store.
    Returns: Response (Flask response object)
    """
    try:
        # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
        resp = requests.post(
            target_url,
            headers=headers,
            data=json.dumps(gemini_payload),
            timeout=(config.UPSTREAM_CONNECT_TIMEOUT, config.UPSTREAM_READ_TIMEOUT)
        )
        print(f"[{time.strftime('%H:%M:%S')}] ← GSK status: {resp.status_code}")

        if resp.status_code >= 400:
            print(f"  ❌ Error body: {resp.text[:1000]}")
            return Response(resp.content, status=resp.status_code,
                            content_type='application/json')

        gemini_body = resp.json()
        openai_body = gemini_to_openai(
            gemini_body,
            session_id,
            session_store,
            original_model=openai_payload.get('model', config.ALLOWED_MODEL),
            streaming=False
        )

        response = Response(json.dumps(openai_body), status=200, content_type='application/json')
        response.headers['X-Session-ID'] = session_id
        return response

    except requests.exceptions.Timeout:
        return Response(json.dumps({"error": {"code": 504, "message": "Upstream timeout"}}),
                        status=504, content_type='application/json')
    except Exception as e:
        return Response(json.dumps({"error": {"code": 502, "message": str(e)}}),
                        status=502, content_type='application/json')


def _stream(gemini_payload: dict, target_url: str, headers: dict,
            openai_payload: dict, session_id: str, session_store: SessionStore) -> Response:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Dispatches streaming OpenAI translated requests upstream, translates chunks, and ensures [DONE].

    Parameters: gemini_payload (dict), target_url (str), headers (dict),
                openai_payload (dict), session_id (str), session_store (SessionStore)
    Returns: Response (Streaming Flask response)
    Enforces R1, R8.
    """
    try:
        # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
        # R1: stream=True on all streaming requests.post() calls
        resp = requests.post(
            target_url,
            headers=headers,
            data=json.dumps(gemini_payload),
            stream=True,
            timeout=(config.UPSTREAM_CONNECT_TIMEOUT, config.UPSTREAM_READ_TIMEOUT)
        )
        print(f"[{time.strftime('%H:%M:%S')}] ← GSK status: {resp.status_code}")

        if resp.status_code >= 400:
            print(f"  ❌ Error body: {resp.text[:1000]}")
            return Response(resp.content, status=resp.status_code,
                            content_type='application/json')

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created       = int(time.time())
        model_name    = openai_payload.get('model', config.ALLOWED_MODEL)

        # Tier 2 fix: OpenAI's stream_options.include_usage is opt-in (default
        # False) — a dedicated usage-only chunk (empty choices[]) should only
        # be emitted if the client explicitly asked for it. Previously this
        # chunk was always synthesized whenever Gemini sent usageMetadata,
        # regardless of client request, silently ignoring the opt-out case.
        include_usage = bool(
            openai_payload.get('stream_options', {}).get('include_usage', False)
        )

        def generate():
            """
            BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
            Generator that yields streaming chunks from upstream, translates them,
            and appends the required OpenAI sentinel.
            Enforces R1 by reading and yielding chunks interactively.
            Enforces R8 by synthesizing the final [DONE] message.
            """
            for line in resp.iter_lines():
                # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
                if not line:
                    continue
                text = line.decode('utf-8')
                if not text.startswith('data: '):
                    continue
                json_str = text[6:]
                try:
                    gemini_chunk = json.loads(json_str)
                except Exception:
                    continue

                openai_chunk = translate_response_chunk(
                    gemini_chunk, completion_id, created, model_name,
                    session_id, session_store
                )
                if openai_chunk is None:
                    continue

                # Suppress the dedicated usage-only chunk (choices == []) unless
                # the client opted in via stream_options.include_usage.
                if not openai_chunk.get('choices') and 'usage' in openai_chunk and not include_usage:
                    continue

                yield f"data: {json.dumps(openai_chunk)}\n\n"

            # R8: data: [DONE]\n\n synthesized after last Gemini streaming chunk
            yield "data: [DONE]\n\n"
            print(f"[{time.strftime('%H:%M:%S')}] ✅ Stream complete, [DONE] sent")

        return Response(generate(),
                        status=200,
                        content_type='text/event-stream',
                        headers={
                            'X-Accel-Buffering': 'no',
                            'Cache-Control': 'no-cache',
                            'X-Session-ID': session_id
                        })

    except requests.exceptions.Timeout:
        return Response(json.dumps({"error": {"code": 504, "message": "Upstream timeout"}}),
                        status=504, content_type='application/json')
    except Exception as e:
        return Response(json.dumps({"error": {"code": 502, "message": str(e)}}),
                        status=502, content_type='application/json')


def handle(raw_body: bytes, token_manager, session_store: SessionStore) -> Response:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Main entry point for OpenAI compatible routes.

    Parameters: raw_body (bytes), token_manager (TokenManager), session_store (SessionStore)
    Returns: Response
    Enforces R14.
    """
    # R14: get_token() is first call in every handler — never cached across requests
    token = token_manager.get_token()

    try:
        openai_payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return Response(json.dumps({"error": {"message": "Invalid JSON body"}}),
                        status=400, content_type='application/json')

    # Derive stable session ID from system message hash — survives token refresh,
    # requires no client-side header echoing (Issue 3 fix)
    session_id = _derive_session_id(openai_payload)
    print(f"[{time.strftime('%H:%M:%S')}] 🔵 OpenAI client | session={session_id}")

    is_streaming = openai_payload.get('stream', False)

    # Layer 3 + 4 — Session read + Translation
    # TranslationError is raised by openai_to_gemini() for incompatible inputs
    # (e.g. https:// image URIs blocked by VPCSC — R18). Return 400 to client.
    try:
        gemini_payload, target_path = openai_to_gemini(openai_payload, session_id, session_store)
    except TranslationError as e:
        print(f"  ❌ TranslationError: {e.message}")
        return Response(
            json.dumps({"error": {"code": 400, "message": e.message,
                                  "status": e.status}}),
            status=400, content_type='application/json'
        )

    target_path = normalize_model(target_path)
    query = 'alt=sse' if is_streaming else ''
    target_url = build_target_url(target_path, query)
    headers = build_headers(token, request.headers)

    print(f"[{time.strftime('%H:%M:%S')}] 🚀 Forwarding to: {target_url}")

    if is_streaming:
        return _stream(gemini_payload, target_url, headers,
                       openai_payload, session_id, session_store)
    else:
        return _send(gemini_payload, target_url, headers,
                     openai_payload, session_id, session_store)
