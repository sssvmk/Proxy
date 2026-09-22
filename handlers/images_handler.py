# BEFORE MODIFYING THIS FILE: Read RULES.md in the project root and follow all rules. No exceptions.
"""
Module: handlers.images_handler
Purpose: Translates OpenAI Images API (POST /images/generations) requests
         into Gemini generateContent calls with image output modality, and
         translates the response back into OpenAI's ImagesResponse shape.
Layer Architecture: Layer 2, 3, 4, 5, 6, 7 (same as openai_handler).
Contracts Served: New — Images API surface (generations only).
Dependencies: json, time, requests, flask, config, transforms.path.

ASSUMPTION FLAGGED (per muni decision, spec compliance review): Gemini's
documented image-generation path (generationConfig.responseModalities
including "IMAGE") is demonstrated by Google on dedicated "-image" model
variants (e.g. gemini-3.1-flash-image). Whether config.ALLOWED_MODEL
(gemini-3.1-pro-preview) itself supports image output modality is NOT
verified — this handler assumes support per explicit direction and lets
GSK's own 400 surface naturally if the model rejects responseModalities,
same as any other unverified translation path in this codebase.

SCOPE: /images/generations only. /images/edits and /images/variations
(multipart file upload contracts) are a separate follow-up — different
enough in request shape (multipart/form-data with an input image) to not
bundle into this pass.

KNOWN LIMITATIONS (flagged explicitly, not silently dropped):
  - response_format="url" cannot be honored — GSK has no storage/hosting
    layer (confirmed elsewhere in this codebase: GET/DELETE on GSK's other
    path aliases return 404, no storage layer exists). Always returns
    b64_json regardless of what the client requested; logged when it
    differs from the request.
  - size is mapped to Gemini's aspectRatio + resolution tier (1K/2K/4K),
    which is NOT the same as OpenAI's exact pixel dimensions. This is a
    best-effort approximation, not an exact-size guarantee.
  - quality, style, user have no Gemini equivalent and are dropped with a
    log line, same convention as unsupported fields elsewhere.
"""
import json
import time
import requests
from flask import Response
import config
from transforms.path import normalize_model, build_target_url, build_headers


def _error_response(status: int, message: str, error_status: str = "INVALID_ARGUMENT") -> Response:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Builds a structured JSON error Response matching this codebase's
    existing error shape convention.
    """
    body = json.dumps({"error": {"code": status, "message": message, "status": error_status}})
    return Response(body, status=status, content_type='application/json')


# OpenAI size string -> best-effort Gemini (aspectRatio, imageSize) mapping.
# APPROXIMATION, not exact pixel compliance — see module docstring.
_SIZE_MAP = {
    "1024x1024": ("1:1", "1K"),
    "1792x1024": ("16:9", "1K"),
    "1024x1792": ("9:16", "1K"),
    "1536x1024": ("3:2", "1K"),
    "1024x1536": ("2:3", "1K"),
}


def _openai_images_request_to_gemini(payload: dict) -> dict:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Translates an OpenAI CreateImageRequest body into a Gemini generateContent
    request body with image output modality.

    Parameters: payload (dict): Parsed OpenAI Images API request body.
    Returns: dict — Gemini generateContent request body.
    Raises: TranslationError-equivalent via ValueError if 'prompt' is missing.

    Field mapping:
      prompt          -> contents[0].parts[0].text
      n               -> generationConfig.candidateCount (safe here — no
                         session/thoughtSignature tracking for a standalone
                         image call, unlike Chat Completions' n>1 restriction)
      size            -> generationConfig.imageConfig.{aspectRatio,imageSize}
                         (approximate — see module docstring)
      quality, style, user, response_format -> no Gemini equivalent, dropped
                         with a log line (response_format handled specially,
                         see images_handler.handle()).
    """
    prompt = payload.get('prompt')
    if not prompt:
        raise ValueError("'prompt' is required.")

    gemini_body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"]
        }
    }

    n = payload.get('n', 1)
    if n and n != 1:
        gemini_body['generationConfig']['candidateCount'] = n

    size = payload.get('size')
    if size and size != 'auto':
        mapped = _SIZE_MAP.get(size)
        if mapped:
            aspect_ratio, image_size = mapped
            gemini_body['generationConfig']['imageConfig'] = {
                "aspectRatio": aspect_ratio,
                "imageSize": image_size
            }
        else:
            print(f"  ⚠️  Unmapped image size '{size}' — no Gemini aspectRatio/imageSize "
                  f"equivalent on file, proceeding with model default")

    for _field in ('quality', 'style', 'user'):
        if _field in payload:
            print(f"  ⚠️  Images field '{_field}'={payload[_field]!r} has no Gemini "
                  f"equivalent — ignored")

    return gemini_body


def _gemini_response_to_images_response(gemini_json: dict, requested_response_format: str) -> dict:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Converts a Gemini generateContent response (one or more candidates,
    each with an inlineData image part) into OpenAI's ImagesResponse shape.

    Parameters: gemini_json (dict): Parsed Gemini generateContent response.
                requested_response_format (str): What the client asked for
                    ("url" or "b64_json") — used only to decide whether to
                    log the b64_json-only limitation, never to change output.
    Returns: dict — OpenAI ImagesResponse shape ({created, data: [...]}).
    Raises: ValueError if no image data is present in any candidate (e.g.
            the model responded with text only / declined the prompt) —
            caller translates this into a client-facing error rather than
            returning an empty, misleadingly-200 response.
    """
    if requested_response_format == 'url':
        print(f"  ⚠️  response_format='url' requested but GSK has no file-hosting "
              f"layer — returning b64_json instead")

    data = []
    for cand in gemini_json.get('candidates', []):
        for part in cand.get('content', {}).get('parts', []):
            if 'inlineData' in part:
                data.append({"b64_json": part['inlineData'].get('data', '')})

    if not data:
        raise ValueError(
            "Gemini returned no image data — the model may have declined the "
            "prompt (check safety ratings) or does not support image output "
            "on this model/gateway configuration."
        )

    return {"created": int(time.time()), "data": data}


def handle(raw_body: bytes, token_manager) -> Response:
    """
    BEFORE MODIFYING THIS FUNCTION: Read RULES.md in the project root and follow all rules. No exceptions.
    Main entry point for POST /images/generations.

    Parameters: raw_body (bytes): Raw request body.
                token_manager (TokenManager): Shared token manager instance.
    Returns: Response
    Enforces R14 (token fetch first).
    """
    token = token_manager.get_token()

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return _error_response(400, "Invalid JSON body")

    try:
        gemini_body = _openai_images_request_to_gemini(payload)
    except ValueError as e:
        return _error_response(400, str(e))

    target_path = normalize_model(f"models/{config.ALLOWED_MODEL}:generateContent")
    target_url = build_target_url(target_path, '')
    headers = build_headers(token, {})

    print(f"[{time.strftime('%H:%M:%S')}] 🚀 Forwarding image generation to: {target_url}")

    try:
        upstream = requests.post(
            target_url, headers=headers, json=gemini_body,
            timeout=(config.UPSTREAM_CONNECT_TIMEOUT, config.UPSTREAM_READ_TIMEOUT)
        )
    except requests.exceptions.RequestException as e:
        return _error_response(504, f"Upstream request failed: {str(e)}", "DEADLINE_EXCEEDED")

    print(f"[{time.strftime('%H:%M:%S')}] ← GSK status: {upstream.status_code}")
    if upstream.status_code >= 400:
        print(f"  ❌ Error body: {upstream.text[:1000]}")
        return Response(upstream.content, status=upstream.status_code,
                        content_type='application/json')

    try:
        images_resp = _gemini_response_to_images_response(
            upstream.json(), payload.get('response_format', 'url')
        )
    except ValueError as e:
        return _error_response(502, str(e), "FAILED_PRECONDITION")

    return Response(json.dumps(images_resp), status=200, content_type='application/json')
