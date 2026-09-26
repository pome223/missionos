# Yokohama CPU PX4/Gazebo verification

A source-derived PLATEAU city crop is loaded into Gazebo with explicit collision
meshes and CPU-rendered cameras. The opt-in runner measures contact controls,
then a fixed PX4 route with seven 30-second holds and a return landing.

- [Japanese measured report](REPORT-ja.md)
- [Standalone observed 3D trajectory replay](index.html)
- [Recorded onboard camera timelapse](onboard-timelapse.mp4)
- [Flight verification](verification-flight.json), [contact verification](verification-contacts.json)
- [Integration contract and commands](../../agents/yokohama-sitl.md)
- [Source attribution](../yokohama-urban-scene/ATTRIBUTION.md)

This is one static, zero-wind qualification flight and one contact trial. No native
VLA/WAM, payload delivery, sea transit, energy measurement, or physical flight is
claimed. The historical native ship-flight results remain a separate experiment.

`reproduce/build_report.py` regenerates the public replay from private run directories
that have passed `scripts/verify_yokohama_sitl.py`. It publishes selected observations
and hashes, not raw container inspection or private workstation paths.

```sh
python docs/examples/yokohama-px4-sitl/reproduce/build_report.py \
  --flight /tmp/yokohama-flight --contacts /tmp/yokohama-contacts \
  --output /tmp/yokohama-report
```

The standalone HTML embeds Three.js (MIT notice retained), original source geometry
(CC BY 4.0), measured positions, and selected camera images. Open `index.html` directly
or serve the parent `docs/examples` directory on loopback. The video is a sibling file.
