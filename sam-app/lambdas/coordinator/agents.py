"""
agents.py — LangGraph Supervisor + Dynamic Specialist Agents
=============================================================
Architecture:
  • AgentRegistry  — in-memory store of agent metadata (name, prompt, etc.)
  • create_agent() — compiles a LangGraph StateGraph for a given agent
  • run_agent()    — executes a compiled graph, returns (reply_text, usage)

LangGraph pattern used: "Single-agent with system prompt injection"
  For the PoC we use a simple ReAct-style chain (no tools needed for pure chat).
  The "supervisor" pattern is expressed as: coordinator Lambda decides WHICH
  agent to call; each agent is an independent LangGraph graph compiled with
  its own injected system prompt.

Extending: add tools (web_search, calculator, code_executor) by appending
them to the `tools` list in _build_graph() below.
"""

import logging
import os
import time
from typing import Any, Optional

# ---------------------------------------------------------------------------
# LangGraph + LangChain imports
# ---------------------------------------------------------------------------
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    BaseMessage,
)
from langchain_core.language_models import BaseChatModel
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict, Annotated

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM factory — returns the configured chat model
# ---------------------------------------------------------------------------

def _get_llm() -> BaseChatModel:
    """
    Return a LangChain chat model based on LLM_PROVIDER env var.
    Supported: openai | anthropic
    """
    provider = os.environ.get("LLM_PROVIDER", "openai").lower()
    model_name = os.environ.get("LLM_MODEL", "gpt-4o")

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model_name,
            temperature=0.7,
            max_tokens=1024,
            api_key=os.environ["OPENAI_API_KEY"],
            # Streaming off for Lambda (simpler response handling)
            streaming=False,
        )
    elif provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=model_name,
            temperature=0.7,
            max_tokens=1024,
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        )
    else:
        raise ValueError(f"Unsupported LLM_PROVIDER: {provider!r}. Use 'openai' or 'anthropic'.")


# ---------------------------------------------------------------------------
# LangGraph State schema
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    """
    Typed state passed through the LangGraph graph nodes.
    `messages` uses the built-in add_messages reducer which appends
    new messages to the list rather than replacing it.
    """
    messages: Annotated[list[BaseMessage], add_messages]
    system_prompt: str    # injected per-agent persona
    usage: dict           # token usage collected from LLM response


# ---------------------------------------------------------------------------
# Graph node: call the LLM
# ---------------------------------------------------------------------------

def _make_llm_node(llm: BaseChatModel):
    """
    Returns a LangGraph node function that:
      1. Prepends the system prompt to the message list
      2. Calls the LLM
      3. Appends the AI reply to state
    """
    def llm_node(state: AgentState) -> dict:
        system_msg = SystemMessage(content=state["system_prompt"])
        messages_with_system = [system_msg] + list(state["messages"])

        logger.debug("LLM call: %d messages, last=%r",
                     len(messages_with_system),
                     messages_with_system[-1].content[:60])

        ai_msg: AIMessage = llm.invoke(messages_with_system)

        # Extract token usage (OpenAI & Anthropic both populate response_metadata)
        usage = {}
        if hasattr(ai_msg, "response_metadata"):
            meta = ai_msg.response_metadata or {}
            # OpenAI
            if "token_usage" in meta:
                usage = {
                    "prompt_tokens": meta["token_usage"].get("prompt_tokens", 0),
                    "completion_tokens": meta["token_usage"].get("completion_tokens", 0),
                    "total_tokens": meta["token_usage"].get("total_tokens", 0),
                }
            # Anthropic
            elif "usage" in meta:
                usage = {
                    "prompt_tokens": meta["usage"].get("input_tokens", 0),
                    "completion_tokens": meta["usage"].get("output_tokens", 0),
                    "total_tokens": (
                        meta["usage"].get("input_tokens", 0) +
                        meta["usage"].get("output_tokens", 0)
                    ),
                }

        return {
            "messages": [ai_msg],   # add_messages reducer appends this
            "usage": usage,
        }

    return llm_node


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

# Cache compiled graphs by agent_id to avoid re-compilation on warm invocations
_COMPILED_GRAPHS: dict[str, Any] = {}


def _build_graph(agent_meta: dict) -> Any:
    """
    Build and compile a LangGraph StateGraph for the given agent.
    Graph topology: START → llm_node → END  (simple single-node chain)

    Extend by adding tool nodes:
        graph.add_node("tools", ToolNode(tools))
        graph.add_conditional_edges("llm", should_use_tools, {...})
    """
    llm = _get_llm()
    llm_node = _make_llm_node(llm)

    graph = StateGraph(AgentState)
    graph.add_node("llm", llm_node)
    graph.set_entry_point("llm")
    graph.add_edge("llm", END)

    compiled = graph.compile()
    logger.info("Graph compiled for agent_id=%s name=%s",
                agent_meta["agent_id"], agent_meta["name"])
    return compiled


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_agent(agent_meta: dict) -> None:
    """
    Eagerly compile and cache the LangGraph for agent_meta.
    Called by the /create-agent route for immediate warm-start.
    """
    agent_id = agent_meta["agent_id"]
    if agent_id not in _COMPILED_GRAPHS:
        _COMPILED_GRAPHS[agent_id] = _build_graph(agent_meta)


def run_agent(
    agent_meta: dict,
    history: list[dict],
) -> tuple[str, dict]:
    """
    Execute the agent's LangGraph graph.

    Parameters
    ----------
    agent_meta : dict   — from AgentRegistry.get()
    history    : list   — [{"role": "user"|"assistant", "content": str}, ...]

    Returns
    -------
    (reply_text, usage_dict)
    """
    agent_id = agent_meta["agent_id"]

    # Lazy-compile if somehow missed (e.g. cold-start after registry rebuild)
    if agent_id not in _COMPILED_GRAPHS:
        _COMPILED_GRAPHS[agent_id] = _build_graph(agent_meta)

    graph = _COMPILED_GRAPHS[agent_id]

    # Convert history dicts → LangChain message objects
    lc_messages: list[BaseMessage] = []
    for msg in history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "user":
            lc_messages.append(HumanMessage(content=content))
        elif role == "assistant":
            lc_messages.append(AIMessage(content=content))
        # Ignore system messages in history — we inject via state

    initial_state: AgentState = {
        "messages": lc_messages,
        "system_prompt": agent_meta["system_prompt"],
        "usage": {},
    }

    t0 = time.perf_counter()
    result = graph.invoke(initial_state)
    elapsed = time.perf_counter() - t0

    # The last message in the final state is the AI reply
    final_messages = result.get("messages", [])
    if not final_messages:
        raise RuntimeError("LangGraph returned no messages")

    last_msg = final_messages[-1]
    reply_text = last_msg.content if isinstance(last_msg.content, str) else str(last_msg.content)
    usage = result.get("usage", {})

    logger.info("Agent %s replied in %.2fs (%d chars) tokens=%s",
                agent_id, elapsed, len(reply_text), usage)

    return reply_text, usage


# ---------------------------------------------------------------------------
# AgentRegistry — in-memory store
# ---------------------------------------------------------------------------

class AgentRegistry:
    """
    Thread-safe (for Lambda single-threaded model) registry of agent metadata.

    Schema per entry:
        {
          "agent_id":     str,
          "name":         str,
          "system_prompt": str,
          "voice_id":     str,   # ElevenLabs
          "avatar_id":    str,   # D-ID presenter / custom avatar
          "created_at":   float, # Unix timestamp
        }

    Production upgrade: replace _store with DynamoDB calls in register()/get().
    """

    def __init__(self):
        self._store: dict[str, dict] = {}

    def register(
        self,
        agent_id: str,
        name: str,
        system_prompt: str,
        voice_id: str,
        avatar_id: str,
        created_at: float,
    ) -> None:
        self._store[agent_id] = {
            "agent_id": agent_id,
            "name": name,
            "system_prompt": system_prompt,
            "voice_id": voice_id,
            "avatar_id": avatar_id,
            "created_at": created_at,
        }

    def get(self, agent_id: str) -> Optional[dict]:
        return self._store.get(agent_id)

    def list_all(self) -> list[dict]:
        return list(self._store.values())

    def __len__(self) -> int:
        return len(self._store)
