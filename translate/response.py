# BEFORE MODIFYING THIS FILE: Read RULES.md in the project root and follow all rules. No exceptions.
"""
Module: translate.response
Purpose: Translates Gemini responses and streaming chunks back to OpenAI format.
Layer Architecture: Layer 4 (Translation - Response).
Contracts Served: Contract 1 (OpenAI Chat Completions).
Dependencies: json, time, uuid, session.
"""
import json
import time
import uuid
from session import SessionStore, ToolCallMeta

FINISH_REASON_MAP = {
    'STOP':              'stop',
    'MAX_TOKENS':        'length',
    'SAFETY':            'content_filter',
    'PROHIBITED_CONTENT':'content_filter',
    'RECITATION':        'content_filter',
    'OTHER':             'stop',
}

def _build_usage(gemini_body: dict) -> dict:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Helper to convert Gemini usageMetadata to OpenAI usage schema.
    """
    usage_meta = gemini_body.get('usageMetadata', {})
    return {
        "prompt_tokens":     usage_meta.get('promptTokenCount', 0),
        "completion_tokens": usage_meta.get('candidatesTokenCount', 0),
        "total_tokens":      usage_meta.get('totalTokenCount', 0),
        "prompt_tokens_details": {
            "cached_tokens": usage_meta.get('cachedContentTokenCount', 0),
            "audio_tokens":  0
        },
        "completion_tokens_details": {
            "reasoning_tokens":              usage_meta.get('thoughtsTokenCount', 0),
            "audio_tokens":                  0,
            "accepted_prediction_tokens":    0,
            "rejected_prediction_tokens":    0
        }
    }

def _build_logprobs(candidate: dict) -> dict | None:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Tier 2 gap fix: converts Gemini's logprobsResult into OpenAI's
    choices[].logprobs schema. Previously this was hardcoded to null in
    every response regardless of whether the client requested logprobs
    (see request-side 'logprobs'/'top_logprobs' → responseLogprobs mapping
    in translate/request.py).

    Gemini shape: candidate.logprobsResult = {
        chosenCandidates: [{token, tokenId, logProbability}, ...],
        topCandidates: [{candidates: [{token, tokenId, logProbability}, ...]}, ...]
    }
    OpenAI shape: {content: [{token, logprob, bytes: null, top_logprobs: [...]}]}

    Parameters: candidate (dict): A single Gemini response candidate.
    Returns: dict in OpenAI logprobs shape, or None if Gemini didn't return any
             (either the client didn't request them, or the model/version
             doesn't support responseLogprobs).
    Raises: None. Any malformed/unexpected shape degrades to None rather
            than raising, since logprobs are supplementary — not worth
            failing the whole response over.
    """
    lp_result = candidate.get('logprobsResult')
    if not lp_result:
        return None

    try:
        chosen = lp_result.get('chosenCandidates', [])
        top_lists = lp_result.get('topCandidates', [])
        content = []
        for idx, tok in enumerate(chosen):
            top_logprobs = []
            if idx < len(top_lists):
                for alt in top_lists[idx].get('candidates', []):
                    top_logprobs.append({
                        "token":   alt.get('token', ''),
                        "logprob": alt.get('logProbability', 0.0),
                        "bytes":   None
                    })
            content.append({
                "token":        tok.get('token', ''),
                "logprob":      tok.get('logProbability', 0.0),
                "bytes":        None,
                "top_logprobs": top_logprobs
            })
        return {"content": content, "refusal": None} if content else None
    except (AttributeError, TypeError, IndexError):
        # Malformed logprobsResult from upstream — degrade gracefully.
        print("  ⚠️  Could not parse Gemini logprobsResult — omitting from response")
        return None


def gemini_to_openai(
    gemini_body: dict,
    session_id: str,
    session_store: SessionStore,
    original_model: str,
    streaming: bool = False
) -> dict:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Converts a non-streaming Gemini REST response into OpenAI Chat Completion format.
    
    Parameters:
      gemini_body (dict): Parsed JSON response from Gemini.
      session_id (str): The active session identifier.
      session_store (SessionStore): The global session store.
      original_model (str): The requested OpenAI model name to echo back.
      streaming (bool): Flag indicating if this is part of a stream (defaults False).
      
    Returns: dict (OpenAI formatted response body)
    Enforces R2, R6, R7, R9, R13.
    """
    candidates = gemini_body.get('candidates', [])
    if not candidates:
        return {
            "id":               f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object":           "chat.completion",
            "created":          int(time.time()),
            "model":            original_model,
            "system_fingerprint": None,
            "service_tier":     "default",
            "choices": [{
                "index":        0,
                "message": {
                    "role":       "assistant",
                    "content":    None,
                    "tool_calls": None,
                    "refusal":    None,
                    "annotations": []
                },
                "finish_reason": "stop",
                "logprobs":      None
            }],
            "usage": _build_usage(gemini_body)
        }

    candidate = candidates[0]
    content = candidate.get('content', {})
    parts = content.get('parts', [])
    finish_reason_raw = candidate.get('finishReason', 'STOP')
    openai_finish_reason = FINISH_REASON_MAP.get(finish_reason_raw, 'stop')

    text_content = ""
    tool_calls = []
    server_parts = []
    
    # R10: read current_turn_step via locked SessionStore method — never direct field access
    step = session_store.get_current_step(session_id)

    for i, part in enumerate(parts):
        # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
        if 'text' in part:
            text_content += part['text']
        elif 'executableCode' in part or 'codeExecutionResult' in part:
            # R13: Server-side tool parts stored in session and re-injected on next model turn
            server_parts.append(part)
        elif 'functionCall' in part:
            # R7: finish_reason: "tool_calls" when functionCall parts present
            openai_finish_reason = 'tool_calls'
            # R9: tool_call_id (OpenAI) == functionCall.id (Gemini)
            call_id = part['functionCall'].get('id', f"call_{uuid.uuid4().hex[:8]}")
            name = part['functionCall']['name']
            args_obj = part['functionCall'].get('args', {})
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

            tool_calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    # R6: functionCall.args (object) → function.arguments (JSON string)
                    "arguments": json.dumps(args_obj)
                }
            })
        elif 'inlineData' in part:
            # WORK-05: GAP-04 — inlineData parts were silently dropped.
            # Convert to OpenAI image_url content block with data: URI.
            # Applies when Gemini returns image bytes from image_generation built-in
            # or any vision response that echoes back image data.
            mime     = part['inlineData'].get('mimeType', 'image/png')
            b64data  = part['inlineData'].get('data', '')
            # Append as a markdown-style image reference in text content.
            # For clients that support content arrays, this is the text fallback;
            # full content-array support requires responses_handler (TODO-01).
            text_content += f"\n[image/inlineData mimeType={mime} size={len(b64data)}chars]"
            print(f"  🖼️  inlineData part ({mime}, {len(b64data)} chars base64) included in response")
        elif 'thoughtSignature' in part:
            pass # Ignore standalone thoughtSignatures

    if tool_calls:
        session_store.increment_step(session_id)

    if server_parts:
        session_store.store_server_side_parts(session_id, server_parts)

    usage = _build_usage(gemini_body)

    return {
        "id":               f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object":           "chat.completion",
        "created":          int(time.time()),
        "model":            original_model,
        "system_fingerprint": None,
        "service_tier":     "default",
        "choices": [{
            "index":        0,
            "message": {
                "role":       "assistant",
                "content":    text_content if text_content else None,
                "tool_calls": tool_calls if tool_calls else None,
                "refusal":    None,
                "annotations": []
            },
            "finish_reason": openai_finish_reason,
            "logprobs":      _build_logprobs(candidate)
        }],
        "usage": usage
    }

def translate_response_chunk(
    gemini_chunk: dict,
    completion_id: str,
    created: int,
    model: str,
    session_id: str,
    session_store: SessionStore
) -> dict:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Converts a single streaming Gemini REST chunk into OpenAI streaming delta format.
    Uses get_and_increment_stream_offset() for atomic read-modify-write of stream_part_offset (R10),
    and get_current_step() for locked read of current_turn_step (R10), so that functionCall
    parts spanning multiple chunks get the correct part_position (R3).

    Parameters:
      gemini_chunk (dict): The parsed JSON delta chunk.
      completion_id (str): Generated UUID shared across all chunks for this stream.
      created (int): Timestamp shared across all chunks.
      model (str): OpenAI model name being emulated.
      session_id (str): The active session identifier.
      session_store (SessionStore): Global session store.

    Returns: dict or None (OpenAI formatted streaming chunk, or None if empty)
    Enforces R2, R3, R6, R7, R9, R10, R13.
    """
    if 'usageMetadata' in gemini_chunk and not gemini_chunk.get('candidates'):
        return {
            "id":      completion_id,
            "object":  "chat.completion.chunk",
            "created": created,
            "model":   model,
            "choices": [],
            "usage":   _build_usage(gemini_chunk)
        }

    candidates = gemini_chunk.get('candidates', [])
    if not candidates:
        return None
        
    candidate = candidates[0]
    content = candidate.get('content', {})
    parts = content.get('parts', [])
    finish_reason_raw = candidate.get('finishReason', 'STOP')
    openai_finish_reason = FINISH_REASON_MAP.get(finish_reason_raw, 'stop')

    text_content = ""
    tool_calls = []
    server_parts = []
    
    # R10: atomically read stream_part_offset and pre-increment by chunk size in one locked operation.
    # Prevents races between concurrent threads processing chunks for the same session.
    offset = session_store.get_and_increment_stream_offset(session_id, len(parts))
    # R10: read current_turn_step via locked SessionStore method — never direct field access
    step = session_store.get_current_step(session_id)

    for i, part in enumerate(parts):
        # BEFORE MODIFYING THIS BLOCK: Read RULES.md in the project root and follow all rules. No exceptions.
        if 'text' in part:
            text_content += part['text']
        elif 'executableCode' in part or 'codeExecutionResult' in part:
            # R13: Server-side tool parts stored in session and re-injected on next model turn
            server_parts.append(part)
        elif 'functionCall' in part:
            # R7: finish_reason: "tool_calls" when functionCall parts present
            openai_finish_reason = 'tool_calls'
            # R9: tool_call_id (OpenAI) == functionCall.id (Gemini)
            call_id = part['functionCall'].get('id', f"call_{uuid.uuid4().hex[:8]}")
            name = part['functionCall']['name']
            args_obj = part['functionCall'].get('args', {})
            thought_sig = part.get('thoughtSignature') or part.get('thought_signature')
            tool_type = part.get('tool_type')

            # R2: thoughtSignature stored in session on every Gemini response containing functionCall
            # part_position uses offset+i so positions are correct across multi-chunk streams (R3)
            session_store.store_tool_call_meta(session_id, call_id, ToolCallMeta(
                name=name,
                tool_type=tool_type,
                thought_signature=thought_sig,
                part_position=offset + i,
                step=step
            ))
            print(f"  💾 Stored thoughtSignature for call_id={call_id} at position={offset + i}")

            tool_calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    # R6: functionCall.args (object) → function.arguments (JSON string)
                    "arguments": json.dumps(args_obj)
                }
            })
        elif 'inlineData' in part:
            # WORK-05: GAP-04 -- inlineData parts in streaming chunks
            mime    = part['inlineData'].get('mimeType', 'image/png')
            b64data = part['inlineData'].get('data', '')
            text_content += f"\n[image/inlineData mimeType={mime} size={len(b64data)}chars]"
            print(f"  \U0001f5bc\ufe0f  inlineData chunk ({mime}) in stream")

    if tool_calls:
        session_store.increment_step(session_id)

    if server_parts:
        session_store.store_server_side_parts(session_id, server_parts)

    delta = {}
    if text_content or not tool_calls:
        delta = {"role": "assistant", "content": text_content}
    if tool_calls:
        delta["tool_calls"] = tool_calls

    if not candidate.get('finishReason'):
        openai_finish_reason = None
    elif tool_calls:
        # R7: enforce tool_calls finish reason
        openai_finish_reason = 'tool_calls'

    chunk_response = {
        "id":      completion_id,
        "object":  "chat.completion.chunk",
        "created": created,
        "model":   model,
        "system_fingerprint": None,
        "service_tier": "default",
        "choices": [{
            "index":         0,
            "delta":         delta,
            "finish_reason": openai_finish_reason,
            "logprobs":      None
        }],
        "usage": None
    }
    
    return chunk_response