import pytest
from pydantic import ValidationError
from shared.protocol_a2a import JsonRpcRequest, JsonRpcResponse, AgentCard

def test_json_rpc_request_validation():
    # Valid Request
    req = JsonRpcRequest(
        jsonrpc="2.0",
        method="tasks/send",
        params={"taskId": "t1", "skillId": "s1", "inputs": {}},
        id=123
    )
    assert req.jsonrpc == "2.0"
    assert req.method == "tasks/send"
    assert req.id == 123

    # Invalid JSON-RPC Version
    with pytest.raises(ValidationError):
        JsonRpcRequest(
            jsonrpc="1.0",
            method="tasks/send",
            params={}
        )

def test_json_rpc_response_validation():
    # Valid Response
    res = JsonRpcResponse(
        jsonrpc="2.0",
        result={"status": "completed"},
        id=123
    )
    assert res.jsonrpc == "2.0"
    assert res.result == {"status": "completed"}
    assert res.error is None

def test_agent_card_validation():
    # Valid Card
    card = AgentCard(
        name="TestAgent",
        description="A test agent",
        version="1.0.0",
        url="http://test-url/agent",
        skills=[
            {
                "id": "test-skill",
                "name": "Test Skill",
                "description": "Skill description",
                "tags": ["test"],
                "examples": []
            }
        ]
    )
    assert card.name == "TestAgent"
    assert len(card.skills) == 1
    assert card.skills[0].id == "test-skill"
