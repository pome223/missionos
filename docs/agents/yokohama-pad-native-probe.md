# Native occupied-pad image probe

This opt-in diagnostic is separate from the CPU aircraft-requested pad flight.
It has no Executor, MAVLink connection, hardware endpoint, or delivery receipt.
The cloud provisioning controller is private; the public commands never create
billable infrastructure. Default tests use a tokenizer double.

`capture_yokohama_pad_views.py` requires `--approve-sitl` and a previously passed
pad/full-flight/payload run. It copies that world's assets into a disposable,
network-isolated CPU Gazebo container. An authored static rig uses the aircraft's
front RGBD and downward camera transforms; observed rig poses join the RGBD at
12 ms or less. The rig is named `x500_0` to reuse the timestamped observer, but
is **not a PX4 aircraft**. No arrival or AP hold is inferred from this name.

The cases are a far occupied view, a 20 m occupied view, a 20 m clear view, and
reoccupation of the same near scene. The camera is 5 m above the pad datum and
stays outside the 6 m pad exclusion radius. The authored connecting line must
be at least 3 m from every frozen building footprint. This checks geometry, not
flight feasibility. Only owned camera/lead/parcel entities accept pose commands.
Pose acknowledgements do not substitute for measured positions.

Each case supplies a fresh 24-frame exact RGB/depth/down join at 4 Hz. The first
eight frames are discarded consistently with the existing native input loader;
the remaining 16 are the WAM history. Native VLA receives the last front/down
pair. Neither model receives lead pose, occupancy labels, actor schedules or
future outcome images. Case identifiers are evidence metadata; they are not
part of the prompt, pixel input, action conditioning or time conditioning.

`probe_yokohama_pad_models.py` checks input hashes before loading the pinned
AeroVLA or ANWM weights. All cases use the identical prompt and generation-time
grammar: forward bin 0 or 20--58, vertical/yaw bin 49, no LAND token. These are
native zero motion or approximately 1--3 m level forward actions. Both options
are available for every case. The zero action means wait; no positive motion is
rewritten into wait or supplied by a fallback. The grammar is a narrow proposal
contract, not general mission understanding or permission to enter the pad.

ANWM uses the existing motion-v4 adapter, index 1, seed 42 and 250 diffusion
steps. It predicts the hold view and immutable native VLA candidate view. When
VLA returns zero, these are identical actions, not independent alternatives.
The prior static-scene adapter is deliberately tested without additional
training. The actor is stationary during each input history; this is not a
test of predicting continuing lead motion or long-horizon pad availability.

`evaluate_yokohama_pad_models.py` reopens the input/model/forecast hash chain,
unaltered VLA delta, CUDA allocator release and existing past-only visible
structure bounds. Its primary three-step check is wait, forward, wait. The
far case is diagnostic. Reoccupation repeats a scene, so the denominator is
not three independent held-out environments. Passing this probe alone would
not establish a successful mission or adoption. Any later flight still needs
fresh current occupancy, bounded AP holding, Rules, explicit simulator scope,
an observed model-segment arrival, independent cargo receipt and ship return.

Before another paid flight-qualification attempt, run
`screen_yokohama_pad_models.py --inputs INPUTS --output SCREEN.json`. It compares
the past-only reference with itself for hold and both forward-range endpoints.
Exit 2 means even a perfect reference cannot satisfy the fixed bounds; this is
an input/gate incompatibility, not learned-model error. Exit 0 admits further
measurement only, not flight or every intermediate action. The current probe
discovered this eligibility issue after GPU startup; that ordering and the
spent diagnostic cost are retained, rather than claiming a passed preflight.

Failing outputs are retained without model resampling, relabeling or tuning on
these cases. Training is a separate experiment requiring varied observations,
a reserved evaluation set, independent execution checks, and a new cost cap
reservation within the user's cumulative authorization. Better image scores
alone do not establish learned mission value.

The human-readable record is [the diagnostic report](../examples/yokohama-pad-native-probe/REPORT-ja.md).
