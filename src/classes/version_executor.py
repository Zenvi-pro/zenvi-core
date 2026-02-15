"""
Version Executor for Parallel Background Execution

This module handles the execution of versions in background threads,
with progress reporting and state isolation.
"""

import copy
from typing import Optional
from PyQt5.QtCore import QObject, pyqtSignal

from classes.logger import log
from classes.version_manager import Version, VersionStatus, get_version_manager
from classes.ai_multi_agent.parallel_executor import _executor


class VersionExecutor(QObject):
    """
    Executes versions in background threads with progress tracking.

    Signals:
        version_progress: Emitted when version progress updates (version_id, progress)
        version_completed: Emitted when version completes (version_id, result_message)
        version_failed: Emitted when version fails (version_id, error_message)
        activity_step_added: Emitted when activity step is added (version_id, label, detail)
    """

    # Progress and status signals
    version_progress = pyqtSignal(str, float)  # version_id, progress (0.0-1.0)
    version_completed = pyqtSignal(str, str)  # version_id, result_message
    version_failed = pyqtSignal(str, str)  # version_id, error_message
    activity_step_added = pyqtSignal(str, str, str)  # version_id, label, detail

    def __init__(self):
        super().__init__()
        self._version_manager = get_version_manager()

    def execute_version(self, version: Version, model_id: str, main_thread_runner):
        """
        Execute a version in a background thread.

        Args:
            version: Version to execute
            model_id: LLM model ID to use
            main_thread_runner: MainThreadToolRunner instance
        """
        # Submit to thread pool for execution
        future = _executor.submit(
            self._run_version_in_thread,
            version,
            model_id,
            main_thread_runner
        )

        # Add callback for completion
        future.add_done_callback(lambda f: self._on_version_complete(version.version_id, f))

    def _run_version_in_thread(self, version: Version, model_id: str, main_thread_runner):
        """
        Run version execution in background thread.

        This method:
        1. Creates an isolated tool runner with version's state
        2. Routes to appropriate sub-agent based on content_type
        3. Executes the sub-agent with version's instructions
        4. Updates version status and progress
        """
        try:
            log.info(f"Starting version execution: {version.version_id} - {version.title}")

            # Update status to running
            version.update_status(VersionStatus.RUNNING)
            self.version_progress.emit(version.version_id, 0.0)

            # Add initial activity step
            version.add_activity_step("Starting task execution", "", False)
            self.activity_step_added.emit(version.version_id, "Starting task execution", "")

            # Create version-aware tool runner wrapper
            version_runner = VersionAwareToolRunner(
                version=version,
                base_runner=main_thread_runner,
                executor=self
            )

            # Route to appropriate sub-agent based on content_type
            from classes.ai_multi_agent import sub_agents

            agent_map = {
                "video": sub_agents.run_video_agent,
                "manim": sub_agents.run_manim_agent,
                "voice_music": sub_agents.run_voice_music_agent,
                "music": sub_agents.run_music_agent,
            }

            agent_runner = agent_map.get(version.content_type)
            if not agent_runner:
                raise ValueError(f"Unknown content type: {version.content_type}")

            # Execute sub-agent with version's instructions
            result = agent_runner(
                model_id=model_id,
                task_or_messages=version.instructions,
                main_thread_runner=version_runner
            )

            # Mark as completed
            version.update_status(VersionStatus.COMPLETED)
            version.update_progress(1.0)
            version.complete_last_activity_step()

            log.info(f"Version execution completed: {version.version_id}")

            return result

        except Exception as e:
            log.error(f"Version execution failed: {version.version_id}", exc_info=True)
            version.update_status(VersionStatus.FAILED, str(e))
            version.update_progress(0.0)
            raise

    def _on_version_complete(self, version_id: str, future):
        """
        Callback when version execution completes.

        Args:
            version_id: ID of the version
            future: Future object from thread pool
        """
        try:
            result = future.result()
            self.version_completed.emit(version_id, result)

            # Emit final progress update
            self.version_progress.emit(version_id, 1.0)

            # Notify version manager of update
            version = self._version_manager.get_version(version_id)
            if version:
                self._version_manager.version_updated.emit(version_id, version.to_dict())

        except Exception as e:
            log.error(f"Version {version_id} failed: {e}", exc_info=True)
            self.version_failed.emit(version_id, str(e))

            # Update version manager
            version = self._version_manager.get_version(version_id)
            if version:
                self._version_manager.version_updated.emit(version_id, version.to_dict())


class VersionAwareToolRunner:
    """
    Wrapper around MainThreadToolRunner that operates on version's isolated state.

    This ensures that tools executed within a version context modify the version's
    project snapshot rather than the global app.project state.
    """

    def __init__(self, version: Version, base_runner, executor: VersionExecutor):
        self.version = version
        self.base_runner = base_runner
        self.executor = executor
        self._tool_count = 0
        self._max_tools_estimate = 10  # Rough estimate for progress calculation

    def run_tool(self, name: str, args_dict: dict):
        """
        Run a tool with version-isolated state.

        This method:
        1. Adds activity step for the tool
        2. Delegates to base runner (which runs on main thread)
        3. Updates progress based on tool count
        4. Completes activity step
        """
        try:
            # Add activity step
            detail = f"Tool: {name}"
            self.version.add_activity_step(f"Executing {name}", detail, False)
            self.executor.activity_step_added.emit(self.version.version_id, f"Executing {name}", detail)

            # Set version context on base runner
            self.base_runner.set_version_context(self.version.version_id, self.version.project_snapshot)

            # Run tool on main thread via base runner
            result = self.base_runner.run_tool(name, args_dict)

            # Get updated state from base runner
            updated_state = self.base_runner.get_version_state(self.version.version_id)
            if updated_state:
                self.version.project_snapshot = updated_state

            # Complete activity step
            self.version.complete_last_activity_step()

            # Update progress (rough estimate based on tool count)
            self._tool_count += 1
            progress = min(0.9, self._tool_count / self._max_tools_estimate)
            self.version.update_progress(progress)
            self.executor.version_progress.emit(self.version.version_id, progress)

            return result

        except Exception as e:
            log.error(f"Tool execution failed in version {self.version.version_id}: {name}", exc_info=True)
            raise


# Global version executor instance
_version_executor = None


def get_version_executor() -> VersionExecutor:
    """Get or create the global version executor instance."""
    global _version_executor
    if _version_executor is None:
        _version_executor = VersionExecutor()
    return _version_executor
