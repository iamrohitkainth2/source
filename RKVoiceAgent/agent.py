import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests
from requests import HTTPError
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Dial, VoiceResponse

from livekit import rtc
from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli
from livekit.agents.voice import Agent, AgentSession, ConversationItemAddedEvent, UserInputTranscribedEvent
from livekit.plugins import deepgram, elevenlabs, openai, silero


# Load local environment variables for development.
# In production (Coolify), set env vars in the UI instead of using a .env file.
load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("rk-voice-agent")


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

    key = os.getenv("ELEVEN_API_KEY", "").strip()
    if not key:
        key = _required_env("ELEVENLABS_API_KEY")
        # Keep plugin compatibility in environments expecting ELEVEN_API_KEY.
        os.environ["ELEVEN_API_KEY"] = key
    return key


_cached_elevenlabs_voice_id: Optional[str] = None


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
    table = os.getenv("AIRTABLE_TABLE", "call_logs")

    url = f"https://api.airtable.com/v0/{base_id}/{table}"
    payload = {
        "fields": {
            "caller_number": call_state.caller_number,
            "duration_seconds": call_state.duration_seconds,
            "transcript": call_state.transcript,
            "created_at": datetime.now(timezone.utc).isoformat(),
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
        raise RuntimeError(
            f"Airtable write failed: status={response.status_code} body={body}"
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


async def entrypoint(ctx: JobContext) -> None:
    """LiveKit worker entrypoint for each inbound call."""

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    participant = await ctx.wait_for_participant()

    caller_number = _get_caller_number(participant)
    call_state = CallState(caller_number=caller_number)
    logger.info("Call started. caller_number=%s participant=%s", caller_number, participant.identity)

    # Fully streaming STT -> LLM -> TTS pipeline.
    # - Deepgram streams partial/final transcripts from live audio.
    # - OpenAI streams tokens as they are generated.
    # - ElevenLabs streams synthesized audio chunks while text is still arriving.
    # This minimizes first-response latency for natural conversation.
    stt = deepgram.STT(model=os.getenv("DEEPGRAM_MODEL", "nova-3"))
    llm = openai.LLM(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"), temperature=0.4)
    elevenlabs_api_key = _get_elevenlabs_api_key()
    tts = elevenlabs.TTS(
        api_key=elevenlabs_api_key,
        voice_id=_resolve_elevenlabs_voice_id(elevenlabs_api_key),
        model=os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
    )
    vad = _build_vad()

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

    agent = Agent(
        instructions=(
            "You are a concise, helpful phone assistant. "
            "Keep replies short and conversational. "
            "If the caller asks for human support, collect their request clearly."
        )
    )

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
    valid = any(validator.validate(url, dict(form), signature) for url in deduped_urls)

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

    # Fail fast for inbound-call routing variables so misconfiguration is visible
    # at startup instead of only when Twilio sends a live call.
    _required_env("OPENAI_API_KEY")
    _required_env("DEEPGRAM_API_KEY")
    _required_env("ELEVENLABS_API_KEY")
    _required_env("ELEVENLABS_VOICE_ID")
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
            host=worker_host,
            port=worker_port,
        )
    )
