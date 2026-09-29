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
| D | Same weights, swap inputs A–D | Pretrained: swapping moving/still history changes the lead's future region by 0.6–0.7 (0–255); swapping the conditioning image by 6–7. After C1: history swap 45 on the trained pair (lead drawn even with a lead-free conditioning image), but on an unseen moving pair it paints the memorized pattern. |

Facts: history frames reach every block's cross-attention directly; the
conditioning image shares the target frame's positional slot. The pretrained
weights do not use the history to place another object. Post-training can make
them use it, but at this scale it learns per-example memorization, not a
transferable motion rule.

Inference (not ablated architecturally): the pretrained prior gives no motion
rule to build on, and small correlated data cannot force one. Sub-token lead size
lets unsupported small blobs fade in late denoising. An earlier claim that no
pathway carries motion, or that frozen early blocks withhold it, was wrong and
is withdrawn.

## Boundaries

Every diagnostic uses one scene, one lead appearance and a fixed camera; the
80% memorization gate is diagnostic, not a delivery adoption criterion. Warm
inference (4.4–20 s) exceeds the 1–3 s horizons, so none of these forecasts is a
live entry signal. A failed gate stops spending on that condition; it is not a
claim that ANWM can never forecast other agents.
