# Bounded ANWM output-head adaptation

This experiment adapts the released ANWM checkpoint to the existing untextured
Yokohama scene. It does not change native flight, Rules or dispatch authority.
No comparison against idealized Rules is required. Previous forecast failures
remain failures under their original protocols.

## Input audit

Pinned upstream `657a80268505fa9149c4df502e35aa0f5bce11e5` uses RGB, a 4:3
center crop, 224-square resize, [-1,1] normalization, and VAE scale 0.18215.
The current adapter matches these. Translation is divided by 3.30 m and then
min/max normalized; physical hold is [-1/3,0,0,0], not all zeros. The released
checkpoint has 16 context slots even though the current YAML says four.

The upstream preprocessing notebook uses 512-square images and horizontal
FOV 90 degrees. After its crop, horizontal/vertical FOV is 90/73.74 degrees.
The current sensor is 640x360 at 60 degrees: after the same crop it covers
46.83/35.98 degrees. Camera geometry is correctly provided to projection, but
the learned model sees a different field of view. The city is untextured and
stationary histories contain little motion. These are observed distribution
differences, not uniquely proven causes of the failed predictions.

Upstream timestamps are assigned from waypoint row indices. Dataset rel_t is
(goal_index-current_index)/128, not measured elapsed seconds. Do not equate
native t4 with four seconds. This adaptation declares rel_t=1/128 for **one
commanded static viewpoint transition**, irrespective of the recorded duration.
It does not calibrate a physical-time dynamics predictor.

Sources:
- [Preprocessing](https://github.com/EmbodiedCity/ANWM.code/blob/657a80268505fa9149c4df502e35aa0f5bce11e5/data/preprocessing/Data_preprocessing_airvln_16.ipynb)
- [Dataset](https://github.com/EmbodiedCity/ANWM.code/blob/657a80268505fa9149c4df502e35aa0f5bce11e5/anwm/data/airvln.py)
- [Rollout](https://github.com/EmbodiedCity/ANWM.code/blob/657a80268505fa9149c4df502e35aa0f5bce11e5/anwm/rollout.py)
- [Training](https://github.com/EmbodiedCity/ANWM.code/blob/657a80268505fa9149c4df502e35aa0f5bce11e5/train.py)

## Data and qualification

`collect_yokohama_adaptation.py --approve-simulation --numpy-wheel <wheel>
--output-dir <new-directory>` starts an isolated networkless, CPU-only Gazebo
renderer. It does not start PX4. A gravity-free camera is repositioned by the
simulator; this is sensor rendering, never executed aircraft movement.

Freeze 12 training and four evaluation sites from the authored route before
rendering. Require at least 15 m separation across all source and target camera
positions between splits; all orientations/actions from a site share its split.
Building appearance may still overlap between these areas: this is within-city
adaptation, not unseen-city generalization. No random frame split is allowed.

Capture 16 fresh stationary RGBD frames at 4 Hz, a later hold view at +1 s,
and a new view 2.8 m forward. Join actual pose and RGBD within 12 ms and require
camera target error <=0.03 m / 0.01 rad. Keep all failed collection attempts.
The pinned PosePublisher message has a zero envelope timestamp, so use the
SceneBroadcaster's timestamped `/world/default/pose/info` stream.

`prepare_yokohama_adaptation.py` checks original hashes, roles, poses and times.
It exports past-only input histories and training target images. Evaluation
images remain on the local host. A strict remote asset inventory rejects extra
files, particularly evaluation targets. Future targets are legitimate training
labels; they must never be mislabeled as inference inputs or withheld data.

## Frozen learning operation

Start from the pinned released EMA. Freeze VAE and all ANWM parameters except
`final_layer.fuse_supervised.{weight,bias}` and `final_layer.linear.{weight,bias}`.
Use the native 1000-step diffusion training loss, AdamW lr=0.0001, zero weight
decay, gradient clipping 1, seed 42, and exactly 512 batch-one updates.
Use the past-only appearance-fill projection with immutable metric geometry.
No target image is fed as projection. No adapter is installed into flight.

This is supervised output-head post-training, not LoRA, RL, full-model training,
a new world model, or an image-renderer bypass. Save only the changed head, bind
it to the original checkpoint and protocol hashes, verify the entire frozen
parameter hash, reset/reload the saved head, then infer on the evaluation inputs.

Generate all eight evaluation predictions before and after training with the
same seed, 250 diffusion steps, 16 context frames and transition index one.
Keep the checkpoint fixed at update 512; no test-based checkpoint selection,
threshold adjustment or hyperparameter search is permitted in this run.

For a *limited adaptation feasibility* result, at least one evaluation site
must pass both hold and forward: RGB MAE <=15, <=10% of pixels with any channel
error >40, inference <=75 s, and visual rejection of invented route-changing
structure. Report all eight pairs, including failures. This does not qualify
native flight or general performance, even if the minimum is met. A fresh
flight protocol and appropriate safety/authority verification are still needed.

GPU spending remains within the previously authorized cumulative USD 15.
Require a closed prior receipt, bounded automatic VM deletion, explicit worst
case reserve, evidence collection, and confirmed owned-resource absence.
