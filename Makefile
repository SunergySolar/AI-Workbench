DC = docker compose -f ai/docker-compose.yml --env-file .env
DC_KOKORO = docker compose -f ai/docker-compose.kokoro.yml --env-file .env
DC_LITE = docker compose -f ai/docker-compose.litellm.yml --env-file .env
DC_UNSLOTH = docker compose -f ai/docker-compose.unsloth.yml --env-file .env
DC_VLLM = docker compose -f ai/docker-compose.vllm.yml --env-file .env
UP_FLAGS ?= -d --remove-orphans

setup:
	cd widget && uv sync && cd ..
	cd widget && uv run claude_usage_widget.py &
	$(DC) up --build $(UP_FLAGS)

up:
	$(DC) up $(UP_FLAGS)

up-litellm:
	$(DC_LITE) up -d litellm

up-unsloth:
	$(DC_UNSLOTH) up -d unsloth

up-vllm:
	$(DC_VLLM) up -d vllm-qwen vllm-llama

up-kokoro:
	$(DC_KOKORO) up -d kokoro-app kokoro-api

down:
	$(DC) stop

down-litellm:
	$(DC_LITE) stop litellm

down-unsloth:
	$(DC_UNSLOTH) stop unsloth

down-vllm:
	$(DC_VLLM) stop vllm-qwen vllm-llama

down-kokoro:
	$(DC_KOKORO) stop kokoro-app kokoro-api

clean:
	$(DC) down --volumes --remove-orphans

very-clean:
	$(DC) down --volumes --remove-orphans --rmi all

clean-litellm:
	$(DC_LITE) stop litellm && $(DC_LITE) rm -f litellm

clean-unsloth:
	$(DC_UNSLOTH) stop unsloth && $(DC_UNSLOTH) rm -f unsloth

clean-vllm:
	$(DC_VLLM) stop vllm-qwen vllm-llama && $(DC_VLLM) rm -f vllm-qwen vllm-llama

clean-kokoro:
	$(DC_KOKORO) stop kokoro-app kokoro-api && $(DC_KOKORO) rm -f kokoro-app kokoro-api

logs:
	$(DC) logs -f

logs-litellm:
	$(DC_LITE) logs -f litellm

logs-unsloth:
	$(DC_UNSLOTH) logs -f unsloth

logs-vllm:
	$(DC_VLLM) logs -f

logs-kokoro:
	$(DC_KOKORO) logs -f

build:
	$(DC) build

build-litellm:
	$(DC_LITE) build litellm

build-unsloth:
	$(DC_UNSLOTH) build unsloth

build-vllm:
	$(DC_VLLM) build

build-kokoro:
	$(DC_KOKORO) build kokoro-app kokoro-api

.PHONY: setup up up-litellm up-unsloth up-vllm up-kokoro down down-litellm down-unsloth down-vllm down-kokoro clean very-clean clean-litellm clean-unsloth clean-vllm clean-kokoro logs logs-litellm logs-unsloth logs-vllm logs-kokoro build build-litellm build-unsloth build-vllm build-kokoro
