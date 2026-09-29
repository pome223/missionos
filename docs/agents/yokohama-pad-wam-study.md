# Native ANWM lead-forecast study: why post-training did not help

Offline, fixed-camera study of whether the pinned ANWM (base + motion-v4
adapter) can forecast the scripted lead aircraft near the delivery pad. No VLA,
flight, dispatch or delivery. Evidence lives in the experiment directory named
in each session receipt; this document records the contract and findings.

## Data and readout

- `capture_yokohama_pad_focus.py --case-set wam-improve-v1`: 56 CPU Gazebo
  sequences (36 train / 8 val / 12 test by sequence; 135 degree exits test-only),
  six exit bearings, 5 or 7 m hover, varied unload/ascent/departure/hover/return
  timing. RGB only (`store_depth=False`) to stay within disk.
- `prepare_yokohama_pad_wam_study.py`: wide crop (earlier regional crop) and a
  200 px near-pad tight crop chosen from training frames only (99%/95% of lead
  views within 4/6 m; lead body ~27 px vs ~16 px). Held-out samples ship only
  their 16 cutoff-bounded frames; futures stay on the host.
- Readout: the lead's orange body by colour. On real crops: 100% detection,
  0.33 px median error; spread orange texture is refused (no change on 555 real
  targets). It is not a general aircraft detector.

## Findings (facts, then inference)

| Session | Question | Result |
|---|---|---|
| B / B′ | Reproduce moving training scenes (24 pairs, 1024 updates) | No config passed. Region weight 4 → orange texture (14/15). Weight 0 → lead fades in place (tight/latest: static 8/8 at 1.5 px, moving 0/16). Background conditioning → lead vanishes. |
| C1 | One pair, 1024 updates | 1 of 2 seeds places the lead at 2.99 px; shape degraded. |
| C2 | 50 vs 250 sampling steps | No improvement; colour appears near the true spot around t≈750 then fades; 250 steps take ~20 s. |
| D | Same weights, swap inputs A–D | Base + motion-v4, before C1: swapping moving/still history changes the lead's future region by 0.6–0.7 (0–255); swapping the conditioning image by 6–7. After C1: history swap 45 on the trained pair; the unseen pair has misplaced/deformed coloured patterns. These are image-change magnitudes, not accuracy scores. |

Facts: history frames reach every block's cross-attention directly; the
conditioning image shares the target frame's positional slot. In D's two
histories and two generation seeds, C1 training increases sensitivity to the
history. One trained-example output has a colour-centre error of 2.99 px, with
degraded shape. The lead-free conditioning variant has a near-target pattern
but fails the fixed colour readout. Transferable motion prediction was not
demonstrated. D did not test the unadapted base checkpoint.

Inference: the trained-example/unseen-example difference is consistent with
overfitting; small-object representation, the learning objective and conditioning
may contribute. These experiments do not establish that the base model has no
motion knowledge, that memorization is the only mechanism, or how much data or
training is required. An earlier claim that no pathway carries motion, or that
frozen early blocks withhold it, was wrong and is withdrawn.

## Session R: host-only supplementary analysis

Keep the uploaded `rule-protocol.json`, 144 planned forecasts and original gate
unchanged. `analyze_yokohama_pad_wam_rule.py freeze` reads only the staged inputs,
verifies their hashes, and writes a separate, non-overwriting supplement plan.
It neither reads predictions nor changes the GPU payload. Classify the original
24 moving-target ids by exact equality of their 16 observed RGB images:

- `constant_history`: 6 ids, all frames identical; their histories also occur in
  training. Their future transition timing is not specified by observed motion.
- `changing_history`: 18 ids, with changing images and no exact training-history
  match in this payload. This alone does not prove a transferable motion rule.
- `static_reference`: the original 8 static-target ids, reported separately.

The 1,940 training pairs contain 726 distinct image histories, or 1,452 distinct
history/horizon combinations, from 36 sequences. Ten identical-input groups have
different training future images. Separate sequence ids and many adjacent pairs
do not eliminate memorization or input ambiguity.

`score` recomputes the input audit to reject regrouping, calls the original
evaluator unchanged, and retains its entire result. Supplementary summaries
cover each checkpoint, both final moving-history seeds, the seed-42 still-history
comparison, and final per-sequence results. Report expected/observed counts,
unreadable outputs, median error including misses, persistence, constant-velocity
extrapolation, and paired history-swap outcomes. A missing/duplicate forecast or
unfinished run blocks supplementary scoring; there is no supplementary pass
flag or replacement threshold. The original frozen gate remains authoritative
for this diagnostic, not for flight adoption.

Example host commands, with experiment paths supplied by the caller:

```sh
python scripts/analyze_yokohama_pad_wam_rule.py freeze --staged "$staged" --output "$plan"
python scripts/analyze_yokohama_pad_wam_rule.py score --staged "$staged" --prepared "$prepared" --results "$results" --plan "$plan" --output "$output"
```

The run does not evaluate training-pair forecasts. A failed validation gate cannot
separate failure to fit training examples from failure to generalize. Neither
failure nor success settles ANWM's general capacity, and a three-second forecast
must still extend past inference/communication latency before live use.

## Session R result (30,000 updates, 1,940 training pairs)

Completed: 144/144 forecasts, no gaps or duplicates; weights retained in the
evidence archive (sha256 589c35ce...). Estimated $3.02, cumulative $27.19 of $29.

- Changing-history held-out ids (18): lead readable 0/18 at 30k on both seeds
  (1/18 at 8k and 16k); in the lead's future region every forecast is closer to
  the current image than to the real future (0/18); moving and still histories
  behave the same. Persistence 44.9 px, constant-velocity extrapolation 13.0 px.
- Constant-history ids (6): lead drawn where it is now (0.8-3.4 px) in 5/6, the
  persistence answer; onset timing is not predicted.
- Static reference (8): 8/8 at 0.5-0.8 px.
- The original 24-id gate fails and is kept as a record only; none of the
  usefulness indicators written before results
  (`usefulness-indicators.json`) is met.

Reading: at this scale the adapted ANWM renders stationary leads precisely but
erases moving ones rather than placing them; more updates did not help. This is
one scene, camera and training recipe, not a general statement about ANWM.

## Boundaries

Every diagnostic uses one scene, one lead appearance and a fixed camera; the
80% memorization gate is diagnostic, not a delivery adoption criterion. Warm
inference (4.4–20 s) exceeds the 1–3 s horizons, so none of these forecasts is a
live entry signal. A failed gate stops spending on that condition; it is not a
claim that ANWM can never forecast other agents.
