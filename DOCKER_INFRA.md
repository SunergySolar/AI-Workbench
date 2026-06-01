# Docker Infrastructure

All services are orchestrated through a single `docker-compose.yml` that includes three sub-composes. Each can also be run independently.

## Main compose

```bash
docker compose up -d
```

`docker-compose.yml` includes:

| Sub-compose | Service(s) | What it runs |
|---|---|---|
| [LiteLLM](DOCKER_LITELLM.md) | `litellm`, `db`, `prometheus` | LiteLLM proxy + PostgreSQL + metrics |
| [Unsloth](DOCKER_UNSLOTH.md) | `unsloth` | Unsloth environment with CUDA-compiled llama.cpp |
| [vLLM](DOCKER_VLLM.md) | `vllm-qwen`, `vllm-llama` | Multi-model vLLM serving |
