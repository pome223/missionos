# AP motion data: CPU admission before further post-training

The previous head, attention and final-block studies used stationary camera
histories. Their failed qualification remains unchanged. This opt-in capture
records actual PX4/Gazebo motion on the existing fixed city AP route. It does
not enable model control, train a model, or change native flight admission.

Use `scripts/yokohama_sitl.py --phase flight --capture-motion-views
--approve-sitl --output-dir <new-directory> --timeout-seconds 1200`.
A decision backend or contact mode is incompatible. Keep the pinned simulator's
one-time disarmed heading initialization and independently observed alignment;
never continuously feed Gazebo ground truth to PX4. All seven 30-second holds,
return, landing/disarm and the existing geometry verifier still apply.

Record only outbound phases D1→D2→D3→delivery, at 4 Hz. Fixed moving intervals
are route fractions 0.10–0.55, 0.48–0.94 and 0.20–0.70 respectively, selected
from the current measured pose and authored endpoints, never from image quality.
Keep at most eight seconds
of each observed AUTO_LOITER interval. Recording is lossless zlib-compressed
640×360 RGB and axial float32 depth with actual onboard intrinsics and separate
sensor/pose timestamps. Nearest Gazebo pose must be within 12 ms. The logged
PX4 sample is explicitly a nearby polling observation, not a synchronized state
at every camera timestamp. Do not interpolate sensor/pose records or fill gaps.
Float32 depth bytes are grouped into four byte planes before compression and
restored exactly before raw-hash verification. Limit capture to 512 frames and 200 MiB, maintaining 96 MiB free space. Exceeding
a bound fails the owned simulator run; prior evidence is retained.

The read-only `scripts/verify_yokohama_motion.py --root <run> --output <json>`
reopens compressed and raw hashes, encodings, actual intrinsics and poses.
It evaluates a fixed 1 Hz grid: 16 consecutive past frames, then **exactly** the
fourth subsequent frame (+1 observed simulator second). Keep every considered
window and its rejection. Sensor intervals must be 0.25±0.004 s, within one
recorded flight phase. Do not choose a later image to improve alignment.

Choose an image target from the past only. Hold requires ≤0.15 m motion over
the final 0.75 s and ≤0.03 rad rotation. Forward requires 1.95–2.55 m forward
motion, ≤0.15 m sideways, ≤0.10 m vertically and ≤0.03 rad rotation over that
same past interval. Its target is exactly 3 m forward with unchanged camera
orientation, using the existing short-action frame convention. Require actual
future target error ≤0.30 m and rotation error ≤0.05 rad. These targets are
**offline prediction candidates**, not separately issued AP commands: only
the authored fixed AP route was dispatched. Future poses cannot rewrite them.

CPU admission requires the full SITL verifier plus ≥4 qualified forward windows
and ≥1 hold window. This demonstrates acquisition and input/target alignment,
not forecast quality. Overlapping windows are correlated and are not independent
trials. The unpartitioned acquisition archive is not a model payload; the native
stationary-history loader must continue to reject it.

Before paid inference/training, separately freeze a nonoverlapping train/test
partition, include all historical adapter/data provenance, prevent future-image
leakage, and check the remaining cumulative USD 15 budget against a bounded
resource lifetime. No learned-control claim follows from CPU admission. AP
remains the sea-leg controller; this dataset has no sea leg or payload release.

The first development run reached the 200 MiB bound during D2→D3 and stopped,
with its container removed. Preserve that failure. Byte-plane compression and
predeclared spatial recording windows address storage; no matching thresholds
or aircraft controls were relaxed.

## Bounded motion-v4 continuation

The CPU geometry screen completed before any motion-v4 model work. Delivery's
initial AUTO_LOITER frames were still settling, so the partition uses the first
four qualified D1→D2 forward windows, first six delivery-leg forward windows,
and first two D2 holds for training (12). The five test records are the first
qualified forward windows at D2→D3 progress 0.60/0.70/0.80/0.90 (each within
+0.04 and with cutoff indices at least 20 frames apart), plus the first D3 hold.
The two nearby training holds and overlapping training histories are correlated.
This partition was fixed after inspecting pose qualification, before generating
model predictions. It does not change the capture or image acceptance bounds.

`prepare_yokohama_motion.py` requires the complete SITL and motion verifications.
It exports 16 past frames per sample with shared, lossless compressed assets.
Training targets are included; test future images and actual future poses remain
on the host. Requested targets are independently reconstructed from the last
past pose. No test target frame may appear in *any* uploaded input history,
including another test history. All train/test history positions and requested
endpoints must be at least 15.6 m apart, reserving 0.6 m for the two bounded
actual endpoint errors. Inventory and hashes are checked before GPU imports.

`train_yokohama_anwm_head.py` accepts `motion-v4` only with the pinned block-v3
adapter, 24 trainable tensors, 2048 updates, seed 42 and learning rate 0.00005.
The evaluation uses moving AP histories in a previously inspected city region;
it is not an unseen-city or pristine held-out scene claim. Old training and
failure provenance remains part of this diagnostic. A model transition index
of 1 is evaluated against +1 observed simulator second here; this does not
establish a general physical-time calibration.

The limited image qualification requires **all five** fixed cases to satisfy
RGB MAE ≤15, fraction of pixels with any channel error >40 ≤0.10, inference
≤75 s, and visual preservation of route-relevant occupied geometry. Small
texture/blur differences alone need not fail. These are separate recorded
trajectories, not paired counterfactual hold/forward commands from one anchor.
Passing would qualify only this offline motion-record diagnostic; native
VLA/WAM city flight remains separately unqualified.
