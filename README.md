# GSK Gemini Proxy v2

## Project Overview
GSK Gemini Proxy v2 (`proxy_v2`) is a robust, production-grade reverse proxy built with Flask. It serves as an intelligent intermediary between API clients (both OpenAI-compatible and native Gemini clients) and the GSK Kong API Gateway. It translates requests dynamically, handles OAuth2 authentication, orchestrates complex multi-turn state (such as tool-calling logic and thought signature injection), and enforces strict security protocols to comply with GSK VPC Service Controls. 

## Architecture/Layers
The proxy follows a strict, traceable 7-layer architecture:
* **Layer 0: Body Reading (`proxy.py`)** - Fully reads and buffers stream payloads (`read_body_once`) before route dispatching.
* **Layer 1: Route Dispatch (`proxy.py`)** - Handles endpoint routing, intercepts discovery capability requests locally (e.g., `/v1/models`), and returns synthetic `501 Not Implemented` responses for storage endpoints lacking upstream support.
* **Layer 2: Token Management (`auth.py`)** - Thread-safe OAuth2 lifecycle management. It fetches, caches, and proactively refreshes Bearer tokens via a background locking mechanism.
* **Layer 3: Session State (`session.py`)** - An in-memory, thread-safe store with TTL tracking. It maintains client states, maps `tool_call_id`s, caches `thoughtSignatures`, and stores server-side tool parts for seamless multi-turn reasoning.
* **Layer 4 & 5: Request/Response Translation (`translate/request.py`, `translate/response.py`)** - Bidirectional schema translation. It reformats OpenAI Chat Completions requests into Gemini REST structures, handles schema sanitization (restricting to OpenAPI-3.0 subset), and converts response formats (including streaming chunk translation).
* **Layer 6: Target Path Generation (`transforms/path.py`)** - Normalizes and rewrites URL paths for the GSK gateway.
* **Layer 7: Execution (`handlers/*.py`)** - Issues HTTP requests to the target URL, orchestrates streaming generators, and yields properly structured chunk deltas back to the client.

## Contracts Served
The proxy implements three concurrent API contracts to serve diverse clients:
* **Contract 1: OpenAI Chat Completions**
  Transparently translates standard OpenAI API requests into Gemini format (`handlers/openai_handler.py`). This includes comprehensive translation of tool definitions, handling of the Images API (`images_handler.py`), and context compaction via the Responses API (`responses_handler.py`).
* **Contract 2: Gemini REST Pass-Through**
  Supports native Gemini payloads, injecting authentication and enforcing pre-flight VPC Service Control scans (e.g., rejecting unauthorized `http://` fileData URIs) before transparently forwarding (`handlers/gemini_handler.py`).
* **Contract 3: Gemini Full Contract with Session Repair**
  Augments Contract 2 by stripping unsupported gateway fields (like `id` on function calls) and repairing or re-injecting missing `thoughtSignatures` for complex multi-turn or parallel tool calls, leveraging Layer 3's `SessionStore` (`handlers/gemini_handler.py`).

## Configuration
The application uses a strict single source of truth for configuration managed via `config.py`. 
It requires a `.env` file at the root of the project with the following required secrets. If any are missing, the application halts immediately with an `EnvironmentError`.

**Required `.env` Variables:**
* `GSK_OAUTH_URL` - The OAuth2 provider URL.
* `GSK_CLIENT_ID` - OAuth client identifier.
* `GSK_CLIENT_SECRET` - OAuth client secret.
* `GSK_GEMINI_BASE_URL` - Base URL for the target GSK Kong Gateway.

**Tunable Constants (Set in `config.py`):**
* `PROXY_PORT` (Default: 5000)
* `ALLOWED_MODEL` (Default: `gemini-3.1-pro-preview`)
* `SESSION_TTL_SECS` (Default: 1800s)
* `UPSTREAM_CONNECT_TIMEOUT`, `UPSTREAM_READ_TIMEOUT`, etc.

## How to Run

1. **Ensure Python 3.8+** is installed on your system.
2. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
   *(This installs `flask`, `requests`, and `python-dotenv`)*
3. **Configure Environment:**
   Create a `.env` file in the project directory matching the configuration requirements outlined above.
4. **Start the Proxy:**
   ```bash
   python proxy.py
   ```
   *The server will warm up the OAuth token to verify credentials, start the background TTL session cleaner, and bind to `localhost:5000` (or the `PROXY_PORT` defined).*