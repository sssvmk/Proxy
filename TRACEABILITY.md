# Proxy v2 — Rules Traceability Table

This table maps the critical rules defined in §13 of the `proxy_v2` specification to the exact files and functions where they are enforced within the codebase.

| # | Rule | Exact File + Function where enforced |
|---|---|---|
| **R1** | `stream=True` on all streaming calls | `handlers/gemini_handler.py` (`_stream`), `handlers/openai_handler.py` (`_stream`) |
| **R2** | `thoughtSignature` stored in session | `translate/response.py` (`gemini_to_openai`, `translate_response_chunk`), `handlers/gemini_handler.py` (`_store_signatures_from_response`, `_repair_thought_signatures`, `_stream`) |
| **R3** | `thoughtSignature` injected at exact `part_position` | `translate/request.py` (`openai_to_gemini`), `handlers/gemini_handler.py` (`_repair_thought_signatures`) |
| **R4** | Parallel calls: signature on first part (`part_position == 0`) | `translate/request.py` (`openai_to_gemini`), `handlers/gemini_handler.py` (`_repair_thought_signatures`) |
| **R5** | Sequential calls: signature on each step's first part | `translate/request.py` (`openai_to_gemini`), `handlers/gemini_handler.py` (`_repair_thought_signatures`) via `expected_position` |
| **R6** | `functionCall.args` → `function.arguments` (JSON string) | `translate/response.py` (`gemini_to_openai`, `translate_response_chunk`) |
| **R7** | `finish_reason: "tool_calls"` when tools present | `translate/response.py` (`gemini_to_openai`, `translate_response_chunk`) |
| **R8** | `data: [DONE]\n\n` synthesized | `handlers/openai_handler.py` (`_stream` -> `generate`) |
| **R9** | `tool_call_id` == `functionCall.id` stored | `translate/response.py` (id extraction/mapping), `translate/request.py` (`_flush_tool_messages`) |
| **R10** | `threading.Lock` per `SessionEntry`; all shared field reads and writes via locked `SessionStore` methods | `session.py` (`SessionEntry`, `SessionStore.get_current_step`, `SessionStore.get_and_increment_stream_offset`) |
| **R11** | Body read via `flask.g.raw_body` only | `proxy.py` (`read_body_once`, `openai_route`, `gemini_route`) |
| **R12** | Model regex targets only `{model-id}` via slice | `transforms/path.py` (`normalize_model`) |
| **R13** | Server-side parts stored and re-injected | `translate/response.py` & `translate/request.py`, `handlers/gemini_handler.py` (`_store_signatures_from_response`) |
| **R14** | `get_token()` is first call in handler | `handlers/openai_handler.py` (`handle`), `handlers/gemini_handler.py` (`handle`) |
| **R15** | No module reads `os.environ` directly except config | `config.py` (global scope), all other modules import `config` |
| **R16** | Raises `EnvironmentError` on missing secrets | `config.py` (startup validation loop) |
| **R17** | Parallel `functionResponse` sent in single user turn | `translate/request.py` (`openai_to_gemini` -> `_flush_tool_messages`) |
| **R18** | `fileData` http/https URIs rejected before forwarding to  (VPCSC) | `handlers/gemini_handler.py` (`_scan_filedata_uris`, `handle`), `translate/request.py` (`_translate_image_url_item`, `openai_to_gemini`) |

*Last verified during Issue 1–6 + New A/B/C fixes — 2026-06-18.*

---

## proxy_v2_spec.md v3.0 — WORK-01 to WORK-10 implementation (2026-06-29)

| WORK item | Files modified/created | TODO ref |
|---|---|---|
| WORK-01 | `translate/request.py` (`_translate_image_url_item`, `TranslationError`) | TODO-08 |
| WORK-02 | `translate/request.py` (`openai_to_gemini` tools loop) | TODO-07 |
| WORK-03 | `translate/request.py` (`openai_to_gemini` field strip) | TODO-01 |
| WORK-04 | `translate/request.py` (`openai_to_gemini` system/developer role) | (gap fix) |
| WORK-05 | `translate/response.py` (`gemini_to_openai`, `translate_response_chunk`) | TODO-09 |
| WORK-06 | `handlers/gemini_handler.py` (`_scan_filedata_uris`, `handle`) | TODO-11 |
| WORK-07 | `proxy.py` (`_not_implemented` + 7 route decorators) | TODO-10 |
| WORK-08 | `proxy.py` (routes), `handlers/responses_handler.py` (new file) | TODO-01, TODO-02 |
| WORK-09 | `proxy.py` (routes), `handlers/responses_handler.py`, `session.py` (response cache) | TODO-03, TODO-04, TODO-05 |
| WORK-10 | `proxy.py` (route), `handlers/responses_handler.py` (`handle_compact`) | TODO-06 |

*Verified by automated test suite (29 functional + integration tests, all passing) — 2026-06-29.*

---

## proxy_v2_spec.md v3.1 — Gap fixes (2026-06-29)

Two missing field mappings added to `translate/request.py` `openai_to_gemini()`.
No other files modified.

| Fix | Field | Mapping | Test |
|---|---|---|---|
| Gap 1 | `stop` (string or list) | `generationConfig.stopSequences` (list) | OA3 |
| Gap 2 | `response_format.type="json_object"` | `generationConfig.responseMimeType="application/json"` | OA13 |
| Gap 2 | `response_format.type="json_schema"` | `generationConfig.responseMimeType + responseSchema` | OA13 |

Checkpoint MD5 before change: `de3bf53446305688c71af6a6fa0ce9c5` (`translate/request.py`)

*Verified by 6 functional unit tests, all passing — 2026-06-29.*

---

## proxy_v2_spec.md v3.2 — Bug fixes from test run (2026-07-02)

Three bugs identified from test_proxy_v2.py run. Fixed in two files.

| Bug | Test | Root cause | Fix | File |
|---|---|---|---|---|
| OC2: HTTP 500 instead of 400 on https:// image URL | OC2 FAIL | `TranslationError` raised in `openai_to_gemini()` but not caught in `handle()` → Flask 500 | Added `except TranslationError` block in `handle()` after `openai_to_gemini()` call | `handlers/openai_handler.py` |
| OA15: HTTP 500 on system message list content | OA15 FAIL | `_derive_session_id()` called `content.encode()` on a list → `AttributeError` | Guard: extract text from list before hashing | `handlers/openai_handler.py` |
| OA9: HTTP 400 on step 2 of sequential tool call | OA9 FAIL | `tool_calls: null` in assistant message treated as truthy; `content: None` passed to Gemini body | Guard `msg.get("tool_calls") or []`; guard `content and isinstance(content, str)` | `translate/request.py` |

Checkpoint MD5 before:
  `handlers/openai_handler.py` : `9dc4acdf81cba5fe7a9eec948637f1a2`
  `translate/request.py`       : `b0d9615cf41328239dd557adcf149bd6`

Checkpoint MD5 after:
  `handlers/openai_handler.py` : `006f4f1e8a5488f4260c8da865786366`
  `translate/request.py`       : `489e6cb6f70518598dc412c5ed3f8f65`

*Verified by 5 functional unit tests, all passing — 2026-07-02.*

---

## proxy_v2_spec.md v3.3 — thinkingConfig injection (2026-07-02)

Added `thinkingConfig: {thinkingLevel: "LOW"}` injection for gemini-3.1-pro-preview.
Without this the model defaults to maximum thinking budget, consuming all tokens on
internal reasoning and producing zero visible output (confirmed by test results:
finishReason=MAX_TOKENS, completion=0 on all complex prompts).

Rules applied:
- Only injected when client has NOT already set thinkingConfig (client override preserved)
- thinkingLevel and thinkingBudget cannot coexist — guard checks both before injecting
- Applies to Contract 1 (translate/request.py) and Contracts 2/3 (gemini_handler.py)

| File | Change | Checkpoint MD5 before | MD5 after |
|---|---|---|---|
| `translate/request.py` | thinkingConfig injected before return | `489e6cb6f70518598dc412c5ed3f8f65` | `34512cd3d8e030fd957352497c85ff73` |
| `handlers/gemini_handler.py` | thinkingConfig injected after _repair_thought_signatures | `ef8b82b8aa55839b50f39d2710ca4b23` | `391ed8e17bb18a1b7552ca45f3a5f9a7` |

Expected resolution: all 47 WARNs (finishReason=MAX_TOKENS, content="") + OA5 FAIL + OA7 FAIL.

*Verified by 4 functional unit tests, all passing — 2026-07-02.*

---

## proxy_v2_spec.md v3.4 — OA9 session_id fix (2026-07-02)

Fixed OA9 step 2 HTTP 400 — sequential multi-step tool calls.

Root cause: `_derive_session_id()` falls back to `str(uuid.uuid4())` when no system
message and no X-Session-ID header. Step 1 and step 2 of a multi-step tool call
get different UUIDs → step 1 thoughtSignature never found in step 2 → proxy
reconstructs assistant turn without thoughtSignature →  rejects with 400.

Fix: added third fallback — hash of the first user message (`usr_{sha256[:32]}`).
The first user message is constant across all steps of a conversation, providing
a stable session anchor without requiring client-side header changes.

Priority order (unchanged for system message and header paths):
1. SHA-256 of system/developer message → `sys_{hash}` (unchanged)
2. X-Session-ID request header (unchanged)
3. NEW: SHA-256 of first user message → `usr_{hash}`
4. random UUID (final fallback for truly stateless requests)

| File | Change | MD5 before | MD5 after |
|---|---|---|---|
| `handlers/openai_handler.py` | `_derive_session_id()` user message fallback | `006f4f1e8a5488f4260c8da865786366` | `2649c961ed9f43c3a67d2197d0e9345a` |

*Verified by 5 functional unit tests, all passing — 2026-07-02.*
