# BEFORE MODIFYING THIS FILE: Read RULES.md in the project root and follow all rules. No exceptions.
"""
Module: handlers.gemini_handler
Purpose: Processes requests from native Gemini clients, performing auth swap and session repair.
Layer Architecture: Layer 2, 3, 5, 6, 7.
Contracts Served: Contract 2, 3.
Dependencies: json, time, uuid, requests, flask, config, transforms.path, session.

 GATEWAY NOTE: The  Kong gateway runs a pre-Gemini-3 schema that does not
recognise the 'id' field on functionCall or functionResponse objects. The
_strip_function_ids() function removes these before forwarding while preserving
them in session for thoughtSignature reconstruction.

 VPCSC NOTE: fileData with http/https URIs are permanently blocked by VPC
Service Controls. _scan_filedata_uris() pre-flight rejects these before forwarding.
gs:// URIs are routable. (R18)

SPEC-CODE SYNC: proxy_v2_spec.md v3.0 §4.9, §9 WORK-06
"""
import json
import time
import uuid
import requests
from flask import request, Response
import config
from transforms.path import normalize_model, build_target_url, build_headers
from session import SessionStore



def _scan_filedata_uris(raw_body: bytes) -> str | None:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Scans contents[].parts[] for fileData parts with http:// or https:// URIs.
     VPCSC permanently blocks these — they must be rejected at the proxy
    before forwarding, not passed to gs:// URIs are safe to forward.

    Parameters: raw_body (bytes): The raw request body.
    Returns: str — error message if a blocked URI is found, None if safe to forward.
    Enforces R18.
    """
    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, Exception):
        return None  # Let the upstream handle malformed JSON

    for turn in body.get('contents', []):
        for part in turn.get('parts', []):
            uri = part.get('fileData', {}).get('fileUri', '')
            if uri.startswith('http://') or uri.startswith('https://'):
                return (
                    f"fileData HTTP/HTTPS URIs are blocked by  VPC Service Controls. "
                    f"Use inlineData (base64) or a GCS gs:// URI instead. "
                    f"Blocked URI: {uri[:80]}"
                )
    return None


def _strip_function_ids(body: dict) -> dict:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Strips 'id' field from all functionCall and functionResponse parts before
    forwarding to the  gateway, which runs a schema that does not accept it.

    Parameters: body (dict): Parsed request body.
    Returns: dict (modified body — same object, mutated in place)
    Side effects: Mutates parts in body['contents'] in place.

    This applies to both camelCase (functionCall/functionResponse) and
    snake_case (function_call/function_response) field names.
    """
    for turn in body.get('contents', []):
        for part in turn.get('parts', []):
            # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
            for key in ('functionCall', 'function_call'):
                if key in part and 'id' in part[key]:
                    part[key].pop('id')
            for key in ('functionResponse', 'function_response'):
                if key in part and 'id' in part[key]:
                    part[key].pop('id')
    return body


def _repair_thought_signatures(raw_body: bytes, session_id: str,
                                session_store: SessionStore) -> bytes:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Validates and silently repairs missing thoughtSignatures from the session
    before forwarding upstream. Also strips 'id' fields for  gateway
    compatibility.

    Parameters: raw_body (bytes), session_id (str), session_store (SessionStore)
    Returns: bytes (The repaired and sanitised JSON body)
    Enforces R3, R4, R5.
    """
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        print("  ⚠️ Warning: Could not parse body JSON, skipping signature repair")
        return raw_body

    if 'contents' not in body:
        return raw_body

    for turn in body['contents']:
        # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
        if turn.get('role') != 'model':
            continue
        if 'parts' not in turn:
            continue

        for j, part in enumerate(turn['parts']):
            # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
            if 'functionCall' not in part:
                continue

            call_id = part['functionCall'].get('id')
            if not call_id:
                continue

            has_sig = 'thoughtSignature' in part or 'thought_signature' in part
            meta = session_store.get_tool_call_meta(session_id, call_id)

            if has_sig:
                # Client sent a signature — store it if not already in session
                if not meta or meta.thought_signature != (
                        part.get('thoughtSignature') or part.get('thought_signature')):
                    from session import ToolCallMeta
                    sig_val = part.get('thoughtSignature') or part.get('thought_signature')
                    session_entry = session_store.get_or_create(session_id)
                    session_store.store_tool_call_meta(session_id, call_id, ToolCallMeta(
                        name=part['functionCall'].get('name', ''),
                        tool_type=part.get('tool_type'),
                        thought_signature=sig_val,
                        part_position=j,
                        step=session_entry.current_turn_step
                    ))
            else:
                if meta and meta.thought_signature:
                    # R3: inject at exact part_position received
                    # R4: parallel calls — inject on first part only (j == 0)
                    # R5: sequential calls — inject on each step's first part (meta.step > 0)
                    if j == 0 or meta.step > 0:
                        part['thoughtSignature'] = meta.thought_signature
                        print(f"  🔗 Repaired thoughtSignature for call_id={call_id} at part_position={j}")
                else:
                    # No session data — inject bypass only at expected position
                    expected_position = meta.part_position if meta else 0
                    if j == expected_position:
                        part['thoughtSignature'] = "skip_thought_signature_validator"
                        print(f"  ⚠️ No thoughtSignature in session for call_id={call_id} — injected bypass value")

    # Strip 'id' fields for GSK gateway compatibility
    body = _strip_function_ids(body)

    return json.dumps(body).encode('utf-8')


def _store_signatures_from_response(body: dict, session_id: str,
                                     session_store: SessionStore) -> None:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Stores thoughtSignatures from upstream responses into the session.

    Parameters: body (dict), session_id (str), session_store (SessionStore)
    Returns: None
    Enforces R2, R13.
    """
    from session import ToolCallMeta
    candidates = body.get('candidates', [])
    if not candidates:
        return

    candidate = candidates[0]
    content = candidate.get('content', {})
    parts = content.get('parts', [])

    session_entry = session_store.get_or_create(session_id)
    step = session_entry.current_turn_step
    tool_calls_found = False
    server_parts = []

    for i, part in enumerate(parts):
        # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
        if 'executableCode' in part or 'codeExecutionResult' in part:
            # R13: Server-side tool parts stored in session and re-injected on next model turn
            server_parts.append(part)
        elif 'functionCall' in part:
            tool_calls_found = True
            call_id = part['functionCall'].get('id')
            if not call_id:
                # Gateway stripped the id — generate synthetic one for session keying
                call_id = f"call_{uuid.uuid4().hex[:8]}"
            name = part['functionCall'].get('name', '')
            thought_sig = part.get('thoughtSignature') or part.get('thought_signature')
            tool_type = part.get('tool_type')

            # R2: thoughtSignature stored in session on every Gemini response containing functionCall
            session_store.store_tool_call_meta(session_id, call_id, ToolCallMeta(
                name=name,
                tool_type=tool_type,
                thought_signature=thought_sig,
                part_position=i,
                step=step
            ))
            print(f"  💾 Stored thoughtSignature for call_id={call_id}")

    if tool_calls_found:
        session_store.increment_step(session_id)
    if server_parts:
        session_store.store_server_side_parts(session_id, server_parts)


def _send(target_url: str, headers: dict, body: bytes,
          session_id: str, session_store: SessionStore) -> Response:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Dispatches non-streaming requests upstream.

    Parameters: target_url (str), headers (dict), body (bytes),
                session_id (str), session_store (SessionStore)
    Returns: Response (Flask response object)
    Enforces R2 via _store_signatures_from_response.
    """
    try:
        # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
        resp = requests.post(
            target_url,
            headers=headers,
            data=body,
            timeout=(config.UPSTREAM_CONNECT_TIMEOUT, config.UPSTREAM_READ_TIMEOUT)
        )
        print(f"[{time.strftime('%H:%M:%S')}] ← GSK status: {resp.status_code}")

        if resp.status_code >= 400:
            print(f"  ❌ Error body: {resp.text[:1000]}")
            return Response(resp.content, status=resp.status_code,
                            content_type=resp.headers.get('Content-Type', 'application/json'))

        try:
            resp_json = resp.json()
            _store_signatures_from_response(resp_json, session_id, session_store)
        except json.JSONDecodeError:
            pass

        response = Response(resp.content, status=resp.status_code,
                            content_type=resp.headers.get('Content-Type', 'application/json'))
        response.headers['X-Session-ID'] = session_id
        return response

    except requests.exceptions.Timeout:
        return Response(json.dumps({"error": {"code": 504, "message": "Upstream timeout"}}),
                        status=504, content_type='application/json')
    except Exception as e:
        return Response(json.dumps({"error": {"code": 502, "message": str(e)}}),
                        status=502, content_type='application/json')


def _stream(target_url: str, headers: dict, body: bytes,
            session_id: str, session_store: SessionStore) -> Response:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Dispatches streaming requests upstream and yields chunks.
    Also parses chunks read-only to store thoughtSignatures into session (R2).

    Parameters: target_url (str), headers (dict), body (bytes),
                session_id (str), session_store (SessionStore)
    Returns: Response (Streaming Flask response)
    Enforces R1, R2.
    """
    try:
        # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
        # R1: stream=True on all streaming requests.post() calls
        resp = requests.post(
            target_url,
            headers=headers,
            data=body,
            stream=True,
            timeout=(config.UPSTREAM_CONNECT_TIMEOUT, config.UPSTREAM_READ_TIMEOUT)
        )
        print(f"[{time.strftime('%H:%M:%S')}] ← GSK status: {resp.status_code}")

        if resp.status_code >= 400:
            print(f"  ❌ Error body: {resp.text[:1000]}")
            return Response(resp.content, status=resp.status_code,
                            content_type=resp.headers.get('Content-Type', 'application/json'))

        def generate():
            """
            BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
            Generator that yields streaming chunks from the upstream response.
            Parses each SSE chunk read-only to extract and store thoughtSignatures.
            Enforces R1: yields chunks without buffering.
            Enforces R2: stores thoughtSignatures from each chunk as it passes through.
            """
            for chunk in resp.iter_content(chunk_size=None):
                if chunk:
                    # R2: Parse chunk to store thoughtSignatures — read-only, does not modify forwarded bytes
                    try:
                        text = chunk.decode('utf-8')
                        for line in text.splitlines():
                            if line.startswith('data: '):
                                json_str = line[6:]
                                try:
                                    parsed = json.loads(json_str)
                                    _store_signatures_from_response(
                                        parsed, session_id, session_store)
                                except (json.JSONDecodeError, Exception):
                                    pass
                    except Exception:
                        pass
                    # R1: Yield original chunk unmodified
                    yield chunk
            print(f"[{time.strftime('%H:%M:%S')}] ✅ Native Gemini stream complete")

        return Response(generate(),
                        status=resp.status_code,
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


def handle(path: str, raw_body: bytes, token_manager,
           session_store: SessionStore) -> Response:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Main entry point for native Gemini routes.

    Parameters: path (str), raw_body (bytes), token_manager (TokenManager),
                session_store (SessionStore)
    Returns: Response
    Enforces R14.
    """
    # R14: get_token() is first call in every handler — never cached across requests
    token = token_manager.get_token()

    session_id = request.headers.get('X-Session-ID') or str(uuid.uuid4())
    print(f"[{time.strftime('%H:%M:%S')}] 🟢 Native Gemini client | session={session_id}")

    path = normalize_model(path)
    target_url = build_target_url(
        path, request.query_string.decode('utf-8') if request.query_string else '')
    headers = build_headers(token, request.headers)

    # R18: Pre-flight fileData URI scan — reject http/https before forwarding 
    # VPCSC permanently blocks these. gs:// URIs are allowed through.
    _uri_err = _scan_filedata_uris(raw_body)
    if _uri_err:
        print(f"  ❌ R18 fileData URI blocked: {_uri_err[:80]}")
        return Response(
            json.dumps({"error": {"code": 400, "message": _uri_err,
                                  "status": "INVALID_ARGUMENT"}}),
            status=400, content_type='application/json'
        )

    # Layer 3 — Session repair + id stripping (Contract 3)
    body = _repair_thought_signatures(raw_body, session_id, session_store)

    # thinkingConfig: inject LOW thinking level for native Gemini clients
    # (Contracts 2 & 3) when the client has not already set it.
    # Same rationale as Contract 1 — gemini-3.1-pro-preview thinking model.
    # Constraint: thinkingLevel and thinkingBudget cannot coexist.
    try:
        _body_obj = json.loads(body)
        _tc = _body_obj.get('generationConfig', {}).get('thinkingConfig', {})
        if not _tc.get('thinkingLevel') and not _tc.get('thinkingBudget'):
            _body_obj.setdefault('generationConfig', {})['thinkingConfig'] = {
                'thinkingLevel': 'LOW'
            }
            body = json.dumps(_body_obj).encode('utf-8')
    except Exception:
        pass  # Malformed JSON — let  return the error

    print(f"[{time.strftime('%H:%M:%S')}] 🚀 Forwarding to: {target_url}")

    is_streaming = ':streamGenerateContent' in path

    if is_streaming:
        return _stream(target_url, headers, body, session_id, session_store)
    else:
        return _send(target_url, headers, body, session_id, session_store)
