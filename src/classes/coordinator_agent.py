"""
@file
@brief Coordinator agent for managing theme application workload
@author Zenvi Team

@section LICENSE

Copyright (c) 2024 Zenvi Core
This file is part of Zenvi Core video editor.
"""

import time
from typing import Dict, Any, List, Optional
from classes.logger import log
from classes.agent_communication_bus import get_communication_bus, AgentMessage


class CoordinatorAgent:
    """
    Coordinates theme application across multiple worker agents.
    Manages workload distribution, agent spawning, and progress reporting.
    """
    
    def __init__(self, theme_data: Dict[str, Any], clip_ids: List[str], components: Dict[str, bool]):
        """
        Initialize coordinator agent
        
        Args:
            theme_data: Theme configuration
            clip_ids: List of clip IDs to process
            components: Dict of which components to apply (color, sound, captions)
        """
        self.theme_data = theme_data
        self.clip_ids = clip_ids
        self.components = components
        self.bus = get_communication_bus()
        
        # Worker agents
        self.color_agents: List[Any] = []
        self.sound_agents: List[Any] = []
        self.caption_agents: List[Any] = []
        
        # Progress tracking
        self.total_clips = len(clip_ids)
        self.completed_clips: Dict[str, Dict[str, bool]] = {
            clip_id: {"color": False, "sound": False, "captions": False}
            for clip_id in clip_ids
        }
        
        # Subscribe to completion messages
        self.bus.subscribe("clip_complete", self._handle_clip_complete)
        self.bus.subscribe("clip_error", self._handle_clip_error)
        self.bus.subscribe("agent_idle", self._handle_agent_idle)
        
        log.info(f"CoordinatorAgent initialized for {self.total_clips} clips")
    
    def execute(self) -> Dict[str, Any]:
        """
        Execute the theme application
        
        Returns:
            Results dict with success status and statistics
        """
        start_time = time.time()
        
        try:
            # 1. Determine how many workers needed
            worker_counts = self._calculate_worker_counts()
            
            log.info(f"Spawning workers: {worker_counts}")
            
            # 2. Spawn worker agents
            if self.components.get("color", True):
                self._spawn_color_agents(worker_counts["color"])
            
            if self.components.get("sound", True):
                self._spawn_sound_agents(worker_counts["sound"])
            
            if self.components.get("captions", False):
                self._spawn_caption_agents(worker_counts["captions"])
            
            # 3. Distribute clips to workers
            self._distribute_clips()
            
            # 4. Start all workers
            self._start_workers()
            
            # 5. Monitor progress until completion
            result = self._monitor_progress()
            
            elapsed = time.time() - start_time
            
            return {
                "success": True,
                "total_clips": self.total_clips,
                "elapsed_time": elapsed,
                **result
            }
            
        except Exception as e:
            log.error(f"Coordinator execution failed: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }
    
    def _calculate_worker_counts(self) -> Dict[str, int]:
        """Calculate how many workers needed for each type"""
        counts = {
            "color": 0,
            "sound": 0,
            "captions": 0
        }
        
        clip_count = self.total_clips
        
        # Color agents: 1 per 10 clips (max 5)
        if self.components.get("color", True):
            counts["color"] = min(max(1, clip_count // 10), 5)
        
        # Sound agents: 1 per 5 clips (max 4)
        if self.components.get("sound", True):
            counts["sound"] = min(max(1, clip_count // 5), 4)
        
        # Caption agents: 1 per 2 clips (max 3, API rate limits)
        if self.components.get("captions", False):
            counts["captions"] = min(max(1, clip_count // 2), 3)
        
        return counts
    
    def _spawn_color_agents(self, count: int):
        """Spawn color grading agents"""
        from classes.color_grading_agent import ColorGradingAgent
        
        for i in range(count):
            agent = ColorGradingAgent(self.theme_data)
            self.color_agents.append(agent)
            log.info(f"Spawned ColorGradingAgent #{i+1}")
    
    def _spawn_sound_agents(self, count: int):
        """Spawn sound grading agents"""
        from classes.sound_grading_agent import SoundGradingAgent
        
        for i in range(count):
            agent = SoundGradingAgent(self.theme_data)
            self.sound_agents.append(agent)
            log.info(f"Spawned SoundGradingAgent #{i+1}")
    
    def _spawn_caption_agents(self, count: int):
        """Spawn caption agents"""
        from classes.caption_agent import CaptionAgent
        
        for i in range(count):
            agent = CaptionAgent(self.theme_data)
            self.caption_agents.append(agent)
            log.info(f"Spawned CaptionAgent #{i+1}")
    
    def _distribute_clips(self):
        """Distribute clips evenly among workers"""
        # Distribute to color agents
        if self.color_agents:
            clips_per_agent = len(self.clip_ids) // len(self.color_agents)
            for i, agent in enumerate(self.color_agents):
                start_idx = i * clips_per_agent
                end_idx = start_idx + clips_per_agent if i < len(self.color_agents) - 1 else len(self.clip_ids)
                assigned = self.clip_ids[start_idx:end_idx]
                agent.assign_clips(assigned)
                log.debug(f"Assigned {len(assigned)} clips to {agent.agent_id}")
        
        # Distribute to sound agents
        if self.sound_agents:
            clips_per_agent = len(self.clip_ids) // len(self.sound_agents)
            for i, agent in enumerate(self.sound_agents):
                start_idx = i * clips_per_agent
                end_idx = start_idx + clips_per_agent if i < len(self.sound_agents) - 1 else len(self.clip_ids)
                assigned = self.clip_ids[start_idx:end_idx]
                agent.assign_clips(assigned)
                log.debug(f"Assigned {len(assigned)} clips to {agent.agent_id}")
        
        # Distribute to caption agents
        if self.caption_agents:
            clips_per_agent = len(self.clip_ids) // len(self.caption_agents)
            for i, agent in enumerate(self.caption_agents):
                start_idx = i * clips_per_agent
                end_idx = start_idx + clips_per_agent if i < len(self.caption_agents) - 1 else len(self.clip_ids)
                assigned = self.clip_ids[start_idx:end_idx]
                agent.assign_clips(assigned)
                log.debug(f"Assigned {len(assigned)} clips to {agent.agent_id}")
    
    def _start_workers(self):
        """Start all worker agents"""
        for agent in self.color_agents + self.sound_agents + self.caption_agents:
            agent.start()
    
    def _monitor_progress(self, timeout: float = 600.0) -> Dict[str, Any]:
        """
        Monitor progress until all clips are complete or timeout
        
        Args:
            timeout: Maximum time to wait in seconds
        
        Returns:
            Results dict
        """
        start_time = time.time()
        last_progress_report = 0
        
        while time.time() - start_time < timeout:
            # Check if all clips are complete
            if self._all_clips_complete():
                log.info("All clips completed successfully")
                break
            
            # Report progress every 5 seconds
            elapsed = time.time() - start_time
            if elapsed - last_progress_report >= 5.0:
                progress = self._calculate_progress()
                log.info(f"Progress: {progress:.1f}% ({elapsed:.1f}s elapsed)")
                last_progress_report = elapsed
            
            time.sleep(0.5)
        
        # Stop all workers
        self._stop_workers()
        
        # Gather statistics
        stats = self._gather_statistics()
        
        return stats
    
    def _all_clips_complete(self) -> bool:
        """Check if all required components are complete for all clips"""
        for clip_id, status in self.completed_clips.items():
            if self.components.get("color", True) and not status["color"]:
                return False
            if self.components.get("sound", True) and not status["sound"]:
                return False
            if self.components.get("captions", False) and not status["captions"]:
                return False
        return True
    
    def _calculate_progress(self) -> float:
        """Calculate overall progress percentage"""
        total_tasks = 0
        completed_tasks = 0
        
        for clip_id, status in self.completed_clips.items():
            if self.components.get("color", True):
                total_tasks += 1
                if status["color"]:
                    completed_tasks += 1
            
            if self.components.get("sound", True):
                total_tasks += 1
                if status["sound"]:
                    completed_tasks += 1
            
            if self.components.get("captions", False):
                total_tasks += 1
                if status["captions"]:
                    completed_tasks += 1
        
        if total_tasks == 0:
            return 100.0
        
        return (completed_tasks / total_tasks) * 100.0
    
    def _stop_workers(self):
        """Stop all worker agents"""
        for agent in self.color_agents + self.sound_agents + self.caption_agents:
            agent.stop()
    
    def _gather_statistics(self) -> Dict[str, Any]:
        """Gather statistics from all workers"""
        stats = {
            "color_stats": [],
            "sound_stats": [],
            "caption_stats": []
        }
        
        for agent in self.color_agents:
            stats["color_stats"].append(agent.get_performance_stats())
        
        for agent in self.sound_agents:
            stats["sound_stats"].append(agent.get_performance_stats())
        
        for agent in self.caption_agents:
            stats["caption_stats"].append(agent.get_performance_stats())
        
        return stats
    
    def _handle_clip_complete(self, message: AgentMessage):
        """Handle clip completion message"""
        payload = message.payload
        clip_id = payload.get("clip_id")
        agent_type = message.sender_type
        
        if clip_id in self.completed_clips:
            if agent_type == "color_grading":
                self.completed_clips[clip_id]["color"] = True
            elif agent_type == "sound_grading":
                self.completed_clips[clip_id]["sound"] = True
            elif agent_type == "caption":
                self.completed_clips[clip_id]["captions"] = True
            
            log.debug(f"Clip {clip_id} {agent_type} completed")
    
    def _handle_clip_error(self, message: AgentMessage):
        """Handle clip error message"""
        payload = message.payload
        clip_id = payload.get("clip_id")
        error = payload.get("error")
        
        log.error(f"Clip {clip_id} failed: {error}")
        # Mark as complete (failed) so we don't wait forever
        # In production, might want to retry
    
    def _handle_agent_idle(self, message: AgentMessage):
        """Handle agent idle message"""
        # Could implement dynamic work reassignment here
        pass
