# Azure App Service Deployment Guide

This guide covers deploying, redeploying, and managing **RKVoiceAgent** on Azure App Service (F1 Free tier).

---

## Deployed Resources

| Resource            | Name                        | Notes                          |
|---------------------|-----------------------------|-------------------------------|
| Resource Group      | `rkvoiceagent-f1-rg`        | Region: Central India          |
| App Service Plan    | `rkvoiceagent-f1-plan`      | SKU: F1 (Free, Linux)          |
| Web App             | `rkvoiceagentf127609`       | Python 3.11                    |
| Public URL          | `https://rkvoiceagentf127609.azurewebsites.net` |           |
| Twilio Webhook URL  | `https://rkvoiceagentf127609.azurewebsites.net/twilio/voice` | Set in Twilio console |
| Health Check URL    | `https://rkvoiceagentf127609.azurewebsites.net/healthz` |      |

---

## Prerequisites

1. [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) installed.
2. Logged in: `az login`
3. Correct subscription active:
   ```powershell
   az account set --subscription "a1aa0e4d-0915-48a4-8c5a-f1410fc99911"
   ```

---

## Redeployment (Code Changes Only)

Use this when you only changed `agent.py`, `requirements.txt`, or other code files.

```powershell
$app = 'rkvoiceagentf127609'
$rg  = 'rkvoiceagent-f1-rg'

# Package and deploy
if (Test-Path '.deploy.zip') { Remove-Item '.deploy.zip' -Force }
Compress-Archive -Path 'agent.py', 'README.md', 'requirements.txt', '.env.example' `
    -DestinationPath '.deploy.zip' -Force
az webapp deploy --resource-group $rg --name $app --src-path '.deploy.zip' --type zip

# Verify health
Invoke-WebRequest -Uri "https://$app.azurewebsites.net/healthz" -UseBasicParsing
```

---

## Full Redeploy (New Resource Group from Scratch)

Use this to recreate everything from zero.

### Step 1 — Create infrastructure

```powershell
$rg   = 'rkvoiceagent-f1-rg'
$plan = 'rkvoiceagent-f1-plan'
$app  = 'rkvoiceagentf127609'    # Must be globally unique; change if taken
$loc  = 'centralindia'

az group create --name $rg --location $loc
az appservice plan create --name $plan --resource-group $rg --sku F1 --is-linux
az webapp create --resource-group $rg --plan $plan --name $app --runtime "PYTHON:3.11"
```

### Step 2 — Set startup command and warmup

```powershell
az webapp config set --resource-group $rg --name $app `
    --startup-file "python agent.py start"

az webapp config appsettings set --resource-group $rg --name $app --settings `
    WEBSITE_WARMUP_PATH=/healthz `
    WEBSITE_WARMUP_STATUSES=200 `
    SCM_DO_BUILD_DURING_DEPLOYMENT=1
```

### Step 3 — Set all required application settings

> **Security note**: Set secrets only in Azure Portal or CLI — never commit real secrets to `.env.example` or the repository.

```powershell
az webapp config appsettings set --resource-group $rg --name $app --settings `
    "LIVEKIT_URL=wss://rkvoiceagent-lkt-y9rjx2rn.livekit.cloud" `
    "LIVEKIT_API_KEY=<your-livekit-api-key>" `
    "LIVEKIT_API_SECRET=<your-livekit-api-secret>" `
    "LIVEKIT_SIP_URI=sip:2zj272hsndm.sip.livekit.cloud" `
    "LIVEKIT_WORKER_HOST=" `
    "LIVEKIT_WORKER_PORT=8082" `
    "DEEPGRAM_API_KEY=<your-deepgram-api-key>" `
    "DEEPGRAM_MODEL=nova-3" `
    "OPENAI_PROVIDER=azure" `
    "OPENAI_API_KEY=<your-openai-api-key>" `
    "OPENAI_MODEL=gpt-4o-mini" `
    "OPENAI_TEMPERATURE=0.4" `
    "AZURE_OPENAI_ENDPOINT=https://rk-first-openai.cognitiveservices.azure.com/" `
    "AZURE_OPENAI_API_KEY=<your-azure-openai-api-key>" `
    "AZURE_OPENAI_AD_TOKEN=" `
    "AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini" `
    "AZURE_OPENAI_MODEL=gpt-4o-mini" `
    "AZURE_OPENAI_API_VERSION=2025-01-01-preview" `
    "ELEVENLABS_API_KEY=<your-elevenlabs-api-key>" `
    "ELEVEN_API_KEY=<your-elevenlabs-api-key>" `
    "ELEVENLABS_VOICE_ID=EXAVITQu4vr4xnSDxMaL" `
    "ELEVENLABS_MODEL=eleven_flash_v2_5" `
    "ELEVENLABS_AUTO_MODE=true" `
    "ELEVENLABS_TEXT_NORMALIZATION=on" `
    "ELEVENLABS_FALLBACK_MODELS=eleven_turbo_v2_5,eleven_multilingual_v2" `
    "TTS_PROBE_TEXT=Hello, this is a voice test." `
    "TTS_PROVIDER=elevenlabs" `
    "TTS_ENABLE_PROVIDER_FALLBACK=true" `
    "OPENAI_TTS_MODEL=gpt-4o-mini-tts" `
    "OPENAI_TTS_VOICE=ash" `
    "AZURE_OPENAI_TTS_DEPLOYMENT=gpt-4o-mini-tts" `
    "AZURE_OPENAI_TTS_ENDPOINT=https://iamro-mmjf6wkb-eastus2.cognitiveservices.azure.com/" `
    "AZURE_OPENAI_TTS_API_VERSION=2025-03-01-preview" `
    "AZURE_OPENAI_TTS_API_KEY=<your-azure-openai-tts-api-key>" `
    "AIRTABLE_PAT=<your-airtable-pat>" `
    "AIRTABLE_BASE_ID=app0rcQ8myFducYxu" `
    "AIRTABLE_TABLE=call_logs" `
    "AIRTABLE_TABLE_ID=tbldwiPlHYYlYa8VG" `
    "TWILIO_AUTH_TOKEN=<your-twilio-auth-token>" `
    "TWILIO_CALL_ROUTING_MODE=webhook" `
    "TWILIO_WEBHOOK_HOST=0.0.0.0" `
    "TWILIO_WEBHOOK_PORT=8000" `
    "TWILIO_WEBHOOK_PUBLIC_URL=https://$app.azurewebsites.net/twilio/voice" `
    "GREETING_TEXT=Hello, I am Rohit's Voice Agent. How can I help you today?" `
    "MAX_CALL_DURATION_SECONDS=300" `
    "VAD_THRESHOLD=0.55" `
    "VAD_MIN_SPEECH_DURATION=0.18" `
    "VAD_MIN_SILENCE_DURATION=0.20" `
    "VAD_SPEECH_PAD=0.12" `
    "INTERRUPT_SPEECH_DURATION=0.2" `
    "MIN_ENDPOINTING_DELAY=0.3" `
    "MAX_ENDPOINTING_DELAY=0.8" `
    "LATENCY_LOG_ENABLED=false" `
    "LATENCY_LOG_TYPES=user_turn,llm,tts,stt" `
    "LOG_LEVEL=INFO" `
    "UVICORN_LOG_LEVEL=INFO"
```

### Step 4 — Deploy code

```powershell
if (Test-Path '.deploy.zip') { Remove-Item '.deploy.zip' -Force }
Compress-Archive -Path 'agent.py', 'README.md', 'requirements.txt', '.env.example' `
    -DestinationPath '.deploy.zip' -Force

az webapp deploy --resource-group $rg --name $app --src-path '.deploy.zip' --type zip
```

### Step 5 — Verify health

```powershell
Start-Sleep -Seconds 30
Invoke-WebRequest -Uri "https://$app.azurewebsites.net/healthz" -UseBasicParsing
# Expected: StatusCode 200, Content: {"status":"ok"}
```

---

## Twilio Webhook Configuration

In the [Twilio Console](https://console.twilio.com/) → Phone Numbers → Active Numbers → your number:

| Field                    | Value                                                             |
|--------------------------|-------------------------------------------------------------------|
| A Call Comes In (Webhook) | `https://rkvoiceagentf127609.azurewebsites.net/twilio/voice`    |
| HTTP Method              | `POST`                                                            |

> **Note**: `TWILIO_CALL_ROUTING_MODE` must be `webhook` in App Service settings (not `sip_trunk`) for the webhook server to start and bind a port. The `.env` file uses `sip_trunk` for local development.

---

## Key App Service Configuration Notes

| Setting                       | Value          | Why                                                                 |
|-------------------------------|----------------|---------------------------------------------------------------------|
| `TWILIO_CALL_ROUTING_MODE`    | `webhook`      | Starts the FastAPI server so App Service warmup probe gets a 200    |
| `TWILIO_WEBHOOK_PORT`         | `8000`         | App Service Linux routes port 80 → internal 8000 by default        |
| `WEBSITE_WARMUP_PATH`         | `/healthz`     | Tells App Service to probe this path instead of `/` during startup  |
| `WEBSITE_WARMUP_STATUSES`     | `200`          | Startup succeeds only when `/healthz` returns HTTP 200              |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `1`         | Runs `pip install -r requirements.txt` automatically on deploy      |

---

## Useful Operational Commands

### Stream live logs
```powershell
az webapp log tail --resource-group rkvoiceagent-f1-rg --name rkvoiceagentf127609
```

### Download all logs
```powershell
az webapp log download --resource-group rkvoiceagent-f1-rg --name rkvoiceagentf127609 --log-file appservice_logs.zip
```

### Restart the app
```powershell
az webapp restart --resource-group rkvoiceagent-f1-rg --name rkvoiceagentf127609
```

### Update a single setting without redeploying
```powershell
az webapp config appsettings set --resource-group rkvoiceagent-f1-rg --name rkvoiceagentf127609 `
    --settings "GREETING_TEXT=Hi, how can I help you?"
```

### View all current settings names
```powershell
az webapp config appsettings list --resource-group rkvoiceagent-f1-rg --name rkvoiceagentf127609 `
    --query "[].name" --output table
```

### Check current app state
```powershell
az webapp show --resource-group rkvoiceagent-f1-rg --name rkvoiceagentf127609 `
    --query "{state:state, host:defaultHostName, sku:sku}" --output table
```

---

## Free Tier Limitations to be Aware Of

| Limitation              | Impact                                                          |
|-------------------------|-----------------------------------------------------------------|
| No Always On            | App sleeps after ~20 min idle; cold start on next call (~15s)  |
| 60-min CPU limit/day    | Heavy load will be throttled                                    |
| 230s startup timeout    | App must bind port and return 200 on `/healthz` within 230s    |
| 5 WebSocket connections | Limits concurrent voice calls                                   |
| 1 GB shared memory      | Adequate for single concurrent call; tighten with more          |
| No SLA                  | Not for production; use B1+ for production workloads            |

---

## Local Development vs App Service Differences

| Setting                      | Local (`.env`)   | App Service                              |
|------------------------------|------------------|------------------------------------------|
| `TWILIO_CALL_ROUTING_MODE`   | `sip_trunk`      | `webhook`                                |
| `TWILIO_WEBHOOK_PORT`        | `8080`           | `8000`                                   |
| `TWILIO_WEBHOOK_PUBLIC_URL`  | *(empty)*        | `https://<app>.azurewebsites.net/twilio/voice` |
| `TTS_PROVIDER`               | `elevenlabs`     | `elevenlabs` (same)                      |
| Secrets loaded from          | `.env` file      | App Service Application Settings         |
