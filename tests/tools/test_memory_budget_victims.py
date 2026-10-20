"""Over-budget batch consolidation: name WHAT to shrink, and fail a typo'd action before the walk.

#141561 measured an end-of-session consolidation burning four consecutive calls at the cap:
the refusal said how many chars were over but not which entries could free them, so the agent
re-derived sizes from its own context and eventually guessed an action name. Two properties
are pinned here:

* the over-budget refusal carries per-entry char counts (and NOT entry text — #97316 removed
  the echo because paying the whole store back grows the context the call exists to shrink);
* an unknown ``action`` is rejected before any op is applied, naming every offending position
  in one response, and the message lists the actions that actually exist.
"""

import json

import pytest

from tools.memory_tool import MemoryStore


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    s = MemoryStore(memory_char_limit=400, user_char_limit=300)
    s.load_from_disk()
    return s


class TestOverBudgetRefusalNamesVictims:
    def test_refusal_carries_entry_sizes_largest_first(self, store):
        store.add("memory", "short fact")
        store.add("memory", "a much longer fact that costs real characters to keep around")
        before = list(store.memory_entries)
        result = store.apply_batch(
            "memory",
            [{"action": "add", "content": "x" * 400}],
        )
        assert result["success"] is False
        assert "free at least" in result["error"]
        largest = result.get("largest_entries")
        assert largest, "an over-budget refusal must name the entries worth shrinking"
        # 1-based indexes into the store the model already holds, biggest first.
        assert [row["index"] for row in largest] == [2, 1]
        assert [row["chars"] for row in largest] == sorted((len(e) for e in before), reverse=True)
        # Nothing was applied.
        assert store.memory_entries == before

    def test_refusal_does_not_echo_entry_text(self, store):
        store.add("memory", "secret-ish fact A that is long enough to matter here")
        store.add("memory", "secret-ish fact B that is also long enough to matter")
        result = store.apply_batch("memory", [{"action": "add", "content": "y" * 400}])
        assert result["success"] is False
        payload = json.dumps(result)
        assert "secret-ish fact A" not in payload
        assert "secret-ish fact B" not in payload


class TestUnknownActionFailsBeforeTheWalk:
    def test_every_bad_action_is_named_in_one_response(self, store):
        store.add("memory", "keep me")
        result = store.apply_batch(
            "memory",
            [
                {"action": "delete", "old_text": "keep me"},
                {"action": "add", "content": "never applied"},
                {"action": "undefined", "old_text": "keep me"},
            ],
        )
        assert result["success"] is False
        message = result["error"]
        assert "Operation 1 (delete)" in message
        assert "Operation 3 (undefined)" in message
        assert "Operation 2" not in message
        assert store.memory_entries == ["keep me"]

    def test_the_message_lists_the_actions_that_exist(self, store):
        store.add("memory", "keep me")
        result = store.apply_batch("memory", [{"action": "delete", "old_text": "keep me"}])
        assert result["success"] is False
        assert "replace" in result["error"], "replace is a supported action and must be listed"

    def test_a_valid_replace_still_goes_through(self, store):
        store.add("memory", "fact one")
        result = store.apply_batch(
            "memory", [{"action": "replace", "old_text": "fact one", "content": "fact one (revised)"}]
        )
        assert result["success"] is True, result
        assert store.memory_entries == ["fact one (revised)"]
