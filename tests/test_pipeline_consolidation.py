"""Test that agent paths delegate to canonical pipeline_query (Simplification Step 4)."""
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _legacy_retrieval_path(monkeypatch):
    """Pin LLAMA_INDEX_DATA_PLANE to False for legacy HybridRetriever path tests."""
    from maia.config import settings
    monkeypatch.setattr(settings, "LLAMA_INDEX_DATA_PLANE", False)


def test_enterprise_agent_ensure_stack_uses_build_stack():
    """Verify EnterpriseAgent._ensure_stack delegates to pipeline_query.build_stack()"""
    from maia.agent.agent import EnterpriseAgent
    
    agent = EnterpriseAgent(tenant_id="test")
    
    # Mock pipeline_query.build_stack to track calls (imported locally in _ensure_stack)
    with patch('maia.pipeline_query.build_stack') as mock_build_stack:
        mock_embedder = MagicMock()
        mock_store = MagicMock()
        mock_retriever = MagicMock()
        mock_reranker = MagicMock()
        mock_llm = MagicMock()
        mock_build_stack.return_value = (mock_embedder, mock_store, mock_retriever, mock_reranker, mock_llm)
        
        # Call _ensure_stack which should delegate to build_stack
        agent._ensure_stack()
        
        # Verify build_stack was called
        mock_build_stack.assert_called_once()


def test_langgraph_retrieve_node_uses_build_stack():
    """Verify langgraph retrieve node uses pipeline_query.build_stack() via _build_stack"""
    from maia.agent.langgraph_agent import AgentState, node_retrieve
    
    # Create a test state
    state = AgentState(
        question="Test question",
        rewritten_query="Test question",
        tenant_id="test",
        session_id="test-session",
    )
    
    # Mock _build_stack
    with patch('maia.agent.langgraph_agent._build_stack') as mock_build_stack:
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = [
            {"chunk_id": "1", "text": "Test", "score": 0.9, "metadata": {}}
        ]
        mock_build_stack.return_value = (None, None, mock_retriever, None, None)
        
        result = node_retrieve(state)
        
        # Verify _build_stack was called
        mock_build_stack.assert_called_once_with("test")
        # Verify retriever.retrieve was called with correct args
        mock_retriever.retrieve.assert_called_once_with(
            "Test question",
            tenant_id="test",
            session_id="test-session"
        )
        # Verify state updated with candidates
        assert "candidates" in result.model_dump()
        assert len(result.candidates) == 1


def test_langgraph_rerank_node_uses_build_stack():
    """Verify langgraph rerank node uses pipeline_query.build_stack() via _build_stack"""
    from maia.agent.langgraph_agent import AgentState, node_rerank
    
    state = AgentState(
        question="Test question",
        tenant_id="test",
        candidates=[
            {"chunk_id": "1", "text": "Test", "score": 0.9, "metadata": {}}
        ],
    )
    
    with patch('maia.agent.langgraph_agent._build_stack') as mock_build_stack:
        mock_reranker = MagicMock()
        mock_reranker.rerank.return_value = [
            {"chunk_id": "1", "text": "Test", "score": 0.9, "metadata": {}, "rerank_score": 0.95}
        ]
        mock_reranker.mode = "fallback"
        mock_build_stack.return_value = (None, None, None, mock_reranker, None)
        
        result = node_rerank(state)
        
        mock_build_stack.assert_called_once_with("test")
        mock_reranker.rerank.assert_called_once_with(
            "Test question",
            [{"chunk_id": "1", "text": "Test", "score": 0.9, "metadata": {}}],
            top_k=3  # settings.TOP_K_FINAL default
        )
        # Verify state updated with used_chunks (not reranked)
        assert "used_chunks" in result.model_dump()
        assert len(result.used_chunks) == 1


def test_langgraph_generate_node_uses_build_stack():
    """Verify langgraph generate node uses pipeline_query.build_stack() via _build_stack"""
    from maia.agent.langgraph_agent import AgentState, node_generate
    
    state = AgentState(
        question="Test question",
        tenant_id="test",
        context="Test context",
        used_chunks=[
            {"chunk_id": "1", "text": "Test content", "metadata": {"filename": "test.md"}}
        ],
    )
    
    with patch('maia.agent.langgraph_agent._build_stack') as mock_build_stack:
        mock_llm = MagicMock()
        mock_llm.chat.return_value = "Test answer"
        mock_llm.mode = "mock"
        mock_build_stack.return_value = (None, None, None, None, mock_llm)
        
        # Also mock session_store.history_text
        with patch('maia.agent.langgraph_agent.session_store') as mock_session_store:
            mock_session_store.history_text.return_value = ""
            
            result = node_generate(state)
        
        mock_build_stack.assert_called_once_with("test")
        mock_llm.chat.assert_called_once()
        # Verify state updated with answer
        assert result.answer == "Test answer"
