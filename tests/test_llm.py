"""The Ollama client against a fake Ollama: readiness messages, routing with
tool calls in both argument shapes, and streaming with exact stats."""

import json

import httpx
import pytest

from dynasty_agent import llm
from dynasty_agent.llm import LLMError, OllamaClient


def client(handler, model="qwen3:4b-instruct"):
    return OllamaClient(model=model, url="http://ollama.test", transport=httpx.MockTransport(handler))


def test_ready_when_the_model_is_installed():
    c = client(lambda r: httpx.Response(200, json={"models": [{"name": "qwen3:4b-instruct"}, {"name": "gemma4:e2b"}]}))
    c.ensure_ready()


def test_not_installed_says_what_to_pull():
    c = client(lambda r: httpx.Response(200, json={"models": [{"name": "gemma4:e2b"}]}))
    with pytest.raises(LLMError, match=r"Run `ollama pull qwen3:4b-instruct`"):
        c.ensure_ready()


def test_a_bare_model_name_matches_its_latest_tag():
    client(lambda r: httpx.Response(200, json={"models": [{"name": "mymodel:latest"}]}), model="mymodel").ensure_ready()


def test_not_running_says_to_open_ollama():
    def refuse(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError, match="Ollama isn't running"):
        client(refuse).ensure_ready()
    with pytest.raises(LLMError, match="Ollama isn't running"):
        client(refuse).route([], [])


def test_route_returns_tool_calls_with_dict_arguments_either_way():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "message": {"content": "", "tool_calls": [
                {"function": {"name": "evaluate_trade", "arguments": {"send_players": ["Jonah Coleman"]}}},
                {"function": {"name": "set_lineup", "arguments": '{"note": "string-shaped"}'}},
            ]},
            "eval_count": 30, "eval_duration": 1_000_000_000, "prompt_eval_count": 500, "prompt_eval_duration": 2_000_000_000,
        })

    calls, text, stats = client(handler).route([{"role": "user", "content": "hi"}], [{"type": "function"}])
    assert calls == [
        {"name": "evaluate_trade", "arguments": {"send_players": ["Jonah Coleman"]}},
        {"name": "set_lineup", "arguments": {"note": "string-shaped"}},
    ]
    assert stats.tokens_per_second == 30 and stats.prompt_tokens_per_second == 250
    # Routing is deterministic and never streams; thinking is off.
    assert seen["options"]["temperature"] == 0 and seen["stream"] is False and seen["think"] is False
    assert seen["keep_alive"] == llm.KEEP_ALIVE


def test_route_with_no_tool_returns_the_text():
    calls, text, _ = client(lambda r: httpx.Response(200, json={"message": {"content": "Paris."}})).route([], [])
    assert calls == [] and text == "Paris."


def test_stream_yields_pieces_then_exact_stats():
    lines = [
        {"message": {"content": "Sell"}, "done": False},
        {"message": {"content": " the pick."}, "done": False},
        {"message": {"content": ""}, "done": True, "eval_count": 70, "eval_duration": 2_000_000_000,
         "load_duration": 18_000_000_000},
    ]
    body = "\n".join(json.dumps(line) for line in lines).encode()
    events = list(client(lambda r: httpx.Response(200, content=body)).stream([{"role": "user", "content": "?"}]))
    assert events[:2] == ["Sell", " the pick."]
    stats = events[-1]
    assert isinstance(stats, llm.Stats) and stats.tokens_per_second == 35.0
    assert stats.loaded_model  # 18 s of loading: shown as "loading model...", not a slow first-words time
    assert stats.first_token_s is not None


def test_a_missing_model_mid_chat_is_an_llm_error():
    with pytest.raises(LLMError, match="ollama pull"):
        list(client(lambda r: httpx.Response(404, json={"error": "model not found"})).stream([]))


def test_llm_error_is_an_agent_error_so_the_chat_keeps_going():
    from dynasty_agent.errors import AgentError

    assert issubclass(LLMError, AgentError)
