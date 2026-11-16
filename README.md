# LoopBridge

A FastAPI control plane for provisioning **Nightscout** instances and building **AndroidAPS** apps through GitHub Actions. It combines account management, tenant provisioning, GitHub OAuth, encrypted build secrets and a small RAG assistant.

## Features

- User registration and JWT authentication
- Nightscout tenant provisioning on Docker
- GitHub OAuth linking for AndroidAPS build workflows
- Optional encrypted GitHub Actions secrets for build artifacts
- Workflow dispatch and run-status polling
- Optional RAG chat over a project knowledge index
- Web frontend for onboarding and tenant management

## Stack

- Python 3.12 / FastAPI
- SQLAlchemy async + SQLite or PostgreSQL
- MongoDB/Docker provisioning for Nightscout
- Redis/ARQ optional background jobs
- Qdrant + embeddings for optional RAG
- GitHub REST API for OAuth, forks, secrets and Actions

## Repository layout

```text
main.py            FastAPI application and API routes
auth.py            JWT/password helpers
models.py          SQLAlchemy models
config.py          local settings
config_prod.py     production settings profile
provisioner.py     Docker/Nightscout provisioning
github_client.py   GitHub OAuth, Actions and secrets
rag_chat.py        RAG answer path
rag_ingest.py      knowledge ingestion
backend/           supporting backend modules
frontend/          onboarding and tenant UI
requirements.txt   Python dependencies
```

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload --host 127.0.0.1 --port 8010
```

Open `http://127.0.0.1:8010`.

> The local `loopbridge.db` database, `.env` and logs are intentionally excluded from Git. Create a fresh environment when running locally.

## Configuration

Create `.env` locally and fill the values you need. Important groups:

- Application: `DOMAIN`, `BACKEND_SECRET_KEY`, `ENCRYPTION_KEY`
- Database: `DATABASE_URL`
- MongoDB provisioning: `MONGO_ADMIN_USER`, `MONGO_ADMIN_PASS`, `MONGO_HOST`
- GitHub OAuth: `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`
- Docker: `DOCKER_NETWORK`, `NIGHTSCOUT_IMAGE`
- Optional RAG: `QDRANT_URL`, `DEEPSEEK_API_KEY`, embedding settings
- Optional email: `POSTMARK_SERVER_TOKEN`, `EMAIL_FROM`

Never commit `.env` or generated keys. `ENCRYPTION_KEY` must be a valid Fernet key and must remain stable across restarts; otherwise encrypted GitHub links cannot be decrypted.

## GitHub OAuth

Create a GitHub OAuth application and set the callback URL to:

```text
https://<your-host>/auth/github/callback
```

The application requests only the scopes needed for the selected workflow. Build secrets are encrypted server-side before being sent to GitHub; they are not stored in plaintext in Git.

## Docker / Nightscout

The provisioner creates a per-tenant Nightscout container and MongoDB database. The Docker socket must not be exposed publicly. Run the API on a private interface behind a reverse proxy and restrict access to the provisioning control plane.

## Security

- Use a long random `BACKEND_SECRET_KEY`.
- Use a stable Fernet `ENCRYPTION_KEY` stored in a secret manager.
- Do not expose the Docker socket or MongoDB port publicly.
- Keep GitHub OAuth secrets and tokens out of logs.
- Treat Nightscout data as health data and apply the appropriate privacy controls.
- This repository contains **no production `.env`, database, uploaded data or credentials**.

## Tests

```bash
pytest
```

The original project did not ship a dedicated test suite; add tests before extending provisioning or authentication behavior.
