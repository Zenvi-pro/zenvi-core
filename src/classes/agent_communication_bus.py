"""
@file
@brief Agent communication bus for multi-agent coordination
@author Zenvi Team

@section LICENSE

Copyright (c) 2024 Zenvi Core
This file is part of Zenvi Core video editor.
"""

import uuid
import time
import threading
import queue
from datetime import datetime
from typing import Dict, Any, List, Callable, Optional
from collections import defaultdict
from classes.logger import log


class AgentMessage:
    """Represents a message between agents"""
    
    def __init__(
        self,
        topic: str,
        payload: Dict[str, Any],
        sender_id: str,
        sender_type: str,
        priority: str = "normal"
    ):
        """
        Initialize an agent message
        
        Args:
            topic: Message topic (e.g., 'clip_complete', 'need_worker')
            payload: Message data
            sender_id: Unique ID of sending agent
            sender_type: Type of sending agent (e.g., 'color', 'sound')
            priority: Message priority ('low', 'normal', 'high', 'critical')
        """
        self.message_id = str(uuid.uuid4())
        self.timestamp = datetime.now()
        self.topic = topic
        self.payload = payload
        self.sender_id = sender_id
        self.sender_type = sender_type
        self.priority = priority
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert message to dictionary"""
        return {
            "message_id": self.message_id,
            "timestamp": self.timestamp.isoformat(),
            "topic": self.topic,
            "payload": self.payload,
            "sender_id": self.sender_id,
            "sender_type": self.sender_type,
            "priority": self.priority
        }


class AgentRegistry:
    """Registry for tracking active agents"""
    
    def __init__(self):
        self.agents: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
    
    def register(self, agent_id: str, agent_type: str, metadata: Optional[Dict] = None):
        """Register a new agent"""
        with self._lock:
            self.agents[agent_id] = {
                "agent_id": agent_id,
                "agent_type": agent_type,
                "status": "active",
                "registered_at": datetime.now(),
                "last_activity": datetime.now(),
                "metadata": metadata or {}
            }
        log.debug(f"Agent registered: {agent_id} ({agent_type})")
    
    def unregister(self, agent_id: str):
        """Unregister an agent"""
        with self._lock:
            if agent_id in self.agents:
                del self.agents[agent_id]
        log.debug(f"Agent unregistered: {agent_id}")
    
    def update_activity(self, agent_id: str):
        """Update agent's last activity timestamp"""
        with self._lock:
            if agent_id in self.agents:
                self.agents[agent_id]["last_activity"] = datetime.now()
    
    def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get agent info"""
        with self._lock:
            return self.agents.get(agent_id)
    
    def get_agents_by_type(self, agent_type: str) -> List[Dict[str, Any]]:
        """Get all agents of a specific type"""
        with self._lock:
            return [
                agent for agent in self.agents.values()
                if agent["agent_type"] == agent_type
            ]
    
    def get_all_agents(self) -> List[Dict[str, Any]]:
        """Get all registered agents"""
        with self._lock:
            return list(self.agents.values())


class AgentCommunicationBus:
    """
    Central message bus for agent communication.
    Enables agents to publish messages, subscribe to topics, and coordinate work.
    """
    
    def __init__(self):
        """Initialize the communication bus"""
        self.message_queue = queue.PriorityQueue()
        self.subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self.registry = AgentRegistry()
        self._running = False
        self._worker_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        
        # Message history (for debugging/monitoring)
        self._message_history: List[AgentMessage] = []
        self._max_history_size = 1000
        
        log.info("AgentCommunicationBus initialized")
    
    def start(self):
        """Start the message processing worker thread"""
        if self._running:
            log.warning("Communication bus already running")
            return
        
        self._running = True
        self._worker_thread = threading.Thread(target=self._process_messages, daemon=True)
        self._worker_thread.start()
        log.info("Communication bus started")
    
    def stop(self):
        """Stop the message processing"""
        if not self._running:
            return
        
        self._running = False
        if self._worker_thread:
            self._worker_thread.join(timeout=5.0)
        log.info("Communication bus stopped")
    
    def publish(
        self,
        topic: str,
        payload: Dict[str, Any],
        sender_id: str,
        sender_type: str,
        priority: str = "normal"
    ):
        """
        Publish a message to the bus
        
        Args:
            topic: Message topic
            payload: Message data
            sender_id: Sender's agent ID
            sender_type: Sender's agent type
            priority: Message priority
        """
        message = AgentMessage(topic, payload, sender_id, sender_type, priority)
        
        # Priority mapping (lower number = higher priority)
        priority_map = {
            "critical": 0,
            "high": 1,
            "normal": 2,
            "low": 3
        }
        priority_val = priority_map.get(priority, 2)
        
        # Add to queue with priority
        self.message_queue.put((priority_val, time.time(), message))
        
        # Update agent activity
        self.registry.update_activity(sender_id)
        
        # Add to history
        self._add_to_history(message)
        
        log.debug(f"Message published: {topic} from {sender_id}")
    
    def subscribe(self, topic: str, callback: Callable[[AgentMessage], None]):
        """
        Subscribe to a topic
        
        Args:
            topic: Topic to subscribe to (supports wildcards: 'clip_*', '*')
            callback: Function to call when message received
        """
        with self._lock:
            self.subscribers[topic].append(callback)
        log.debug(f"New subscription to topic: {topic}")
    
    def unsubscribe(self, topic: str, callback: Callable):
        """Unsubscribe from a topic"""
        with self._lock:
            if topic in self.subscribers and callback in self.subscribers[topic]:
                self.subscribers[topic].remove(callback)
                log.debug(f"Unsubscribed from topic: {topic}")
    
    def broadcast_to_type(self, agent_type: str, topic: str, payload: Dict[str, Any], sender_id: str):
        """
        Broadcast message to all agents of a specific type
        
        Args:
            agent_type: Target agent type
            topic: Message topic
            payload: Message data
            sender_id: Sender's agent ID
        """
        target_agents = self.registry.get_agents_by_type(agent_type)
        payload["broadcast_to_type"] = agent_type
        payload["target_count"] = len(target_agents)
        
        self.publish(
            topic=f"broadcast_{agent_type}",
            payload=payload,
            sender_id=sender_id,
            sender_type="system",
            priority="normal"
        )
        log.debug(f"Broadcast to {len(target_agents)} {agent_type} agents")
    
    def request_agent_spawn(
        self,
        agent_type: str,
        requester_id: str,
        task_data: Dict[str, Any],
        priority: str = "normal"
    ) -> str:
        """
        Request spawning of a new agent
        
        Args:
            agent_type: Type of agent to spawn
            requester_id: ID of requesting agent
            task_data: Initial task data for new agent
            priority: Request priority
        
        Returns:
            Request ID (not agent ID - that comes later)
        """
        request_id = str(uuid.uuid4())
        
        self.publish(
            topic="spawn_agent_request",
            payload={
                "request_id": request_id,
                "agent_type": agent_type,
                "task_data": task_data,
                "requester_id": requester_id
            },
            sender_id=requester_id,
            sender_type="agent",
            priority=priority
        )
        
        log.info(f"Agent spawn requested: {agent_type} (request: {request_id})")
        return request_id
    
    def register_agent(self, agent_id: str, agent_type: str, metadata: Optional[Dict] = None):
        """
        Register a new agent with the bus
        
        Args:
            agent_id: Unique agent ID
            agent_type: Agent type
            metadata: Optional agent metadata
        """
        self.registry.register(agent_id, agent_type, metadata)
        
        # Publish registration event
        self.publish(
            topic="agent_registered",
            payload={"agent_id": agent_id, "agent_type": agent_type},
            sender_id=agent_id,
            sender_type=agent_type,
            priority="low"
        )
    
    def unregister_agent(self, agent_id: str):
        """Unregister an agent"""
        agent_info = self.registry.get_agent(agent_id)
        if agent_info:
            self.publish(
                topic="agent_unregistered",
                payload={"agent_id": agent_id},
                sender_id=agent_id,
                sender_type=agent_info["agent_type"],
                priority="low"
            )
            self.registry.unregister(agent_id)
    
    def get_agent_status(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get current status of an agent"""
        return self.registry.get_agent(agent_id)
    
    def get_all_agents(self) -> List[Dict[str, Any]]:
        """Get all registered agents"""
        return self.registry.get_all_agents()
    
    def get_message_history(self, topic_filter: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """
        Get message history
        
        Args:
            topic_filter: Optional topic to filter by
            limit: Maximum number of messages to return
        
        Returns:
            List of message dictionaries
        """
        messages = self._message_history[-limit:]
        
        if topic_filter:
            messages = [m for m in messages if m.topic == topic_filter]
        
        return [m.to_dict() for m in messages]
    
    def _process_messages(self):
        """Worker thread that processes messages"""
        log.info("Message processing thread started")
        
        while self._running:
            try:
                # Get message with timeout
                try:
                    _, _, message = self.message_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                
                # Find matching subscribers
                self._dispatch_message(message)
                
                self.message_queue.task_done()
                
            except Exception as e:
                log.error(f"Error processing message: {e}", exc_info=True)
        
        log.info("Message processing thread stopped")
    
    def _dispatch_message(self, message: AgentMessage):
        """Dispatch message to subscribers"""
        with self._lock:
            # Exact topic match
            if message.topic in self.subscribers:
                for callback in self.subscribers[message.topic]:
                    try:
                        callback(message)
                    except Exception as e:
                        log.error(f"Error in subscriber callback: {e}", exc_info=True)
            
            # Wildcard match (e.g., 'clip_*')
            for topic_pattern, callbacks in self.subscribers.items():
                if '*' in topic_pattern:
                    if self._topic_matches_pattern(message.topic, topic_pattern):
                        for callback in callbacks:
                            try:
                                callback(message)
                            except Exception as e:
                                log.error(f"Error in wildcard subscriber: {e}", exc_info=True)
    
    def _topic_matches_pattern(self, topic: str, pattern: str) -> bool:
        """Check if topic matches pattern with wildcards"""
        if pattern == '*':
            return True
        
        pattern_parts = pattern.split('*')
        if len(pattern_parts) == 2:
            prefix, suffix = pattern_parts
            return topic.startswith(prefix) and topic.endswith(suffix)
        
        return topic == pattern
    
    def _add_to_history(self, message: AgentMessage):
        """Add message to history"""
        self._message_history.append(message)
        
        # Trim history if too large
        if len(self._message_history) > self._max_history_size:
            self._message_history = self._message_history[-self._max_history_size:]


# Global singleton instance
_bus_instance: Optional[AgentCommunicationBus] = None
_bus_lock = threading.Lock()


def get_communication_bus() -> AgentCommunicationBus:
    """Get the global communication bus instance"""
    global _bus_instance
    
    if _bus_instance is None:
        with _bus_lock:
            if _bus_instance is None:
                _bus_instance = AgentCommunicationBus()
                _bus_instance.start()
    
    return _bus_instance
