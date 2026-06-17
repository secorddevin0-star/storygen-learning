# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repository Is

A **progressive learning project** that builds an AI-powered children's story generator step by step. The repo contains 11 numbered folders, each representing a milestone from a basic agent to a production CI/CD deployment. The canonical reference implementation is `06_Final_Solution/` and `04b_Manual_Deployment_Done/` (which the CI/CD pipeline deploys). Each `*_Ready` folder contains the starting scaffold for a learning exercise; the corresponding `*_Done` folder contains the completed solution.

## Environment Setup

### Shared Python Virtual Environment

A single `.venv` lives at the repo root and is shared by all backend folders.

```bash
# First-time setup (run once)
cd 00_Starting_Here && ./setup-shared-venv.sh

# Activate from any location
source .venv/bin/activate   # from repo root
source ../../.venv/bin/activate  # from inside any backend/ subfolder
```

### Environment Variables

Copy `env.template` to `.env` at the repo root and fill in:

```
GOOGLE_API_KEY=           # Google AI Studio API key
GOOGLE_CLOUD_PROJECT_ID=  # GCP project ID
GOOGLE_GENAI_USE_VERTEXAI=FALSE  # FALSE = AI Studio (free), TRUE = Vertex AI
GENMEDIA_BUCKET=          # GCS bucket name for generated images (e.g. <project-id>-bucket)
```

Each backend folder also reads a local `.env` — typically you create it via `cp env.example .env` inside `backend/`.

### SSL Certificate (required for ADK)

ADK's Google API calls require this environment variable:

```bash
export SSL_CERT_FILE=$(python -m certifi)
```

## Development Commands

### Full Stack (Docker — recommended)

```bash
docker-compose up --build   # starts frontend (:3000) and backend (:8000)
docker-compose down
```

### Backend (standalone)

```bash
source .venv/bin/activate
cd <stage>/backend
export SSL_CERT_FILE=$(python -m certifi)
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Or use the stage's `start.sh` script, which handles auth, venv activation, frontend build, and backend launch:

```bash
cd 01a_First_Agent_Ready && ./start.sh
```

### ADK Web Interface (interactive agent testing)

```bash
source .venv/bin/activate
cd <stage>/backend
adk web .
```

### Frontend

```bash
cd <stage>/frontend
npm install
npm run dev      # dev server on :3000
npm run build    # production build (outputs to out/ for static serving)
npm run lint     # ESLint
```

## Architecture

### Two-Agent Pipeline

Each request runs a sequential two-step workflow in `main.py`:

1. **StoryAgent** (`story_agent/agent.py`) — `LlmAgent` backed by `gemini-2.5-flash`. Takes user keywords, returns a structured JSON object with the complete story, character list, and 4 scenes. No tools; output is pure text parsed as JSON in `main.py`.

2. **Image generation** — handled by `DirectImageFunction` (wrapping `ImagenTool`), called directly in `main.py` rather than via an ADK agent tool call. Uses Vertex AI `imagegeneration@006` (Imagen). Images are uploaded to GCS and returned as public URLs; base64 is used as fallback when no bucket is configured.

In stage `03*`, there is also a standalone `image_agent.py` (`LlmAgent` + `ImagenTool` as an ADK tool) used for evaluation and interactive ADK web testing.

### Story JSON Schema (the contract between agents)

StoryAgent always returns this structure:

```json
{
  "story": "Complete narrative text...",
  "main_characters": [
    { "name": "...", "description": "Very detailed visual description..." }
  ],
  "scenes": [
    { "index": 1, "title": "The Setup", "description": "Action/setting only — no character appearance", "text": "Scene narrative..." },
    { "index": 2, "title": "The Inciting Incident", ... },
    { "index": 3, "title": "The Climax", ... },
    { "index": 4, "title": "The Resolution", ... }
  ]
}
```

**Key design decision:** `scene.description` contains only action/setting. Character visual details live exclusively in `main_characters[].description`. `main.py` reconstructs the full image prompt by combining the scene description with all character descriptions, so every Imagen call includes consistent character references.

### WebSocket Protocol

Frontend connects to `ws://backend/ws/{user_id}`. Message types (JSON `{type, data}`):

| Direction | Type | Meaning |
|-----------|------|---------|
| → server | `generate_story` | `data` = keywords string |
| → server | `ping` | keepalive |
| ← client | `connected` | handshake confirmation |
| ← client | `processing` | story generation started |
| ← client | `story_complete` | `data` = story text with `[SCENE N]` markers |
| ← client | `image_generated` | `data` = `{index, scene_title, gcs_url?, base64?, stored_in_bucket, error?}` |
| ← client | `turn_complete` | all generation done |
| ← client | `error` | `message` = error string |

### Frontend–Backend Integration

The Next.js frontend is built as a static export (`npm run build` → `out/`). In production and Docker, the FastAPI backend mounts and serves the static files directly. `STATIC_FILES_DIR` env var controls the path (defaults to `../frontend/out`). `NEXT_PUBLIC_BACKEND_URL` is baked into the frontend build at build time to point WebSocket connections at the correct backend URL.

## Deployment

### Manual (Cloud Run)

See `04a_Manual_Deployment_Ready/` → `04b_Manual_Deployment_Done/` for step-by-step scripts.

### CI/CD (GitHub Actions)

`06_Final_Solution/.github/workflows/ci-cd.yml` triggers on pushes to `main`. Pipeline:
1. Enable GCP APIs, create Artifact Registry repo, GCS bucket, and Secret Manager secret
2. Build and push backend Docker image from `04b_Manual_Deployment_Done/backend`
3. Build and push frontend Docker image (injects `NEXT_PUBLIC_BACKEND_URL` from step 2 output)
4. Deploy both to Cloud Run (backend port 8080, frontend port 3000)
5. Health check `GET /health`

Configuration lives in `06_Final_Solution/.github/config/deployment.yml` (project ID, region, service names, resource limits). The Google API key is stored in Secret Manager and injected as `GOOGLE_API_KEY` at runtime; it is NOT in environment variables directly.
