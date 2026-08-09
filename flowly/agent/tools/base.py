"""Base class for agent tools."""

from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    """
    Abstract base class for agent tools.
    
    Tools are capabilities that the agent can use to interact with
    the environment, such as reading files, executing commands, etc.
    """
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name used in function calls."""
        pass
    
    @property
    @abstractmethod
    def description(self) -> str:
        """Description of what the tool does."""
        pass
    
    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """JSON Schema for tool parameters."""
        pass
    
    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """
        Execute the tool with given parameters.
        
        Args:
            **kwargs: Tool-specific parameters.
        
        Returns:
            String result of the tool execution.
        """
        pass

    @property
    def toolset(self) -> str | None:
        """Optional routing group override.

        Built-in tools are centrally classified by name. Plugins may override
        this property to participate in a named platform toolset.
        """
        return None

    @property
    def supported_platforms(self) -> frozenset[str] | None:
        """Platforms that may advertise this tool, or ``None`` for all."""
        return None

    @property
    def discovery_source(self) -> str | None:
        """Connected provider/plugin label used by progressive discovery."""
        return None

    def is_available(self) -> bool:
        """Cheap runtime capability check used before schema advertisement."""
        return True
    
    def to_schema(self) -> dict[str, Any]:
        """Convert tool to OpenAI function schema format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        }
