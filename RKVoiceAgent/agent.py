import asyncio
import logging
import os
import threading
import time
from urllib.parse import quote
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from requests import HTTPError
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Dial, VoiceResponse

from livekit import rtc
from livekit.agents import (
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    APITimeoutError,
    AutoSubscribe,
    DEFAULT_API_CONNECT_OPTIONS,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
    tts as lk_tts,
)
from livekit.agents.voice import (
    Agent,
    AgentSession,
    ConversationItemAddedEvent,
    MetricsCollectedEvent,
    UserInputTranscribedEvent,
)
from livekit.plugins import deepgram, elevenlabs, openai, silero

try:
    import azure.cognitiveservices.speech as speechsdk  # pyright: ignore[reportMissingImports]
except Exception:
    speechsdk = None


# Load local environment variables for development.
# In production (Coolify), set env vars in the UI instead of using a .env file.
load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("rk-voice-agent")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_csv_set(name: str, default_csv: str) -> set[str]:
    raw = os.getenv(name, default_csv)
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


LATENCY_LOG_ENABLED = _env_bool("LATENCY_LOG_ENABLED", True)
LATENCY_LOG_TYPES = _env_csv_set("LATENCY_LOG_TYPES", "user_turn,llm,tts,stt")

DEFAULT_SYSTEM_PROMPT = (
    "You are a professional customer support voice assistant for incoming phone calls. "
    "Be polite, clear, and empathetic. Keep replies short and conversational. "
    "Ask one question at a time, confirm key details before taking action, and avoid jargon. "
    "If the caller asks for human support, collect their request clearly and summarize next steps."
)

STRICT_FLORA_SCOPE_RULES = (
    "Critical behavior rules: Only answer as Flora customer support for bouquet, order, "
    "delivery, and pricing-related queries. Do not provide generic assistant answers outside "
    "Flora support scope. If asked something unrelated, politely decline and ask a Flora-related "
    "question. Never invent prices, stock availability, or delivery commitments; when uncertain, "
    "say you will confirm with the team."
)


def _get_system_prompt() -> str:
    custom_prompt = os.getenv("SYSTEM_PROMPT", "").strip()
    base_prompt = custom_prompt or DEFAULT_SYSTEM_PROMPT
    return f"{base_prompt}\n\n{STRICT_FLORA_SCOPE_RULES}"


def _ms(value_seconds: Optional[float]) -> Optional[int]:
    if value_seconds is None:
        return None
    return int(round(value_seconds * 1000))


def _log_latency_metrics(caller_number: str, event: MetricsCollectedEvent) -> None:
    """Emit per-turn latency logs from LiveKit's built-in metrics events."""

    if not LATENCY_LOG_ENABLED:
        return

    metric = event.metrics
    metric_type = getattr(metric, "type", "unknown")
    metadata = getattr(metric, "metadata", None)
    provider = getattr(metadata, "model_provider", None) if metadata else None
    model = getattr(metadata, "model_name", None) if metadata else None

    if metric_type == "eou_metrics":
        if "user_turn" not in LATENCY_LOG_TYPES:
            return
        logger.info(
            "Latency user_turn caller_number=%s speech_id=%s end_of_utterance_ms=%s transcription_ms=%s turn_completed_cb_ms=%s",
            caller_number,
            getattr(metric, "speech_id", None),
            _ms(getattr(metric, "end_of_utterance_delay", None)),
            _ms(getattr(metric, "transcription_delay", None)),
            _ms(getattr(metric, "on_user_turn_completed_delay", None)),
        )
        return

    if metric_type == "llm_metrics":
        if "llm" not in LATENCY_LOG_TYPES:
            return
        logger.info(
            "Latency llm caller_number=%s speech_id=%s model_provider=%s model_name=%s ttft_ms=%s duration_ms=%s tokens_per_second=%.2f",
            caller_number,
            getattr(metric, "speech_id", None),
            provider,
            model,
            _ms(getattr(metric, "ttft", None)),
            _ms(getattr(metric, "duration", None)),
            float(getattr(metric, "tokens_per_second", 0.0) or 0.0),
        )
        return

    if metric_type == "tts_metrics":
        if "tts" not in LATENCY_LOG_TYPES:
            return
        logger.info(
            "Latency tts caller_number=%s speech_id=%s segment_id=%s model_provider=%s model_name=%s ttfb_ms=%s duration_ms=%s audio_ms=%s streamed=%s",
            caller_number,
            getattr(metric, "speech_id", None),
            getattr(metric, "segment_id", None),
            provider,
            model,
            _ms(getattr(metric, "ttfb", None)),
            _ms(getattr(metric, "duration", None)),
            _ms(getattr(metric, "audio_duration", None)),
            getattr(metric, "streamed", None),
        )
        return

    if metric_type == "stt_metrics":
        if "stt" not in LATENCY_LOG_TYPES:
            return
        logger.info(
            "Latency stt caller_number=%s model_provider=%s model_name=%s request_ms=%s audio_ms=%s streamed=%s",
            caller_number,
            provider,
            model,
            _ms(getattr(metric, "duration", None)),
            _ms(getattr(metric, "audio_duration", None)),
            getattr(metric, "streamed", None),
        )



class CallState:
    """Tracks per-call state we need for transcript and Airtable logging."""

    def __init__(self, caller_number: str) -> None:
        self.caller_number = caller_number
        self.started_at = time.time()
        self.transcript_lines: List[str] = []

    def add_user_line(self, text: str) -> None:
        if text and text.strip():
            self.transcript_lines.append(f"caller: {text.strip()}")

    def add_agent_line(self, text: str) -> None:
        if text and text.strip():
            self.transcript_lines.append(f"agent: {text.strip()}")

    @property
    def duration_seconds(self) -> int:
        return int(max(0, time.time() - self.started_at))

    @property
    def transcript(self) -> str:
        return "\n".join(self.transcript_lines)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _get_elevenlabs_api_key() -> str:
    """Resolve ElevenLabs API key across common env var names."""

    # Prefer the canonical variable to avoid stale legacy keys overriding valid config.
    canonical_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    legacy_key = os.getenv("ELEVEN_API_KEY", "").strip()

    if canonical_key:
        key = canonical_key
        if legacy_key and legacy_key != canonical_key:
            logger.warning(
                "Both ELEVENLABS_API_KEY and ELEVEN_API_KEY are set with different values. "
                "Using ELEVENLABS_API_KEY and overriding ELEVEN_API_KEY for compatibility."
            )
    elif legacy_key:
        key = legacy_key
        logger.warning(
            "Using legacy ELEVEN_API_KEY because ELEVENLABS_API_KEY is empty. "
            "Set ELEVENLABS_API_KEY to avoid ambiguity."
        )
    else:
        key = _required_env("ELEVENLABS_API_KEY")

    # Keep plugin compatibility in environments expecting ELEVEN_API_KEY.
    os.environ["ELEVEN_API_KEY"] = key
    return key


_cached_elevenlabs_voice_id: Optional[str] = None
_elevenlabs_unavailable_until: float = 0.0
_elevenlabs_last_failure_reason: str = ""


def _tts_provider() -> str:
    provider = os.getenv("TTS_PROVIDER", "openai").strip().lower()
    if provider not in {"openai", "elevenlabs", "azure_speech"}:
        raise RuntimeError("Invalid TTS_PROVIDER. Use 'openai', 'elevenlabs', or 'azure_speech'.")
    return provider


def _resolve_elevenlabs_voice_id(api_key: str) -> str:
    """Return a usable ElevenLabs voice ID, with safe fallback if needed."""

    global _cached_elevenlabs_voice_id
    if _cached_elevenlabs_voice_id:
        return _cached_elevenlabs_voice_id

    requested_voice_id = _required_env("ELEVENLABS_VOICE_ID")
    voices_url = "https://api.elevenlabs.io/v1/voices"

    try:
        response = requests.get(
            voices_url,
            headers={"xi-api-key": api_key},
            timeout=10,
        )
        response.raise_for_status()
        voices = response.json().get("voices", [])
    except HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in {401, 403}:
            raise RuntimeError(
                "ElevenLabs authentication failed while validating voices. "
                "Verify ELEVENLABS_API_KEY (and remove stale ELEVEN_API_KEY if present)."
            ) from exc
        logger.exception(
            "Failed to fetch ElevenLabs voices. Using configured voice_id=%s",
            requested_voice_id,
        )
        _cached_elevenlabs_voice_id = requested_voice_id
        return requested_voice_id
    except Exception:
        logger.exception(
            "Failed to fetch ElevenLabs voices. Using configured voice_id=%s",
            requested_voice_id,
        )
        _cached_elevenlabs_voice_id = requested_voice_id
        return requested_voice_id

    for voice in voices:
        if voice.get("voice_id") == requested_voice_id:
            _cached_elevenlabs_voice_id = requested_voice_id
            return requested_voice_id

    if voices:
        fallback_voice_id = str(voices[0].get("voice_id", "")).strip()
        fallback_voice_name = str(voices[0].get("name", "unknown")).strip()
        if fallback_voice_id:
            logger.warning(
                "Configured ELEVENLABS_VOICE_ID=%s not found. Falling back to voice_id=%s (%s)",
                requested_voice_id,
                fallback_voice_id,
                fallback_voice_name,
            )
            _cached_elevenlabs_voice_id = fallback_voice_id
            return fallback_voice_id

    raise RuntimeError(
        "ElevenLabs returned no usable voices for this API key. "
        "Set a valid ELEVENLABS_VOICE_ID for your account."
    )


def _build_tts(elevenlabs_api_key: str) -> elevenlabs.TTS:
    """Build ElevenLabs TTS with resilient defaults for streaming calls."""

    model = os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5").strip() or "eleven_turbo_v2_5"
    auto_mode = os.getenv("ELEVENLABS_AUTO_MODE", "false").strip().lower() == "true"
    text_normalization = os.getenv("ELEVENLABS_TEXT_NORMALIZATION", "on").strip().lower()
    if text_normalization not in {"auto", "off", "on"}:
        text_normalization = "on"

    return elevenlabs.TTS(
        api_key=elevenlabs_api_key,
        voice_id=_resolve_elevenlabs_voice_id(elevenlabs_api_key),
        model=model,
        auto_mode=auto_mode,
        apply_text_normalization=text_normalization,
    )


def _get_tts_fallback_models(primary_model: str) -> List[str]:
    """Return fallback ElevenLabs models in priority order."""

    raw = os.getenv(
        "ELEVENLABS_FALLBACK_MODELS",
        "eleven_turbo_v2_5,eleven_multilingual_v2",
    )
    models = [m.strip() for m in raw.split(",") if m.strip()]

    # Preserve order while removing duplicates and skipping current model.
    deduped: List[str] = []
    for model in models:
        if model != primary_model and model not in deduped:
            deduped.append(model)

    return deduped


def _tts_engine_model(tts_engine: lk_tts.TTS) -> str:
    return str(getattr(tts_engine, "model", "unknown"))


async def _tts_probe_has_audio(tts_engine: lk_tts.TTS, text: str) -> bool:
    """Run a small synthesis probe and confirm at least one audio frame arrives."""

    stream = tts_engine.synthesize(text)
    try:
        async with stream:
            async for ev in stream:
                if getattr(ev, "frame", None) is not None:
                    return True
    except Exception as exc:
        logger.warning(
            "TTS probe failed for model=%s error=%s",
            _tts_engine_model(tts_engine),
            exc,
        )

    return False


async def _ensure_elevenlabs_tts_ready(tts_engine: elevenlabs.TTS) -> elevenlabs.TTS:
    """Ensure selected model can synthesize audio; auto-fallback when needed."""

    probe_text = os.getenv("TTS_PROBE_TEXT", "Hello, this is a voice test.").strip()
    if not probe_text:
        probe_text = "Hello, this is a voice test."

    current_model = _tts_engine_model(tts_engine)
    if await _tts_probe_has_audio(tts_engine, probe_text):
        logger.info("TTS probe succeeded with model=%s", current_model)
        return tts_engine

    for fallback_model in _get_tts_fallback_models(current_model):
        try:
            tts_engine.update_options(model=fallback_model)
        except Exception:
            logger.exception("Failed to set ElevenLabs fallback model=%s", fallback_model)
            continue

        if await _tts_probe_has_audio(tts_engine, probe_text):
            logger.warning(
                "Switched ElevenLabs TTS model from %s to fallback %s after empty audio probe",
                current_model,
                fallback_model,
            )
            return tts_engine

    raise RuntimeError(
        "ElevenLabs TTS produced no audio frames during startup probe. "
        "Set ELEVENLABS_MODEL to a supported model and verify account voice/model access."
    )


def _build_openai_tts() -> openai.TTS:
    """Build fallback OpenAI-compatible TTS (supports Azure OpenAI)."""

    tts_model = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts").strip() or "gpt-4o-mini-tts"
    tts_voice = os.getenv("OPENAI_TTS_VOICE", "ash").strip() or "ash"
    provider = os.getenv("OPENAI_PROVIDER", "openai").strip().lower()

    if provider == "azure":
        deployment = (
            os.getenv("AZURE_OPENAI_TTS_DEPLOYMENT", "").strip()
            or os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip()
            or None
        )
        tts_endpoint = (
            os.getenv("AZURE_OPENAI_TTS_ENDPOINT", "").strip()
            or os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
        )
        api_version = (
            os.getenv("AZURE_OPENAI_TTS_API_VERSION", "").strip()
            or os.getenv("AZURE_OPENAI_API_VERSION", "").strip()
            or os.getenv("OPENAI_API_VERSION", "").strip()
            or None
        )
        api_key = (
            os.getenv("AZURE_OPENAI_TTS_API_KEY", "").strip()
            or os.getenv("AZURE_OPENAI_API_KEY", "").strip()
            or None
        )
        ad_token = os.getenv("AZURE_OPENAI_AD_TOKEN", "").strip() or None
        if not ad_token:
            os.environ.pop("AZURE_OPENAI_AD_TOKEN", None)

        kwargs: Dict[str, Any] = {
            "model": tts_model,
            "voice": tts_voice,
            "azure_endpoint": tts_endpoint or _required_env("AZURE_OPENAI_ENDPOINT"),
            "azure_deployment": deployment,
            "api_version": api_version,
        }
        if api_key:
            kwargs["api_key"] = api_key
        if ad_token:
            kwargs["azure_ad_token"] = ad_token

        return openai.TTS.with_azure(**kwargs)

    return openai.TTS(
        model=tts_model,
        voice=tts_voice,
        api_key=os.getenv("OPENAI_API_KEY", "").strip() or None,
    )


def _azure_speech_output_format() -> tuple[str, int, int, Any]:
    if speechsdk is None:
        raise RuntimeError(
            "Azure Speech SDK is not installed. Add azure-cognitiveservices-speech to requirements.txt"
        )

    output = os.getenv("AZURE_SPEECH_OUTPUT_FORMAT", "raw16khz16bitmonopcm").strip().lower()
    format_map = {
        "raw16khz16bitmonopcm": (16000, 1, speechsdk.SpeechSynthesisOutputFormat.Raw16Khz16BitMonoPcm),
        "raw24khz16bitmonopcm": (24000, 1, speechsdk.SpeechSynthesisOutputFormat.Raw24Khz16BitMonoPcm),
        "raw48khz16bitmonopcm": (48000, 1, speechsdk.SpeechSynthesisOutputFormat.Raw48Khz16BitMonoPcm),
    }
    return (output, *format_map.get(output, format_map["raw16khz16bitmonopcm"]))


class _AzureSpeechChunkedStream(lk_tts.ChunkedStream):
    def __init__(self, *, tts: "AzureSpeechTTS", input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: AzureSpeechTTS = tts

    async def _run(self, output_emitter: lk_tts.AudioEmitter) -> None:
        try:
            request_id, audio_data = await asyncio.wait_for(
                asyncio.to_thread(self._tts._synthesize_blocking, self.input_text),
                timeout=max(1.0, float(self._conn_options.timeout)) + 5.0,
            )
            output_emitter.initialize(
                request_id=request_id,
                sample_rate=self._tts.sample_rate,
                num_channels=self._tts.num_channels,
                mime_type="audio/pcm",
            )
            output_emitter.push(audio_data)
            output_emitter.flush()
        except APITimeoutError:
            raise
        except APIStatusError:
            raise
        except asyncio.TimeoutError:
            raise APITimeoutError("Azure Speech synthesis timed out.") from None
        except Exception as exc:
            raise APIConnectionError(str(exc) or "Azure Speech synthesis failed.") from exc


class AzureSpeechTTS(lk_tts.TTS):
    def __init__(self) -> None:
        output_name, sample_rate, num_channels, speech_format = _azure_speech_output_format()
        super().__init__(
            capabilities=lk_tts.TTSCapabilities(streaming=False),
            sample_rate=sample_rate,
            num_channels=num_channels,
        )
        self._key = _required_env("AZURE_SPEECH_KEY")
        self._region = _required_env("AZURE_SPEECH_REGION")
        self._voice = os.getenv("AZURE_SPEECH_VOICE", "en-US-AriaNeural").strip() or "en-US-AriaNeural"
        self._speech_format = speech_format
        self._output_name = output_name

    @property
    def model(self) -> str:
        return f"{self._voice}/{self._output_name}"

    @property
    def provider(self) -> str:
        return f"azure_speech:{self._region}"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> _AzureSpeechChunkedStream:
        return _AzureSpeechChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def _synthesize_blocking(self, text: str) -> tuple[str, bytes]:
        if speechsdk is None:
            raise RuntimeError("Azure Speech SDK unavailable")

        speech_config = speechsdk.SpeechConfig(subscription=self._key, region=self._region)
        speech_config.speech_synthesis_voice_name = self._voice
        speech_config.set_speech_synthesis_output_format(self._speech_format)
        synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=None)

        result = synthesizer.speak_text_async(text).get()
        request_id = str(getattr(result, "result_id", "") or "")
        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            audio_data = bytes(getattr(result, "audio_data", b""))
            if not audio_data:
                raise APIConnectionError("Azure Speech returned empty audio data.")
            return request_id, audio_data

        if result.reason == speechsdk.ResultReason.Canceled:
            details = speechsdk.SpeechSynthesisCancellationDetails.from_result(result)
            error_details = (getattr(details, "error_details", "") or "Azure Speech synthesis canceled").strip()
            lowered = error_details.lower()
            if "401" in lowered or "403" in lowered or "unauthorized" in lowered or "forbidden" in lowered:
                raise APIStatusError(error_details, status_code=401, request_id=request_id)
            raise APIConnectionError(error_details)

        raise APIConnectionError(f"Azure Speech returned unexpected result reason: {result.reason}")


async def _build_resilient_tts(elevenlabs_api_key: Optional[str] = None) -> lk_tts.TTS:
    """Build TTS with a provider fallback to keep calls alive.

    IMPORTANT: Do not reuse a TTS engine across calls. LiveKit may close the
    underlying HTTP/WS client session when a call ends, which makes a cached
    engine fail on the next call with "Session is closed".
    """

    global _elevenlabs_unavailable_until
    global _elevenlabs_last_failure_reason

    preferred_provider = _tts_provider()
    allow_provider_fallback = os.getenv("TTS_ENABLE_PROVIDER_FALLBACK", "true").strip().lower() == "true"
    elevenlabs_retry_cooldown = int(os.getenv("ELEVENLABS_RETRY_COOLDOWN_SECONDS", "300"))
    probe_text = os.getenv("TTS_PROBE_TEXT", "Hello, this is a voice test.").strip() or "Hello, this is a voice test."

    if preferred_provider == "azure_speech":
        primary = AzureSpeechTTS()
        if await _tts_probe_has_audio(primary, probe_text):
            return primary
        if not allow_provider_fallback:
            raise RuntimeError("Azure Speech TTS probe failed and provider fallback is disabled.")

        fallback = _build_openai_tts()
        if await _tts_probe_has_audio(fallback, probe_text):
            logger.warning(
                "Using fallback TTS provider: OpenAI-compatible (provider=%s model=%s)",
                fallback.provider,
                _tts_engine_model(fallback),
            )
            return fallback

        raise RuntimeError(
            "Both Azure Speech and OpenAI-compatible TTS probes failed. "
            "Check provider credentials and network connectivity."
        )

    # Primary provider: ElevenLabs.
    if preferred_provider == "elevenlabs":
        if not elevenlabs_api_key:
            elevenlabs_api_key = _get_elevenlabs_api_key()
        now = time.time()
        in_cooldown = now < _elevenlabs_unavailable_until
        if in_cooldown:
            remaining = int(max(1, _elevenlabs_unavailable_until - now))
            logger.warning(
                "Skipping ElevenLabs probe for %ss due to recent failure: %s",
                remaining,
                _elevenlabs_last_failure_reason or "unknown",
            )
        else:
            try:
                primary = await _ensure_elevenlabs_tts_ready(_build_tts(elevenlabs_api_key))
                _elevenlabs_unavailable_until = 0.0
                _elevenlabs_last_failure_reason = ""
                return primary
            except Exception as exc:
                _elevenlabs_last_failure_reason = str(exc) or exc.__class__.__name__
                _elevenlabs_unavailable_until = time.time() + max(0, elevenlabs_retry_cooldown)
                logger.warning(
                    "Primary ElevenLabs TTS failed startup probe; cooling down for %ss. reason=%s",
                    max(0, elevenlabs_retry_cooldown),
                    _elevenlabs_last_failure_reason,
                )
                if not allow_provider_fallback:
                    raise

        fallback = _build_openai_tts()
        if await _tts_probe_has_audio(fallback, probe_text):
            logger.warning(
                "Using fallback TTS provider: OpenAI-compatible (provider=%s model=%s)",
                fallback.provider,
                _tts_engine_model(fallback),
            )
            return fallback

        raise RuntimeError(
            "Both ElevenLabs and OpenAI-compatible TTS probes failed. "
            "Check outbound network access and provider credentials."
        )

    # Primary provider: OpenAI-compatible TTS.
    primary = _build_openai_tts()
    if await _tts_probe_has_audio(primary, probe_text):
        return primary

    raise RuntimeError(
        "OpenAI-compatible TTS probe failed. Check TTS model/deployment and credentials."
    )


def _get_call_routing_mode() -> str:
    """Return how Twilio routes inbound calls to LiveKit.

    Supported values:
    - webhook: Twilio Voice webhook -> this app returns TwiML SIP dial
    - sip_trunk: Twilio sends SIP INVITE directly to LiveKit trunk
    """

    mode = os.getenv("TWILIO_CALL_ROUTING_MODE", "webhook").strip().lower()
    if mode not in {"webhook", "sip_trunk"}:
        raise RuntimeError(
            "Invalid TWILIO_CALL_ROUTING_MODE. Use 'webhook' or 'sip_trunk'."
        )
    return mode


def _validate_startup_configuration(routing_mode: str) -> None:
    """Log non-fatal config warnings to make setup issues obvious."""

    provider = os.getenv("OPENAI_PROVIDER", "openai").strip().lower()
    if provider == "azure":
        azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
        if azure_endpoint and not azure_endpoint.startswith("https://"):
            logger.warning(
                "AZURE_OPENAI_ENDPOINT should start with https:// (current=%s)",
                azure_endpoint,
            )
    else:
        openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        if openai_key and not openai_key.startswith("sk-"):
            logger.warning(
                "OPENAI_API_KEY does not look like an OpenAI key (expected prefix 'sk-'). "
                "Check your .env/Coolify variables."
            )

    if routing_mode == "sip_trunk":
        logger.info(
            "SIP trunk mode active: Twilio webhook endpoint is not used by this app. "
            "Authorize calls in LiveKit inbound trunk and attach a Credential List or IP ACL on Twilio Termination."
        )


def _build_llm() -> openai.LLM:
    """Build OpenAI-compatible LLM, supporting Azure OpenAI when configured."""

    provider = os.getenv("OPENAI_PROVIDER", "openai").strip().lower()
    temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.4"))

    if provider == "azure":
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip()
        model = os.getenv("AZURE_OPENAI_MODEL", "").strip() or deployment or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        api_version = (
            os.getenv("AZURE_OPENAI_API_VERSION", "").strip()
            or os.getenv("OPENAI_API_VERSION", "").strip()
            or None
        )
        api_key = os.getenv("AZURE_OPENAI_API_KEY", "").strip() or None
        ad_token = os.getenv("AZURE_OPENAI_AD_TOKEN", "").strip() or None

        # Avoid SDK fallback to an empty bearer token when env var exists but is blank.
        if not ad_token:
            os.environ.pop("AZURE_OPENAI_AD_TOKEN", None)

        azure_kwargs = {
            "model": model,
            "azure_endpoint": _required_env("AZURE_OPENAI_ENDPOINT"),
            "azure_deployment": deployment or None,
            "api_version": api_version,
            "temperature": temperature,
        }
        if api_key:
            azure_kwargs["api_key"] = api_key
        if ad_token:
            azure_kwargs["azure_ad_token"] = ad_token

        return openai.LLM.with_azure(**azure_kwargs)

    return openai.LLM(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=temperature,
    )


def _build_vad() -> silero.VAD:
    """Create a tuned Silero VAD instance.

    These values are intentionally exposed as env vars so you can tune behavior
    without changing code.
    """

    # Higher threshold = stricter speech detection (fewer false positives).
    threshold = float(os.getenv("VAD_THRESHOLD", "0.55"))

    # Minimum speech window required before VAD confirms "someone is talking".
    min_speech_duration = float(os.getenv("VAD_MIN_SPEECH_DURATION", "0.18"))

    # How long silence must continue before VAD marks speech as ended.
    # Increase this if callers are cut off mid-sentence.
    min_silence_duration = float(os.getenv("VAD_MIN_SILENCE_DURATION", "0.6"))

    # Adds a tiny pre-roll so the first word is less likely to be clipped.
    speech_pad = float(os.getenv("VAD_SPEECH_PAD", "0.12"))

    return silero.VAD.load(
        activation_threshold=threshold,
        min_speech_duration=min_speech_duration,
        min_silence_duration=min_silence_duration,
        prefix_padding_duration=speech_pad,
    )


def _extract_message_text(item: object) -> str:
    """Best-effort extraction of assistant text from a conversation item."""

    role = getattr(item, "role", None)
    if role != "assistant":
        return ""

    content = getattr(item, "content", None)
    if not isinstance(content, list):
        return ""

    parts: List[str] = []
    for chunk in content:
        if isinstance(chunk, str):
            parts.append(chunk)
            continue

        text_attr = getattr(chunk, "text", None)
        if isinstance(text_attr, str):
            parts.append(text_attr)
            continue

        if isinstance(chunk, dict) and isinstance(chunk.get("text"), str):
            parts.append(chunk["text"])

    return " ".join(p.strip() for p in parts if p and p.strip())


def _get_caller_number(participant: rtc.RemoteParticipant) -> str:
    """Try common places where SIP caller metadata is stored."""

    attrs: Dict[str, str] = dict(participant.attributes or {})
    for key in (
        "sip.phoneNumber",
        "sip.phone_number",
        "sip.from",
        "phone_number",
        "caller_number",
    ):
        value = attrs.get(key)
        if value:
            return value

    return participant.identity or "unknown"


def _log_to_airtable(call_state: CallState) -> None:
    """Write a completed call record to Airtable.

    IMPORTANT: Any failure here must not crash call handling.
    """

    pat = _required_env("AIRTABLE_PAT")
    base_id = _required_env("AIRTABLE_BASE_ID")
    # Prefer table ID (tbl...) when available, since names can be renamed and
    # may include characters that require URL encoding.
    table_ref = os.getenv("AIRTABLE_TABLE_ID", "").strip() or os.getenv("AIRTABLE_TABLE", "call_logs")
    table_ref = quote(table_ref, safe="")

    field_caller = os.getenv("AIRTABLE_FIELD_CALLER_NUMBER", "caller_number").strip() or "caller_number"
    field_duration = os.getenv("AIRTABLE_FIELD_DURATION_SECONDS", "duration_seconds").strip() or "duration_seconds"
    field_transcript = os.getenv("AIRTABLE_FIELD_TRANSCRIPT", "transcript").strip() or "transcript"
    field_created = os.getenv("AIRTABLE_FIELD_CREATED_AT", "created_at").strip() or "created_at"

    url = f"https://api.airtable.com/v0/{base_id}/{table_ref}"
    payload = {
        "fields": {
            field_caller: call_state.caller_number,
            field_duration: call_state.duration_seconds,
            field_transcript: call_state.transcript,
            field_created: datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    }

    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {pat}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )
    try:
        response.raise_for_status()
    except HTTPError as exc:
        body = response.text.strip()
        hint = ""
        if response.status_code == 403:
            hint = (
                " hint=Verify PAT scopes (data.records:write), base access, and table ref "
                "(prefer AIRTABLE_TABLE_ID=tbl...)."
            )
        raise RuntimeError(
            f"Airtable write failed: status={response.status_code} body={body}{hint}"
        ) from exc


async def _force_end_after_limit(
    ctx: JobContext,
    session: AgentSession,
    max_call_seconds: int,
) -> None:
    """Cost-protection watchdog that ends very long calls."""

    await asyncio.sleep(max_call_seconds)
    logger.warning("Max call duration reached (%s seconds). Ending call.", max_call_seconds)

    try:
        speech = session.say(
            "We have reached the maximum call duration for safety. Goodbye.",
            allow_interruptions=False,
        )
        await speech.wait_for_playout()
    except Exception:
        logger.exception("Failed to send max-duration warning to caller")

    try:
        await ctx.room.disconnect()
    except Exception:
        logger.exception("Failed to disconnect room after max duration")


async def _wait_for_participant_disconnect(
    room: rtc.Room,
    participant: rtc.RemoteParticipant,
) -> None:
    """Wait until the caller leaves, compatible with livekit rtc room events."""

    identity = participant.identity
    fut: asyncio.Future[None] = asyncio.Future()

    def _on_participant_disconnected(p: rtc.RemoteParticipant) -> None:
        if p.identity == identity and not fut.done():
            fut.set_result(None)

    def _on_connection_state_changed(state: int) -> None:
        if state == rtc.ConnectionState.CONN_DISCONNECTED and not fut.done():
            fut.set_result(None)

    room.on("participant_disconnected", _on_participant_disconnected)
    room.on("connection_state_changed", _on_connection_state_changed)

    try:
        # In this sdk, remote_participants is keyed by participant identity.
        if room.remote_participants.get(identity) is None:
            return
        await fut
    finally:
        room.off("participant_disconnected", _on_participant_disconnected)
        room.off("connection_state_changed", _on_connection_state_changed)


def prewarm(proc: JobProcess) -> None:
    """Pre-load heavy models once per worker process.

    Called by LiveKit before the first job is dispatched. Storing objects in
    proc.userdata means entrypoint reuses them instead of rebuilding on every
    call — saves ~300-600ms of per-call startup time and avoids reloading the
    Silero VAD model from disk on each call.
    """
    from livekit.agents import JobProcess  # noqa: F401 (re-import for type reference)

    proc.userdata["vad"] = _build_vad()
    proc.userdata["stt"] = deepgram.STT(model=os.getenv("DEEPGRAM_MODEL", "nova-3"))
    proc.userdata["llm"] = _build_llm()
    if _tts_provider() == "elevenlabs":
        proc.userdata["elevenlabs_api_key"] = _get_elevenlabs_api_key()
    else:
        proc.userdata["elevenlabs_api_key"] = None
    logger.info("Prewarm complete: VAD/STT/LLM pre-built and ready")


async def entrypoint(ctx: JobContext) -> None:
    """LiveKit worker entrypoint for each inbound call."""

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    participant = await ctx.wait_for_participant()

    caller_number = _get_caller_number(participant)
    call_state = CallState(caller_number=caller_number)
    logger.info("Call started. caller_number=%s participant=%s", caller_number, participant.identity)

    # Reuse pre-warmed objects where available (set by prewarm() above).
    # Fully streaming STT -> LLM -> TTS pipeline.
    # - Deepgram streams partial/final transcripts from live audio.
    # - OpenAI streams tokens as they are generated.
    # - ElevenLabs streams synthesized audio chunks while text is still arriving.
    # This minimizes first-response latency for natural conversation.
    userdata = getattr(ctx.proc, "userdata", {}) or {}
    stt = userdata.get("stt") or deepgram.STT(model=os.getenv("DEEPGRAM_MODEL", "nova-3"))
    llm = userdata.get("llm") or _build_llm()
    elevenlabs_api_key = userdata.get("elevenlabs_api_key")
    vad = userdata.get("vad") or _build_vad()
    tts = await _build_resilient_tts(elevenlabs_api_key)

    session = AgentSession(
        vad=vad,
        stt=stt,
        llm=llm,
        tts=tts,
        # Enables barge-in: if caller starts speaking, agent speech is interrupted.
        allow_interruptions=True,
        min_interruption_duration=float(os.getenv("INTERRUPT_SPEECH_DURATION", "0.2")),
        min_endpointing_delay=float(os.getenv("MIN_ENDPOINTING_DELAY", "0.3")),
        max_endpointing_delay=float(os.getenv("MAX_ENDPOINTING_DELAY", "2.2")),
    )

    @session.on("user_input_transcribed")
    def _on_user_speech(ev: UserInputTranscribedEvent) -> None:
        if ev.is_final:
            call_state.add_user_line(ev.transcript)

    @session.on("conversation_item_added")
    def _on_agent_speech(ev: ConversationItemAddedEvent) -> None:
        text = _extract_message_text(ev.item)
        call_state.add_agent_line(text)

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent) -> None:
        _log_latency_metrics(call_state.caller_number, ev)

    agent = Agent(instructions=_get_system_prompt())

    await session.start(agent=agent, room=ctx.room)

    max_call_seconds = int(os.getenv("MAX_CALL_DURATION_SECONDS", "900"))
    watchdog = asyncio.create_task(_force_end_after_limit(ctx, session, max_call_seconds))

    try:
        speech = session.say(
            os.getenv("GREETING_TEXT", "Hello, thanks for calling. How can I help you today?"),
            allow_interruptions=True,
        )
        # Compatible across livekit-agents versions where wait_if_not_interrupted
        # may require an awaitable argument.
        await speech.wait_for_playout()

        # Keep the job alive until the caller disconnects.
        await _wait_for_participant_disconnect(ctx.room, participant)
    finally:
        watchdog.cancel()
        try:
            await watchdog
        except asyncio.CancelledError:
            pass

        try:
            _log_to_airtable(call_state)
            logger.info(
                "Call log saved to Airtable. caller_number=%s duration=%s",
                call_state.caller_number,
                call_state.duration_seconds,
            )
        except Exception:
            # Logging failures should never crash the voice call flow.
            logger.exception("Airtable logging failed for caller=%s", call_state.caller_number)


# Twilio webhook server with request signature validation.
# Twilio should hit /twilio/voice, and this endpoint returns TwiML that forwards
# the call to your LiveKit SIP URI.
app = FastAPI()


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/twilio/voice")
async def twilio_voice_webhook(request: Request) -> Response:
    auth_token = _required_env("TWILIO_AUTH_TOKEN")
    validator = RequestValidator(auth_token)

    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    from_number = str(form.get("From", ""))
    logger.info("Twilio webhook received. call_sid=%s from=%s", call_sid, from_number)

    signature = request.headers.get("X-Twilio-Signature", "")
    # When running behind a reverse proxy, the internal request URL can differ
    # from the public URL Twilio used to compute its signature. Prefer explicit
    # config first, then try forwarded headers, then fall back to request.url.
    # We validate against multiple candidates to avoid false negatives.
    configured_public_url = os.getenv("TWILIO_WEBHOOK_PUBLIC_URL", "").strip()
    f_proto = request.headers.get("x-forwarded-proto", "").strip()
    f_host = request.headers.get("x-forwarded-host", "").strip()

    candidate_urls: List[str] = []
    if configured_public_url:
        candidate_urls.append(configured_public_url)

    if f_proto and f_host:
        forwarded_url = f"{f_proto}://{f_host}{request.url.path}"
        if request.url.query:
            forwarded_url = f"{forwarded_url}?{request.url.query}"
        candidate_urls.append(forwarded_url)

    candidate_urls.append(str(request.url))

    # Preserve order while removing duplicates.
    deduped_urls = list(dict.fromkeys(candidate_urls))
    bypass_signature_validation = _env_bool("TWILIO_DISABLE_SIGNATURE_VALIDATION", False)
    valid = any(validator.validate(url, dict(form), signature) for url in deduped_urls)

    if bypass_signature_validation and not valid:
        logger.warning(
            "TWILIO_DISABLE_SIGNATURE_VALIDATION=true: bypassing invalid signature for call_sid=%s",
            call_sid,
        )
        valid = True

    if not valid:
        logger.warning(
            "Rejected Twilio webhook due to invalid signature. call_sid=%s candidate_urls=%s",
            call_sid,
            deduped_urls,
        )
        return Response(content="Forbidden", status_code=403)

    livekit_sip_uri = _required_env("LIVEKIT_SIP_URI")
    sip_auth_username = os.getenv("TWILIO_SIP_AUTH_USERNAME", "").strip()
    sip_auth_password = os.getenv("TWILIO_SIP_AUTH_PASSWORD", "").strip()

    twiml = VoiceResponse()
    dial = Dial()
    sip_kwargs = {}
    if sip_auth_username and sip_auth_password:
        sip_kwargs["username"] = sip_auth_username
        sip_kwargs["password"] = sip_auth_password

    # Optional digest auth is required when LiveKit inbound trunk is configured
    # with auth credentials. If not configured on the trunk, leave env vars empty.
    dial.sip(livekit_sip_uri, **sip_kwargs)
    twiml.append(dial)

    return Response(content=str(twiml), media_type="application/xml")


def _start_webhook_server_in_background() -> None:
    import uvicorn

    host = os.getenv("TWILIO_WEBHOOK_HOST", "0.0.0.0")
    port = int(os.getenv("TWILIO_WEBHOOK_PORT", "8080"))

    thread = threading.Thread(
        target=uvicorn.run,
        kwargs={
            "app": app,
            "host": host,
            "port": port,
            "log_level": os.getenv("UVICORN_LOG_LEVEL", "info").lower(),
        },
        daemon=True,
    )
    thread.start()
    logger.info("Twilio webhook server started on http://%s:%s", host, port)


if __name__ == "__main__":
    routing_mode = _get_call_routing_mode()
    llm_provider = os.getenv("OPENAI_PROVIDER", "openai").strip().lower()
    tts_provider = _tts_provider()

    # Fail fast for inbound-call routing variables so misconfiguration is visible
    # at startup instead of only when Twilio sends a live call.
    if llm_provider == "azure":
        _required_env("AZURE_OPENAI_ENDPOINT")
        if not os.getenv("AZURE_OPENAI_API_KEY", "").strip() and not os.getenv(
            "AZURE_OPENAI_AD_TOKEN", ""
        ).strip():
            raise RuntimeError(
                "Azure OpenAI requires AZURE_OPENAI_API_KEY or AZURE_OPENAI_AD_TOKEN"
            )
        if not os.getenv("AZURE_OPENAI_API_VERSION", "").strip() and not os.getenv(
            "OPENAI_API_VERSION", ""
        ).strip():
            raise RuntimeError(
                "Azure OpenAI requires AZURE_OPENAI_API_VERSION (or OPENAI_API_VERSION)"
            )
    else:
        _required_env("OPENAI_API_KEY")
    _required_env("DEEPGRAM_API_KEY")
    if tts_provider == "elevenlabs":
        _required_env("ELEVENLABS_API_KEY")
        _required_env("ELEVENLABS_VOICE_ID")
    elif tts_provider == "azure_speech":
        _required_env("AZURE_SPEECH_KEY")
        _required_env("AZURE_SPEECH_REGION")
    if routing_mode == "webhook":
        _required_env("TWILIO_AUTH_TOKEN")
        _required_env("LIVEKIT_SIP_URI")
    else:
        logger.info(
            "TWILIO_CALL_ROUTING_MODE=sip_trunk: skipping Twilio webhook server and signature checks"
        )

    _validate_startup_configuration(routing_mode)

    # Run webhook listener and LiveKit worker in one long-running process.
    if routing_mode == "webhook":
        _start_webhook_server_in_background()

    worker_host = os.getenv("LIVEKIT_WORKER_HOST", "")
    worker_port = int(os.getenv("LIVEKIT_WORKER_PORT", "8081"))
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            host=worker_host,
            port=worker_port,
        )
    )
