# Dynamic Video AI Agent Platform 🎭🎬

> **PoC built with AWS SAM · LangGraph · ElevenLabs TTS · D-ID Video Avatars**
> 
> Create specialized AI personas on-the-fly and chat with them — your agent replies with a rendered talking-head video using your (or any) D-ID avatar.

---

## Table of Contents
1. [Architecture Overview](#architecture-overview)
2. [Prerequisites & API Keys](#prerequisites--api-keys)
3. [Getting Your Free API Keys](#getting-your-free-api-keys)
4. [How to Create Your Own D-ID Likeness Avatar](#how-to-create-your-own-d-id-likeness-avatar)
5. [Local Development (sam local)](#local-development-sam-local)
6. [Deploy to AWS](#deploy-to-aws)
7. [Running the Frontend](#running-the-frontend)
8. [End-to-End Test Walkthrough](#end-to-end-test-walkthrough)
9. [Project Structure](#project-structure)
10. [Configuration Reference](#configuration-reference)
11. [Troubleshooting](#troubleshooting)
12. [Future Scaling](#future-scaling)

---

## Architecture Overview

```
Browser (HTML + Tailwind)
│
│  POST /create-agent  ──►  Lambda (Coordinator)
│                              │
│                              ├─ AgentRegistry.register()
│                              └─ LangGraph graph compiled (cached)
│
│  POST /chat  ──────────►  Lambda (Coordinator)
│                              │
│                         ┌────▼────────────────────────────┐
│                         │  LangGraph (StateGraph)          │
│                         │   system_prompt injected         │
│                         │   gpt-4o / claude-3.5 LLM call   │
│                         └────┬────────────────────────────┘
│                              │  reply_text
│                         ┌────▼──────────────┐
│                         │  ElevenLabs TTS    │  → audio bytes (MP3)
│                         └────┬──────────────┘
│                              │  base64 audio URL
│                         ┌────▼──────────────┐
│                         │  D-ID /talks API   │  → video_url (MP4)
│                         └────┬──────────────┘
│                              │
│◄─────────────────────────────┘  { reply, video_url, audio_url }
│
Video plays automatically in chat bubble
```

**Key choices:**
- **Single Lambda** handles all routes — no over-engineering for a PoC
- **LangGraph StateGraph** compiles once per agent, cached in Lambda memory (warm-start)
- **In-memory sessions** — fast, zero cost; swap to DynamoDB trivially
- **HTTP API Gateway** (not REST API) — 71% cheaper, lower latency
- **Base64 audio URI to D-ID** — no S3 bucket needed for PoC

---

## Prerequisites & API Keys

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.12+ | `python --version` |
| AWS CLI | v2+ | `aws --version` |
| AWS SAM CLI | 1.120+| `sam --version` |
| Node.js | 20+ (optional) | Only for Vite frontend |
| Docker | Latest (optional) | For `sam build --use-container` |

---

## Getting Your Free API Keys

### 1. OpenAI API Key (`OPENAI_API_KEY`)

1. Go to **https://platform.openai.com/api-keys**
2. Click **+ Create new secret key**
3. Copy the key (starts with `sk-proj-...`)
4. **Free tier**: $5 credit on signup — more than enough to test

> **Alternatively use Anthropic** (same steps):
> - https://console.anthropic.com/settings/keys
> - Set `LLM_PROVIDER=anthropic` and `LLM_MODEL=claude-3-5-sonnet-20241022`

---

### 2. ElevenLabs API Key (`ELEVENLABS_API_KEY`)

1. Sign up at **https://elevenlabs.io** (free tier: 10,000 chars/month)
2. Go to **Profile → API Key** (top-right avatar menu)
3. Click **Copy** on your API key
4. Browse free voices at **https://elevenlabs.io/voice-library**
   - Click any voice → **ID** button → copy the voice ID

**Recommended free voices:**
| Name | Voice ID | Style |
|------|----------|-------|
| Rachel | `21m00Tcm4TlvDq8ikWAM` | Calm, female |
| Adam | `pNInz6obpgDQGcFmaJgB` | Neutral, male |
| Bella | `EXAVITQu4vr4xnSDxMaL` | Warm, female |
| Josh | `TxGEqnHWrfWFTfGW9XjX` | Deep, male |

---

### 3. D-ID API Key (`DID_API_KEY`)

1. Sign up at **https://studio.d-id.com** (free: 5 minutes of video/month)
2. Go to **Account → API** (or **https://studio.d-id.com/account/api**)
3. Copy your API key
4. Encode it for the `Authorization: Basic` header:

```bash
# Encode  your_email:your_api_key  in base64
python -c "import base64; print('Basic ' + base64.b64encode(b'you@email.com:YOUR_KEY').decode())"
```

5. Set `DID_API_KEY` to the full `Basic <base64>` string

**D-ID Free stock avatars:**
| Presenter | ID |
|-----------|----|
| Amy (female, professional) | `amy-jcu8MFXSuU` |
| Anna (female, warm) | `anna-costume1-cDEyAA` |
| Josh (male, friendly) | `josh-7b9...` |

Browse all: **https://docs.d-id.com/reference/presenters**

---

## How to Create Your Own D-ID Likeness Avatar

This is the coolest feature — your agent speaks with **your own face**.

### Step 1: Go to D-ID Studio
Navigate to **https://studio.d-id.com/agents**

### Step 2: Create a New Agent
1. Click **"+ New Agent"** (blue button, top right)
2. In the presenter selection screen, click **"Upload Photo"**

### Step 3: Upload Your Photo
1. Upload a **clear, front-facing photo** of yourself
   - Requirements: ≥ 512×512 px, face clearly visible, neutral expression
   - JPG or PNG format
   - Good lighting, no sunglasses
2. D-ID will process the photo (takes ~30 seconds)

### Step 4: Get Your Custom Avatar ID
1. After processing, your avatar appears in the presenter grid
2. Click on it — note the URL changes to something like:
   `https://studio.d-id.com/agents?presenter=pers_AbCdEfGh`
3. The ID after `pers_` is your **CUSTOM_AVATAR_ID**
4. Alternatively, use the D-ID API:

```bash
curl https://api.d-id.com/presenters \
  -H "Authorization: Basic YOUR_BASE64_KEY" | python -m json.tool
```

Look for your uploaded presenter in the JSON response — copy the `"id"` field.

### Step 5: Use Your Custom Avatar
In `sam-app/env.json` (for local) or `samconfig.toml` (for deploy):
```
DID_PRESENTER_ID=YOUR_CUSTOM_AVATAR_ID
```

Or when creating an agent via the UI — expand **"Advanced options"** and paste your avatar ID.

---

## Local Development (sam local)

### Step 1: Install SAM CLI
```powershell
# Windows — using winget
winget install Amazon.SAM-CLI

# Or download from: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html
```

### Step 2: Clone / enter project
```powershell
cd C:\dynamic-video-agents-poc-sam\sam-app
```

### Step 3: Create your local environment file
```powershell
Copy-Item env.json.example env.json
# Now edit env.json with your real API keys
notepad env.json
```

### Step 4: Install layer dependencies
```powershell
# Install Python packages into the layer directory
pip install -r layers\dependencies\requirements.txt -t layers\dependencies\python\
```

### Step 5: Build the SAM app
```powershell
sam build
```

Expected output:
```
Build Succeeded
Built Artifacts  : .aws-sam/build
...
DependenciesLayer: layers/dependencies/
CoordinatorFunction: lambdas/coordinator/
```

### Step 6: Start local API
```powershell
sam local start-api --env-vars env.json --port 3001 --warm-containers EAGER
```

The API is now running at `http://localhost:3001`

### Step 7: Verify health endpoint
```powershell
Invoke-WebRequest -Uri "http://localhost:3001/health" -Method GET | Select-Object -ExpandProperty Content
```

Expected: `{"status": "healthy", "agents_loaded": 0, ...}`

---

## Deploy to AWS

### Step 1: Configure AWS credentials
```powershell
aws configure
# Enter: Access Key ID, Secret Access Key, Region (e.g. us-east-1), json
```

### Step 2: Edit samconfig.toml
Open `sam-app\samconfig.toml` and fill in `parameter_overrides` with your real API keys.

### Step 3: First deploy (guided)
```powershell
cd C:\dynamic-video-agents-poc-sam\sam-app
sam build
sam deploy --guided
```

Follow the prompts — SAM will ask for stack name, region, and confirmation.

### Step 4: Note the API URL
After deploy completes, you'll see:
```
Outputs
-------
Key   ApiBaseURL
Value https://abc123xyz.execute-api.us-east-1.amazonaws.com/dynamic-video-agents-poc-sam
```

Copy this URL — you'll need it for the frontend.

### Subsequent deploys
```powershell
sam build ; sam deploy
```

### Tear down (delete all resources)
```powershell
sam delete
```

---

## Running the Frontend

The frontend is pure HTML/JS — no build step required.

### Option A: Open directly in browser (simplest)
```powershell
# Set your API URL first
$env:API_URL = "http://localhost:3001"  # or your deployed API Gateway URL

# Open in browser
Start-Process "C:\dynamic-video-agents-poc-sam\frontend\index.html"
```

> **Note**: When opening `file://` directly, you may hit CORS issues with the API.
> Use Option B for local development.

### Option B: Serve with Python (recommended for local dev)
```powershell
cd C:\dynamic-video-agents-poc-sam\frontend
python -m http.server 5500
```

Then open: **http://localhost:5500?api=http://localhost:3001**

The `?api=` query param tells `app.js` where the backend is.

### Option C: Serve with Node.js live-server
```powershell
npm install -g live-server
cd C:\dynamic-video-agents-poc-sam\frontend
live-server --port=5500 --open="/?api=http://localhost:3001"
```

### Option D: Deploy to S3 + CloudFront (production)

```powershell
# Create S3 bucket
$BUCKET = "video-agents-frontend-$(Get-Random)"
aws s3 mb "s3://$BUCKET" --region us-east-1

# Enable static website hosting
aws s3 website "s3://$BUCKET" --index-document index.html

# Upload files
aws s3 sync C:\dynamic-video-agents-poc-sam\frontend "s3://$BUCKET" --delete

# Make public
aws s3api put-bucket-policy --bucket $BUCKET --policy '{
  "Version":"2012-10-17",
  "Statement":[{"Effect":"Allow","Principal":"*","Action":"s3:GetObject","Resource":"arn:aws:s3:::BUCKET/*"}]
}'.Replace('BUCKET',$BUCKET)

# Set API URL by adding to index.html before <script src="app.js">:
# <script>window.API_BASE_URL = "https://YOUR-API-GATEWAY-URL";</script>
```

---

## End-to-End Test Walkthrough

### 1. Start everything
```powershell
# Terminal 1: Start SAM backend
cd C:\dynamic-video-agents-poc-sam\sam-app
sam local start-api --env-vars env.json --port 3001

# Terminal 2: Serve frontend
cd C:\dynamic-video-agents-poc-sam\frontend
python -m http.server 5500
```

### 2. Open the app
Navigate to: **http://localhost:5500?api=http://localhost:3001**

You should see the green "Connected" dot in the sidebar footer.

### 3. Create your first agent
1. Click **"Create New Agent"** (blue button in sidebar)
2. Click **"🏛️ Rome Historian"** quick template to fill the form
3. (Optional) Expand **Advanced options** → paste your `CUSTOM_AVATAR_ID`
4. Click **Create Agent** → you'll see a spinner while the Lambda warms up
5. The agent appears in the sidebar and is auto-selected

### 4. Chat with your agent
1. Type: **"Tell me about Julius Caesar's assassination"**
2. Press **Enter** or click the send button
3. Watch the loading states:
   - `🧠 Thinking…` — LangGraph calling OpenAI/Anthropic
   - `🔊 Generating speech…` — ElevenLabs TTS
   - `🎬 Rendering your video…` — D-ID polling
4. The agent's text reply appears, then a video plays automatically

### 5. Try voice input
1. Click the 🎙️ microphone button (turns red when active)
2. Speak your question
3. Click again to stop — speech is auto-sent

### 6. Try with your own avatar
1. Create a new agent (or edit existing)
2. In **Advanced options → D-ID Avatar ID**, paste your custom presenter ID
3. The video will now use your own face!

### 7. Quick API test (without browser)
```powershell
# Health check
Invoke-RestMethod -Uri "http://localhost:3001/health"

# Create agent
$body = @{
  name          = "Test Agent"
  system_prompt = "You are a helpful and enthusiastic test assistant. Keep responses short."
} | ConvertTo-Json

$agent = Invoke-RestMethod -Uri "http://localhost:3001/create-agent" `
  -Method POST -ContentType "application/json" -Body $body

Write-Host "Agent ID: $($agent.agent_id)"

# Chat (text only, no video)
$chat = @{
  agent_id       = $agent.agent_id
  message        = "Hello! What can you help me with?"
  generate_video = $false
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://localhost:3001/chat" `
  -Method POST -ContentType "application/json" -Body $chat | Select-Object reply, session_id
```

---

## Project Structure

```
dynamic-video-agents-poc-sam/
│
├── sam-app/                          # AWS SAM backend
│   ├── template.yaml                 # SAM/CloudFormation template
│   ├── samconfig.toml                # SAM CLI config (deploy params)
│   ├── env.json.example              # → copy to env.json, fill keys
│   ├── .env.example                  # Human-readable env var reference
│   │
│   ├── lambdas/
│   │   └── coordinator/
│   │       ├── app.py                # Lambda handler (routing, sessions)
│   │       ├── agents.py             # LangGraph agent logic
│   │       ├── video.py              # ElevenLabs TTS + D-ID video
│   │       └── requirements.txt      # IDE reference (layer has the actual installs)
│   │
│   └── layers/
│       └── dependencies/
│           ├── requirements.txt      # Packages installed into Lambda Layer
│           └── python/               # pip install target (git-ignored)
│
├── frontend/
│   ├── index.html                    # Full UI (Tailwind CDN, templates)
│   ├── app.js                        # All JS logic (no framework, no build)
│   └── style.css                     # Animations + scrollbars + custom styles
│
├── README.md                         # This file
└── .gitignore
```

---

## Configuration Reference

All config is via environment variables. Set them in `env.json` (local) or `samconfig.toml` (deployed).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OPENAI_API_KEY` | If using OpenAI | — | OpenAI API key |
| `ANTHROPIC_API_KEY` | If using Anthropic | — | Anthropic API key |
| `LLM_PROVIDER` | No | `openai` | `openai` or `anthropic` |
| `LLM_MODEL` | No | `gpt-4o` | Model name |
| `ELEVENLABS_API_KEY` | Yes | — | ElevenLabs API key |
| `ELEVENLABS_VOICE_ID` | No | `21m00Tcm4TlvDq8ikWAM` | Default TTS voice |
| `DID_API_KEY` | Yes | — | D-ID API key (`Basic <b64>`) |
| `DID_PRESENTER_ID` | No | `amy-jcu8MFXSuU` | Default D-ID avatar |
| `DID_DRIVER_URL` | No | `""` | Optional D-ID driver video |
| `LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `CORS_ORIGIN` | No | `*` | CORS allowed origin |

---

## Troubleshooting

### `sam local start-api` — "No module named 'langchain'"
**Cause**: Layer dependencies not installed.
```powershell
pip install -r sam-app\layers\dependencies\requirements.txt -t sam-app\layers\dependencies\python\
sam build
```

### D-ID returns 401 Unauthorized
**Cause**: Incorrect API key format.
```powershell
# Re-encode correctly:
python -c "import base64; print('Basic ' + base64.b64encode(b'you@email.com:YOUR_API_KEY').decode())"
```
Set `DID_API_KEY` to the FULL string including `Basic `.

### D-ID talk job times out
**Cause**: D-ID can take 20-60s for first job. The Lambda timeout is set to 120s.
- Check your D-ID account isn't rate-limited (free tier: 5 min/month)
- Try with `generate_video: false` first to verify text+TTS works

### ElevenLabs returns 401
**Cause**: Invalid API key or wrong header format.
- Verify key at https://elevenlabs.io/app/profile/api-key
- The key goes in `xi-api-key` header (handled automatically by `video.py`)

### Frontend shows "API unreachable"
**Cause**: Backend not running or wrong port.
```powershell
# Verify API is up:
Invoke-RestMethod -Uri "http://localhost:3001/health"

# If using sam local, make sure you passed --port 3001
# If deployed, check your API Gateway URL in CloudFormation outputs
```

### Speech recognition not working
- Works in **Chrome** and **Edge** only (Web Speech API)
- Must be served over **HTTP/HTTPS** (not `file://`)
- Allow microphone permission when browser prompts

### Agents disappear on page refresh
**By design** (in-memory Lambda store). The frontend stores agents in `localStorage`,
so they reappear in the sidebar — but the Lambda has lost the compiled graph.
**Fix**: The `/chat` route lazy-recompiles the graph automatically. Just chat and it works.

---

## Future Scaling

This PoC is production-ready to extend. Here's the upgrade path:

### 1. Persistent Sessions → DynamoDB
Replace the `SESSIONS` dict in `app.py` with DynamoDB calls:
```python
import boto3
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['SESSIONS_TABLE'])

# Write session:  table.put_item(Item={...})
# Read session:   table.get_item(Key={'session_id': sid})
```
Add `DynamoDB Table` to `template.yaml` and a policy to the Lambda.

### 2. Multiple Specialists → True LangGraph Supervisor
Replace the single-node graph in `agents.py` with a **Supervisor + Worker** topology:
```python
# Supervisor decides which specialist to call
# Workers: historian, finance_advisor, scientist, etc.
from langgraph.graph import StateGraph
# ... add conditional edges for routing
```

### 3. Tools for Agents
Add tools (web search, Python REPL, calculator) to the agent graph:
```python
from langchain_community.tools import DuckDuckGoSearchRun
from langgraph.prebuilt import ToolNode

tools = [DuckDuckGoSearchRun()]
tool_node = ToolNode(tools)
llm_with_tools = llm.bind_tools(tools)
```

### 4. Amazon Bedrock (no OpenAI dependency)
Swap `langchain_openai.ChatOpenAI` for `langchain_aws.ChatBedrock`:
```python
from langchain_aws import ChatBedrock
llm = ChatBedrock(model_id="anthropic.claude-3-5-sonnet-20241022-v2:0")
```
Remove `OPENAI_API_KEY` — use IAM roles for Bedrock access.

### 5. Streaming Responses
Use API Gateway WebSocket API + Lambda streaming for real-time token output:
- Replace HTTP API with WebSocket API in `template.yaml`
- Use `langchain` streaming callbacks to push tokens as they arrive

### 6. Audio Improvements
- **Amazon Polly** as ElevenLabs fallback (free tier, always available)
- **Custom voice cloning** in ElevenLabs (Professional plan)
- **Real-time voice** with ElevenLabs streaming API

### 7. Video Enhancements
- **HeyGen API** as D-ID alternative (sometimes faster)
- **Tavus** for ultra-realistic real-time video avatars
- **Pre-rendered idle loops** + lipsync only (reduces D-ID costs 10×)

### 8. Production Hardening
- Move API keys to **AWS Secrets Manager**
- Add **API Gateway API Key** authentication
- Add **WAF** in front of API Gateway
- Use **CloudFront** in front of S3 for the frontend
- Add **X-Ray tracing** to Lambda for performance analysis
- **DLQ** on Lambda for failed chat requests

---

## License

MIT — use freely for personal projects and PoCs. Commercial use: check ElevenLabs, D-ID, and OpenAI ToS.

---

*Built with ❤️ using AWS SAM 2026 best practices.*
