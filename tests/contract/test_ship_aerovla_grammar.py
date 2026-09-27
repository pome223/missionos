"""Format constraints preserve every native action bin and terminal proposal."""

import re

import pytest

from scripts.ship_aerovla import ActionGrammar, parse_proposal
from src.runtime.ship_vla_adapter import decode_action


class TokenizerDouble:
    # Explicit tokenizer double, using the pinned vocabulary's relevant pieces.
    vocab = {**{str(i): 100 + i for i in range(10)}, "▁": 120, "L": 121, "AND": 122}
    eos_token_id = 2

    def get_vocab(self):
        return self.vocab

    def decode(self, ids, **kwargs):
        inverse = {v: k.replace("▁", " ") for k, v in self.vocab.items()}
        return "".join(inverse[i] for i in ids)


def tokens(text):
    return [TokenizerDouble.vocab["▁" if k == " " else k] for k in re.findall(r"AND|.", text)]


def accepts(grammar, text):
    prefix = []
    for token in tokens(text):
        assert token in grammar.allowed(prefix), (text, prefix)
        prefix.append(token)
    assert grammar.eos in grammar.allowed(prefix)


def test_all_bins_remain_available_in_every_dimension_and_land_is_not_suppressed():
    grammar = ActionGrammar(TokenizerDouble())
    for bin_id in range(99):
        for position in range(3):
            bins = [49, 49, 49]
            bins[position] = bin_id
            text = " ".join(map(str, bins))
            accepts(grammar, text)
            accepts(grammar, text + " LAND")
    accepts(grammar, "LAND")
    accepts(grammar, " 0 49 49")
    with pytest.raises(ValueError, match="terminal_proposal"):
        decode_action("0 49 49</s>")
    with pytest.raises(ValueError, match="terminal_proposal"):
        decode_action("LAND</s>")


@pytest.mark.parametrize("text", ["", "49", "49 49", "49 49 ", "L", "49 49 49 L"])
def test_incomplete_actions_cannot_end(text):
    grammar = ActionGrammar(TokenizerDouble())
    assert grammar.eos not in grammar.allowed(tokens(text))


@pytest.mark.parametrize("text", ["99", "01", "  ", "49  49", "49 49 49 49"])
def test_malformed_prefix_is_never_repaired(text):
    with pytest.raises(ValueError, match="Invalid generated"):
        ActionGrammar(TokenizerDouble()).allowed(tokens(text))


def test_saved_native_failure_remains_rejected_and_foreign_tokens_are_not_allowed():
    # The third cohort's original model bytes are retained, never reinterpreted.
    failed = "故4924 49</s>"
    assert parse_proposal(failed)["kind"] == "rejected"
    with pytest.raises(ValueError, match="malformed_action"):
        decode_action(failed)
    grammar = ActionGrammar(TokenizerDouble())
    assert 31969 not in grammar.allowed([])
    with pytest.raises(ValueError, match="Invalid generated"):
        grammar.allowed([31969])


def test_callback_ignores_prompt_and_rejects_multi_observation_batch():
    class SequenceDouble:
        def tolist(self):
            return [31969, 30000, *tokens("49 49 49")]

    grammar = ActionGrammar(TokenizerDouble())
    callback = grammar.bind(2)
    assert grammar.eos in callback(0, SequenceDouble())
    with pytest.raises(ValueError, match="single observation"):
        callback(1, SequenceDouble())


def test_unexpected_token_decoding_fails_before_inference():
    class WrongTokenizer(TokenizerDouble):
        def decode(self, ids, **kwargs):
            return "unexpected"

    with pytest.raises(ValueError, match="token decoding"):
        ActionGrammar(WrongTokenizer())


def test_inspection_phase_restricts_only_vertical_bins_and_preserves_terminal_proposals():
    grammar = ActionGrammar(TokenizerDouble(), vertical_bins=(47, 51))
    assert grammar.policy == "aerovla_inspection_grammar.v1"
    for forward in range(99):
        accepts(grammar, f"{forward} 49 49")
    for yaw in range(99):
        accepts(grammar, f"49 49 {yaw}")
    for vertical in range(47, 52):
        accepts(grammar, f"55 {vertical} 49")
    accepts(grammar, "LAND")
    accepts(grammar, "0 49 49 LAND")
    # The original 3.57 m descent proposal is not rewritten or reclassified.
    assert parse_proposal("55 84 49</s>")["down_m"] == pytest.approx(3.5714285714)
    with pytest.raises(ValueError, match="Invalid generated"):
        grammar.allowed(tokens("55 84"))
    for vertical in [46, 52]:
        with pytest.raises(ValueError, match="Invalid generated"):
            grammar.allowed(tokens(f"55 {vertical}"))


@pytest.mark.parametrize("bounds", [(52, 60), (30, 48), (-1, 51), (49, 99), (False, 51), (49,)])
def test_invalid_inspection_range_fails_closed(bounds):
    with pytest.raises(ValueError, match="vertical-bin range"):
        ActionGrammar(TokenizerDouble(), vertical_bins=bounds)


def test_short_segment_yaw_mask_preserves_native_choices_and_terminal_rejection():
    grammar = ActionGrammar(TokenizerDouble(), vertical_bins=(47, 51), yaw_bins=(38, 60))
    assert grammar.policy == "aerovla_short_segment_grammar.v1"
    for forward in range(99):
        accepts(grammar, f"{forward} 49 49")
    for yaw in range(38, 61):
        accepts(grammar, f"79 48 {yaw}")
        assert abs(decode_action(f"79 48 {yaw}")[1][2]) < 0.25
    accepts(grammar, "LAND")
    accepts(grammar, "0 49 49")
    # The first city's frozen model response stays a rotation-only proposal.
    assert decode_action("79 48 0</s>")[1][2] == pytest.approx(-1.1)
    for yaw in [0, 37, 61, 98]:
        with pytest.raises(ValueError, match="Invalid generated"):
            grammar.allowed(tokens(f"79 48 {yaw}"))


@pytest.mark.parametrize("bounds", [(50, 60), (38, 48), (False, 60), (38,), (-1, 60)])
def test_bad_short_segment_mask_rejects(bounds):
    with pytest.raises(ValueError, match="yaw-bin range"):
        ActionGrammar(TokenizerDouble(), vertical_bins=(47, 51), yaw_bins=bounds)


def test_compact_city_allows_short_native_steps_and_refusal_without_rewriting():
    grammar = ActionGrammar(
        TokenizerDouble(), vertical_bins=(47, 51), yaw_bins=(45, 53), forward_bins=(20, 58)
    )
    assert grammar.policy == "aerovla_compact_city_grammar.v1"
    accepts(grammar, "LAND")
    accepts(grammar, "0 49 49")
    for forward in range(20, 59):
        for yaw in range(45, 54):
            accepts(grammar, f"{forward} 49 {yaw}")
            _, (distance, _, angle) = decode_action(f"{forward} 49 {yaw}")
            assert 1 <= distance <= 3 and abs(angle) < 0.1
    for text in ["19 49 49", "59 49 49", "30 49 44", "30 49 54"]:
        with pytest.raises(ValueError):
            grammar.allowed(tokens(text))


@pytest.mark.parametrize("bounds", [(0, 58), (20, 99), (True, 58), (59, 20), (20,)])
def test_invalid_compact_bounds_refused(bounds):
    with pytest.raises(ValueError, match="forward-bin"):
        ActionGrammar(
            TokenizerDouble(), vertical_bins=(47, 51), yaw_bins=(45, 53), forward_bins=bounds
        )
