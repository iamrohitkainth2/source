# Swaram AI Voice Agent

A real-time voice AI agent built with [LiveKit Agents](https://docs.livekit.io/agents/), [Sarvam AI](https://www.sarvam.ai/), and Azure OpenAI. It listens to speech in Hindi (or other Indian languages), understands it using GPT-4o-mini, and responds with natural-sounding Indian voice synthesis.

## Architecture

```
User Speech → Sarvam STT (Saaras v3) → Azure OpenAI GPT-4o-mini → Sarvam TTS (Bulbul v3) → Voice Response
```

| Component | Service | Model |
|-----------|---------|-------|
| STT (Speech-to-Text) | Sarvam AI | `saaras:v3` |
| LLM (Language Model) | Azure OpenAI | `gpt-4o-mini` |
| TTS (Text-to-Speech) | Sarvam AI | `bulbul:v3` |
| Real-time Transport | LiveKit | — |

## Prerequisites

- Python 3.10+
- A [LiveKit Cloud](https://cloud.livekit.io/) project
- An [Azure OpenAI](https://portal.azure.com/) resource with a `gpt-4o-mini` deployment
- A [Sarvam AI](https://www.sarvam.ai/) API key

## Setup

### 1. Install dependencies

```bash
pip install livekit-agents livekit-plugins-openai livekit-plugins-sarvam python-dotenv
```

### 2. Configure environment variables

Create a `.env` file in the project root:

```env
# LiveKit
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your_livekit_api_key
LIVEKIT_API_SECRET=your_livekit_api_secret

# Sarvam AI
SARVAM_API_KEY=your_sarvam_api_key

# Azure OpenAI
AZURE_OPENAI_ENDPOINT=https://your-resource.cognitiveservices.azure.com/
AZURE_OPENAI_API_KEY=your_azure_openai_api_key
AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini
AZURE_OPENAI_MODEL=gpt-4o-mini
AZURE_OPENAI_API_VERSION=2025-01-01-preview
```

> **Important:** Never commit your `.env` file to version control. Add it to `.gitignore`.

## Running the Agent

### Development mode (hot-reload)

```bash
python agent.py dev
```

### Console mode (local mic/speaker test, no LiveKit server needed)

```bash
python agent.py console
```

### Production mode

```bash
python agent.py start
```

## Configuration

### Language

The agent is currently configured for Hindi (`hi-IN`). To change the language, update these fields in `agent.py`:

```python
# STT language
stt=sarvam.STT(
    language="hi-IN",   # e.g. "en-IN", "ta-IN", "te-IN", "unknown" for auto-detect
    ...
)

# TTS language
tts=sarvam.TTS(
    target_language_code="hi-IN",   # must match STT language
    ...
)
```

### TTS Voice / Speaker

Change the `speaker` parameter in the `sarvam.TTS` config:

| Gender | Speakers |
|--------|----------|
| Female | `priya`, `simran`, `ishita`, `kavya` |
| Male   | `aditya`, `anand`, `rohan` |

### Agent Instructions

Edit the `instructions` string in `VoiceAgent.__init__()` to change the agent's personality and behaviour.

## Project Structure

```
SwaramAiVoiceAgent/
├── agent.py      # Main agent logic
├── .env          # Environment variables (not committed)
└── README.md     # This file
```

## Supported Languages (Sarvam AI)

`hi-IN` · `en-IN` · `bn-IN` · `gu-IN` · `kn-IN` · `ml-IN` · `mr-IN` · `od-IN` · `pa-IN` · `ta-IN` · `te-IN`

Use `"unknown"` in the STT config to enable automatic language detection.
