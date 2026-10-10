import pytest

from app import config
from app.pipeline.download import NotAudio, sniff_extension
from app.pipeline.grade import (
    GraderOutput,
    build_user_message,
    conflicts,
    load_grader,
    no_speech_grade,
    repoint_evidence,
    segment_index,
    validate,
)
from app.pipeline.import_csv import clean_id, folder_call_id, load_disposition_groups
from app.pipeline.signals import Signal, Turn, detect
from app.pipeline.speakers import (
    Seg,
    accept_fix,
    apply_term_fixes,
    entries_from_raw,
    map_by_rules,
    merge_turns,
    split_sentences,
    usable_fixes,
)
from app.sarvam import RateLimiter, llm_cost_inr, stt_cost_inr


def turns(*pairs: tuple[str, str], gap: float = 1.0) -> list[Turn]:
    out, t = [], 0.0
    for i, (speaker, text) in enumerate(pairs):
        out.append(Turn(i, speaker, t, t + 2.0, text))
        t += 2.0 + gap
    return out


def types(signals: list[Signal]) -> set[str]:
    return {s.type for s in signals}


CLOSE = ("BOT", "IndiaMART चुनने के लिए धन्यवाद। आपका दिन शुभ हो।")

# ---- signals -------------------------------------------------------------------------------


def test_clean_call_has_no_signals() -> None:
    call = turns(
        ("BOT", "Hello, आपको full body massage chair की requirement है ना?"),
        ("CALLER", "जी।"),
        ("BOT", "आप अपनी city और state भी बता दीजिए please।"),
        ("CALLER", "जयपुर।"),
        CLOSE,
    )
    assert detect(call, duration_s=30, language_probability=0.99, disposition_group="success") == []


def test_loop_when_bot_repeats_a_question() -> None:
    q = "क्या आप अपनी city और state फिर से बता सकते हैं?"
    call = turns(("BOT", q), ("CALLER", "Shopian"), ("BOT", q), ("CALLER", "Shopian J&K"), CLOSE)
    found = [s for s in detect(call) if s.type == "loop"]
    assert [s.utterance_idx for s in found] == [2]


def test_short_acknowledgements_are_not_a_loop() -> None:
    call = turns(("BOT", "ठीक है।"), ("CALLER", "जी"), ("BOT", "ठीक है।"), ("CALLER", "जी"), ("BOT", "ठीक है।"), CLOSE)
    assert "loop" not in types(detect(call)) and "frustration" not in types(detect(call))


def test_human_request_in_both_scripts() -> None:
    for text in ("मुझे किसी customer care वाले से बात करनी है", "kisi insaan se baat karao", "एजेंट से बात कराओ"):
        assert "human_request" in types(detect(turns(("BOT", "बताइए"), ("CALLER", text), CLOSE)))


def test_frustration_phrase_and_repeat_and_hellos() -> None:
    assert "frustration" in types(detect(turns(("CALLER", "कितनी बार बताऊं आपको"), CLOSE)))
    repeat = turns(("CALLER", "मुझे दो सौ kg चाहिए"), ("BOT", "कितना चाहिए?"), ("CALLER", "मुझे दो सौ kg चाहिए"), CLOSE)
    assert "frustration" in types(detect(repeat))
    hellos = turns(("CALLER", "Hello?"), ("BOT", "जी"), ("CALLER", "हेलो"), ("BOT", "जी"), ("CALLER", "Hello."), CLOSE)
    assert "frustration" in types(detect(hellos))


def test_requirement_rejected_and_disposition_suspect() -> None:
    call = turns(
        ("BOT", "Hello, आपको steel pipe की requirement है ना?"),
        ("CALLER", "नहीं, मैंने ये नहीं मांगा था"),
        CLOSE,
    )
    assert {"requirement_rejected", "disposition_suspect"} <= types(detect(call, disposition_group="success"))
    assert "disposition_suspect" not in types(detect(call, disposition_group="caller_no_need"))


def test_confirmation_is_not_a_rejection() -> None:
    call = turns(("BOT", "आपको chair की requirement है ना?"), ("CALLER", "हाँ जी बिल्कुल"), CLOSE)
    assert "requirement_rejected" not in types(detect(call))


def test_abrupt_end_variants() -> None:
    unanswered = turns(("BOT", "आपको chair की requirement है ना?"), ("CALLER", "जी"), ("BOT", "आपको कितने piece चाहिए?"))
    assert "abrupt_end" in types(detect(unanswered))
    no_close = turns(("BOT", "आपको कितने piece चाहिए?"), ("CALLER", "दस"))
    assert "abrupt_end" in types(detect(no_close))
    assert "abrupt_end" not in types(detect(turns(("BOT", "बताइए"), ("CALLER", "दस"), CLOSE, ("CALLER", "Okay"))))


def test_low_confidence_and_long_silence() -> None:
    sorry = "क्षमा कीजिए मैं समझ नहीं पाई।"
    call = turns(("BOT", sorry), ("CALLER", "जयपुर"), ("BOT", sorry + " फिर से बताइए"), CLOSE)
    assert "low_confidence" in types(detect(call))
    assert "low_confidence" in types(detect(turns(CLOSE), language_probability=0.4))
    assert "long_silence" in types(detect(turns(("BOT", "बताइए"), CLOSE, gap=7.0)))
    assert "long_silence" not in types(detect(turns(("BOT", "बताइए"), CLOSE, gap=3.0)))


# ---- speakers ------------------------------------------------------------------------------


def test_split_sentences_separates_an_embedded_reply() -> None:
    seg = Seg("0", 0.0, 10.0, "Hello, आपको chair की requirement है ना? जी। ठीक है, size 5.5 चाहिए ना?")
    parts = split_sentences([seg])
    assert [p.text for p in parts] == ["Hello, आपको chair की requirement है ना?", "जी।", "ठीक है, size 5.5 चाहिए ना?"]
    assert parts[0].start_s == 0.0 and parts[-1].end_s == 10.0
    assert merge_turns(parts) == [seg]


def test_transcript_repair_only_accepts_small_safe_edits() -> None:
    heard = "additive में K2ATAA, K3ATAA या ADTA में से कौन सा चाहिए?"
    fixed = "additive में K2 EDTA, K3 EDTA या EDTA में से कौन सा चाहिए?"
    assert accept_fix(heard, fixed) == fixed
    assert accept_fix("आपको pen concrete mixer की requirement है ना?", "आपको pan concrete mixer की requirement है ना?").count("pan") == 1
    # a number changed, a rewrite, or an empty answer: keep what the recogniser heard
    assert accept_fix("मुझे 200 kg चाहिए", "मुझे 500 kg चाहिए") == "मुझे 200 kg चाहिए"
    assert accept_fix("मुझे 200 kg चाहिए", "मुझे kg चाहिए") == "मुझे 200 kg चाहिए"
    assert accept_fix("हाँ जी ठीक है", "The caller agrees and confirms the order") == "हाँ जी ठीक है"
    assert accept_fix("जी।", "") == "जी।"
    # merged turns keep the original only where something changed
    merged = merge_turns([Seg("BOT", 0, 1, "pan mixer", raw="pen mixer"), Seg("BOT", 1, 2, "चाहिए?")])
    assert (merged[0].text, merged[0].raw) == ("pan mixer चाहिए?", "pen mixer चाहिए?")


def test_term_fixes_are_filtered_and_applied() -> None:
    turns = [
        Seg("BOT", 0, 1, "आपको K3 Aeta blood collection tube की requirement है ना?"),
        Seg("CALLER", 1, 2, "1000 piece चाहिए"),
        Seg("BOT", 2, 3, "additive में K2ATAA, K3ATAA या ADTA में से कौन सा चाहिए?"),
    ]
    transcript = "\n".join(t.text for t in turns)
    proposed = [
        ("K2ATAA", "K2 EDTA"), ("K3ATAA", "K3 EDTA"), ("ADTA", "EDTA"), ("Aeta", "EDTA"),
        ("1000 piece", "100 piece"),  # changes a number: rejected
        ("not in call", "anything"),  # not present: rejected
        ("है", "हैं"),  # too short to be a term: rejected
        ("EDTA", "EDTA"),  # no change: rejected
    ]  # fmt: skip
    fixes = usable_fixes(proposed, transcript)
    assert set(fixes) == {("K2ATAA", "K2 EDTA"), ("K3ATAA", "K3 EDTA"), ("ADTA", "EDTA"), ("Aeta", "EDTA")}
    fixed = apply_term_fixes(turns, fixes)
    assert fixed[0].text == "आपको K3 EDTA blood collection tube की requirement है ना?" and "Aeta" in fixed[0].raw
    assert fixed[1] is turns[1] and fixed[1].raw == ""  # untouched turn keeps no "heard" note
    assert fixed[2].text == "additive में K2 EDTA, K3 EDTA या EDTA में से कौन सा चाहिए?"


def test_folder_call_id() -> None:
    from pathlib import Path

    assert folder_call_id(Path("1000_90643234_F0C4W3KG5.mp3"), set()) == "90643234"
    assert folder_call_id(Path("1000_90643234_F0C4W3KG5.mp3"), {"90643234"}) == "1000_90643234_F0C4W3KG5"
    assert folder_call_id(Path("my call.mp3"), set()) == "my call"


def test_entries_and_rule_mapping() -> None:
    raw = {
        "diarized_transcript": {
            "entries": [
                {"transcript": "Hello?", "start_time_seconds": 0.2, "end_time_seconds": 0.8, "speaker_id": "1"},
                {"transcript": "मैं IndiaMART से बोल रही हूँ", "start_time_seconds": 1.1, "end_time_seconds": 3.6, "speaker_id": "0"},
            ]
        }
    }
    entries = entries_from_raw(raw)
    assert [e.speaker for e in entries] == ["1", "0"]
    assert map_by_rules(entries) == ("0", "pattern")
    assert map_by_rules(entries[:1]) == (None, "single_speaker")
    assert entries_from_raw({"transcript": ""}) == []


# ---- grader --------------------------------------------------------------------------------


def grade_json(**overrides: object) -> GraderOutput:
    out = no_speech_grade("BL Enriched").model_dump()
    out.update(overrides)
    return GraderOutput.model_validate(out)


TRANSCRIPT = turns(("BOT", "आपको steel pipe की requirement है ना?"), ("CALLER", "नहीं,  मुझे PVC pipe चाहिए"), CLOSE)


def test_grader_file_parses_and_template_fills() -> None:
    prompt = load_grader()
    assert prompt.schema["name"] == "call_grade" and prompt.schema["strict"] is True
    assert "{{BOT_GOAL}}" not in prompt.system and "{{FATAL_RUBRIC}}" not in prompt.system
    assert "senior quality auditor" in prompt.system and "## USER MESSAGE TEMPLATE" not in prompt.system
    call = {
        "call_id": "1", "duration_s": 42.4, "disposition": "BL Enriched", "disposition_group": "success",
        "is_call_answered": "1", "language_code": "hi-IN", "language_probability": 0.987, "speaker_method": "llm_sentences",
    }  # fmt: skip
    message = build_user_message(call, TRANSCRIPT, [Signal("requirement_rejected", 1, "reply to read-back S0")])
    assert "{{" not in message
    assert "DISPOSITION (system-logged, verify it): BL Enriched" in message
    assert "[S1] CALLER (3.0s): नहीं,  मुझे PVC pipe चाहिए" in message
    assert "- requirement_rejected at S1: reply to read-back S0" in message
    assert "- none" in build_user_message(call, TRANSCRIPT, [])


def test_schema_fields_match_the_pydantic_model() -> None:
    assert set(load_grader().schema["schema"]["required"]) == set(GraderOutput.model_fields)


def test_validate_accepts_a_good_fatal() -> None:
    good = grade_json(
        grade="FATAL", fatal_code="F2", failure_category="prompt", non_fatal_issues=[],
        evidence=[{"segment_id": "S1", "quote": "नहीं, मुझे PVC  pipe चाहिए", "supports": "grade"}],
    )  # fmt: skip
    assert validate(good, TRANSCRIPT, "BL Enriched") == []


def test_validate_catches_each_rule() -> None:
    def problems(**kw: object) -> str:
        return " | ".join(validate(grade_json(**kw), TRANSCRIPT, "BL Enriched"))

    assert "fatal_code is null" in problems(grade="FATAL", fatal_code=None)
    assert 'supports == "grade"' in problems(grade="FATAL", fatal_code="F2")
    assert 'cannot be "none"' in problems(grade="FATAL", fatal_code="F2", failure_category="none")
    # handled without a retry: a contradiction goes to review, minor issues with no cause are accepted
    assert problems(grade="NON_FATAL", fatal_code="F2") == ""
    assert conflicts(grade_json(grade="NON_FATAL", fatal_code="F2")) == {"non_fatal_but_fatal_code_given"}
    assert problems(failure_category="none") == "" and conflicts(grade_json(failure_category="none")) == set()
    invented = [{"segment_id": "S1", "quote": "मुझे steel pipe चाहिए", "supports": "grade"}]
    assert "not exact text" in problems(evidence=invented)
    wrong_segment = [{"segment_id": "S9", "quote": "PVC pipe", "supports": "grade"}]
    assert "not exact text" in problems(evidence=wrong_segment)
    assert "disposition_check.logged" in " | ".join(validate(grade_json(), TRANSCRIPT, "Abruptly Disconnected Call"))


def test_quote_matching_forgives_punctuation_but_not_words() -> None:
    def problems(quote: str, segment: str = "S1") -> list[str]:
        out = grade_json(evidence=[{"segment_id": segment, "quote": quote, "supports": "outcome"}])
        repoint_evidence(out, TRANSCRIPT)
        return validate(out, TRANSCRIPT, "BL Enriched")

    assert problems("“नहीं मुझे PVC pipe चाहिए.”") == []  # quote marks, comma and full stop differ
    assert problems("नहीं, मुझे PVC pipe चाहिए", segment="S0") == []  # right quote, wrong segment: re-pointed
    assert problems("[S1] CALLER (3.0s): नहीं, मुझे PVC pipe चाहिए"[20:], segment="[S1] CALLER (3.0s)") == []
    assert problems("नहीं, मुझे PVC pipe चाहिए था")  # an added word
    assert problems("नहीं, मुझे PVC pipe चाहिये")  # a changed vowel sign
    assert problems("…")  # nothing left after punctuation


def test_segment_index() -> None:
    assert (segment_index("S12"), segment_index("12"), segment_index(" s3 "), segment_index("x")) == (12, 12, 3, None)
    # the model often copies the transcript's own formatting
    assert segment_index("[S0]") == 0
    assert segment_index("[S16] CALLER (84.8s): और half inch wide चाहिए।") == 16


# ---- data ----------------------------------------------------------------------------------


def test_clean_id_and_disposition_groups() -> None:
    assert (clean_id("91046386.0"), clean_id(" 5 "), clean_id("")) == ("91046386", "5", "")
    groups = load_disposition_groups()
    assert groups["BL Enriched"] == "success" and groups["Abruptly Disconnected Call"] == "abrupt"
    assert groups["Job Related_BL Not Approved"] == "caller_no_need" and groups["Queue Drop"] == "technical"


@pytest.mark.skipif(not config.CSV_PATH.is_file(), reason="organisers' CSV not present")
def test_csv_counts_match_the_brief() -> None:
    import pandas as pd

    frame = pd.read_csv(config.CSV_PATH, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    recorded = frame[frame["call_recording_url"].str.strip() != ""]
    assert (len(frame), frame["fk_leap_call_records_id"].nunique(), len(recorded)) == (1000, 1000, 717)
    counts = recorded["disposition_remarks"].value_counts()
    assert (counts["BL Enriched"], counts["Abruptly Disconnected Call"], counts["BL Approved"]) == (449, 118, 64)


def test_sniff_extension() -> None:
    assert sniff_extension(b"ID3\x04\x00\x00") == ".mp3"
    assert sniff_extension(b"\xff\xfb\x90\x00") == ".mp3"
    assert sniff_extension(b"RIFF\x00\x00\x00\x00WAVE") == ".wav"
    with pytest.raises(NotAudio, match="needs a login"):
        sniff_extension(b"<!DOCTYPE html>")
    with pytest.raises(NotAudio, match="unknown file type"):
        sniff_extension(b"\x00\x01\x02\x03")


def test_limiter_and_costs() -> None:
    limiter = RateLimiter(rpm=60)
    assert all(limiter.try_acquire(now=1000.0) == 0 for _ in range(10))
    assert limiter.try_acquire(now=1000.0) == pytest.approx(1.0)
    assert limiter.try_acquire(now=1001.0) == 0
    assert llm_cost_inr(1_000_000, 1_000_000) == pytest.approx(29.28 + 73.20)
    assert stt_cost_inr(3600) == pytest.approx(45.0)


# ---- root causes ---------------------------------------------------------------------------


def test_call_weight_and_priority_score() -> None:
    from app.root_causes import call_weight, score

    assert call_weight("FATAL", [], "requirement_confirmed") == 3
    assert call_weight("NON_FATAL", ["frustration"], "requirement_confirmed") == 2
    assert call_weight("NON_FATAL", [], "call_dropped") == 2  # did not reach its goal
    assert call_weight("NON_FATAL", [], "requirement_updated") == 1
    # 5 of 20 analysed calls, all Fatal: frequency 0.25, severity 1.0, priority 25
    assert score([3] * 5, 20) == {"frequency": 0.25, "severity": 1.0, "priority": 25.0}
    # 4 of 20 calls: two Fatal, one serious, one minor -> severity (3+3+2+1)/4/3 = 0.75
    mixed = score([3, 3, 2, 1], 20)
    assert (mixed["frequency"], mixed["severity"], mixed["priority"]) == (0.2, 0.75, pytest.approx(15.0))
    assert score([3] * 20, 20)["priority"] == 100.0
    assert score([], 20) == {"frequency": 0.0, "severity": 0.0, "priority": 0.0}
    assert score([3], 0)["priority"] == 0.0


def test_grouping_puts_every_call_in_exactly_one_cause() -> None:
    from app.root_causes import _Cause, assign

    proposed = [
        _Cause(name="Closes without read-back", description="d", category="prompt", call_ids=["1", "2", "2", "99"]),
        _Cause(name="Loops on city", description="d", category="made-up", call_ids=["2", "3"]),
        _Cause(name="Empty", description="d", category="flow", call_ids=["99"]),
    ]
    out = assign(proposed, ["1", "2", "3", "4"])
    assert [(c["name"], c["call_ids"]) for c in out] == [
        ("Closes without read-back", ["1", "2"]),  # duplicate and unknown ids dropped
        ("Loops on city", ["3"]),  # "2" already belongs to the first cause
        ("Other", ["4"]),  # nobody claimed it
    ]
    assert out[1]["category"] == "flow"  # unknown category replaced
    assert sorted(i for c in out for i in c["call_ids"]) == ["1", "2", "3", "4"]