"""LangChain integration package for MAIA.

Holds the LangChain *abstraction layer* (LLM adapter, prompts, tools) that sits
on top of MAIA's existing CloudflareLLM + tool registry.  This is NOT a rewrite
of the RAG data plane — it is the glue that lets LangGraph nodes speak
LangChain's Runnable / Tool / Prompt contracts while reusing every existing
guardrail, tenant-isolation check, and mock fallback.

Layout:
    llm.py          CloudflareLangChainAdapter (wraps CloudflareLLM)
    prompts.py      ChatPromptTemplate wrappers of the existing SYSTEM_PROMPT
    tools.py        LangChain BaseTool wrappers over TOOL_REGISTRY
"""
