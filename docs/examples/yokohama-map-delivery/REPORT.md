# Yokohama 3D map delivery

A predeparture marker sets the goal. The server validates candidate terrain,
route reachability and collision margins within the existing 30 m candidate
region. The operator approves a plan bound to goal, route, scene and runtime.
Changing the goal invalidates approval; flight-time edits are rejected.
The demonstrated goal was 10 m east of the original delivery pad.

## Recorded simulator outcome

One CPU PX4/Gazebo delivery completed cargo receipt, ship return, landing and
disarm. Four applicable verifiers passed: decisions, pad queue, payload and SITL.
The fifth optional pad-advisory verifier is not applicable because that advisory
was not enabled. This does not omit the Rules queue or cargo/return checks.

Jev (`jev-1.13.0`) made one actual HTTP decision, returning `continue`, in
205.088 ms. It added no waiting and never granted dispatch authority. Independent
Rules retained authority. City VLA/WAM decisions used CPU fixtures; native
VLA/WAM inference, GPU execution and physical aircraft were not exercised.

All 13 holds passed with unchanged 3 s settle, 30 s hold and 0.6 m altitude-error
bounds. PAYLOAD-CLIMB maximum sampled vertical error was 0.05333 m. This single
run is not a guarantee of performance across conditions or a causal explanation
of earlier failures. Background altitude collection remained separate from the
control loop; 514 of 4,116 diagnostic rows were fresh for comparison. Provider,
receiver, collector and owned simulator processes were cleaned up.

The recorded flight used the reviewed execution snapshot. This publication
preserves the aircraft control and safety contracts, but disables live Jev and
credential loading. It supplies no private grant, ledger or automatic initializer;
the consumed original grant remains separate and unchanged. Offline mock tests
retain irreversible reservation, replay, restart and crash-boundary coverage.

## Execution snapshot and public source

The retained flight used private execution commit
`04ed47cf04f0bd94540fb73a994bb0e3aab0ee5b`, which is not a public checkout target.
The [source correspondence receipt](source-provenance.json) compares all 55
approved runtime hashes with that commit's Git blobs and this publication;
all 55 recorded hashes match the private execution source. Of 56 audited files
(including the separate launcher), 53 runtime files remain byte-identical.
The three differences close the private live boundary:

| File | Public change |
|---|---|
| `src/intelligence/yokohama_jev_live.py` | Disables live sends, replaces private grant identity, and removes the private-account default grant location. Mock tests inject an absolute isolated ledger path. |
| `src/intelligence/yokohama_pad_jev.py` | Rejects live configuration before credential, ledger or HTTP access. |
| `scripts/run_yokohama_jev_live_gateway.py` | Rejects launch before loading credentials. |

The mission uploader SHA-256 is
`b3673e9edc9ac89e0397937ccb18a63b9fd3aef5ecdc86cc97c34a3ae01b25af`
in the saved approval, private snapshot and public source. Flight worker,
altitude conversion, Rules, goal planning, cargo receiver and return verification
also retain their recorded runtime hashes. The receipt contains no private
ledger, grant identity, runtime logs or credentials.

The remaining publication changes add the camera exporter/media, documentation,
mock/export tests and the CI geometry dependency. They do not establish another
simulator flight. The public HTTP/browser fixture smoke is a separate check of
goal selection, approval, fixture receipt/return and UI rendering. It is not
PX4/Gazebo flight evidence and did not invoke Jev. The one retained full flight
and the public fixture smoke must be read as distinct observations.

## Video

The video and preview depict the **Yokohama City / Project PLATEAU** 3D city
model, licensed under **CC BY 4.0**. MissionOS selected the neighborhood,
transformed coordinates and geometry, added display materials and conservative
collision proxies, and added the demonstration route/pad. These modifications
are not municipal or MLIT-authored plans. [Full attribution, source and license](../yokohama-urban-scene/ATTRIBUTION.md)
apply to the derived video and preview as well as the scene. Credits and the
source dataset URL are visible in the video and preview themselves.

`camera-timelapse.mp4` is an offline export of this run's actual saved simulator
camera images at **12× simulation time**, with no audio. The primary stream is
634 onboard images from simulation 4.336 s to 1334.252 s. The exporter makes no provider/model invocation claim from camera images alone;
Jev execution above is established separately by the verified run evidence.
Their spacing is
1.748–2.500 simulation seconds. A previous camera image is held until the next
recorded sample; no intermediate camera motion is synthesized. Any source gap
above 3 s is explicitly labelled by the exporter.

The inset uses the same run's recorded pad-queue/delivery camera images and only
a prior frame within 2 s; inset camera timestamps are shown. All source images
were validated against the saved SHA-256 hashes. No other run is mixed in.

This is a **saved simulation camera image timelapse**, not a recording of the
3D map GUI or an unbroken high-frame-rate flight recording. Simulated time and
playback speed are shown, and landing/disarm are established by verifier
evidence rather than by interpreting the video alone.

The public export receipt records video codec, size, duration, source timing and
hashes. This export is 110.92 s, 640×480, 2662 encoded
frames and 3,174,886 bytes (3.03 MiB), within the 8 MiB preparation cap. Raw runtime logs, private database, budget identity and credentials are
not part of this publication. H.264/yuv420p and MP4 fast-start are used for browser
compatibility. The uploaded GitHub README player was checked in an anonymous
Chrome browser: controls were present, duration was 110.92 s, resolution was
640×480, playback advanced and no media error occurred. After a 315 s wait, the
old temporary redirect returned 403; the canonical GitHub attachment returned
200 with the same MP4 hash and a new browser played it. These observations
verify reopening after expiry, not uninterrupted playback in an indefinitely
open tab.

## Opening the attached video

The README embeds the [original GitHub video link](https://github.com/user-attachments/assets/2230d559-6cf9-4b80-b559-25ffa761ec21), and also provides
an explicit link to open it afresh. It never saves a signed storage redirect.
GitHub attachment requests can redirect to storage URLs valid for 300 seconds.
An expired redirect is distinct from a missing video: reopen the original GitHub
link or reload the README to request the current redirect. Do not bookmark or
share the temporary storage address. A [repository MP4 copy](camera-timelapse.mp4)
is available as a fallback. This does not guarantee indefinite playback in a tab
left open or control GitHub/browser caching.

## Offline verification

The initial public snapshot passed 3,929 tests, changed-file lint and source
type checking. Its loopback HTTP fixture smoke exercised the production
Gateway boundary; anonymous Chrome rendered the GUI and completed the fixture
flow. Subsequent review changes require their own tests and CI results, recorded
in the PR rather than inheriting that earlier test count. The smoke exercises goal selection → approval → fixture
runner → cargo/return verification without a simulator or provider API. Tests
of the provider path inject mock responses; they are not real Jev invocations.
No new flight or external API call is needed to export this recording.

```sh
python -m pip install -e ".[dev,urban-map]"
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src \
  python -m pytest -q
python scripts/yokohama_map_server.py
```

The loopback server defaults to `http://127.0.0.1:8897`; fixture execution requires
`MISSIONOS_YOKOHAMA_MAP_BACKEND=fixture` and `RUN_MISSIONOS_YOKOHAMA_MAP_FIXTURE=1`.
Open `/missionos/yokohama/map`, select a safe goal, approve, run and check the
completion status. This local run is explicitly labelled fixture.
