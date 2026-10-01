# Bounded attention adaptation after the output-head pilot

This is an opt-in offline continuation of [the head pilot](yokohama-adaptation.md).
It changes no native-flight authority or decision gate. The earlier 0/4 result
remains unchanged. Do not require superiority to Rules.

## Frozen scope

Start with the released checkpoint and the prior saved head with SHA256
`7d69f3d7eccfee89cdd43b0fe22f5622da3d7cc03d2554be64f02d05c1a07222`.
Train only `final_layer.fuse_supervised`, `final_layer.linear`, and
`final_layer.attn.mha` (eight tensors). All other ANWM parameters and the VAE
remain frozen. The added attention layer handles matching projected tokens to
the predicted representation; adapting it is a hypothesis, not a proven cause.

Freeze 2,048 updates, batch one, AdamW lr 0.00005, zero weight decay,
gradient clipping 1, native 1,000-step diffusion loss and seed 42. No checkpoint
selection or test-based tuning during this run. This continues a previously
trained head: differences cannot be attributed to the attention layer alone,
because the number of updates also changes. It is not LoRA or reinforcement learning.

`collect_yokohama_adaptation.py --plan-version attention-v2` recollects the
12 training sites and uses four new evaluation sites at authored segment-one
fractions 0.44, 0.455, 0.47 and 0.485. Both source and endpoint positions must be
at least 15 m from **every** prior source/endpoint, including previous evaluation
sites. Plan v1 remains the default. All data are fresh timestamped Gazebo RGBD
with stationary history and a teleported camera, not AP/aircraft motion.

These four nearby new sites lie in one corridor. Buildings can be shared across
views; the result is neither four independent scenarios nor unseen-city
generalization. Previous evaluated images informed this design and are not
reused as a new held-out test. Only the new 24 training targets go to the GPU;
eight new evaluation targets remain local.

Record eight forecasts with the prior head and eight after the fixed final
update, using identical observations/actions, seed and 250 sampling steps.
Reopen the serialized adapter, compare each finite tensor and its hash, and
verify the full frozen-parameter hash before admitting the recorded run.
One commanded static-view transition is still the time condition, not seconds.

## Minimum result and cost

At least one new site must pass **both** hold and 2.8 m forward: RGB MAE <=15,
at most 10% of pixels with any channel error >40, inference <=75 seconds,
and manual confirmation that route-changing buildings/water were not invented.
Report all eight images. This qualifies only limited offline adaptation; it
does not automatically authorize or qualify native flight, delivery or safety.

Cumulative authorized GPU cap remains USD 15. Bind the new reservation to the
previous closed receipt, use a maximum one-hour auto-delete VM, retain all
failures and verify deletion of the owned instance and disk. Raw histories,
weights, cloud configuration and credentials are excluded from publication.
