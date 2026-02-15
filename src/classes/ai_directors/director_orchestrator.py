"""
Director Orchestrator

Coordinates multiple directors through analysis, debate, and consensus phases.
"""

from typing import List, Dict, Any
import concurrent.futures
from classes.logger import log
from classes.ai_directors.director_agent import Director, DirectorAnalysis
from classes.ai_directors.director_plan import DirectorPlan, DebateMessage


class DirectorOrchestrator:
    """
    Orchestrates multiple directors through the analysis and debate process.

    Flow:
    1. Phase 1: Parallel analysis (each director analyzes independently)
    2. Phase 2: Structured debate (directors respond to each other)
    3. Phase 3: Consensus synthesis (aggregate into unified plan)
    """

    def __init__(
        self,
        directors: List[Director],
        max_debate_rounds: int = 3,
        max_workers: int = 3,
    ):
        self.directors = directors
        self.max_debate_rounds = max_debate_rounds
        self.max_workers = max_workers

    def run_directors(
        self,
        model_id: str,
        task: str,
        main_thread_runner,
        project_data: Dict[str, Any],
    ) -> DirectorPlan:
        """
        Run full director workflow: analysis → debate → plan.

        Args:
            model_id: LLM model to use
            task: User's task/request
            main_thread_runner: MainThreadToolRunner for tool execution
            project_data: Current project state

        Returns:
            DirectorPlan with aggregated recommendations
        """
        log.info(f"Running {len(self.directors)} directors for task: {task}")
        self.model_id = model_id
        self.main_thread_runner = main_thread_runner

        # Phase 1: Parallel analysis
        analyses = self._parallel_analysis(project_data)

        # Phase 2: Structured debate
        debate_messages = self._run_debate(analyses)

        # Phase 3: Synthesize consensus plan
        plan = self._synthesize_consensus(task, analyses, debate_messages)

        log.info(f"Generated plan with {len(plan.steps)} steps")
        return plan

    def _parallel_analysis(self, project_data: Dict[str, Any]) -> List[DirectorAnalysis]:
        """
        Run directors in parallel for independent analysis.

        Args:
            project_data: Current project state

        Returns:
            List of DirectorAnalysis from each director
        """
        log.info(f"Phase 1: Parallel analysis with {len(self.directors)} directors")

        # Get analysis tools
        from classes.ai_directors.director_tools import get_director_analysis_tools_for_langchain
        analysis_tools = get_director_analysis_tools_for_langchain()

        analyses = []

        # Run directors in parallel with ThreadPoolExecutor
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_director = {
                executor.submit(
                    director.analyze_project,
                    self.model_id,
                    analysis_tools,
                    self.main_thread_runner
                ): director
                for director in self.directors
            }

            for future in concurrent.futures.as_completed(future_to_director):
                director = future_to_director[future]
                try:
                    analysis = future.result()
                    analyses.append(analysis)
                    log.info(f"{director.name}: Analysis complete")
                except Exception as e:
                    log.error(f"{director.name}: Analysis failed: {e}", exc_info=True)

        return analyses

    def _run_debate(self, analyses: List[DirectorAnalysis]) -> List[DebateMessage]:
        """
        Run structured debate between directors.

        Args:
            analyses: Initial analyses from each director

        Returns:
            List of DebateMessages from the debate
        """
        log.info(f"Phase 2: Running debate ({self.max_debate_rounds} rounds)")

        debate_messages = []

        # Add initial analyses as debate messages
        for analysis in analyses:
            message = DebateMessage(
                director_id=analysis.director_id,
                director_name=analysis.director_name,
                round_number=0,
                message_type="analysis",
                content=analysis.analysis_text,
                references=[],
            )
            debate_messages.append(message)

        # Run debate rounds
        for round_num in range(1, self.max_debate_rounds + 1):
            log.info(f"Debate round {round_num}/{self.max_debate_rounds}")

            # Each director critiques peer analyses
            for director in self.directors:
                try:
                    response = director.critique_peer_analysis(
                        self.model_id,
                        analyses,
                        round_num,
                    )

                    # Convert response to debate message
                    content = f"Round {round_num} response from {director.name}"
                    if response.agreements:
                        content += f"\n\nAgreements: {', '.join(response.agreements)}"
                    if response.disagreements:
                        content += f"\n\nDisagreements: {', '.join(response.disagreements)}"
                    if response.new_insights:
                        content += f"\n\nNew Insights: {', '.join(response.new_insights)}"
                    if response.revised_recommendations:
                        content += f"\n\nRevised Recommendations: {', '.join(response.revised_recommendations)}"

                    message = DebateMessage(
                        director_id=director.id,
                        director_name=director.name,
                        round_number=round_num,
                        message_type="critique",
                        content=content,
                        references=[a.director_id for a in analyses if a.director_id != director.id],
                    )
                    debate_messages.append(message)

                except Exception as e:
                    log.error(f"{director.name}: Debate round {round_num} failed: {e}", exc_info=True)

            # Check for convergence (simplified: just run all rounds for now)
            # TODO: Implement convergence detection

        return debate_messages

    def _synthesize_consensus(
        self,
        task: str,
        analyses: List[DirectorAnalysis],
        debate_messages: List[DebateMessage],
    ) -> DirectorPlan:
        """
        Synthesize consensus plan from analyses and debate.

        Args:
            task: Original user task
            analyses: Director analyses
            debate_messages: Debate transcript

        Returns:
            DirectorPlan with aggregated recommendations
        """
        log.info("Phase 3: Synthesizing consensus plan")

        # Create plan
        plan = DirectorPlan(
            title=f"Director Plan: {task}",
            summary="Aggregated recommendations from directors",
            created_by=[d.id for d in self.directors],
        )

        # Add debate transcript
        for message in debate_messages:
            plan.add_debate_message(message)

        # Synthesize plan using LLM
        try:
            from classes.ai_llm_registry import get_model
            from langchain_core.messages import SystemMessage, HumanMessage

            # Build synthesis prompt
            director_summaries = []
            for analysis in analyses:
                director_summaries.append(
                    f"**{analysis.director_name}:**\n{analysis.analysis_text[:800]}"
                )

            synthesis_prompt = f"""Based on the director analyses below, create a concrete action plan to improve this video.

Original Task: {task}

Director Analyses:
{chr(10).join(director_summaries)}

Create a plan with 3-7 specific, actionable steps. For each step, provide:
1. Description: What to do
2. Rationale: Why this improves the video
3. Confidence: How confident (0.0-1.0)

Focus on the most impactful improvements where directors agree or complement each other.

Format your response as a numbered list with clear, specific actions."""

            llm = get_model(self.model_id)
            messages = [
                SystemMessage(content="You are a video editing expert synthesizing feedback from multiple directors into an actionable plan."),
                HumanMessage(content=synthesis_prompt),
            ]

            response = llm.invoke(messages)
            response_text = response.content if hasattr(response, 'content') else str(response)

            # Store synthesis as summary
            plan.summary = response_text

            # Parse response into steps
            # For now, create a simple set of steps based on common recommendations
            steps = self._parse_plan_steps(response_text, analyses)
            for step in steps:
                plan.add_step(step)

        except Exception as e:
            log.error(f"Plan synthesis failed: {e}", exc_info=True)
            # Fallback: create basic plan
            from classes.ai_directors.director_plan import PlanStep, PlanStepType
            import uuid

            step = PlanStep(
                step_id=str(uuid.uuid4()),
                type=PlanStepType.EDIT_TIMELINE,
                description=f"Review director feedback and apply improvements manually",
                agent="video",
                tool_name="manual_review",
                tool_args={},
                rationale="Plan synthesis encountered an error",
                confidence=0.5,
            )
            plan.add_step(step)

        # Calculate overall confidence
        if analyses:
            plan.confidence = sum(a.confidence for a in analyses) / len(analyses)

        return plan

    def _parse_plan_steps(self, plan_text: str, analyses: List[DirectorAnalysis]):
        """
        Parse plan text into PlanStep objects.

        Args:
            plan_text: LLM-generated plan text
            analyses: Director analyses for context

        Returns:
            List of PlanStep objects
        """
        from classes.ai_directors.director_plan import PlanStep, PlanStepType
        import uuid

        steps = []

        # Simple parsing: Look for numbered items
        lines = plan_text.split('\n')
        current_step = None
        step_description = ""

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Check if line starts a new step (e.g., "1.", "2.", "Step 1:", etc.)
            if any(line.startswith(f"{i}.") or line.startswith(f"Step {i}") for i in range(1, 20)):
                # Save previous step
                if step_description:
                    steps.append(self._create_step_from_description(step_description))

                # Start new step
                step_description = line
            else:
                # Continue current step
                if step_description:
                    step_description += " " + line

        # Add last step
        if step_description:
            steps.append(self._create_step_from_description(step_description))

        # If parsing failed, create a generic step
        if not steps:
            steps.append(PlanStep(
                step_id=str(uuid.uuid4()),
                type=PlanStepType.EDIT_TIMELINE,
                description="Apply director recommendations",
                agent="video",
                tool_name="manual_action",
                tool_args={},
                rationale="Based on director consensus",
                confidence=0.7,
            ))

        return steps

    def _create_step_from_description(self, description: str) -> 'PlanStep':
        """Create a PlanStep from a text description."""
        from classes.ai_directors.director_plan import PlanStep, PlanStepType
        import uuid

        # Clean up description (remove numbering)
        desc = description
        for i in range(1, 20):
            desc = desc.replace(f"{i}. ", "").replace(f"Step {i}: ", "").replace(f"Step {i}. ", "")

        # Determine step type based on keywords
        desc_lower = desc.lower()
        if any(word in desc_lower for word in ['cut', 'trim', 'split', 'clip']):
            step_type = PlanStepType.SPLIT_CLIP
        elif any(word in desc_lower for word in ['transition', 'fade', 'dissolve']):
            step_type = PlanStepType.ADD_TRANSITION
        elif any(word in desc_lower for word in ['audio', 'sound', 'music', 'volume']):
            step_type = PlanStepType.ADJUST_AUDIO
        elif any(word in desc_lower for word in ['effect', 'filter', 'color', 'grading']):
            step_type = PlanStepType.ADD_EFFECT
        elif any(word in desc_lower for word in ['reorder', 'move', 'rearrange']):
            step_type = PlanStepType.REORDER_CLIPS
        else:
            step_type = PlanStepType.EDIT_TIMELINE

        return PlanStep(
            step_id=str(uuid.uuid4()),
            type=step_type,
            description=desc[:200],  # Limit length
            agent="video",
            tool_name="manual_action",  # Will be mapped to actual tools in executor
            tool_args={},
            rationale="Based on director recommendations",
            confidence=0.7,
        )


def run_directors(model_id: str, task: str, director_ids: List[str], main_thread_runner) -> str:
    """
    Entry point for running directors from root agent.

    Args:
        model_id: LLM model to use
        task: User's task
        director_ids: List of director IDs to use
        main_thread_runner: MainThreadToolRunner

    Returns:
        Status message
    """
    try:
        from classes.ai_directors.director_loader import get_director_loader

        # Load directors
        loader = get_director_loader()
        directors = []
        for director_id in director_ids:
            director = loader.load_director(director_id)
            if director:
                directors.append(director)
            else:
                log.warning(f"Failed to load director: {director_id}")

        if not directors:
            return "Error: No directors loaded"

        # Run orchestrator
        orchestrator = DirectorOrchestrator(directors)
        plan = orchestrator.run_directors(model_id, task, main_thread_runner, {})

        # Display plan in UI
        try:
            from classes.app import get_app
            app = get_app()
            if hasattr(app, 'window') and hasattr(app.window, 'dockPlanReview'):
                # Show plan in review UI
                app.window.dockPlanReview.show_plan(plan)
                return f"Directors analyzed the project and created a plan with {len(plan.steps)} steps. Review the plan in the Plan Review panel."
            else:
                return f"Directors analyzed the project. Generated plan with {len(plan.steps)} steps. Plan Review UI not available."
        except Exception as e:
            log.error(f"Failed to display plan in UI: {e}", exc_info=True)
            return f"Directors analyzed the project. Generated plan with {len(plan.steps)} steps, but failed to display UI."

    except Exception as e:
        log.error(f"run_directors failed: {e}", exc_info=True)
        return f"Error: {e}"
