# BEFORE MODIFYING THIS FILE: Read RULES.md in the project root and follow all rules. No exceptions.
"""
Module: translate.request
Purpose: Translates OpenAI Chat Completions requests into Gemini REST requests.
Layer Architecture: Layer 4 (Translation - Request).
Contracts Served: Contract 1 (OpenAI Chat Completions).
Dependencies: json, session.SessionStore.

 GATEWAY NOTE: The Kong gateway runs a pre-Gemini-3 schema that does
not recognise the 'id' field on functionCall or functionResponse objects.
The 'id' is stripped before forwarding but preserved in SessionStore so the
proxy can maintain tool_call_id <-> name <-> thoughtSignature mapping internally.

 VPCSC NOTE: The  Kong gateway blocks all fileData with http:// or
https:// URIs via VPC Service Controls. Such URIs must be rejected at the
proxy with HTTP 400. data: URIs must be converted to inlineData. gs:// URIs
are routable and forwarded as fileData. (R18)

SPEC-CODE SYNC: proxy_v2_spec.md v3.0 §4.6, §9 WORK-01 to WORK-04
"""
import json
from session import SessionStore


class TranslationError(Exception):
    """
    BEFORE MODIFYING THIS CLASS: Read RULES.md in the project root and follow all rules. No exceptions.
    Raised by openai_to_gemini() when a request cannot be translated due to
    a  constraint or unsupported feature. The message is returned as a
    400 response body to the client by openai_handler.handle().

    Attributes:
        message (str): Human-readable error description for the client.
        status  (str): Gemini-style status string (e.g. "INVALID_ARGUMENT").
    """
    def __init__(self, message: str, status: str = "INVALID_ARGUMENT"):
        super().__init__(message)
        self.message = message
        self.status  = status


def _translate_image_url_item(item: dict) -> dict:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Converts an OpenAI image_url content item into the correct Gemini part.
    Enforces R18: rejects http/https fileData URIs, converts data: URIs to
    inlineData, forwards gs:// URIs as fileData.

    Parameters: item (dict): OpenAI content item with type == "image_url".
    Returns: dict — Gemini part (inlineData or fileData).
    Raises: TranslationError on http/https URIs or unrecognised schemes.
    """
    url  = item.get('image_url', {}).get('url', '')
    detail = item.get('image_url', {}).get('detail', 'auto')  # low/high/auto/original

    if url.startswith('data:'):
        # data:image/png;base64,ABC123  ->  inlineData
        # R18: data: URIs become inlineData — confirmed working by M2-M15 tests
        try:
            header, b64data = url.split(',', 1)
            mime = header.split(';')[0].split(':')[1]  # e.g. "image/png"
        except (ValueError, IndexError):
            raise TranslationError(
                f"Malformed data: URI in image_url — could not extract mimeType.",
                "INVALID_ARGUMENT"
            )
        return {"inlineData": {"mimeType": mime, "data": b64data}}

    elif url.startswith('gs://'):
        # R18: gs:// URIs forwarded as fileData —  routes GCS (confirmed M14)
        mime = item.get('image_url', {}).get('mime_type', 'image/jpeg')
        return {"fileData": {"fileUri": url, "mimeType": mime}}

    elif url.startswith('http://') or url.startswith('https://'):
        # R18: VPCSC permanent block — confirmed M14 test
        raise TranslationError(
            "fileData HTTP/HTTPS URIs are blocked by  VPC Service Controls. "
            "Use base64 inlineData (data: URI) or a GCS gs:// URI instead.",
            "INVALID_ARGUMENT"
        )

    else:
        raise TranslationError(
            f"Unrecognised image URI scheme (expected data:, gs://, not http/https): "
            f"{url[:60]}",
            "INVALID_ARGUMENT"
        )


# Keys that are valid JSON Schema but are not recognised by Gemini's
# OpenAPI-3.0-subset function-parameter parser.  returns a 400
# ("Unknown name ... Cannot find field") if these are present anywhere
# in the schema tree, including nested under properties/items.
_UNSUPPORTED_SCHEMA_KEYS = frozenset({
    '$schema', '$id', '$defs', 'definitions', '$comment',
    'additionalProperties', 'unevaluatedProperties',
})


def _sanitize_json_schema(schema):
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Recursively rewrites a JSON Schema (Draft 2020-12 style, as commonly
    emitted by zod-to-json-schema / ai-sdk tool generators) into the
    restricted OpenAPI-3.0 subset Gemini's functionDeclarations.parameters
    accepts. Applied to every tool's `parameters` object before forwarding.

    Handles:
      - Strips $schema/$id/$defs/definitions/$comment/additionalProperties
        (Gemini's parser rejects unknown field names outright — GAP fix,
        see opencode $schema / exclusiveMinimum 400 errors).
      - Converts numeric `exclusiveMinimum`/`exclusiveMaximum` (JSON Schema
        2020-12 form) into `minimum`/`maximum` (Gemini/OpenAPI 3.0 form).
        This is a best-effort, slightly lossy conversion (boundary value
        becomes inclusive) — acceptable for LLM tool-parameter guidance,
        which is advisory rather than a strict validator.
      - Recurses into properties, items, and the schema-composition
        keywords (anyOf/oneOf/allOf) so nested tool schemas are cleaned too.

    Parameters: schema (dict | list | Any): A JSON Schema fragment.
    Returns: The sanitized schema, same shape, safe for  forwarding.
    Raises: None.
    Side effects: None — returns a new structure, does not mutate input.
    """
    if isinstance(schema, list):
        return [_sanitize_json_schema(item) for item in schema]

    if not isinstance(schema, dict):
        return schema

    cleaned = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED_SCHEMA_KEYS:
            continue

        if key == 'exclusiveMinimum' and isinstance(value, (int, float)):
            # Draft 2020-12 numeric form → OpenAPI 3.0 inclusive `minimum`.
            # Only convert if the schema hasn't already set `minimum` itself.
            cleaned.setdefault('minimum', value)
            continue

        if key == 'exclusiveMaximum' and isinstance(value, (int, float)):
            cleaned.setdefault('maximum', value)
            continue

        if key in ('properties', 'patternProperties') and isinstance(value, dict):
            cleaned[key] = {k: _sanitize_json_schema(v) for k, v in value.items()}
        elif key in ('items', 'not') and isinstance(value, (dict, list)):
            cleaned[key] = _sanitize_json_schema(value)
        elif key in ('anyOf', 'oneOf', 'allOf') and isinstance(value, list):
            cleaned[key] = [_sanitize_json_schema(v) for v in value]
        else:
            cleaned[key] = value

    return cleaned


def openai_to_gemini(
    openai_payload: dict,
    session_id: str,
    session_store: SessionStore
) -> tuple[dict, str]:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Converts OpenAI payload format into Gemini REST schema.

    Parameters:
      openai_payload (dict): Parsed JSON from the client request.
      session_id (str): Current session identifier.
      session_store (SessionStore): The global session store instance.

    Returns: tuple[dict, str] (The translated gemini body dict, the target path suffix string)
    Raises: TranslationError on unsupported URI schemes or untranslatable content.
    Enforces R3, R4, R5, R9, R13, R18.
    Side effects: Reads from SessionStore, potentially clears turn state.

     gateway constraint: 'id' field is stripped from functionCall and
    functionResponse before forwarding. call_id is stored in session only.

     VPCSC constraint: image_url with http/https raises TranslationError (R18).
    data: URIs converted to inlineData. gs:// URIs forwarded as fileData.
    """
    # ── WORK-03: Strip Responses API fields —  rejects these with 400 ────
    # store, previous_response_id, background, input are Interactions/Responses
    # API fields not supported by the  generateContent backend.
    for _field in ('store', 'previous_response_id', 'background', 'metadata',
                   'include', 'truncation', 'reasoning', 'input'):
        openai_payload.pop(_field, None)

    gemini_body = {}
    contents = []

    # ── Map basic generation config fields ────────────────────────────────────
    if 'max_tokens' in openai_payload:
        gemini_body.setdefault('generationConfig', {})['maxOutputTokens'] = openai_payload['max_tokens']
    if 'temperature' in openai_payload:
        gemini_body.setdefault('generationConfig', {})['temperature'] = openai_payload['temperature']
    if 'top_p' in openai_payload:
        gemini_body.setdefault('generationConfig', {})['topP'] = openai_payload['top_p']

    # Gap 1 fix: stop → stopSequences
    # OpenAI 'stop' accepts a string or list of strings; Gemini requires a list.
    # Affects test OA3. No existing behaviour changed — field was previously ignored.
    if 'stop' in openai_payload:
        stop_val = openai_payload['stop']
        gemini_body.setdefault('generationConfig', {})['stopSequences'] = (
            stop_val if isinstance(stop_val, list) else [stop_val]
        )

    # Gap 2 fix: response_format → generationConfig.responseMimeType / responseSchema
    # OpenAI Chat Completions path was missing this mapping.
    # The Responses API path in responses_handler.py already handles text.format.
    # Affects test OA13. No existing behaviour changed — field was previously ignored.
    if 'response_format' in openai_payload:
        fmt = openai_payload['response_format']
        fmt_type = fmt.get('type', '')
        if fmt_type == 'json_object':
            gemini_body.setdefault('generationConfig', {})['responseMimeType'] = 'application/json'
        elif fmt_type == 'json_schema':
            schema = fmt.get('json_schema', {}).get('schema')
            gemini_body.setdefault('generationConfig', {})['responseMimeType'] = 'application/json'
            if schema:
                gemini_body['generationConfig']['responseSchema'] = schema

    # Tier 1 fix: n>1 was previously silently accepted then truncated to a
    # single choice in translate/response.py (only candidates[0] is ever
    # read). That's silent data loss — a client requesting 3 completions
    # would get 1 with no indication anything was dropped. Gemini's
    # candidateCount is not wired into the session/thoughtSignature model
    # (which assumes exactly one active candidate's tool-call chain per
    # turn), so rather than mis-serve, reject explicitly and tell the
    # client why.
    if 'n' in openai_payload and openai_payload['n'] not in (None, 1):
        raise TranslationError(
            "This proxy does not support n>1 (multiple completions per request). "
            "Gemini's candidateCount is not wired into the session/thoughtSignature "
            "tracking model, which assumes a single active candidate. Send separate "
            "requests instead.",
            "INVALID_ARGUMENT"
        )

    # Tier 2 fix: seed → generationConfig.seed
    if 'seed' in openai_payload:
        gemini_body.setdefault('generationConfig', {})['seed'] = openai_payload['seed']

    # Tier 2 fix: presence_penalty / frequency_penalty → generationConfig equivalents
    if 'presence_penalty' in openai_payload:
        gemini_body.setdefault('generationConfig', {})['presencePenalty'] = openai_payload['presence_penalty']
    if 'frequency_penalty' in openai_payload:
        gemini_body.setdefault('generationConfig', {})['frequencyPenalty'] = openai_payload['frequency_penalty']

    # Tier 2 fix: logprobs / top_logprobs → generationConfig.responseLogprobs / logprobs
    # OpenAI: logprobs (bool) enables the feature, top_logprobs (int, 0-20) sets the count.
    # Gemini: responseLogprobs (bool) enables it, logprobs (int) sets the count.
    if openai_payload.get('logprobs'):
        gemini_body.setdefault('generationConfig', {})['responseLogprobs'] = True
        if 'top_logprobs' in openai_payload:
            gemini_body['generationConfig']['logprobs'] = openai_payload['top_logprobs']

    # Tier 2 fix: reasoning_effort → thinkingConfig.thinkingLevel
    # Distinct from the Responses-API 'reasoning' object (already stripped above).
    # Only applied if the client hasn't already set thinkingConfig directly —
    # the LOW-default injection later in this function respects this too
    # since it only fires when thinkingLevel/thinkingBudget are both absent.
    if 'reasoning_effort' in openai_payload:
        effort = str(openai_payload['reasoning_effort']).upper()
        if effort in ('LOW', 'MEDIUM', 'HIGH'):
            gemini_body.setdefault('generationConfig', {}).setdefault(
                'thinkingConfig', {})['thinkingLevel'] = effort
        else:
            print(f"  ⚠️  Unrecognised reasoning_effort '{openai_payload['reasoning_effort']}' — ignored")

    # ── WORK-02: Map tools — function + built-in types ────────────────────────
    # Previous code silently dropped non-function tools (GAP-01).
    # Now maps web_search, code_interpreter, file_search to Gemini equivalents.
    if 'tools' in openai_payload:
        func_decls = []
        for tool in openai_payload['tools']:
            t = tool.get('type', '')
            if t == 'function':
                # Unwrap OpenAI .function wrapper into Gemini functionDeclarations
                func = tool['function']
                func_decl = {"name": func['name']}
                if 'description' in func:
                    func_decl['description'] = func['description']
                if 'parameters' in func:
                    # Tier 1 fix: sanitize JSON-Schema-only keywords ($schema,
                    # exclusiveMinimum, etc.) that Gemini's OpenAPI-3.0-subset
                    # parser rejects with 400. Fixes opencode/ai-sdk tool defs.
                    func_decl['parameters'] = _sanitize_json_schema(func['parameters'])
                func_decls.append(func_decl)
            elif t == 'web_search':
                gemini_body.setdefault('tools', []).append({"google_search": {}})
                print(f"  🔧 Mapped web_search → google_search")
            elif t == 'code_interpreter':
                gemini_body.setdefault('tools', []).append({"code_execution": {}})
                print(f"  🔧 Mapped code_interpreter → code_execution")
            elif t == 'file_search':
                gemini_body.setdefault('tools', []).append({"retrieval": {}})
                print(f"  🔧 Mapped file_search → retrieval")
            else:
                # computer_use, image_generation, tool_search — no Gemini equivalent
                print(f"  ⚠️  Unsupported tool type '{t}' — skipped (no Gemini equivalent)")

        if func_decls:
            existing_tools = gemini_body.setdefault('tools', [])
            existing_tools.append({"functionDeclarations": func_decls})

    elif 'functions' in openai_payload:
        # Tier 1 fix: legacy pre-2023 OpenAI function-calling style. Clients
        # still using 'functions' instead of 'tools' were previously silently
        # dropped entirely — the request went upstream with zero tools and
        # the model had no way to call anything. Map to the same
        # functionDeclarations path as the modern 'tools' field.
        func_decls = []
        for func in openai_payload['functions']:
            func_decl = {"name": func['name']}
            if 'description' in func:
                func_decl['description'] = func['description']
            if 'parameters' in func:
                func_decl['parameters'] = _sanitize_json_schema(func['parameters'])
            func_decls.append(func_decl)
        if func_decls:
            gemini_body.setdefault('tools', []).append({"functionDeclarations": func_decls})
            print(f"  🔧 Mapped legacy 'functions' ({len(func_decls)}) → functionDeclarations")

    # ── Map tool_choice to Gemini functionCallingConfig ───────────────────────
    if 'tool_choice' in openai_payload:
        choice  = openai_payload['tool_choice']
        tc_mode = "AUTO"
        allowed = []
        if choice == "none":
            tc_mode = "NONE"
        elif choice == "required":
            tc_mode = "ANY"
        elif isinstance(choice, dict) and choice.get("type") == "function":
            tc_mode = "ANY"
            allowed.append(choice['function']['name'])
        gemini_body['toolConfig'] = {"functionCallingConfig": {"mode": tc_mode}}
        if allowed:
            gemini_body['toolConfig']['functionCallingConfig']['allowedFunctionNames'] = allowed

    elif 'function_call' in openai_payload:
        # Tier 1 fix: legacy counterpart to 'tool_choice'.
        choice  = openai_payload['function_call']
        tc_mode = "AUTO"
        allowed = []
        if choice == "none":
            tc_mode = "NONE"
        elif choice == "auto":
            tc_mode = "AUTO"
        elif isinstance(choice, dict) and 'name' in choice:
            tc_mode = "ANY"
            allowed.append(choice['name'])
        gemini_body['toolConfig'] = {"functionCallingConfig": {"mode": tc_mode}}
        if allowed:
            gemini_body['toolConfig']['functionCallingConfig']['allowedFunctionNames'] = allowed

    # ── Process messages into Gemini contents array ───────────────────────────
    messages = openai_payload.get('messages', [])
    consecutive_tool_messages = []

    for msg in messages:
        # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
        role    = msg.get('role')
        content = msg.get('content')

        # WORK-04: System / Developer — handle both string and list content
        # Previous code (GAP-03) silently skipped list content — systemInstruction never set.
        if role in ('system', 'developer'):
            if isinstance(content, str) and content:
                gemini_body['systemInstruction'] = {"parts": [{"text": content}]}
            elif isinstance(content, list):
                text_parts = [
                    {"text": item['text']}
                    for item in content
                    if item.get('type') == 'text' and item.get('text')
                ]
                if text_parts:
                    gemini_body['systemInstruction'] = {"parts": text_parts}
            continue

        # Buffer tool result messages — flushed as a single user turn (R17)
        # Tier 1 fix: legacy role:'function' messages (pre-tool_calls era) were
        # previously unhandled by any branch below and silently discarded,
        # losing the function result entirely. They use 'name' instead of
        # 'tool_call_id' — _flush_tool_messages() now handles both shapes.
        if role in ('tool', 'function'):
            consecutive_tool_messages.append(msg)
            continue

        # Non-tool message encountered — flush any buffered tool messages first
        if consecutive_tool_messages:
            _flush_tool_messages(consecutive_tool_messages, session_id, session_store, contents)
            consecutive_tool_messages = []

        # User message
        if role == 'user':
            # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
            parts = []
            if isinstance(content, str):
                # New user text turn — reset session turn state
                session_store.clear_turn(session_id)
                parts.append({"text": content})
            elif isinstance(content, list):
                session_store.clear_turn(session_id)
                for item in content:
                    if item.get('type') == 'text':
                        parts.append({"text": item.get('text', '')})
                    elif item.get('type') == 'image_url':
                        # WORK-01: R18 — URI-scheme router replaces hardcoded fileData
                        # Raises TranslationError on http/https (propagates to handler → 400)
                        parts.append(_translate_image_url_item(item))
                    elif item.get('type') == 'image':
                        # OpenAI Responses API image block with inlineData
                        src = item.get('source', {})
                        if src.get('type') == 'base64':
                            parts.append({"inlineData": {
                                "mimeType": src.get('media_type', 'image/jpeg'),
                                "data":     src.get('data', '')
                            }})
                    elif item.get('type') == 'input_audio':
                        # Audio content block — inlineData with audio mimeType
                        audio = item.get('input_audio', {})
                        parts.append({"inlineData": {
                            "mimeType": f"audio/{audio.get('format', 'wav')}",
                            "data":     audio.get('data', '')
                        }})
                    elif item.get('type') == 'file':
                        # File content block (PDF etc.)
                        file_obj = item.get('file', {})
                        if file_obj.get('file_data'):
                            # Inline base64 file
                            parts.append({"inlineData": {
                                "mimeType": file_obj.get('file_type', 'application/pdf'),
                                "data":     file_obj['file_data']
                            }})
                        elif file_obj.get('file_url'):
                            url = file_obj['file_url']
                            if url.startswith('gs://'):
                                parts.append({"fileData": {
                                    "fileUri":  url,
                                    "mimeType": file_obj.get('file_type', 'application/pdf')
                                }})
                            elif url.startswith('http://') or url.startswith('https://'):
                                raise TranslationError(
                                    "fileData HTTP/HTTPS URIs are blocked by  VPC Service Controls. "
                                    "Use base64 inlineData or a GCS gs:// URI instead.",
                                    "INVALID_ARGUMENT"
                                )
            # Merge consecutive user turns into one
            if contents and contents[-1]['role'] == 'user':
                contents[-1]['parts'].extend(parts)
            else:
                contents.append({"role": "user", "parts": parts})

        # Assistant message — reconstruct Gemini model turn with thoughtSignature
        elif role == 'assistant':
            # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
            # OA9 fix 3a: tool_calls can be JSON null (explicit None) not just missing.
            # Guard with 'or []' so null is treated as no tool calls.
            _assistant_tool_calls = msg.get('tool_calls') or []
            if _assistant_tool_calls:
                # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
                parts = []
                for i, tc in enumerate(_assistant_tool_calls):
                    # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
                    call_id  = tc['id']
                    meta     = session_store.get_tool_call_meta(session_id, call_id)
                    args_val = tc['function'].get('arguments', '{}')
                    if isinstance(args_val, str):
                        # Same protobuf.Struct constraint as functionResponse.response
                        # (see _flush_tool_messages) — functionCall.args must be an
                        # object. json.loads() succeeds without raising on a bare
                        # scalar, so a malformed/unconventional 'arguments' string
                        # (e.g. "42") must still be caught and wrapped, not just a
                        # hard decode failure.
                        try:
                            parsed = json.loads(args_val)
                            args_val = parsed if isinstance(parsed, dict) else {"value": parsed}
                        except json.JSONDecodeError:
                            args_val = {}
                    elif not isinstance(args_val, dict):
                        args_val = {"value": args_val}

                    #  gateway constraint: 'id' stripped from functionCall before forwarding.
                    # call_id preserved in session for name/thoughtSignature reconstruction.
                    part = {
                        "functionCall": {
                            "name": meta.name if meta else tc['function']['name'],
                            "args": args_val
                            # NOTE: 'id' intentionally omitted —  gateway rejects it
                        }
                    }

                    if meta and meta.thought_signature:
                        # R3: thoughtSignature injected at exact same part_position
                        # R4: Parallel calls: signature on first part only (i == 0)
                        # R5: Sequential calls: signature on each step's first part (meta.step > 0)
                        if i == 0 or meta.step > 0:
                            part["thoughtSignature"] = meta.thought_signature
                            print(f"  🔗 Reattached thoughtSignature for call_id={call_id}")
                    if meta and meta.tool_type:
                        part["tool_type"] = meta.tool_type

                    parts.append(part)

                # R13: Re-inject server-side tool parts (built-in tool context)
                server_parts = session_store.get_server_side_parts(session_id)
                if server_parts:
                    parts = server_parts + parts

                contents.append({"role": "model", "parts": parts})
            elif content and isinstance(content, str):
                # OA9 fix 3c: content is explicitly None when tool_calls present.
                # Only append text turn when content is a non-empty string.
                contents.append({"role": "model", "parts": [{"text": content}]})

    # Flush any trailing tool messages
    if consecutive_tool_messages:
        _flush_tool_messages(consecutive_tool_messages, session_id, session_store, contents)

    gemini_body['contents'] = contents

    # thinkingConfig: inject LOW thinking level when client has not set it.
    # gemini-3.1-pro-preview is a thinking model — without this the model
    # consumes the entire token budget on internal reasoning and produces
    # zero visible output. LOW minimises reasoning tokens while keeping
    # the model functional. Client-supplied thinkingConfig is preserved.
    # Constraint: thinkingLevel and thinkingBudget cannot coexist (400 from ).
    _existing_tc = gemini_body.get('generationConfig', {}).get('thinkingConfig', {})
    if not _existing_tc.get('thinkingLevel') and not _existing_tc.get('thinkingBudget'):
        gemini_body.setdefault('generationConfig', {})['thinkingConfig'] = {
            'thinkingLevel': 'LOW'
        }

    # Determine target path suffix
    model        = openai_payload.get('model', 'gemini-3.1-pro-preview')
    is_streaming = openai_payload.get('stream', False)
    suffix = (f"models/{model}:streamGenerateContent"
              if is_streaming else f"models/{model}:generateContent")

    return gemini_body, suffix


def _flush_tool_messages(messages: list, session_id: str,
                          session_store: SessionStore, contents: list) -> None:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Groups consecutive OpenAI tool result messages into a single Gemini user turn.

    Parameters:
      messages (list): List of consecutive tool messages from the OpenAI payload.
      session_id (str): The current session ID.
      session_store (SessionStore): The global session store.
      contents (list): The translated contents array being built (mutated in place).

    Returns: None
    Enforces R9, R17.

     gateway constraint: 'id' stripped from functionResponse before forwarding.
    The name is looked up from session using tool_call_id as key.
    """
    parts = []
    # R17: All functionResponse parts in a single user turn — never interleaved
    for msg in messages:
        # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
        if msg.get('role') == 'function':
            # Tier 1 fix: legacy shape — {"role": "function", "name": ..., "content": ...}.
            # No tool_call_id exists in this era of the API; 'name' is authoritative
            # and there is no session-stored thoughtSignature/meta to look up.
            name = msg.get('name', 'unknown_function')
        else:
            # R9: tool_call_id (OpenAI) == functionCall.id (Gemini) — looked up from session
            call_id = msg['tool_call_id']
            meta    = session_store.get_tool_call_meta(session_id, call_id)
            name    = meta.name if meta else call_id

        content = msg.get('content', '{}')
        if isinstance(content, str):
            # Gemini's functionResponse.response is a protobuf.Struct — it MUST
            # be a JSON object. json.loads() succeeds without raising on a bare
            # scalar (e.g. the string "21.61018278497431" parses cleanly to a
            # float), so checking only for JSONDecodeError let scalars through
            # unwrapped, producing a 400 from  ("Invalid value ... Struct").
            # Any non-dict parse result — number, bool, string, list — must be
            # wrapped the same way a decode failure already is.
            try:
                parsed = json.loads(content)
                content = parsed if isinstance(parsed, dict) else {"result": parsed}
            except json.JSONDecodeError:
                content = {"result": content}
        elif not isinstance(content, dict):
            # Non-string, non-dict content (e.g. a client sending a raw int)
            # must also be wrapped — same Struct constraint applies.
            content = {"result": content}

        parts.append({
            "functionResponse": {
                # NOTE: 'id' intentionally omitted —  gateway rejects it
                "name":     name,
                "response": content
            }
        })
    contents.append({"role": "user", "parts": parts})
