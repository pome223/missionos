# Offline AeroVLA image-pair boundary

`scripts/yokohama_offline_vla_pair.py` provides pure input review and exact raw
text analysis. It loads no model and makes no network or dispatch call. It does
not enable the existing visual contract or change its two-second response lease.

One saved open/blocked RGB pair uses the same camera pose, eight-metre goal,
neutral instruction and generation settings. The inference payload contains only
the instruction and two RGB PNG byte strings. Case labels, geometry, obstacle
coordinates, evaluation metadata and expected answers stay outside that payload.
The shared instruction names neither a barrier nor a desired hold response.

The proposed model settings retain deterministic generation and twenty new
tokens, with no action mask. The existing compact-city generation grammar forces
positive translation and must not be used to compare whether the model proposes
hold. Raw responses, including invalid text and EOS markers, remain unchanged.
One final `</s>` is recorded separately while keeping the original raw text.
The decoder describes only exact supported proposals; it never repairs an output
or grants authority. Identical responses in both cases remain valid observations.

Real inference requires independent service identity, pinned weights/custom-code
hashes and actual request/response receipts. The helper's outputs alone never
prove inference. It makes at most two callback calls and stops on the first error
without retry. Synthetic CPU callbacks are interface tests only.

The October 11 authorized cloud preflight stopped before VM creation because no
verified cached AeroVLA weights/runtime source was available. The project had no
instances, disks or snapshots; its two custom images were unrelated GR00T runtime
images explicitly without weights. The previous AeroVLA bootstrap downloaded
weights into a disposable boot disk, which had been deleted. No model download,
warmup, GPU VM, comparison inference, WAM or flight was performed in this phase.
Do not treat the available default-region pricing table as a qualified regional
quote. Preserve this abort outcome and the original RGB/evidence archives.

A future trial requires a verified cache and a completed current regional price,
storage and transfer review before creation. The approved attempt limits were one
L4 VM, one warmup and two comparisons, no retries, a ten-minute creation-to-deletion
budget, USD 1 for the attempt and USD 8 in aggregate. No retry extends a deadline.
An offline two-image result would not qualify obstacle avoidance, general learned
performance or operational response freshness.
