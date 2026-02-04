"""
Shared LangChain helpers for chat providers.
"""
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from classes.ai_chat_functionality import ChatSession


def to_langchain_messages(session: "ChatSession"):
    """Convert ChatSession history into LangChain message objects."""
    from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
    from classes.ai_chat_functionality import MessageRole

    role_map = {
        MessageRole.USER: HumanMessage,
        MessageRole.ASSISTANT: AIMessage,
        MessageRole.SYSTEM: SystemMessage,
    }

    history: List = []
    for msg in session.messages:
        message_cls = role_map.get(msg.role)
        if not message_cls:
            continue
        history.append(message_cls(content=msg.content))
    return history
