from gateway.response_filters import (
    is_autonomous_silence_response,
    is_intentional_silence_agent_result,
    is_intentional_silence_response,
)


def test_exact_silence_tokens_are_intentional_silence():
    for token in ("[SILENT]", " SILENT ", "NO_REPLY", "no reply"):
        assert is_intentional_silence_response(token)


def test_autonomous_silence_accepts_marker_with_own_line_note():
    """The loose rule for cron/webhook lanes: marker + explanation suppresses."""
    assert is_autonomous_silence_response("[SILENT]")
    assert is_autonomous_silence_response("[SILENT]\n\nNothing new this tick.")
    assert is_autonomous_silence_response("2 deals filtered\n\n[SILENT]")
    assert is_autonomous_silence_response("no_reply\nduplicate inbound, already handled")
    assert is_autonomous_silence_response("[SILENT] No changes detected")


def test_translated_sentinel_is_silence_in_every_form_the_english_one_is():
    """#110935: a lane that answers the cron instruction in its own language translates the
    sentinel; ``[静默]`` must suppress delivery exactly like ``[SILENT]`` (exact, own-line note,
    reordered lines, bracketless, edge punctuation)."""
    assert is_intentional_silence_response("[静默]")
    assert is_intentional_silence_response("**沉默**")
    assert is_autonomous_silence_response("[静默]\n\nNothing new this tick.")
    assert is_autonomous_silence_response("2 deals filtered\n\n[沉默]")
    assert is_autonomous_silence_response("静默")


def test_prose_mentioning_the_translated_sentinel_is_delivered():
    assert not is_intentional_silence_response("status: 静默 means the lane is quiet")
    assert not is_autonomous_silence_response("the lane said 静默 mid-sentence and kept talking")


def test_autonomous_lane_agrees_with_interactive_lane_on_cjk_punctuation_variants():
    """A Chinese lane emits fullwidth brackets or a trailing ``。``; cron/webhook must suppress
    exactly what the interactive predicate suppresses, or the two lanes drift on the new tokens."""
    for variant in ("【静默】", "静默。", "【沉默】", "沉默。", "**[静默]**", "NO_REPLY."):
        assert is_intentional_silence_response(variant)
        assert is_autonomous_silence_response(variant) == is_intentional_silence_response(variant), variant


def test_autonomous_lane_suppresses_a_one_edit_typo_of_a_marker():
    """#147526: a model that mistypes the sentinel (``[SLIENT]``) still chose silence, but the exact
    matcher called it a report and delivered the whole token to the chat. The autonomous lane takes a
    marker one edit away, in the same whole-response / own-first-line / own-last-line shapes it already
    accepts for exact markers — including the adjacent transposition the real leak was."""
    for variant in (
        "[SLIENT]",
        "SLIENT",
        "[SILENTS]",
        "[SILNT]",
        "NO-REPLY",
        "[SLIENT]\n\nNothing new this tick.",
        "2 deals filtered\n\n[SLIENT]",
    ):
        assert is_autonomous_silence_response(variant), variant


def test_interactive_lane_keeps_its_exact_match_contract():
    """The near-miss tolerance is autonomous-lane only: a room's reply that happens to look like a
    typoed sentinel must still be delivered, so the interactive rule cannot widen with it."""
    for variant in ("[SLIENT]", "SLIENT", "NO-REPLY", "[SILENTS]"):
        assert not is_intentional_silence_response(variant), variant


def test_a_typo_inside_a_sentence_is_still_content():
    """Only a marker-shaped response (whole, own first line, own last line) is judged; a report that
    quotes the typo mid-sentence is delivered, exactly like one quoting ``[SILENT]``."""
    assert not is_autonomous_silence_response("the marker [SLIENT] was typed by the model")
    assert not is_autonomous_silence_response("we saw [SLIENT] in the log and then it continued")


def test_near_miss_tolerance_does_not_swallow_short_real_answers():
    """The edit bound keeps the rule marker-shaped: prose, ordinary words and CJK markers are out of
    reach. ``静音`` (mute) is one edit from ``静默``, which is why fuzzy matching stays ASCII-only."""
    for token in ("TALENT", "SILENCE!", "no reply needed here", "静音", "[静音]", "静音。"):
        assert not is_autonomous_silence_response(token), token


def test_near_miss_reports_the_response_text_for_the_delivery_log():
    """The suppression log has to carry what was swallowed; an exact marker is not a near-miss."""
    from gateway.response_filters import near_miss_silence_marker

    assert near_miss_silence_marker("[SLIENT]") == "[SLIENT]"
    assert near_miss_silence_marker("2 deals filtered\n\n[SLIENT]") == "[SLIENT]"
    assert near_miss_silence_marker("[SILENT]") is None
    assert near_miss_silence_marker("the marker [SLIENT] was typed by the model") is None
    assert near_miss_silence_marker("") is None
    assert near_miss_silence_marker(None) is None
