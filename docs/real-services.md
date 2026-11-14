# Real Service Testing

## OpenAI-compatible models

Set these environment variables in a local env file or in the shell:

- SUPER_AGENT_BASE_URL: provider API base, usually ending in /v1
- SUPER_AGENT_API_KEY: provider key, or any local gateway token
- SUPER_AGENT_MODEL: provider model identifier
- SUPER_AGENT_MODEL_TIMEOUT_SECONDS: request timeout, default 60

Then run:

    uv run super-agent model-check
    uv run pytest tests/integration/test_real_model.py -rs

The real test is skipped when any required variable is missing. The rest of the test suite never requires external credentials.

## Provider examples

| Provider type | Base URL example | Notes |
| --- | --- | --- |
| OpenAI-compatible cloud | https://api.openai.com/v1 | Use the provider model name and API key. |
| DeepSeek-compatible | https://api.deepseek.com/v1 | Use the account key and a supported chat model. |
| OpenRouter-compatible | https://openrouter.ai/api/v1 | Use the routed model identifier. |
| Ollama local | http://localhost:11434/v1 | The key may be a local placeholder if the gateway accepts it. |
| LM Studio local | http://localhost:1234/v1 | Use the loaded local model identifier. |

## Magpie local model gateway

Magpie is supported as a first-class text ModelGateway. Its local gateway exposes OpenAI Chat Completions and a unified model catalog for subscriptions, provider API keys, local Ollama or LM Studio models, and routing groups.

Default project settings use the local gateway at port 3425. Set the Magpie base URL, app identity key, and a model ID from the catalog. Then run the magpie-check command to inspect the gateway and visible hosted models.

    uv run super-agent magpie-check

To send a real chat request through Magpie, set the normal model variables to the same gateway URL and a catalog model ID, then run the model-check command. Magpie image/video routes remain separate media capabilities; this project uses its text gateway for ModelGateway calls.

Example configuration (use a model ID returned by `magpie-check`):

    SUPER_AGENT_BASE_URL=http://127.0.0.1:3425/v1
    SUPER_AGENT_API_KEY=magpie-super-ai-agent
    SUPER_AGENT_MODEL=cpa/gpt-6.1-sol

The adapter was verified against a running local Magpie gateway: the model catalog returned 19 models and a hosted-model chat request returned `model-ok`. Do not commit the API key or put it in logs.

The control panel registers the read-only `file.read` tool for the configured
`SUPER_AGENT_WORKSPACE_ROOT` (default: the process working directory). The tool is
capability-checked and path-restricted; command execution and writes remain disabled
by default.

External systems can submit the same Agent runtime through
`POST /api/webhooks/{source}`. Set `SUPER_AGENT_WEBHOOK_SECRET` to require an
`X-Super-Agent-Signature: sha256=<hex>` HMAC-SHA256 header over the canonical
JSON request body.

### Why it is not shown as an Agent inside Magpie

Magpie's official integration model separates three cases: an app can call the local gateway with no Magpie code changes; an Agents-page row is provided by a built-in Magpie adapter; and a new provider/model source is provided by a plugin. There is no runtime API for an external app to register an Agent row. A Super AI Agent row therefore requires a pull request to Magpie's `internal/agent` adapter registry and a Magpie release. This repository currently implements the first case: Super AI Agent is a text client of Magpie and can use every model returned by `/v1/models`.

## Database and service checks

For the local default database:

    uv run alembic downgrade base
    uv run alembic upgrade head
    uv run pytest

For PostgreSQL, set SUPER_AGENT_DATABASE_URL to a PostgreSQL SQLAlchemy URL and run the same migration commands after starting PostgreSQL.

