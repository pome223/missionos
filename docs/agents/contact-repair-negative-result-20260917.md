# From candidate headroom to contact-limited repair: a bounded negative result

**Status:** research line paused; no repair adoption or mission-success improvement established.

**Evidence cutoff:** September 17, 2026.

**Scope:** one LIBERO book-and-caddy task, simulator-only, progressively selected development states.

## Abstract

This investigation began with an upstream question: can causally different repair
candidates create enough outcome diversity to make selection worthwhile? On a
50-start targeted panel, the recorded incumbent, best fixed family, and hindsight
oracle all succeeded on 29 starts. New interventions added no terminal successes.
Most starts lacked the grasp required to admit those interventions.

The investigation therefore moved from selection to primitive execution. Pose
holding improved local grasp qualification, but composition with transport did
not improve mission outcomes. Native stable free-space grasps could complete a
10 mm lift using the same position controller, whereas repaired grasps showed
asymmetric, displaced, edge-loaded contacts. Supported orientation alignment
followed by a small longitudinal translation improved contact geometry beyond a
matched hold comparator. It also reduced short-horizon drift and grasp-relative
motion, including during a lift.

Those improvements did **not** establish recovery of the mission. Natural contact
with the caddy remained after the lift. A final bounded lateral escape probe
reduced contact force but did not release contact in any of four prepared
endpoints. The book center moved, but rotation kept its contacting corner almost
stationary. The research line was paused at that boundary rather than expanded
into an open-ended contact-controller optimization project.

The negative result is specific: the tested repair sequence did not establish
natural support release or improved mission success under its bounds. It is not
proof that contact-geometry repair, regrasping, world models, or selection cannot
help in other conditions.

## 1. Relationship to MissionOS

The public [candidate-generation proposal, PR #100](https://github.com/pome223/missionos/pull/100)
asks whether distinct executable interventions create a meaningful selection
problem. It follows the public research discussions in
[PR #98](https://github.com/pome223/missionos/pull/98) and
[PR #99](https://github.com/pome223/missionos/pull/99).
This report documents the subsequent diagnosis; it does not amend those PRs or
claim that their proposed qualification gates have passed.

The authority split remains:

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

The experiments described here used local research simulator sessions, recorded
policy actions, deterministic repair commands, and simulator observations. They
were not an integrated production approval/dispatch demonstration. A qualified
grasp, a completed local lift, a contact-release observation, and a verified
mission completion are separate events. No hardware result is claimed.

No new selector, WAM ranking, or experience-learning improvement was established
by this investigation. Mission success in the simulator comparisons refers to
the local terminal conjunction (inside the target region, supported, released,
upright, and stable completion), subject to protected-object preservation. It
is distinct from an operational MissionOS mission-completion claim.

## 2. Experimental setting and denominators

The setting was the LIBERO-10 book-in-caddy task (task index 5), using the
MuJoCo/robosuite simulator and a Panda-style gripper. The book's collision-box
full dimensions were approximately 28.68 × 110.38 × 134.40 mm: thickness, width,
and longitudinal extent. The modeled pad face was 16 × 16 mm.

Control observations were taken every 50 ms. Contact force, object pose, gripper
geometry, and terminal predicates came from privileged simulator state. This is
not a demonstration that a deployed perception system can estimate these
features accurately.

The successive panels were different and must not be pooled:

| Panel | Construction and purpose |
| --- | --- |
| 50 starts | Five book translations around each of ten recorded prefixes; 40 recovery-targeted starts and 10 retention controls. Not 50 independent native episodes. |
| Six starts | Development probes for grasp qualification and replay composition. |
| 16 starts | Additional perturbations around known starts for a gated close-only comparison; not broad task generalization. |
| Eight starts | Pose-hold and transport development panel. Four qualified for the later primitive probes. |
| Four repaired states | `dev-0`, `dev-1`, `dev-4`, `dev-7`; selected after pose-hold qualification. |
| Three native states | Stable free-space grasps admitted from four native episode searches; qualification-conditioned results. |
| Two late-stage states | `dev-4` and `dev-7`, two nearby perturbations of the same recorded prefix. Highly correlated, repeatedly inspected development states. |

Later operations and diagnostic gates were chosen adaptively from earlier
results. A condition frozen before a particular run does not make that run an
independent held-out validation of the overall research process.

## 3. From candidate generation to failed mission composition

### Candidate headroom

The 50-start evaluation proposed 250 arms but executed only 65 admitted simulator
trials. It retained 185 rejections: 180 required an observed grasp and five used
an unsupported primitive. Thus, rejection was not counted as successful execution
or silently removed from the start denominator. On 45 of 50 starts, the four new
proposals could not pass the observed-grasp admission requirement.

| Metric | Result |
| --- | ---: |
| Recorded incumbent success | 29/50 |
| Best fixed family success | 29/50 |
| Hindsight oracle success | 29/50 |
| New candidate successful trials | 0 |
| Preservation-aware success divergence | 8% |
| Predicate-vector divergence | 10% |
| Oracle gain over best fixed | 0 percentage points |
| Unknown outcomes / preservation violations | 0 / 0 |

Some outcome divergence existed, but it did not create beneficial selection
headroom. The oracle was bounded by the admitted candidate set; it was not an
oracle over all physically possible repairs. This result justified withholding
selector work, not declaring selection generally useless.

### Grasp and composition

| Experiment | Baseline | Intervention | Interpretation |
| --- | ---: | ---: | --- |
| Six-state local grasp qualification | Close-only: 2/6 | Position-correction recovery: 0/6 | More control did not improve this local gate. |
| Six-state mission composition | Recorded continuation: 3/6 | Close then replay: 1/6 | One added recovery, three regressions; net −2. |
| 16-state gated composition | Recorded continuation: 12/16 | Conditional close then replay: 9/16 | Zero added recoveries, three regressions. |
| Eight-state local grasp qualification | Close-only: 2/8 | Anchored pose hold: 4/8 | Local qualification improved. |
| Eight-state mission composition | Recorded continuation: 5/8 | Close/replay: 1/8; pose-hold/replay: 3/8; pose-hold/goal feedback: 0/8 | Qualification did not translate into mission improvement. |
| Eight-state clearance composition | Recorded continuation: 5/8 | Direct goal feedback: 0/8; clearance feedback: 0/8 | All four admitted clearance attempts failed during the lift phase. |

The other four starts in the last row did not qualify for transport. Therefore
0/8 does not mean eight executed transport trajectories all failed in motion.
These comparisons recorded no preservation violations, but preservation of
protected objects did not imply preservation of baseline mission successes.

The clearance controller held entry XY while ascending and required observed
release of the designated obstruction contact before advancing to traversal and
descent. Its failure before traversal did not establish that a clearance path
would be ineffective if its lift primitive were executable.

## 4. What a local lift meant

The diagnostic lift used an immutable hand target 10 mm above the post-preparation
hand position in world Z, P=1 position feedback, a maximum 1 mm command norm per
tick, a closed gripper, and a 120-tick budget. Completion required:

- Both hand and book rise between 9 and 11 mm.
- Hand target error at most 1 mm.
- Grasp-relative translation within 1 mm and rotation within 2° of the stable
  window's reference for at least 0.5 seconds.
- Loaded bilateral contact, uprightness, and preservation maintained.

Probe guards included 15 mm total hand/object travel, 3 mm lateral displacement,
and 5 mm grasp-relative translation. Earlier qualification had a separate 5 mm
travel bound. These bounds were phase-specific, not one global mission envelope.

A summed sequence of incremental controller commands is not the desired endpoint
displacement, mechanical work, or a direct actuator-gain measurement. Early
command-to-motion ratios were useful diagnostics but did not identify the cause
of poor tracking by themselves.

**Local lift completion did not require environmental contact release.** Contact
release was subsequently measured as a separate endpoint. It should not be read
as unsupported transport or mission completion.

## 5. Contact ablation and the stable native control

At four repaired states, the contact-retained 10 mm probe completed on 1/4 starts.
A paired artificial contact ablation completed on 0/4. The ablation disabled
book/robot-to-environment collisions while retaining grasp and robot self-contact
semantics. It was a diagnostic simulator intervention, not an executable way to
remove support.

With environment force zero, the book moved laterally by 3.06–3.16 mm while the
hand moved only about 0.33–0.45 mm before the guard stopped the trials. The
ablation itself could create a transient response, so these results did not
isolate free-space controller quality.

A separate native control searched four original episodes without book offsets
or collision suppression. Candidate prefixes were fixed before qualification;
at most the first three eligible prefixes per episode, separated by 30 ticks,
were considered. The first admitted state per episode was used. Six qualification
attempts admitted three states; one episode's three candidates all failed.

Admission required one second of zero environment force, bilateral pad force at
least 0.5 N per side, bounded pairwise hand/object/relative pose variation
(1 mm / 2°), pad-aperture variation at most 0.5 mm, and per-pad force variation
at most the larger of 0.5 N or 20% of its mean. These are development conditions,
not a validated transport-readiness classifier.

| Native admitted state | Hand rise | Book rise | Maximum book lateral displacement |
| --- | ---: | ---: | ---: |
| native-31 | 9.77 mm | 9.71 mm | 0.23 mm |
| native-35 | 9.77 mm | 9.75 mm | 0.26 mm |
| native-9 | 9.76 mm | 9.69 mm | 0.46 mm |

All three completed with zero observed environment contact force and maintained
grasp/preservation. This demonstrated controller capability in these selected
conditions. It did not establish general controller correctness or exclude
configuration-dependent IK/control effects.

## 6. The geometry of repaired versus native grasps

Force imbalance was defined as `abs(L − R) / (L + R)`. Contact centers were
weighted by normal force. Pad-local distances below refer to the modeled contact
face, not the physical fingertip mesh endpoint.

| State | Pad-force imbalance | Pad aperture | Offset along grasp axis |
| --- | ---: | ---: | ---: |
| Native successful states | 0.001–3.61% | 28.52 mm | 0.009–0.057 mm |
| dev-0 | 6.73% | 29.74 mm | 3.36 mm |
| dev-1 | 33.19% | 33.15 mm | 19.58 mm |
| dev-4 | 46.03% | 33.67 mm | 9.52 mm |
| dev-7 | 47.88% | 33.66 mm | 9.42 mm |

| State | Longitudinal mismatch between contact centers | Contact-line angle to book thickness axis | Left/right contact points |
| --- | ---: | ---: | ---: |
| Native successful states | 0.40–1.88 mm | 4.7–13.7° | 4/4 |
| dev-0 | 19.49 mm | 37.3° | 2/2 |
| dev-1 | 16.40 mm | 35.3° | 1/1 |
| dev-4 | 22.57 mm | 45.6° | 1/1 |
| dev-7 | 22.54 mm | 45.6° | 1/1 |

Native force-weighted pad centers were 1.16–3.53 mm from the pad-face center.
Repaired centers were 8.23–11.30 mm away. In dev-1 both sides were near the distal
pad edge. In dev-0/4/7 the left side was near the proximal edge and the right side
near the distal edge; dev-4/7 were corner contacts.

This was more specific than insufficient grip force: the repaired state often
contained displaced, oblique, edge-loaded bilateral contacts. However, contact
point count in MuJoCo is not a measurement of real contact area or wrench capacity.
Also, dev-0 could complete a contact-retained lift despite edge loading. These
seven states did not justify a universal threshold or causal classification.

## 7. Temporal diagnosis: lift was not necessary for the initial drift

Replay of eleven existing lift arms reproduced book positions and orientations
exactly at the recorded observation ticks. The proposed sequence “contact center
moves to an edge, then rotation, then lateral drift” was not observed as a common
ordering. In dev-4/7, contact centers were already at corners and barely moved
while the book displaced. Rotation and lateral drift appeared within the same
first 50 ms sample, which cannot resolve their sub-tick ordering.

A subsequent paired ablation compared pose hold with lift from identical prepared
states, using the same closed gripper, feedback gains, and guards. Only the
vertical target differed. Early-stopped pairs were compared at their common time.

| State | Common duration | Book lateral displacement: hold / lift | Relative rotation: hold / lift |
| --- | ---: | ---: | ---: |
| dev-0 | 300 ms | 1.93 / 1.75 mm | 0.962 / 0.962° |
| dev-1 | 300 ms | 2.17 / 2.23 mm | 0.800 / 0.800° |
| dev-4 | 200 ms | 3.09 / 3.10 mm | 1.169 / 1.169° |
| dev-7 | 200 ms | 3.14 / 3.15 mm | 1.172 / 1.171° |

Lift input was not necessary for the early drift in these ablated states.
Force imbalance decreased during some failures, while native successful grasps
redistributed their force-weighted contact centers without large object-relative
motion. Neither force balance nor contact-center displacement alone was a
sufficient stability indicator.

The evidence supports dependence on external support in the tested states, but
does not separate all effects of abrupt contact removal, stored contact loads,
compliance, friction, or configuration-dependent control.

## 8. Supported alignment and the coordinate-sensitive guard

The first alignment operation held hand position and closed the gripper while
rotating the hand's grasp axis toward the book thickness axis. It used a frozen
shortest-rotation target, at most 20° target change, 0.5° rotation command per
tick, and 0.5 mm positional correction per tick.

A raw change in object-in-hand translation conflated deliberate relative
reorientation with unwanted motion. Under the explicit hypothesis that the book
should remain stationary in world coordinates during supported alignment,
expected relative pose is:

```text
expected_object_in_hand(t) = inverse(observed_hand_world(t)) * object_world(entry)
```

The residual was evaluated against that expected transform, using **observed**
hand pose rather than subtracting commanded angles. Its translation norm equals
book world displacement; it is not an independent sensor of “true slip.” The
supported phase retained 5 mm hand/book world-travel guards and introduced a
2° book world-rotation guard. After support removal, the original grasp-relative
guards applied again. A rigidly attached hand and book would keep their relative
pose constant; supported reorientation deliberately does not assume that attachment.

The original three-second operation stopped dev-4/7 at the raw 5 mm relative
bound. With the reference-specific guard, both ran the full three seconds but
still missed alignment. Dev-1 stopped on actual hand travel over 5 mm; that was
not a coordinate artifact.

The exploratory geometry gate required, over a final 0.5-second window:
longitudinal mismatch at most 5 mm, contact-line angle at most 15°, both pad
centroid perimeter margins at least 2 mm, and relative-pose variation within
1 mm / 2°. It was a development gate, not a certified readiness predicate.

### Five-second extension

Only dev-4/7 were extended from three to five seconds, without changing gains or
guards. Their first three seconds matched the prior run exactly. The book's
rotation peaked and then decreased, so linear extrapolation of earlier rotation
would have incorrectly predicted that the 2° guard must fire first.

| Final measurement | dev-4 | dev-7 |
| --- | ---: | ---: |
| Hand target orientation error | 0.16° | 0.15° |
| Book world rotation | 0.52° | 0.54° |
| Longitudinal contact mismatch | 15.93 mm | 15.99 mm |
| Contact-line angle | 29.5° | 29.6° |

The hand nearly reached its orientation target, but edge loading remained.
Neither state passed the geometry gate. A supported hold-only control stopped
around 3.2 seconds at the 5 mm hand-travel guard. Orientation arrival and contact
redistribution were therefore distinct problems.

## 9. A directional intervention improved contact geometry

From independently reproduced five-second alignment endpoints, three arms were
run: hold, book-longitudinal positive translation, and negative translation.
The translation targets were 1 mm for two seconds, then 2 mm for two seconds,
with a 0.25 mm command-norm cap. Attained orientation was held. Original supported
world-travel and rotation limits were retained rather than reset.

The book-longitudinal positive direction was approximately downward in world
coordinates here. It was **not** the world +X escape direction used later.

| Arm / time | dev-4 mismatch | dev-7 mismatch |
| --- | ---: | ---: |
| Entry | 15.93 mm | 15.99 mm |
| Hold, four seconds | 9.14 mm | 9.31 mm |
| Positive, 1 mm target / two seconds | 6.23 mm | 6.32 mm |
| Positive, 2 mm target / four seconds | 4.56 mm | 4.60 mm |
| Negative, 2 mm target / four seconds | 9.75 mm | 9.87 mm |

Positive translation improved geometry beyond natural hold evolution. Negative
translation still improved relative to entry, but less than hold; it should not
be described as an absolute worsening from the initial state.

At the positive endpoint, contact-line angles were about 10.7° and pad centroid
perimeter margins about 5.7–5.9 mm. Both states satisfied the previously defined
geometry gate when the final window was evaluated. Grasp and preservation were
maintained. The hand actually displaced only 0.43–0.45 mm along the book axis
(about 1.10 mm in total), despite a 2 mm target. Command targets were not reported
as realized motion.

## 10. Did better geometry improve behavior?

### Artificial support removal followed by 300 ms hold

The comparator was **alignment plus four seconds of hold**, not the original
unaligned repair state. The intervention was alignment plus four seconds of
positive translation. Prepared trajectories matched their earlier records.

| Measurement | Alignment + hold | Alignment + translation |
| --- | ---: | ---: |
| Book lateral displacement, dev-4 | 0.365 mm | 0.207 mm |
| Book lateral displacement, dev-7 | 0.367 mm | 0.207 mm |
| Relative rotation, both states | About 0.20° | About 0.10° |
| Relative translation, both states | About 0.29 mm | About 0.16 mm |

All four arms completed with zero environment force and maintained grasp and
preservation. The intervention reduced measured drift in these states. However,
the matched control was already stable over this short interval. The comparison
did not convert a failed endpoint into a successful one, nor prove that geometry
alone caused the difference: posture, loading, and velocity also changed.

### Artificial support removal followed immediately by 10 mm lift

| Measurement | Alignment + hold | Alignment + translation |
| --- | ---: | ---: |
| Lift completion | 2/2 | 2/2 |
| Completion time | 3.4 s | 3.4 s |
| Book rise | About 9.54 mm | About 9.53 mm |
| Maximum book lateral displacement | 1.91 mm approximately | 1.57 mm approximately |
| Maximum relative translation | 0.88–0.89 mm | About 0.53 mm |
| Maximum relative rotation | About 0.44° | 0.30–0.31° |

The added translation improved local motion quality, but not completion rate or
time. This comparator had no observed lift-success headroom. These results do
not justify a success-rate superiority claim or a selector benchmark.

### Natural-contact lift

The same preparation and lift were repeated without collision suppression.

| Measurement | Alignment + hold | Alignment + translation |
| --- | ---: | ---: |
| Local lift completion | 2/2 | 2/2 |
| Book rise | 9.02–9.04 mm | 9.18–9.20 mm |
| Completion time | 4.00–4.05 s | 3.65 s |
| Maximum relative translation | 1.06–1.08 mm | 0.84–0.86 mm |
| Final book–caddy force | 1.06–1.07 N | About 0.64 N |
| Environmental contact release | 0/2 | 0/2 |

Local improvement persisted, but the book remained in contact with the caddy.
A completed 10 mm diagnostic lift was not a successful unsupported extraction.

## 11. Contact direction and the final escape probe

Exact replays of all four natural-contact lift endpoints found a corner of the
book contacting the same caddy side geom, `desk_caddy_1_g4`. The environment-to-book
normal was world +X, with dot product zero against world +Z. Contact normal sign
was interpreted using the [MuJoCo contact convention](https://mujoco.readthedocs.io/en/3.2.6/XMLreference.html).

Static rigid-translation queries predicted that +X by 0.5 mm would open roughly
0.499 mm of separation, while +Z by up to 2 mm would not open a gap to that side
face. Some generic distance queries returned inconsistent zero distances after
virtual displacement. An independent oriented-box separating-axis calculation
against all 17 caddy boxes corroborated the directional result. This was a
geometric calculation, not an IK, grasp-coupling, or dynamic feasibility proof.

The final experiment replayed the four endpoints and compared matched hold with
a fixed world +X target of 0.5 mm. Translation commands were capped at 0.1 mm per
tick, with a 60-tick maximum. Height and attained orientation were held. Existing
lift guards remained, with additional endpoint-relative hand X excursion at most
0.5 mm and Y/Z deviations at most 1 mm. No extra upward target was commanded.

The protocol would freeze attained hand position at the first clear-contact
observation and require a further 0.5 seconds of clear hold. Clearance required
zero observed book-environment normal force, no penetrating book-environment
contact, and more than 10 micrometers of separating gap against every caddy box.

| Preparation | Hand +X motion | Book-center +X motion | Final contact force | Release + hold |
| --- | ---: | ---: | ---: | ---: |
| Alignment + hold, two states | 0.463–0.467 mm | 0.177 mm approximately | 0.663–0.674 N | 0/2 |
| Alignment + translation, two states | 0.208–0.210 mm | 0.071 mm approximately | 0.485–0.488 N | 0/2 |

All eight trials, including the four matched holds, completed their budgets and
maintained loaded grasp and preservation. None released contact. Hold alone also
reduced contact force somewhat, but retained contact.

The book's minimum-X corner moved only about 0.00007–0.00030 mm in the escape
arms, despite center motion of about 0.07–0.18 mm. Book orientation changed by
about 0.05–0.12°. This explains why ideal rigid translation was a poor prediction
of the realized contact response: rotation kept the contact-side corner nearly
stationary. It is consistent with pivot-like motion but is **not** a fitted
instantaneous screw axis or a causal proof of a fixed pivot. A proposed final
rotation-axis fit was not performed and is not claimed here.

## 12. Interpretation, limits, and stopping decision

The strongest supported findings are:

1. The evaluated candidate set had zero oracle gain over the best fixed family.
2. Local grasp qualification could improve while mission composition regressed.
3. Selected native free-space grasps could execute the lift; repaired grasps had
   measurably different contact geometry and behavior.
4. Orientation alignment and contact-center redistribution were distinct.
5. A directional intervention improved geometry beyond matched hold evolution
   and reduced local drift, without improving ablated lift success rate.
6. Natural contact release remained unachieved by the tested bounded sequence.
7. A static separating direction did not imply that the grasp would transmit a
   corresponding rigid translation to the book.

The study does not establish a generally valid transport-ready predicate. Its
geometry gate rejected a control that nevertheless succeeded at a short ablated
lift. The late development states were correlated, adaptively selected, and not
held out. No confidence interval or broad population-level effect is supported
by two nearby states. Artificial contact ablation is a strong intervention with
its own transients. Thresholds were experiment design choices, not calibrated
safety certificates. Numerical simulator contact effects may not transfer to
hardware.

The stopping decision was to pause this line rather than continue tuning gains,
extending budgets, or relaxing bounds until a favorable result appeared. Local
improvements should be retained as findings, not promoted into a mission-repair
capability claim. Possible future work is a separately scoped primitive that
establishes a suitable grasp earlier, or explicit supported regrasping. Neither
has been implemented or validated by this report.

## 13. Evidence availability and verification

This is a reviewed English summary of privately retained simulator protocols,
action journals, per-tick contact measurements, reset readbacks, and audit
outputs. It intentionally does not publish raw episodes, simulator state
snapshots, private task data, machine paths, endpoints, or execution harnesses.
The state labels in the tables identify bounded comparisons; they are not
sufficient inputs to reproduce the trajectories.

During the investigation, paired arms used independently restored starts;
prepared-state digests were compared, and reused action prefixes were checked
against recorded book positions and orientations. The report was checked against
the retained summaries and selected per-tick records. Exact replay checks support
consistency of those runs, not independent replication or external validity.
An early contact-control summary had a stale arm-name aggregation; audited rows,
rather than that summary's success count, are used here. Failed geometry-analysis
attempts and the subsequent distance-query correction were retained locally.

Public readers can inspect the methods, denominators, endpoint definitions, and
reported comparisons, but **cannot reproduce or independently audit the robotics
numbers from this documentation alone**. The public fixture paths in PR #100 do
not reproduce these simulator results. No private-run command is presented as a
publicly executable reproduction recipe.

This publication changes documentation only. Documentation validation checks
formatting and local links; it does not rerun the simulator or establish an
operational MissionOS runtime boundary. See the accompanying PR's
**E2E / Runtime Verification** section for the exact publication checks and their
limitations.
