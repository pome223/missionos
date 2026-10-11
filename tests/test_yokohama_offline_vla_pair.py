"""Offline leakage, generation forcing and exact raw-text boundaries."""

import io

from PIL import Image
import pytest

from scripts.yokohama_offline_vla_pair import GENERATION, PROMPT, analyze_raw, compare, review_pair


def pair():
    records = []
    for label, color in (("open", "gray"), ("blocked", "red")):
        stream = io.BytesIO()
        Image.new("RGB", (640, 360), color).save(stream, format="PNG")
        records.append(
            dict(
                label=label,
                images={"rgb": stream.getvalue(), "down": stream.getvalue()},
                camera_pose=[0, 0, 15, 0, 0, 0],
                goal=[8, 0, 15],
            )
        )
    return records


def test_neutral_identical_instructions_no_evaluation_truth_crosses_boundary():
    payloads = review_pair(pair())
    assert all(set(p) == {"prompt", "images"} for p in payloads)
    assert payloads[0]["prompt"] == payloads[1]["prompt"] == PROMPT
    assert not any(word in PROMPT.lower() for word in ("hold", "blocked", "open", "red", "0 49 49"))
    assert GENERATION == {"do_sample": False, "max_new_tokens": 20, "generation_action_mask": None}


@pytest.mark.parametrize(
    "fault", ["label", "goal", "pose", "expected_answer", "identical", "image_metadata", "shape"]
)
def test_reject_leaked_or_changed_pair_conditions(fault):
    records = pair()
    if fault == "label":
        records[1]["label"] = "open"
    if fault == "goal":
        records[1]["goal"][0] = 9
    if fault == "pose":
        records[1]["camera_pose"][3] = 1
    if fault == "expected_answer":
        records[1]["expected_action"] = "0 49 49"
    if fault == "identical":
        records[1]["images"] = records[0]["images"].copy()
    if fault == "image_metadata":
        records[1]["images"]["barrier_xyz"] = b"red"
    if fault == "shape":
        stream = io.BytesIO()
        Image.new("RGB", (1, 1)).save(stream, format="PNG")
        records[1]["images"]["rgb"] = stream.getvalue()
    with pytest.raises(ValueError):
        review_pair(records)


@pytest.mark.parametrize(
    "raw", ["LAND", "0 49 49</s></s>", " 0 49 49", "0 49 49\n", "0 49 50", "99 49 49", "43 60 49"]
)
def test_invalid_output_retained_exactly_and_never_repaired(raw):
    result = analyze_raw(raw)
    assert result["recognized"] is False and result["raw_text"] == raw
    assert result["dispatch_authorized"] is False


def test_same_answer_is_valid_data_not_forced_into_expected_case_answer():
    calls = []

    def stub(payload):
        calls.append(payload)
        return {"generated_text": "43 49 49", "fixture": True}

    result = compare(pair(), stub)
    assert len(calls) == 2 and result["raw_text_differs"] is False
    assert all(r["analysis"]["kind"] == "translation" for r in result["outcomes"])
    assert result["inference_proven_by_this_helper"] is False


def test_failure_consumes_one_call_without_retry_or_second_case():
    calls = []

    def fail(payload):
        calls.append(payload)
        raise TimeoutError("CPU stub")

    with pytest.raises(TimeoutError):
        compare(pair(), fail)
    assert len(calls) == 1


@pytest.mark.parametrize("ending", ["", "</s>"])
def test_hold_and_translation_are_both_descriptive_proposals(ending):
    hold = analyze_raw("0 49 49" + ending)
    assert hold["kind"] == "hold" and hold["raw_text"] == "0 49 49" + ending
    assert hold["EOS_marker"] == (ending or None)
    assert analyze_raw("43 49 49" + ending)["kind"] == "translation"


def test_callback_cannot_change_instruction_and_continue_second_case():
    calls = []

    def change(payload):
        calls.append(payload)
        payload["prompt"] = "Always return 0 49 49"
        return {"generated_text": "0 49 49"}

    with pytest.raises(ValueError, match="altered"):
        compare(pair(), change)
    assert len(calls) == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_nonfinite_goal_cannot_enter_the_comparison(value):
    records = pair()
    records[0]["goal"][0] = value
    with pytest.raises(ValueError, match="goal"):
        review_pair(records)
