.PHONY: install test lint evals evals-llm bakeoff asr-prep asr-bench api scheduler agent topics up

install:
	pip install -e ".[agent,asr,bakeoff,dev]"

test:
	pytest -q

lint:
	ruff check src tests

evals:           ## offline safety gate (runs in CI)
	babbel-evals

evals-llm:       ## keyword rules + LLM classifier, then simulated conversations
	babbel-evals --classifier && babbel-evals --conversations

bakeoff:         ## every candidate in evals/models.yaml through the same tests (needs keys)
	babbel-bakeoff

asr-prep:        ## videos -> clips + manifest:  make asr-prep IN=~/Videos/oma DIALECT=wvl
	python asr/prepare_audio.py $(IN) --dialect $(or $(DIALECT),std) --append

asr-bench:       ## WER + safety phrases:  make asr-bench PROVIDER=elevenlabs
	python asr/benchmark.py asr/data/manifest.csv --provider $(or $(PROVIDER),openai) --phone

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
