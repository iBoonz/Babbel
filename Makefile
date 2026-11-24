.PHONY: install test lint evals api scheduler agent topics up

install:
	pip install -e ".[agent,asr,dev]"

test:
	pytest -q

lint:
	ruff check src tests

evals:           ## offline safety gate (runs in CI)
	babbel-evals

evals-llm:       ## keyword rules + LLM classifier, then simulated conversations
	babbel-evals --classifier && babbel-evals --conversations

api:
	babbel-api

scheduler:
	babbel-scheduler

agent:
	python -m babbel.agent.worker dev

topics:
	babbel-topics

up:
	docker compose up --build
