# Convenience targets for POSIX shells. On Windows without `make`, the same steps are:
#   uv run python scripts/init_db.py
#   uv run python scripts/run_demo.py
#   uv run pytest -q
# (scripts/run_dev.ps1 and scripts/run_dev.sh start the two services by hand.)

UV ?= uv
PORT_AGENT ?= 8000
PORT_BUSINESS ?= 8001

.PHONY: bootstrap dev dev-http agent business seed demo test lint format spike clean-db docker-build docker-up docker-migrate

bootstrap: ## install deps + create tables + load demo fixtures
	$(UV) sync --group dev
	$(UV) run python scripts/init_db.py

dev: ## agent (inprocess driver) + business API, no docker
	BACKEND_DRIVER=inprocess ./scripts/run_dev.sh

dev-http: ## agent calling the business API over real HTTP
	BACKEND_DRIVER=http ./scripts/run_dev.sh

agent:
	$(UV) run python -m uvicorn shopping_backend.agent_api.main:app --host 127.0.0.1 --port $(PORT_AGENT) --reload

business:
	$(UV) run python -m uvicorn shopping_backend.business_api.main:app --host 127.0.0.1 --port $(PORT_BUSINESS) --reload

seed:
	$(UV) run python scripts/init_db.py

demo: ## the seven end-to-end checks against live services
	$(UV) run python scripts/run_demo.py

test:
	$(UV) run pytest -q

lint:
	$(UV) run ruff check src tests scripts

format:
	$(UV) run ruff format src tests scripts

spike: ## re-verify the LangGraph 1.x assumptions the design rests on
	$(UV) run python scripts/spike_langgraph.py

clean-db: ## dry-run of what would be deleted
	$(UV) run python scripts/reset_db.py

# --- M7: requires Docker, which is not installed on the current machine ---

docker-build: ## build both image targets (business, agent)
	docker build -f deploy/Dockerfile --target business -t shopping-business .
	docker build -f deploy/Dockerfile --target agent    -t shopping-agent .

docker-up: ## mysql + business + agent
	docker compose -f deploy/docker-compose.yml up -d

docker-migrate:
	docker compose -f deploy/docker-compose.yml run --rm business alembic upgrade head
