"""#spice OpenAI-compatible engine: transformer, config guards, and a mocked turn."""

from __future__ import annotations

import json
import types

from poiesis.agent import openai_turn
from poiesis.agent.openai_turn import run_openai_turn
from poiesis.agent.spice_tools import challenges_to_markdown, json_to_markdown, run_fetch


class FakeDB:
    async def fetch_all(self, *a, **k):
        return []

    async def fetch_one(self, *a, **k):
        return None  # no settings row → no cached challenges


async def _drain(gen):
    return [ev async for ev in gen]


# ── transformer ──────────────────────────────────────────────────────────────

def test_json_to_markdown_nested():
    md = json_to_markdown({"a": 1, "b": [{"c": True}], "d": None})
    assert "- **a**: 1" in md
    assert "- **c**: true" in md
    assert "- **d**: _null_" in md


def test_json_to_markdown_empties():
    assert "_(empty object)_" in json_to_markdown({})
    assert "_(empty list)_" in json_to_markdown([])


def test_challenges_to_markdown_exact_shape():
    assert challenges_to_markdown([]) == "_No challenges defined._"
    items = [
        {"id": "29-strip-dance", "category": "dare", "point_value": 400,
         "important": True, "min_req": "2 people",
         "description": "strip   dance\n goth  girl style"},
        {"id": "07-quiet", "category": "chill", "point_value": 50,
         "important": False, "description": "just  vibe"},
    ]
    md = challenges_to_markdown(items)
    lines = md.split("\n")
    # matches the TS: `- **id** (category: X, N pts[, min ..][, IMPORTANT]): desc`
    assert lines[0] == (
        "- **29-strip-dance** (category: dare, 400 pts, min 2 people, IMPORTANT): "
        "strip dance goth girl style"
    )
    assert lines[1] == "- **07-quiet** (category: chill, 50 pts): just vibe"


async def test_run_fetch_rejects_bad_url():
    assert "not an http" in await run_fetch('{"url": "ftp://x"}')
    assert "POIESIS_SPICE_CHALLENGES_URL" in await run_fetch("{}")  # no url, no default
    assert "could not parse" in await run_fetch("{not json")


# ── config guards ────────────────────────────────────────────────────────────

async def test_turn_errors_without_model(monkeypatch):
    # key is optional for local servers; the model guard still returns before any network
    monkeypatch.setenv("POIESIS_SPICE_API_KEY", "")
    monkeypatch.setenv("POIESIS_SPICE_MODEL", "")
    evs = await _drain(run_openai_turn(
        db=FakeDB(), channel={"id": "spice", "soul_path": None, "model": None}, history=[],
        user_message="hi", message_id="x",
    ))
    assert evs[0]["type"] == "error" and "POIESIS_SPICE_MODEL" in evs[0]["message"]


# ── full turn with a mocked provider ─────────────────────────────────────────

def _chunk(content=None, tool_calls=None, finish=None):
    delta = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = types.SimpleNamespace(delta=delta, finish_reason=finish)
    return types.SimpleNamespace(choices=[choice])


def _tc(index, id=None, name=None, args=None):
    fn = types.SimpleNamespace(name=name, arguments=args)
    return types.SimpleNamespace(index=index, id=id, function=fn)


class FakeCompletions:
    def __init__(self, rounds):
        self._rounds = rounds
        self._i = 0

    async def create(self, **kwargs):
        chunks = self._rounds[self._i]
        self._i += 1

        async def gen():
            for c in chunks:
                yield c
        return gen()


class FakeClient:
    def __init__(self, rounds):
        self.chat = types.SimpleNamespace(completions=FakeCompletions(rounds))


async def test_full_turn_with_tool_call(monkeypatch):
    monkeypatch.setenv("POIESIS_SPICE_API_KEY", "k")
    monkeypatch.setenv("POIESIS_SPICE_MODEL", "test-model")

    rounds = [
        [  # round 1: some text, then a fetch tool call streamed in pieces
            _chunk(content="Let me look. "),
            _chunk(tool_calls=[_tc(0, id="call_1", name="fetch", args='{"url":')]),
            _chunk(tool_calls=[_tc(0, args='"https://x/j"}')]),
            _chunk(finish="tool_calls"),
        ],
        [  # round 2: final answer
            _chunk(content="Here is the data."),
            _chunk(finish="stop"),
        ],
    ]
    monkeypatch.setattr(openai_turn, "AsyncOpenAI", lambda **k: FakeClient(rounds))
    monkeypatch.setitem(openai_turn._TOOL_RUNNERS, "fetch",
                        lambda args, env: _coro("FETCHED: " + args))

    evs = await _drain(run_openai_turn(
        db=FakeDB(), channel={"id": "spice", "soul_path": None}, history=[],
        user_message="grab the json", message_id="x",
    ))
    types_seq = [e["type"] for e in evs]
    assert types_seq == ["text", "tool_call", "tool_result", "text", "done"]

    tool_call = next(e for e in evs if e["type"] == "tool_call")
    assert tool_call["name"] == "fetch" and tool_call["id"] == "call_1"
    tool_res = next(e for e in evs if e["type"] == "tool_result")
    assert tool_res["id"] == "call_1" and "FETCHED" in tool_res["result"]

    done = evs[-1]
    assert done["content"] == "Let me look. Here is the data."
    assert done["session_id"] is None
    seg_types = [s["type"] for s in done["segments"]]
    assert seg_types == ["text", "tool_call", "text"]
    assert done["segments"][1]["result_summary"].startswith("FETCHED")


async def _coro(v):
    return v


# ── challenges injected into the prompt (no tool call) ───────────────────────

class RecordingCompletions:
    def __init__(self):
        self.last_messages = None

    async def create(self, **kwargs):
        self.last_messages = kwargs["messages"]
        assert "tools" not in kwargs, "#spice should send no tools"

        async def gen():
            yield _chunk(content="Do challenge abc.")
            yield _chunk(finish="stop")
        return gen()


class RecordingClient:
    def __init__(self):
        self.chat = types.SimpleNamespace(completions=RecordingCompletions())


async def test_challenges_injected_into_system_prompt(monkeypatch):
    monkeypatch.setenv("POIESIS_SPICE_API_KEY", "")
    monkeypatch.setenv("POIESIS_SPICE_MODEL", "m")

    class DBWithChallenges(FakeDB):
        async def fetch_one(self, *a, **k):  # store.get_setting reads this row
            return {"value": json.dumps("- **abc** (category: dare, 10 pts): do it")}

    client = RecordingClient()
    monkeypatch.setattr(openai_turn, "AsyncOpenAI", lambda **k: client)
    evs = await _drain(run_openai_turn(
        db=DBWithChallenges(),
        channel={"id": "spice", "soul_path": None, "allowed_tools": "[]"},
        history=[], user_message="what next?", message_id="x",
    ))
    assert evs[-1]["type"] == "done"
    sys_msg = client.chat.completions.last_messages[0]
    assert sys_msg["role"] == "system"
    assert "- **abc**" in sys_msg["content"]
    assert "current challenges" in sys_msg["content"].lower()
