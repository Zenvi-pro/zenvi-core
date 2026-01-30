"""
Gemini chat provider powered by LangChain with tool binding support.
Supports both native tool calling (Gemini models) and structured prompts (Gemma models).
"""
import os
import json
import re

from classes.logger import log
from utility.ai.langchain_utils import to_langchain_messages


class GeminiChatProvider:
    """LangChain-backed Gemini chat provider with tool support."""

    # Models that support native function calling
    TOOL_CALLING_MODELS = {
        "gemini-pro", "gemini-2.0-flash"
    }

    def __init__(self, model: str = "gemini-2.0-flash", temperature: float = 0.2):
        self.model = model or "gemini-2.0-flash"
        self.temperature = temperature
        self._client = None
        self._supports_tools = self._check_tool_support()
        self._allows_system = self._check_system_support()

    def _check_tool_support(self) -> bool:
        """Check if the selected model supports native tool calling."""
        # Check if model name contains any known tool-calling model
        model_lower = self.model.lower()
        return any(supported in model_lower for supported in self.TOOL_CALLING_MODELS)

    def _check_system_support(self) -> bool:
        """Gemma 3 models reject system/developer messages."""
        return "gemma-3" not in self.model.lower()

    def _lazy_client(self):
        """Initialize client and conditionally bind tools."""
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
            from utility.ai.tools import get_export_tools
        except ImportError as exc:
            log.error(f"Failed to import LangChain dependencies: {exc}")
            raise RuntimeError(
                f"Missing LangChain dependencies: {exc}. Install with 'pip install langchain langchain-google-genai python-dotenv'."
            ) from exc

        api_key = self._resolve_api_key()
        if not api_key:
            raise RuntimeError("Add GEMINI_API_KEY to your .env to use Gemini chat.")

        # Initialize client
        self._client = ChatGoogleGenerativeAI(
            model=self.model,
            google_api_key=api_key,
            temperature=self.temperature,
        )
        
        # Only bind tools if model supports it
        if self._supports_tools:
            tools = get_export_tools()
            self._client = self._client.bind_tools(tools)
            log.debug(f"Gemini provider initialized with model '{self.model}' and {len(tools)} tools (native calling)")
        else:
            log.debug(f"Gemini provider initialized with model '{self.model}' (structured prompt mode)")

    def _resolve_api_key(self) -> str:
        """Load API key from .env or environment variables."""
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            pass
        
        return os.getenv("GEMINI_API_KEY") or ""

    def _build_tool_prompt(self) -> str:
        """Build structured prompt describing available tools for non-tool-calling models."""
        from utility.ai.tools import get_export_tools
        
        tools = get_export_tools()
        tool_descriptions = []
        
        for tool in tools:
            # Extract tool information
            name = tool.name
            description = tool.description
            
            # Get argument schema from the tool
            args_schema = []
            if hasattr(tool, 'args_schema') and tool.args_schema:
                schema = tool.args_schema
                # Support Pydantic v2 (model_fields), v1 (__fields__), or plain dict schemas
                if hasattr(schema, "model_fields"):
                    fields_iter = schema.model_fields.items()
                else:
                    fields_iter = []

                for field_name, field_info in fields_iter:
                    annotation = getattr(field_info, "annotation", None) or getattr(field_info, "outer_type_", None) or type(field_info)
                    field_type = annotation.__name__ if hasattr(annotation, "__name__") else str(annotation)
                    if hasattr(field_info, "is_required"):
                        required = field_info.is_required()
                    else:
                        required = getattr(field_info, "required", False)
                    args_schema.append(f"  - {field_name} ({field_type}){' [required]' if required else ' [optional]'}")
            
            tool_desc = f"""
Tool: {name}
Description: {description}
Arguments:
{chr(10).join(args_schema) if args_schema else '  (no arguments)'}
"""
            tool_descriptions.append(tool_desc.strip())
        
        return f"""
You have access to the following tools to help users with video editing tasks:

{chr(10).join(tool_descriptions)}

To use a tool, respond with a JSON object in this exact format:
{{"tool": "tool_name", "args": {{"arg1": "value1", "arg2": "value2"}}}}

If you don't need to use a tool, respond normally with text.
Only use tools when the user explicitly requests an action that requires them.
"""

    def _inject_tool_prompt(self, history: list, allow_system: bool = True) -> list:
        """Inject tool instructions into the conversation for non-tool-calling models."""
        from langchain_core.messages import HumanMessage, SystemMessage
        
        tool_prompt = self._build_tool_prompt()
        
        if allow_system:
            # Check if there's already a system message
            if history and isinstance(history[0], SystemMessage):
                # Append to existing system message
                history[0].content = f"{history[0].content}\n\n{tool_prompt}"
            else:
                # Prepend new system message
                history.insert(0, SystemMessage(content=tool_prompt))
        else:
            # Prepend as human text for models that cannot accept system instructions
            history.insert(0, HumanMessage(content=tool_prompt))
        
        return history

    def _normalize_history(self, history: list) -> list:
        """Downgrade system messages when the model does not accept them."""
        from langchain_core.messages import HumanMessage, SystemMessage

        if self._allows_system:
            return history

        system_messages = [m for m in history if isinstance(m, SystemMessage)]
        if not system_messages:
            return history

        # Combine system instructions and prepend/merge into the first human message
        system_text = "\n\n".join(m.content for m in system_messages)
        history = [m for m in history if not isinstance(m, SystemMessage)]

        for msg in history:
            if isinstance(msg, HumanMessage):
                msg.content = f"{system_text}\n\n{msg.content}"
                break
        else:
            history.insert(0, HumanMessage(content=system_text))

        return history

    def _parse_tool_call(self, response_text: str) -> tuple:
        """
        Parse structured tool call from response text.
        Returns (tool_name, args_dict) or (None, None) if no tool call found.

        This implementation is more robust than the single-line regex: it finds the
        "tool" token in the text and extracts the surrounding JSON object by brace
        matching, allowing for multiline/pretty-printed responses.
        """
        # Quick check for presence of the token
        if '"tool"' not in response_text:
            log.debug("No tool token found in response text while parsing tool call")
            return None, None

        try:
            # Find first occurrence of the "tool" token
            idx = response_text.find('"tool"')

            # Find the opening brace before the token
            start = response_text.rfind('{', 0, idx)
            if start == -1:
                log.debug("Could not find opening brace for tool JSON")
                return None, None

            # Now find the matching closing brace using a simple stack
            depth = 0
            end = -1
            for i in range(start, len(response_text)):
                ch = response_text[i]
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i
                        break

            if end == -1:
                log.debug("Could not find matching closing brace for tool JSON")
                return None, None

            json_text = response_text[start : end + 1]
            tool_call = json.loads(json_text)
            tool_name = tool_call.get("tool")
            args = tool_call.get("args", {})

            log.info(f"Parsed tool call: {tool_name} with args {args}")
            return tool_name, args

        except Exception as exc:
            log.warning(f"Failed to parse tool call from response: {exc}")
            log.debug(f"Response text was: {response_text}")
            return None, None

    def _execute_tool(self, tool_name: str, args: dict) -> str:
        """Execute a tool by name with given arguments."""
        from utility.ai.tools import get_export_tools
        
        tools = get_export_tools()
        tool_map = {tool.name: tool for tool in tools}
        
        if tool_name not in tool_map:
            log.warning(f"Requested unknown tool: {tool_name}")
            return f"Error: Unknown tool '{tool_name}'"
        
        try:
            tool = tool_map[tool_name]
            log.info(f"Invoking tool '{tool_name}' with args: {args}")
            result = tool.invoke(args)
            log.info(f"Tool '{tool_name}' returned: {result}")
            return str(result)
        except Exception as exc:
            log.error(f"Tool execution error: {exc}")
            return f"Error executing tool: {exc}"

    def generate(self, session: "ChatSession") -> str:
        """Generate response using Gemini/Gemma with appropriate tool handling."""
        if not self._client:
            self._lazy_client()

        history = to_langchain_messages(session)
        
        # For non-tool-calling models, inject tool instructions
        if not self._supports_tools:
            history = self._inject_tool_prompt(history, allow_system=self._allows_system)

        # Models like Gemma 3 reject system messages; convert them to user text
        history = self._normalize_history(history)
        
        # Invoke the model
        try:
            response = self._client.invoke(history)
        except Exception as exc:
            try:
                from google.api_core import exceptions as gexc
                if isinstance(exc, gexc.ResourceExhausted):
                    log.warning(f"Gemini request throttled: {exc}")
                    return "Gemini rate limit hit. Please wait a few seconds and try again."
            except Exception:
                pass
            raise

        # Extract response content
        response_text = response.content if hasattr(response, "content") else str(response)
        # Log the model response for diagnostics (truncated)
        try:
            snippet = response_text if len(response_text) < 800 else response_text[:800] + '...'
        except Exception:
            snippet = '<unprintable response>'
        log.info(f"Gemini response snippet: {snippet}")
        
        # For non-tool-calling models, check for structured tool calls
        if not self._supports_tools:
            tool_name, args = self._parse_tool_call(response_text)
            if tool_name:
                log.info(f"Detected tool call: {tool_name} with args {args}")
                tool_result = self._execute_tool(tool_name, args)
                # Return both the tool call acknowledgment and result
                return f"{response_text}\n\n{tool_result}"
        
        return response_text