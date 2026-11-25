# Darwin — common tasks. `make help` lists them.
.DEFAULT_GOAL := help
COMPOSE := docker compose -f docker/docker-compose.yml

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## install dependencies and the secret-guard git hook
	uv sync
	git config core.hooksPath scripts/git-hooks

flagsmith-up: ## start self-hosted Flagsmith with Experimentation on :8000
	$(COMPOSE) up -d
	@until curl -sf http://localhost:8000/health/liveness/ >/dev/null; do sleep 2; done; echo "Flagsmith is up on http://localhost:8000"

flagsmith-setup: ## create admin user, project, environment, keys and warehouse; write .env
	uv run python scripts/setup_flagsmith.py

flagsmith-down: ## stop Flagsmith (keeps its data)
	$(COMPOSE) down

flagsmith-reset: ## wipe Flagsmith and Darwin's run data, then set both up again
	$(COMPOSE) down -v
	rm -f docker/.flagsmith-local.json data/lab.json data/transcripts.jsonl data/flagsmith-commands.jsonl
	$(MAKE) flagsmith-up flagsmith-setup

init: ## create the shopbot_brain flag and metrics; register gen1 as champion
	uv run darwin init

shopbot: ## serve ShopBot on :8100 (chat page + API)
	uv run darwin shopbot

lab: ## serve the Darwin Lab dashboard on :8200
	uv run darwin lab

round: ## send 150 simulated customers to ShopBot
	uv run darwin round --chats 150

evolve: ## ask the Coach for a challenger
	uv run darwin evolve

status: ## show lineage, experiment and pending approvals
	uv run darwin status

test: ## run the test suite (no network, no API spend)
	uv run pytest

demo: ## print the demo steps
	@echo "1. make flagsmith-up flagsmith-setup init   (once)"
	@echo "2. make shopbot        (terminal 1)"
	@echo "3. make lab            (terminal 2) → http://127.0.0.1:8200"
	@echo "4. make round          (terminal 3) → gen1 baseline traffic"
	@echo "5. In the Lab: Ask the Coach → review → Approve launch"
	@echo "6. make round          → traffic for the experiment; the Lab checks Flagsmith"
	@echo "7. In the Lab: Approve ship / kill. Repeat from 5 with goal 'cut cost'."

.PHONY: help setup flagsmith-up flagsmith-setup flagsmith-down flagsmith-reset init shopbot lab round evolve status test demo
