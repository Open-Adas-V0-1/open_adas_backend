# Open_Adas_Backend

## Getting started

```bash
# 1. Copy env template and fill in local values
cp .env.example .env

# 2. Start Postgres + MinIO (reads DB_* / MINIO_* vars from .env at the repo root)
docker compose -f docker/docker-compose.yml --env-file .env up -d

# 3. Install dependencies
uv sync   # or: pip install -r requirements.txt

# 3b. Apply DB migrations
alembic upgrade head

# 4. Start the app
uvicorn app.main:app --reload
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 5. Check the DB + storage connection
curl http://127.0.0.1:8000/health
# => {"db":"ok","storage":"ok"}
```
source .venv/bin/activate

## LLM factory (T3)

Provider, model, key and base URL are all env-driven — set in `.env`:
`LLM_BACKEND` (gpt|ollama|gemini|claude|capgemini), `LLM_MODEL_ID`, `LLM_API_KEY`,
`LLM_BASE_URL`. Per-node overrides: `LLM_OVERRIDE_{NODE}_BACKEND` / `_MODEL_ID`.

```bash
python -m scripts.smoke_test_t3
```