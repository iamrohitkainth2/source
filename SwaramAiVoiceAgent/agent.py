import logging
import os
from dotenv import load_dotenv
from livekit.agents import JobContext, WorkerOptions, cli
from livekit.agents.voice import Agent, AgentSession
from livekit.plugins import openai, sarvam

# Load environment variables
load_dotenv()

# Set up logging
logger = logging.getLogger("voice-agent")
logger.setLevel(logging.INFO)


class VoiceAgent(Agent):
    def __init__(self) -> None:
        azure_kwargs = {
            "model": os.getenv("AZURE_OPENAI_MODEL", "gpt-4o-mini").strip(),
            "azure_endpoint": os.getenv("AZURE_OPENAI_ENDPOINT", "").strip(),
            "azure_deployment": os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip() or None,
            "api_key": os.getenv("AZURE_OPENAI_API_KEY", "").strip() or None,
            "api_version": os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01").strip(),
            "temperature": 0.4,
        }

        super().__init__(
            # Your agent's personality and instructions
            instructions="""
                You are a helpful voice assistant.
                Be friendly, concise, and conversational.
                Speak naturally as if you're having a real conversation.
            """,
            
            # Saaras v3 STT - Converts speech to text
            stt=sarvam.STT(
                language="hi-IN", #"unknown",  # Auto-detect language, or use "en-IN", "hi-IN", etc.
                model="saaras:v3",
                mode="transcribe"
            ),
            
            # OpenAI LLM (Azure) - The "brain" that processes and generates responses
            llm=openai.LLM.with_azure(**azure_kwargs),
            
            # Bulbul TTS - Converts text to speech
            tts=sarvam.TTS(
                target_language_code="hi-IN",
                model="bulbul:v3",
                speaker="priya"  # Female: priya, simran, ishita, kavya | Male: aditya, anand, rohan
            ),
        )
    
    async def on_enter(self):
        """Called when user joins - agent starts the conversation"""
        self.session.generate_reply()


async def entrypoint(ctx: JobContext):
    """Main entry point - LiveKit calls this when a user connects"""
    logger.info(f"User connected to room: {ctx.room.name}")
    
    # Create and start the agent session
    session = AgentSession()
    await session.start(
        agent=VoiceAgent(),
        room=ctx.room
    )


if __name__ == "__main__":
    # Run the agent
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
