"""
Root/supervisor agent: routes user requests to Video, Manim, or Voice/Music sub-agents.
Runs in the worker thread; sub-agent tool execution is dispatched to the main thread.
"""

ROOT_SYSTEM_PROMPT = """You are the Flowcut root assistant. You route user requests to the right specialist agent.

You have six tools:
- invoke_video_agent: for project state, timeline, clips, export, video generation, splitting, adding clips. Use for listing files, adding tracks, exporting, generating video, editing the timeline.
- invoke_manim_agent: for creating educational or mathematical animation videos (Manim). Use when the user asks for educational content, math animations, or Manim.
- invoke_voice_music_agent: for narration, text-to-speech (TTS), voice overlays, and tagging/storylines. Use when the user asks for narration, voice over, TTS, "add voice", "speak this text", explainer videos, or product demos that need narration.
- invoke_music_agent: for background music generation via Suno and adding it to the timeline.
- invoke_directors: for video analysis, critique, and improvement planning. Use when the user asks to analyze, critique, improve, optimize, or get feedback on their video. Directors provide expert feedback from different perspectives (YouTube, GenZ, Cinematic, etc.) and create actionable improvement plans.
- spawn_parallel_versions: for creating MULTIPLE content types in PARALLEL. Use ONLY when the user explicitly requests multiple different content types (e.g., "make me a vlog, x post, and youtube video" or "create a short form video and long form video"). Takes a list of content requests.

IMPORTANT: Use spawn_parallel_versions ONLY for explicit multi-content requests. For single content requests, use the appropriate invoke_* tool. When using spawn_parallel_versions, pass a list where each item has:
- title: Short name for the version (e.g., "YouTube Video", "X Post", "Vlog")
- content_type: One of "video", "manim", "voice_music", or "music"
- instructions: Specific task instructions for that version

Use invoke_directors when the user wants to:
- Analyze or critique their video
- Get feedback or suggestions for improvement
- Optimize for a specific platform (YouTube, TikTok, etc.)
- Understand what's working or not working
- Get expert perspectives on their editing

Respond concisely with the result."""


def run_root_agent(model_id, messages, main_thread_runner):
    """
    Run the root agent with invoke_* tools. Sub-agents run in this thread;
    their tools run on the main thread via main_thread_runner.
    Returns the final response string.
    """
    from classes.ai_agent_runner import run_agent_with_tools

    # Build invoke_* tools that pass model_id and main_thread_runner into sub-agents
    def make_invoke_with_model():
        from langchain_core.tools import tool
        from classes.ai_multi_agent import sub_agents
        mid = model_id
        runner = main_thread_runner

        @tool
        def invoke_video_agent(task: str) -> str:
            """Route to the video/timeline agent. Use for: list files, add clips, export, timeline editing, generate video, split clips."""
            return sub_agents.run_video_agent(mid, task, runner)

        @tool
        def invoke_manim_agent(task: str) -> str:
            """Route to the Manim agent for educational/math animation videos."""
            return sub_agents.run_manim_agent(mid, task, runner)

        @tool
        def invoke_voice_music_agent(task: str) -> str:
            """Route to the voice/music agent for narration and music."""
            return sub_agents.run_voice_music_agent(mid, task, runner)

        @tool
        def invoke_music_agent(task: str) -> str:
            """Route to the music agent for Suno background music generation and timeline insertion."""
            return sub_agents.run_music_agent(mid, task, runner)

        @tool
        def invoke_directors(task: str, director_ids: list) -> str:
            """
            Route to directors for video analysis, critique, and improvement planning.

            Use when user wants to:
            - Analyze or critique their video
            - Get feedback or improvement suggestions
            - Optimize for a platform (YouTube, TikTok, etc.)
            - Understand what's working or not working

            Args:
                task: User's request or question
                director_ids: List of director IDs to use (e.g., ["youtube_director", "genz_director", "cinematic_director"])
                             Available directors: youtube_director, genz_director, cinematic_director

            Returns:
                Analysis results and improvement plan
            """
            from classes.ai_directors.director_orchestrator import run_directors
            return run_directors(mid, task, director_ids, runner)

        @tool
        def spawn_parallel_versions(content_requests: list) -> str:
            """
            Spawn multiple parallel versions for different content types.

            Use ONLY when user explicitly requests multiple content types.

            Args:
                content_requests: List of dicts, each with:
                    - title: str - Display name (e.g., "YouTube Video")
                    - content_type: str - One of "video", "manim", "voice_music", "music"
                    - instructions: str - Task instructions for this version

            Returns:
                Status message with version IDs and links to switch
            """
            from classes.app import get_app
            from classes.version_manager import get_version_manager
            from classes.version_executor import get_version_executor
            import copy

            try:
                app = get_app()
                version_manager = get_version_manager()
                version_executor = get_version_executor()

                # Get current project state as base snapshot
                base_snapshot = copy.deepcopy(app.project._data)

                # Create versions for each content request
                versions = []
                for req in content_requests:
                    title = req.get("title", "Untitled")
                    content_type = req.get("content_type", "video")
                    instructions = req.get("instructions", "")

                    # Create version
                    version = version_manager.create_version(
                        title=title,
                        content_type=content_type,
                        instructions=instructions,
                        base_snapshot=base_snapshot
                    )
                    versions.append(version)

                    # Submit for execution in background
                    version_executor.execute_version(version, mid, runner)

                # Return status message
                version_list = "\n".join([f"- {v.title} (ID: {v.version_id})" for v in versions])
                return (
                    f"Started parallel execution of {len(versions)} versions:\n{version_list}\n\n"
                    "Each version is now generating in the background. "
                    "You'll see progress cards in the chat. "
                    "Click 'Switch to this version' on any card when complete to view the results."
                )

            except Exception as e:
                from classes.logger import log
                log.error(f"spawn_parallel_versions failed: {e}", exc_info=True)
                return f"Error starting parallel execution: {e}"

        return [invoke_video_agent, invoke_manim_agent, invoke_voice_music_agent, invoke_music_agent, invoke_directors, spawn_parallel_versions]

    root_tools = make_invoke_with_model()
    # Root tools run in worker thread (no main-thread wrap)
    return run_agent_with_tools(
        model_id=model_id,
        messages=messages,
        tools=root_tools,
        main_thread_runner=None,  # do not wrap; invoke_* run in worker thread
        system_prompt=ROOT_SYSTEM_PROMPT,
        max_iterations=10,
    )
