from pydantic import BaseModel, Field, field_validator
from typing import List, Optional, Dict, Any, Union

# --- JSON-RPC 2.0 Spec Envelopes ---

class JsonRpcRequest(BaseModel):
    jsonrpc: str = Field("2.0", description="JSON-RPC version, must be '2.0'")
    method: str = Field(..., description="The name of the method to invoke")
    params: Dict[str, Any] = Field(default_factory=dict, description="A structured dictionary of arguments")
    id: Optional[Union[str, int]] = Field(None, description="Request identifier. If null/omitted, it is a notification")

    @field_validator("jsonrpc")
    @classmethod
    def validate_jsonrpc(cls, v: str) -> str:
        if v != "2.0":
            raise ValueError("jsonrpc version must be exactly '2.0'")
        return v

class JsonRpcErrorDetails(BaseModel):
    code: int = Field(..., description="Error code indicating the error type")
    message: str = Field(..., description="Short summary of the error")
    data: Optional[Any] = Field(None, description="Optional detailed error context/stack trace")

class JsonRpcResponse(BaseModel):
    jsonrpc: str = Field("2.0")
    result: Optional[Any] = Field(None, description="Result payload if method call was successful")
    error: Optional[JsonRpcErrorDetails] = Field(None, description="Error details if method call failed")
    id: Optional[Union[str, int]] = Field(None)

    @field_validator("jsonrpc")
    @classmethod
    def validate_jsonrpc(cls, v: str) -> str:
        if v != "2.0":
            raise ValueError("jsonrpc version must be exactly '2.0'")
        return v


# --- A2A agent.json Card Discovery Structures ---

class AgentCapabilities(BaseModel):
    streaming: bool = Field(default=False, description="Supports real-time Server-Sent Events (SSE) updates")
    pushNotifications: bool = Field(default=False, description="Supports outgoing event notifications")
    stateTransitionHistory: bool = Field(default=True, description="Logs historical task state transitions")

class AgentSkill(BaseModel):
    id: str = Field(..., description="Unique skill ID (slug format)")
    name: str = Field(..., description="Human-readable skill name")
    description: str = Field(..., description="Description of what the skill accomplishes")
    tags: List[str] = Field(default_factory=list, description="Categorization tags")
    examples: List[str] = Field(default_factory=list, description="Query examples representing typical invocations")

class AgentCard(BaseModel):
    name: str = Field(..., description="The name of the agent service")
    description: str = Field(..., description="Summary of the agent's capabilities")
    version: str = Field(..., description="Service semantic version (e.g. 1.0.0)")
    protocolVersion: str = Field("0.3.0", description="Supported A2A protocol version")
    url: str = Field(..., description="The main service endpoint processing JSON-RPC calls")
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    defaultInputModes: List[str] = Field(default_factory=lambda: ["application/json"])
    defaultOutputModes: List[str] = Field(default_factory=lambda: ["application/json"])
    skills: List[AgentSkill] = Field(default_factory=list)


# --- A2A Task payloads ---

class TaskSendParams(BaseModel):
    taskId: str = Field(..., description="Unique identifier for the task execution tracking")
    skillId: str = Field(..., description="The ID of the skill to execute")
    inputs: Dict[str, Any] = Field(..., description="Arbitrary parameters for the skill execution")
    callbackUrl: Optional[str] = Field(None, description="Optional URL to POST the result upon completion")

class TaskSendResult(BaseModel):
    taskId: str
    status: str = Field(..., description="Status (e.g. working, submitted, completed)")
    estimatedDurationSeconds: Optional[int] = Field(None, description="Estimated work duration")
