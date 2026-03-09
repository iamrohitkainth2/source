I want to build a self-hosted inbound voice agent using LiveKit Agents framework. Here's everything you need to know:

What it should do:

Answer inbound phone calls
Have a conversation using AI
Log every call including full transcript, caller number, duration, and timestamp to Airtable
Tech stack to use:

LiveKit Agents (Python) for the voice pipeline
LiveKit Cloud for the server (I'll provide API keys)
Deepgram for STT
OpenAI for LLM
ElevenLabs for TTS
Silero for VAD
Twilio for inbound phone calls via SIP
Airtable for storing call logs and transcripts
Project structure I want:

A clean single agent.py file as the main entry point
A .env.example file showing all required environment variables but with no real values
A .gitignore that excludes .env
A requirements.txt with all dependencies
A README.md explaining what each env variable is and where to get it

Airtable setup:
I will create the Airtable table myself and provide the PAT (Personal Access Token) and Base ID as environment variables. The table is called call_logs with these fields: caller_number (text), duration_seconds (number), transcript (long text), created_at (date). Just use the PAT and Base ID from the env to connect and insert records after each call.

Critical technical requirements — please implement all of these carefully:

Latency optimization — use LLM streaming combined with TTS streaming so the agent starts speaking as soon as the first tokens arrive, not after the full response is generated. This is critical for the conversation to feel natural.

VAD tuning — configure Silero VAD properly so it doesn't cut the caller off mid-sentence but also doesn't have awkward silence waiting for them to finish. Add comments explaining what each VAD parameter does so I can tune it.

Barge-in / interruption handling — if the caller speaks while the agent is talking, the agent should stop immediately and listen. Make sure this is explicitly configured.

Streaming pipeline — ensure the STT → LLM → TTS pipeline is fully streaming end to end, not processing in chunks or waiting for complete responses at any stage.

Error handling — if Airtable logging fails after a call, the call itself should not crash. Wrap all logging in try/except with clear error messages in the logs.

Security — all API keys must come from environment variables, nothing hardcoded. The .env file must never be included in any uploads.

Cost protection — add a maximum call duration limit (configurable via env variable) so a stuck or looping call doesn't rack up API costs indefinitely.

Twilio webhook validation — validate that incoming requests actually come from Twilio and not someone spoofing the endpoint.

Deployment target:
This will be deployed on a Hostinger VPS using Coolify via tar file upload, no GitHub involved. The agent needs to run as a persistent long-running process. Make sure there are no serverless assumptions. Include a note in the README on how to set the environment variables inside Coolify's UI instead of using a .env file in production.

Code style:
Write clean, well-commented code so a non-developer can understand what each section does.
After building, give me a checklist of everything I need to set up before the agent will work — including LiveKit Cloud account, Deepgram account, OpenAI account, ElevenLabs account, Twilio SIP setup, and Airtable PAT + Base ID.