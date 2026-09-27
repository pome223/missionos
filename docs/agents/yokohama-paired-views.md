# Yokohama withheld-view diagnostic

The opt-in CPU capture extends the city fixture flight with **evaluation-only**
RGBD streams. It does not enable learned control. Independent geometry Rules,
fresh authorization, PX4 mission ACK, measured arrival, and session revocation
retain their existing meanings.

## Capture boundary

Run `scripts/yokohama_sitl.py --phase flight --decision-backend fixture
--capture-paired-views --approve-sitl --output-dir <new-directory>` with the
optional scene dependencies installed. Native and contact modes reject the
paired-capture flag: the bounded in-memory sensor history cannot guarantee the
first post-input frame after a slow native call.

At D1 and D2, record the existing 24-frame WAM input, then a 24-frame hold outcome
before authorization, then a 24-frame endpoint outcome after measured arrival
and two seconds of settling. All captures use exact RGB/depth timestamps at
4 Hz and measured Gazebo poses within 12 ms. Evaluation capture fails if the
first post-cutoff frame has fallen out of the buffer. The active AP hold remains
checked throughout capture. Output uses `yokohama_rgbd_evaluation.v1` with empty
history indices, `future_frames_included=true`, and `evaluation_only=true`;
`load_capture` rejects that schema as model input.

`scripts/verify_yokohama_paired.py --root <run> --export-dir <new-directory>`
reopens hashed capture assets, binds the pair to the recorded activated permit
and WAM mailbox, verifies input arrays exactly, and separates `inputs/` from
`heldout/`. The hold evaluation frame is the fourth post-input frame (+1 observed
simulator second); the endpoint evaluation frame is the first after settling.
Their poses must be within 0.30 m / 0.05 rad of the **original requested** camera
target. Actual future poses never replace the model's requested target.
The original native and SITL verifiers must also pass.

## Appearance hypothesis

`fill_infinite_appearance` is an offline diagnostic, not an enabled native
inference mode. It uses only the last past RGB image and raw depth. Reverse
rotation maps target rays to source rays at infinite distance. Only positive
infinite depth on upward rays (negative world NED Z) can supply appearance.
NaN, negative infinity, zero depth, horizontal/downward rays and out-of-view
rays remain unfilled. The upstream projection's metric-valid pixels are
unchanged at sensor resolution. Resampling can blend boundary colors.

The separate appearance mask must never become a metric depth, visibility,
collision-clearance, or free-space mask. An infinite-range return is only an
appearance hypothesis; it is not general semantic sky recognition.

## Frozen offline evaluation

Use two locations, hold and the fixed fixture translation at each, and three
conditions per pair: original projection/t4, appearance projection/t4, and
appearance projection/t1. Keep seed 42, 250 diffusion steps, released checkpoint
and 16-frame context fixed. There is no t0 condition, whole-current-image
substitution, future-image upload, model retraining, candidate rewrite, or
post-result threshold adjustment.

Dataset time offsets 1 and 4 are not verified physical seconds. The source
training loader uses a frame-index difference divided by 128; its metric
translation normalization uses waypoint spacing 3.30 m. Report the actual
elapsed observation time separately, especially for the endpoint capture.

For this conservative near-view appearance pilot, predeclare RGB MAE <= 15,
no more than 10% of pixels with any channel error > 40, and inference <= 75 s.
A condition must pass all four pairs and visual inspection for invented water,
skylines or changed route geometry before considering a new native-flight
protocol. These are appearance criteria, not navigation success or generic
safety guarantees. A numeric pass alone never authorizes dispatch. Preserve all
conditions and failures; keep prior runs' looser consistency metrics unchanged.

The GPU payload contains only past history, the original candidate requests,
the last raw depth and frozen source/protocol. Keep evaluation frames local
until inference has finished. Bound instance lifetime, include disk/IP/transfer
reserves in the cumulative estimate, collect evidence, delete owned resources,
and verify absence before closing the estimate.
