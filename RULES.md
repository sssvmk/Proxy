# RULES.md — proxy_v2 Modification Protocol

> **These rules MUST be followed by any LLM or human making changes.**
> **No exceptions. No shortcuts.**
> **Read this file completely before touching any code, spec, or configuration.**

---

## Why These Rules Exist

`proxy_v2` is a production proxy implementing three concurrent API contracts
(OpenAI Chat Completions, Gemini REST pass-through, Gemini full contract with
session repair). A change to one module can silently break another contract.
Session state, thoughtSignature injection, streaming generators, and thread
safety are tightly coupled across files. These rules exist to prevent
regressions, maintain spec-code alignment, and ensure every change is
traceable, tested, and reversible.

---

## RULE 1 — READ SPECIFICATION FIRST

Before touching any code, read in full:

1. `proxy_v2_spec.md` — the authoritative implementation specification
2. The **module-level docstring** of the file you intend to change
3. The **class-level docstring** of every class in scope
4. The **method/function-level docstring** of every function in scope
5. All **inline comments** in the blocks you will modify

Every function has a documented contract. Every inline `# RN:` comment marks
a Critical Rule from §13 of the spec. Understand what the code is doing and
why before proposing any change.

**You may not claim to understand a function you have not read in full.**

---

## RULE 2 — READ THE ENTIRE CODE FILE

Read every line of every file you intend to modify, end to end, before writing
a single character of new code.

- A change to one block can break another block in the same file.
- Import order, module-level globals, and shared state matter.
- Never modify code you have not read.

If a change requires touching multiple files, read all of them in full before
starting.

---

## RULE 3 — IMPACT ASSESSMENT BEFORE CODING

For any proposed change, produce a written impact assessment covering:

1. **Affected functions** — which functions are directly modified
2. **Affected callers** — which other functions call the modified functions
3. **Affected contracts** — which of Contract 1 / 2 / 3 are impacted
4. **Session state impact** — does the change affect what is stored or read
   from `SessionStore`? Which `ToolCallMeta` fields are affected?
5. **Streaming impact** — does the change affect any `generate()` generator
   or streaming response path?
6. **Critical Rules impact** — which R1–R17 rules does the change touch?
7. **Backward compatibility** — are existing sessions, stored signatures, or
   in-flight requests affected?
8. **Failure modes** — what breaks if the change has a bug? What is the
   observable symptom (400 from upstream, client hang, silent wrong output)?
9. **Edge cases** — parallel tool calls, sequential multi-step tool calls,
   missing session state, concurrent clients, token expiry during request

**Present this assessment to the user and WAIT for explicit approval before
writing any code.**

---

## RULE 4 — CREATE A CHECKPOINT

Before writing any code, create a checkpoint of all files you intend to
modify:

```
proxy_v2/checkpoints/CP_<YYYYMMDD_HHMMSS>/
    ├── <copy of every .py file being modified>
    ├── proxy_v2_spec.md
    └── MD5SUMS.txt
```

Record the MD5 hash of every file in `MD5SUMS.txt`:

```
md5sum config.py auth.py session.py ... > checkpoints/CP_<timestamp>/MD5SUMS.txt
```

The checkpoint enables full rollback at any point. Never skip this step even
for "small" changes — the smallest changes cause the hardest-to-diagnose bugs.

---

## RULE 5 — IMPLEMENT ONLY AFTER EXPLICIT USER APPROVAL

Do not implement, generate, or modify any code without the user saying:

> "yes", "go ahead", "implement", "make the change"

or equivalent explicit approval.

The correct workflow is:

```
Identify issue or improvement
    → Document it (Rule 3 impact assessment)
    → Present to user
    → STOP and WAIT
    → Implement only on explicit approval
```

If you identify multiple issues, list all of them. Do not fix one while
reporting another. Do not implement anything speculatively.

---

## RULE 6 — PREPARE AND EXECUTE TEST CASES

After implementing, prepare test cases covering:

**Normal paths:**
- Single-turn text: OpenAI client → proxy → Gemini → OpenAI response
- Single-turn text: Native Gemini client → proxy → Gemini response
- Streaming text: OpenAI client receives delta chunks + `data: [DONE]`
- Streaming text: Native Gemini client receives raw SSE chunks

**Tool call paths:**
- Single tool call: `finish_reason: "tool_calls"` returned; `arguments` is
  JSON string
- Tool result return: `functionResponse` reconstructed with correct `name`
- Multi-step sequential: `thoughtSignature` reattached on every step
- Parallel tool calls: `thoughtSignature` on first part only; all
  `functionResponse` parts in single user turn

**Edge cases:**
- Missing `thoughtSignature` in session → bypass value injected
- Missing `X-Session-ID` header → UUID generated and returned
- Concurrent clients → session isolation verified
- Token expiry during request → refresh occurs without error

**Failure paths:**
- `400` from upstream → returned verbatim
- `429` from upstream → returned verbatim, proxy does NOT retry
- `504` timeout → JSON error returned
- Malformed JSON body → handled gracefully

Execute all tests. If human help is needed to run tests, ask explicitly.
**Do not claim tests passed without running them.**

---

## RULE 7 — CONFIRM TESTS PASSED

Report test results to the user stating:

- Which tests passed
- Which tests failed
- Why each failure occurred
- Whether failures are in scope of the change or pre-existing

**Do not proceed to UAT if any test in scope of the change failed.**

---

## RULE 8 — INFORM USER FOR UAT

After all tests pass, inform the user that User Acceptance Testing (UAT) is
required. Provide the UAT checklist:

**What to verify:**
- [ ] Gemini CLI single-turn request routes correctly and returns response
- [ ] Gemini CLI tool call completes full multi-turn cycle
- [ ] OpenAI client single-turn request returns correctly shaped response
- [ ] OpenAI streaming client receives delta chunks and `data: [DONE]`
- [ ] OpenAI client tool call: `finish_reason: "tool_calls"` received
- [ ] OpenAI client second tool turn: `thoughtSignature` correctly reattached
- [ ] `X-Session-ID` header returned in all responses
- [ ] Proxy startup banner printed with correct config values
- [ ] Token refresh logged correctly on expiry

**What commands to run:**
```bash
# Start proxy
python proxy.py

# Gemini CLI test
gemini -m gemini-3.1-pro-preview "Hello"

# OpenAI client test (curl)
curl -X POST http://localhost:5000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Session-ID: test-session-001" \
  -d '{"model":"gemini-3.1-pro-preview","messages":[{"role":"user","content":"Hello"}]}'
```

**What output to expect:**
- HTTP 200 with valid JSON body matching OpenAI or Gemini schema
- `X-Session-ID` present in response headers
- No `400 INVALID_ARGUMENT` errors from upstream
- Logs show correct emoji markers (🔵/🟢, 🚀, ←, 💾, 🔗)

---

## RULE 9 — REMOVE CHECKPOINT ONLY ON UAT PASS

Never delete a checkpoint automatically or speculatively.

Only delete a checkpoint after the user explicitly confirms:

> "UAT passed" or "checkpoint can be removed"

Checkpoints are retained indefinitely until this confirmation. Disk space is
not a reason to delete a checkpoint without user approval.

---

## RULE 10 — UPDATE SPEC SIMULTANEOUSLY WITH CODE

In the same edit session that changes code, update `proxy_v2_spec.md` to
reflect the change. Spec and code must never drift.

Specifically update:
- The relevant section describing the changed function or behaviour
- The Critical Rules table (§13) if a rule is added, changed, or removed
- The Testing Checklist (§15) if new scenarios are introduced
- The SPEC-CODE SYNC RECORD at the top of the spec

**A code change without a simultaneous spec update is incomplete.**

---

## RULE 11 — VERIFY SPEC-CODE SYNC

After every change, verify:

1. Every modified function has a docstring that accurately describes its
   current behaviour
2. Every inline `# RN:` comment correctly identifies the rule enforced
3. `TRACEABILITY.md` is updated to reflect any new or changed rule
   enforcement locations
4. The SPEC-CODE SYNC RECORD in `proxy_v2_spec.md` is updated with:
   - Current date
   - Version number (increment minor for bug fixes, major for new features)
   - Files changed
   - Rules affected

---

## Quick Reference — Correct Change Workflow

```
1. Read RULES.md (this file)                    ← you are here
2. Read proxy_v2_spec.md                        ← Rule 1
3. Read all affected .py files end to end       ← Rule 2
4. Write impact assessment                      ← Rule 3
5. Present assessment, WAIT for approval        ← Rule 5
6. Create checkpoint                            ← Rule 4
7. Implement change                             ← Rule 5 (after approval)
8. Update spec simultaneously                   ← Rule 10
9. Verify spec-code sync                        ← Rule 11
10. Prepare and run test cases                  ← Rule 6
11. Report test results                         ← Rule 7
12. Inform user for UAT                         ← Rule 8
13. On UAT pass: remove checkpoint on approval  ← Rule 9
```

---

## File Header Reference

Every `.py` file in this project contains the following header block as a
reminder. If you see this header, you are in a file governed by these rules:

```python
# ╔══════════════════════════════════════════════════════╗
# ║  MODIFICATION RULES — see RULES.md before editing   ║
# ║  R1: Read spec first  R2: Read full file             ║
# ║  R3: Impact assessment  R4: Checkpoint first         ║
# ║  R5: Wait for approval  R10: Update spec with code   ║
# ╚══════════════════════════════════════════════════════╝
```

---

## Critical Rules Cross-Reference

Any change touching the following areas must explicitly address the
corresponding Critical Rule from §13 of `proxy_v2_spec.md`:

| Area | Rules |
|---|---|
| Streaming responses | R1 |
| `thoughtSignature` storage | R2 |
| `thoughtSignature` injection position | R3 |
| Parallel function calls | R4, R17 |
| Sequential function calls | R5 |
| Tool argument serialisation | R6 |
| `finish_reason` mapping | R7 |
| SSE stream termination | R8 |
| `tool_call_id` ↔ `functionCall.id` mapping | R9 |
| Session concurrency | R10 |
| Request body reading | R11 |
| Model path rewriting | R12 |
| Server-side tool parts | R13 |
| Token management | R14 |
| Configuration access | R15, R16 |

---

*Last updated: 2026-06-18 | proxy_v2 v2.0*
