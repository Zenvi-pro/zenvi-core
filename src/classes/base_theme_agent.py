"""
@file
@brief Base class for theme application agents
@author Zenvi Team

@section LICENSE

Copyright (c) 2024 Zenvi Core
This file is part of Zenvi Core video editor.
"""

import uuid
import time
import threading
from typing import Dict, Any, List, Optional, Callable
from abc import ABC, abstractmethod
from classes.logger import log
from classes.agent_communication_bus import get_communication_bus, AgentMessage


class BaseThemeAgent(ABC):
    """
    Base class for all theme application agents.
    Provides common functionality for communication, progress reporting, and error handling.
    """
    
    def __init__(self, agent_type: str, theme_data: Dict[str, Any]):
        """
        Initialize the base agent
        
        Args:
            agent_type: Type of agent (e.g., 'color', 'sound', 'caption')
            theme_data: Theme configuration data
        """
        self.agent_id = f"{agent_type}_{uuid.uuid4().hex[:8]}"
        self.agent_type = agent_type
        self.theme_data = theme_data
        self.bus = get_communication_bus()
        
        # Agent state
        self.status = "idle"  # idle, processing, waiting, error, completed
        self.assigned_clips: List[str] = []
        self.processed_clips: List[str] = []
        self.failed_clips: List[Dict[str, Any]] = []
        self.current_clip: Optional[str] = None
        
        # Performance tracking
        self.start_time: Optional[float] = None
        self.processing_times: List[float] = []
        
        # Threading
        self._worker_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
        
        # Register with bus
        self.bus.register_agent(
            self.agent_id,
            self.agent_type,
            metadata={"theme_id": theme_data.get("theme_id", "unknown")}
        )
        
        # Subscribe to relevant topics
        self._setup_subscriptions()
        
        log.info(f"{self.agent_type} agent initialized: {self.agent_id}")
    
    def _setup_subscriptions(self):
        """Set up message bus subscriptions"""
        # Subscribe to clip assignments
        self.bus.subscribe("clip_assigned", self._handle_clip_assignment)
        
        # Subscribe to help responses (if we requested help)
        self.bus.subscribe("help_response", self._handle_help_response)
        
        # Subscribe to shutdown signals
        self.bus.subscribe("shutdown_all", self._handle_shutdown)
    
    def start(self):
        """Start the agent worker thread"""
        if self._running:
            log.warning(f"Agent {self.agent_id} already running")
            return
        
        self._running = True
        self.status = "idle"
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        log.info(f"Agent {self.agent_id} started")
    
    def stop(self):
        """Stop the agent"""
        if not self._running:
            return
        
        self._running = False
        if self._worker_thread:
            self._worker_thread.join(timeout=10.0)
        
        self.bus.unregister_agent(self.agent_id)
        log.info(f"Agent {self.agent_id} stopped")
    
    def assign_clips(self, clip_ids: List[str]):
        """
        Assign clips to this agent
        
        Args:
            clip_ids: List of clip IDs to process
        """
        with self._lock:
            self.assigned_clips.extend(clip_ids)
        
        log.info(f"Agent {self.agent_id} assigned {len(clip_ids)} clips")
    
    def _worker_loop(self):
        """Main worker loop - processes assigned clips"""
        log.debug(f"Agent {self.agent_id} worker loop started")
        
        while self._running:
            try:
                # Get next clip to process
                clip_id = None
                with self._lock:
                    if self.assigned_clips:
                        clip_id = self.assigned_clips.pop(0)
                        self.current_clip = clip_id
                
                if clip_id:
                    self._process_clip_wrapper(clip_id)
                else:
                    # No clips to process - idle
                    if self.status == "processing":
                        self.status = "idle"
                        self._report_idle()
                    time.sleep(0.5)
                    
            except Exception as e:
                log.error(f"Error in worker loop for {self.agent_id}: {e}", exc_info=True)
                time.sleep(1.0)
        
        log.debug(f"Agent {self.agent_id} worker loop stopped")
    
    def _process_clip_wrapper(self, clip_id: str):
        """Wrapper around process_clip with error handling and timing"""
        self.status = "processing"
        start_time = time.time()
        
        try:
            # Report start
            self._report_progress(clip_id, "started", 0.0)
            
            # Process the clip (implemented by subclass)
            result = self.process_clip(clip_id)
            
            # Calculate time
            elapsed = time.time() - start_time
            self.processing_times.append(elapsed)
            
            # Mark as processed
            with self._lock:
                self.processed_clips.append(clip_id)
                self.current_clip = None
            
            # Report completion
            self._report_completion(clip_id, elapsed, result)
            
        except Exception as e:
            log.error(f"Error processing clip {clip_id}: {e}", exc_info=True)
            
            # Mark as failed
            with self._lock:
                self.failed_clips.append({
                    "clip_id": clip_id,
                    "error": str(e),
                    "timestamp": time.time()
                })
                self.current_clip = None
            
            # Report error
            self._report_error(clip_id, str(e))
    
    @abstractmethod
    def process_clip(self, clip_id: str) -> Dict[str, Any]:
        """
        Process a single clip - must be implemented by subclass
        
        Args:
            clip_id: Clip ID to process
        
        Returns:
            Dictionary with processing results
        """
        pass
    
    def _report_progress(self, clip_id: str, stage: str, progress: float):
        """Report progress for a clip"""
        self.bus.publish(
            topic="clip_progress",
            payload={
                "clip_id": clip_id,
                "agent_id": self.agent_id,
                "stage": stage,
                "progress": progress
            },
            sender_id=self.agent_id,
            sender_type=self.agent_type,
            priority="normal"
        )
    
    def _report_completion(self, clip_id: str, elapsed_time: float, result: Dict[str, Any]):
        """Report clip processing completion"""
        self.bus.publish(
            topic="clip_complete",
            payload={
                "clip_id": clip_id,
                "agent_id": self.agent_id,
                "elapsed_time": elapsed_time,
                "result": result
            },
            sender_id=self.agent_id,
            sender_type=self.agent_type,
            priority="normal"
        )
        log.info(f"Agent {self.agent_id} completed clip {clip_id} in {elapsed_time:.2f}s")
    
    def _report_error(self, clip_id: str, error_message: str):
        """Report clip processing error"""
        self.bus.publish(
            topic="clip_error",
            payload={
                "clip_id": clip_id,
                "agent_id": self.agent_id,
                "error": error_message
            },
            sender_id=self.agent_id,
            sender_type=self.agent_type,
            priority="high"
        )
    
    def _report_idle(self):
        """Report that agent is idle and available for work"""
        self.bus.publish(
            topic="agent_idle",
            payload={
                "agent_id": self.agent_id,
                "processed_count": len(self.processed_clips),
                "failed_count": len(self.failed_clips)
            },
            sender_id=self.agent_id,
            sender_type=self.agent_type,
            priority="low"
        )
    
    def request_help(self, problem: str, clip_id: str, details: Optional[Dict] = None):
        """
        Request help from other agents
        
        Args:
            problem: Description of the problem
            clip_id: Clip ID having issues
            details: Optional additional details
        """
        self.bus.publish(
            topic="request_help",
            payload={
                "problem": problem,
                "clip_id": clip_id,
                "agent_id": self.agent_id,
                "agent_type": self.agent_type,
                "details": details or {}
            },
            sender_id=self.agent_id,
            sender_type=self.agent_type,
            priority="high"
        )
        log.info(f"Agent {self.agent_id} requested help for clip {clip_id}")
    
    def request_worker(self, reason: str, suggested_clips: Optional[List[str]] = None):
        """
        Request spawning of additional worker agent
        
        Args:
            reason: Reason for needing additional worker
            suggested_clips: Optional list of clips for new worker
        """
        self.bus.request_agent_spawn(
            agent_type=self.agent_type,
            requester_id=self.agent_id,
            task_data={
                "reason": reason,
                "suggested_clips": suggested_clips or [],
                "theme_data": self.theme_data
            },
            priority="normal"
        )
    
    def _handle_clip_assignment(self, message: AgentMessage):
        """Handle clip assignment messages"""
        payload = message.payload
        
        # Check if this assignment is for us
        target_agent = payload.get("target_agent_id")
        if target_agent and target_agent != self.agent_id:
            return
        
        # Check if it's for our agent type
        target_type = payload.get("target_agent_type")
        if target_type and target_type != self.agent_type:
            return
        
        clip_ids = payload.get("clip_ids", [])
        if clip_ids:
            self.assign_clips(clip_ids)
    
    def _handle_help_response(self, message: AgentMessage):
        """Handle help responses from other agents"""
        payload = message.payload
        
        # Check if this response is for us
        if payload.get("target_agent_id") == self.agent_id:
            log.info(f"Agent {self.agent_id} received help response")
            # Subclasses can override to handle specific help
    
    def _handle_shutdown(self, message: AgentMessage):
        """Handle shutdown signal"""
        log.info(f"Agent {self.agent_id} received shutdown signal")
        self.stop()
    
    def get_status(self) -> Dict[str, Any]:
        """Get current agent status"""
        with self._lock:
            avg_time = sum(self.processing_times) / len(self.processing_times) if self.processing_times else 0
            
            return {
                "agent_id": self.agent_id,
                "agent_type": self.agent_type,
                "status": self.status,
                "assigned_count": len(self.assigned_clips),
                "processed_count": len(self.processed_clips),
                "failed_count": len(self.failed_clips),
                "current_clip": self.current_clip,
                "avg_processing_time": avg_time,
                "total_processing_time": sum(self.processing_times)
            }
    
    def get_performance_stats(self) -> Dict[str, Any]:
        """Get detailed performance statistics"""
        with self._lock:
            if not self.processing_times:
                return {
                    "total_clips": 0,
                    "avg_time": 0,
                    "min_time": 0,
                    "max_time": 0,
                    "total_time": 0
                }
            
            return {
                "total_clips": len(self.processed_clips),
                "avg_time": sum(self.processing_times) / len(self.processing_times),
                "min_time": min(self.processing_times),
                "max_time": max(self.processing_times),
                "total_time": sum(self.processing_times),
                "failed_clips": len(self.failed_clips)
            }
