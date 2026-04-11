import pytest
from src.backend.workflows.launch_contracts import (
    evaluate_launch_contract,
    LaunchContractResult,
    PreconditionResult,
)

def test_launch_contract_happy_path():
    contract = {
        "schema_version": "launch_contract.v1",
        "preconditions": [
            {"type": "context_key_present", "key": "arxiv_id", "required": True},
            {"type": "namespace_write_access", "required": False}
        ]
    }
    context = {
        "arxiv_id": "1234.5678",
        "policy_state": {"namespace": "test"}
    }
    
    result = evaluate_launch_contract(contract, context)
    assert result.launchable is True
    assert len(result.precondition_results) == 2
    assert all(r.satisfied for r in result.precondition_results)

def test_launch_contract_missing_precondition():
    contract = {
        "schema_version": "launch_contract.v1",
        "preconditions": [
            {"type": "context_key_present", "key": "arxiv_id", "required": True},
        ]
    }
    context = {}
    
    result = evaluate_launch_contract(contract, context)
    assert result.launchable is False
    assert len(result.precondition_results) == 1
    
    r = result.precondition_results[0]
    assert r.type == "context_key_present"
    assert r.satisfied is False
    assert r.required is True
    assert "Missing required context key" in r.reason
    assert r.context_key == "arxiv_id"

def test_launch_contract_unknown_type():
    contract = {
        "schema_version": "launch_contract.v1",
        "preconditions": [
            {"type": "magic_wand_present", "required": True},
        ]
    }
    
    result = evaluate_launch_contract(contract, {})
    assert result.launchable is False
    assert len(result.precondition_results) == 1
    
    r = result.precondition_results[0]
    assert r.type == "magic_wand_present"
    assert r.satisfied is False
    assert "Unknown precondition type" in r.reason

def test_launch_contract_exception_isolation(monkeypatch):
    import src.backend.workflows.launch_contracts as lc
    
    def buggy_evaluator(prec, context):
        raise ValueError("Simulated evaluator crash")
        
    monkeypatch.setitem(lc.PRECONDITION_EVALUATORS, "buggy_type", buggy_evaluator)
    
    contract = {
        "schema_version": "launch_contract.v1",
        "preconditions": [
            {"type": "buggy_type", "required": True},
            {"type": "context_key_present", "key": "x", "required": True}
        ]
    }
    context = {"x": "1"}
    
    result = evaluate_launch_contract(contract, context)
    assert result.launchable is False
    assert len(result.precondition_results) == 2
    
    buggy_res = next(r for r in result.precondition_results if r.type == "buggy_type")
    assert buggy_res.satisfied is False
    assert "Evaluator raised exception" in buggy_res.reason
    
    valid_res = next(r for r in result.precondition_results if r.type == "context_key_present")
    assert valid_res.satisfied is True
