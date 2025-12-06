"""Diagnostic script to inspect the tool listing shown to the LLM."""
import os
import sys
sys.path.insert(0, os.path.abspath('.'))

from src.backend.integrations.internal_mcp.orchestrator import InternalMCPChatOrchestrator
from src.backend.integrations.internal_mcp.gateway import Gateway

def main():
    # Create orchestrator
    gateway = Gateway()
    orchestrator = InternalMCPChatOrchestrator(gateway)
    
    # Get tool listing with namespace (authenticated state)
    print("\n" + "="*80)
    print("TOOL LISTING WITH NAMESPACE (AUTHENTICATED)")
    print("="*80)
    listing = orchestrator._tool_listing()
    print(listing)
    
    # Get full instruction message with namespace
    print("\n" + "="*80)
    print("FULL INSTRUCTION MESSAGE (AUTHENTICATED)")
    print("="*80)
    instruction = orchestrator._instruction_message(user_namespace="#V#michael_witbrock")
    print(instruction)
    
    # Check if RAG tools are in the listing
    print("\n" + "="*80)
    print("RAG TOOL CHECK")
    print("="*80)
    rag_tools = ["rag_list_indexed", "rag_get_item", "search_knowledge_base", "rag_status"]
    for tool in rag_tools:
        if tool in listing:
            print(f"✅ {tool} - FOUND in tool listing")
        else:
            print(f"❌ {tool} - MISSING from tool listing")

if __name__ == "__main__":
    main()
