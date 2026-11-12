.PHONY: install download-files web secrets token mcp billing webserver agent console test lint format eval-text eval-voice probes cost rag-eval

PY := uv run

install:            ## Python deps (incl. dev) + frontend deps
	uv sync
	cd web && npm ci

download-files:     ## fetch plugin model files once (the Docker image bakes them in)
	$(PY) python -m livekit.agents download-files

web:                ## build the browser client into web/dist (served by the web server)
	cd web && npm run build

secrets:            ## print fresh values for the shared secrets in .env
	@$(PY) python -c "import secrets as s; [print(f'{k}={s.token_urlsafe(n)}') for k, n in (('MCP_JWT_SECRET', 48), ('BILLING_AGENT_TOKEN', 24), ('INTERNAL_API_TOKEN', 24), ('OPERATOR_PASSCODE', 9))]"

AUDIENCE ?=

token:              ## mint the voice agent's MCP token (needs MCP_JWT_SECRET); Docker: AUDIENCE=http://mcp:8765/mcp
	$(PY) python -m receptionist.mcp_server.mint_token --sub voice-agent $(if $(AUDIENCE),--audience $(AUDIENCE))

mcp:                ## MCP server (booking + RAG) on :8765
	$(PY) python -m receptionist.mcp_server

billing:            ## A2A billing agent on :8766
	$(PY) python -m receptionist.billing_agent

webserver:          ## token server, operator console and web client on :8000
	$(PY) python -m receptionist.web

agent:              ## LiveKit voice agent worker (dev mode, hot reload)
	$(PY) python -m receptionist.agent dev

console:            ## talk to the agent in your terminal (mic + speakers), no browser
	$(PY) python -m receptionist.agent console

test:               ## full offline test suite (voice harness tests need livekit-server on PATH)
	$(PY) pytest -q

lint:
	$(PY) ruff check src tests
	$(PY) ruff format --check src tests
	cd web && npm run lint --if-present && npm run typecheck --if-present

format:
	$(PY) ruff format src tests
	$(PY) ruff check --fix src tests

ARCH ?= pipeline realtime

eval-text:          ## persona simulations over text, graded (needs OPENAI_API_KEY + provider keys)
	$(PY) python -m receptionist.evals text --arch $(ARCH)

eval-voice:         ## persona simulations over real audio through a local LiveKit server
	$(PY) python -m receptionist.evals voice --arch $(ARCH)

probes:             ## scripted TTFA / barge-in / backchannel probes over real audio
	$(PY) python -m receptionist.evals probes --arch $(ARCH) --calls 5

cost:               ## per-call cost model for each architecture
	$(PY) python -m receptionist.evals.cost

rag-eval:           ## retrieval quality on the golden set
	$(PY) python -m receptionist.mcp_server.rag.evaluate
