# RK Voice Agent (LiveKit + Twilio SIP)

Self-hosted inbound phone voice agent using LiveKit Agents (Python), Deepgram (STT), OpenAI (LLM), ElevenLabs (TTS), Silero (VAD), Twilio SIP, and Airtable logging.

## What this project does

- Answers inbound phone calls (via Twilio webhook -> SIP -> LiveKit)
- Runs a fully streaming STT -> LLM -> TTS conversation pipeline
- Supports barge-in (caller can interrupt agent speech)
- Logs each completed call to Airtable with caller number, duration, transcript, and timestamp
- Enforces a configurable max call duration to limit runaway cost
- Validates Twilio webhook signatures so spoofed requests are rejected

## Files

- `agent.py`: Main long-running process. Starts both:
  - LiveKit worker (voice agent)
  - FastAPI webhook server for Twilio (`/twilio/voice`)
- `.env.example`: Required environment variables and defaults
- `requirements.txt`: Python dependencies
- `.gitignore`: Excludes `.env` and common local artifacts

## Environment variables

Copy `.env.example` to `.env` for local development only.
In production (Coolify), set the same keys in the Coolify environment UI.

### LiveKit

- `LIVEKIT_URL`: LiveKit Cloud URL (wss://...)
- `LIVEKIT_API_KEY`: LiveKit API key
- `LIVEKIT_API_SECRET`: LiveKit API secret
- `LIVEKIT_SIP_URI`: SIP URI Twilio should dial (your LiveKit SIP ingress URI)

### Deepgram

- `DEEPGRAM_API_KEY`: Deepgram API key
- `DEEPGRAM_MODEL`: STT model (default `nova-3`)

### OpenAI

- `OPENAI_API_KEY`: OpenAI API key
- `OPENAI_MODEL`: LLM model (default `gpt-4o-mini`)

### ElevenLabs

- `ELEVENLABS_API_KEY`: ElevenLabs API key
- `ELEVENLABS_VOICE_ID`: Voice ID for speech output
- `ELEVENLABS_MODEL`: TTS model (default `eleven_flash_v2_5`)

### Airtable

- `AIRTABLE_PAT`: Airtable personal access token
- `AIRTABLE_BASE_ID`: Airtable base ID
- `AIRTABLE_TABLE`: Airtable table name (default `call_logs`)

Expected Airtable fields in table `call_logs`:
- `caller_number` (text)
- `duration_seconds` (number)
- `transcript` (long text)
- `created_at` (date)

### Twilio webhook security

- `TWILIO_AUTH_TOKEN`: Twilio Auth Token used to validate `X-Twilio-Signature`
- `TWILIO_WEBHOOK_HOST`: Webhook bind host (default `0.0.0.0`)
- `TWILIO_WEBHOOK_PORT`: Webhook bind port (default `8080`)
- `TWILIO_WEBHOOK_PUBLIC_URL`: Optional exact public URL Twilio calls (recommended behind reverse proxies)
- `TWILIO_SIP_AUTH_USERNAME`: Optional SIP digest username for LiveKit inbound trunk auth
- `TWILIO_SIP_AUTH_PASSWORD`: Optional SIP digest password for LiveKit inbound trunk auth

### Call behavior / tuning

- `GREETING_TEXT`: Initial greeting sent to caller
- `MAX_CALL_DURATION_SECONDS`: Hard limit for call duration (cost protection)
- `VAD_THRESHOLD`: VAD confidence threshold
- `VAD_MIN_SPEECH_DURATION`: Min speech duration to start speech segment
- `VAD_MIN_SILENCE_DURATION`: Min silence duration to end speech segment
- `VAD_SPEECH_PAD`: Small pre/post padding around detected speech
- `INTERRUPT_SPEECH_DURATION`: Speech needed to trigger interruption
- `MIN_ENDPOINTING_DELAY`: Lower bound of endpointing delay
- `MAX_ENDPOINTING_DELAY`: Upper bound of endpointing delay

### Logging

- `LOG_LEVEL`: Base application log level (default `INFO`)
- `UVICORN_LOG_LEVEL`: Webhook server log level (default `info`)
- `LATENCY_LOG_ENABLED`: Enable/disable latency logs emitted from LiveKit metrics events (default `true`)
- `LATENCY_LOG_TYPES`: Comma-separated latency categories to emit (default `user_turn,llm,tts,stt`)

## Local run

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python agent.py start
```

## Twilio webhook setup

1. Expose your webhook endpoint publicly (for example via reverse proxy on your VPS).
2. In Twilio phone number Voice settings, set webhook URL to:
   - `https://<your-domain>/twilio/voice`
3. Set method to `POST`.
4. Ensure `TWILIO_AUTH_TOKEN` is configured in environment.

The webhook validates Twilio signature and returns TwiML that dials your `LIVEKIT_SIP_URI`.

Use `start` mode for stable operation. `dev` mode enables file watching/reload and can spawn additional processes that may conflict on the webhook port.

## Coolify deployment notes (Hostinger VPS)

- Deploy this project as a long-running service (not serverless).
- In Coolify, add all variables from `.env.example` in the service environment section.
- Do not upload a real `.env` file to production.
- Start command:
  - `python agent.py start`
- Ensure your container/service exposes the webhook port (`TWILIO_WEBHOOK_PORT`, default `8080`) through your reverse proxy.

## Setup checklist before first call

1. LiveKit Cloud
2. Create project and obtain `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`.
3. Configure SIP ingress and capture `LIVEKIT_SIP_URI`.
4. Deepgram
5. Create API key and set `DEEPGRAM_API_KEY`.
6. OpenAI
7. Create API key and set `OPENAI_API_KEY`.
8. ElevenLabs
9. Create API key (`ELEVENLABS_API_KEY`) and choose voice ID (`ELEVENLABS_VOICE_ID`).
10. Twilio
11. Buy/configure inbound phone number.
12. Configure Voice webhook URL to `/twilio/voice`.
13. Confirm `TWILIO_AUTH_TOKEN` matches your Twilio console token.
14. Airtable
15. Create base + table `call_logs` with required fields.
16. Create PAT and set `AIRTABLE_PAT` + `AIRTABLE_BASE_ID`.
17. Deployment
18. Set all env vars in Coolify UI.
19. Ensure service is always running and publicly reachable for Twilio webhooks.
20. Place a test call and verify Airtable row creation after hang-up.

## Notes

- Airtable logging failures are caught and logged so active calls are not crashed.
- Transcript capture stores both caller and agent committed utterances.
