"""#147526: the cron lane must not deliver a mistyped silence sentinel as a report.

The scheduled lane is where the leak was measured (``[SLIENT]`` reached a Telegram chat and became the
job's last non-silent output, which the next tick replayed as continuity). The matcher lives in
:mod:`gateway.response_filters`; these cases pin that the cron entry point delegates to it.
"""

from cron.scheduler import _is_cron_silence_response


def test_cron_lane_suppresses_a_typoed_sentinel():
    assert _is_cron_silence_response("[SLIENT]")
    assert _is_cron_silence_response("[SLIENT]\n\nNothing new this tick.")
    assert _is_cron_silence_response("2 deals filtered\n\n[SLIENT]")


def test_cron_lane_still_delivers_a_report_that_quotes_the_typo():
    assert not _is_cron_silence_response("the marker [SLIENT] appeared in the log, then the run continued")
