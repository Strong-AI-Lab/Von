import pytest
import json
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.backend.mcp_server.mcp_stdio_server import list_tools

@pytest.mark.asyncio
async def test_manifest_parity():
    """
    Verify that vontology_mcp.json matches the tools defined in mcp_stdio_server.py.
    This prevents manifest drift where tools are implemented but not advertised.
    """
    # 1. Get tools from code
    code_tools = await list_tools()
    code_tool_map = {t.name: t for t in code_tools}
    
    # 2. Get tools from manifest
    manifest_path = project_root / "src" / "backend" / "mcp_server" / "vontology_mcp.json"
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest_data = json.load(f)
    
    manifest_tools = manifest_data.get("tools", [])
    manifest_tool_map = {t["name"]: t for t in manifest_tools}
    
    # 3. Compare
    code_names = set(code_tool_map.keys())
    manifest_names = set(manifest_tool_map.keys())
    
    missing_in_manifest = code_names - manifest_names
    missing_in_code = manifest_names - code_names
    
    assert not missing_in_manifest, f"Tools implemented but missing from manifest: {missing_in_manifest}"
    assert not missing_in_code, f"Tools in manifest but not implemented: {missing_in_code}"
    
    # Optional: Compare schemas (basic check)
    for name in code_names:
        code_tool = code_tool_map[name]
        manifest_tool = manifest_tool_map[name]
        
        # Check description presence
        assert manifest_tool.get("description"), f"Tool {name} missing description in manifest"
        
        # Check input schema presence
        assert "inputSchema" in manifest_tool, f"Tool {name} missing inputSchema in manifest"
        
        # We could do deep comparison of schemas here, but existence is a good start.
