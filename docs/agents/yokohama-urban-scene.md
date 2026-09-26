# Yokohama urban scene contract

The [public scene bundle](../examples/yokohama-urban-scene/REPORT-ja.md) is a static,
source-derived scene design. It is not a native model receipt, simulation flight,
mission permit, delivery receipt or field deployment. No runtime controller or
SITL world was changed by adding the bundle.

## Inputs and coordinates

`reproduce/sources.json` pins seven ZIP members of the official Yokohama archive,
including two building meshes, two road meshes, two bridge meshes and one TIN
member. Acquisition is opt-in, bounded below 50 MB compressed, conditional on
ETag, and checked with exact Content-Range, member CRC and SHA256 before parsing.
A missing cache without `--allow-download` rejects. A corrupt cache rejects
without retrying over the network. This is a frozen scene builder, not a general
CityGML importer. Original compressed members remain outside the repository.

CityGML EPSG:6697 positions are latitude, longitude, source vertical height.
Horizontal transformation uses EPSG:6668 → EPSG:6677 with `always_xy=True`, then
subtracts the projected origin at lon 139.6442, lat 35.4481. Z is unchanged.
Scene JSON, OBJ and route use east/north/up metres. GLB uses east/up/south metres.
The JSON has explicit source CRS and origin metadata. Never pass source Z directly
to PX4 home-relative or AGL commands.

The 600 m square selects whole intersecting buildings and bridges, and intersecting
road polygons / TIN triangles. Some geometry extends beyond the nominal square;
there are no invented closing walls at its boundary. Use LOD2 building surfaces
with interior rings preserved; the LOD1 fallback is unused in the published crop.
Buildings, bridges and terrain are source meshes. Roads retain source LOD1 Z=0 in
JSON/GLB; the HTML's nearest-TIN road drape is explicitly a display aid. Water is a
synthetic reference plane, not a source coastline polygon or tide model.

## Authored route and collision proxies

The three judgment points and delivery pad are authored scenario elements. Route
selection screened candidate paths against source-derived building projections;
the selected route is frozen in `reproduce/make_route.py`. No model chose it.
D1/D2/D3 are mandatory candidate observation locations. The route is also split
into 18 proposed legs of at most 20 m. No leg is an execution permit, and no fixed
subdivision establishes that a VLA can execute that displacement.

`buildings-bridges.obj` contains original triangulated surfaces, without a
watertightness claim. `collision-prisms.obj` extrudes each building/bridge's entire
XY projection from its source minimum to maximum Z. It therefore conservatively
fills vertical openings and varying roof levels; horizontal courtyards remain.
A 0.02 m horizontal expansion followed by 0.005 m topology-preserving simplification
removes numerical slivers. The builder asserts containment of the original
projection. These values clean topology, not source accuracy or wind margins.
The collision proxy passes edge-incidence and winding checks but has not been
loaded into a simulator collision engine.

The reported 7.698 m clearance is to the original projection at the route's height
band, not the expanded OBJ or actual vehicle envelope. The separate 1 m radius
calculation is an illustrative margin. The 101 ground samples are not a continuous
terrain-clearance proof. D1→D3 and D2→delivery occlusion is also verified against the
original 3D triangles, independently of the conservative projection. Visibility
of a point is not recognition of a building or an action-conditioned forecast.

## Rebuild / runtime boundary

See the report for dependency installation and the `reproduce/rebuild.py` command.
Use a dedicated Python environment and an explicit work directory. The packaged
CLI was rerun against the pinned input cache, without downloads or inference;
153 LOD2 buildings, 79 roads, 4 bridges and 35,916 source triangles were recreated.
Missing and corrupt cache cases must reject before geometry generation. Do not
run the validation stages with Python `-O`, which disables their assertions.

The standalone HTML embeds Three.js and scene data. Inspect its actual WebGL
rendering and control behavior in a browser, not only its DOM. The generated GLB also passed Khronos glTF Validator 2.0.0-dev.3.10
with zero issues; the separate receipt names that version. The rebuild retains that
receipt only if its GLB hash equals the published asset. This is not simulator
collision certification. `geometry-checks.json` and `browser-checks.json` state what ran.

## Next integration gate

1. Load source visual meshes and conservative collision proxies into an opt-in
   stationary-world simulator. Validate coordinate mapping, depth observations,
   ground contact and building contact before connecting a flight controller.
2. Verify model-free AP transit and stable hold at an inland D1. Author the actual
   offshore connection separately; this bundle contains no verified 1 km route.
3. Replace/extend the current colored-pillar WAM decoder for generic building
   observations. Freeze acceptance cases and absolute success/latency/cost limits
   before paid model work. Superiority over idealized Rules is not required.
4. Bind native observations, model decisions, Rules, permits, dispatch ACKs and
   independently verified motion to the urban loop. Revoke/stop inference before
   sea handoff. Preserve the native-flight and fixture evidence distinctions.

Geometry/derived assets use the source CC BY 4.0 attribution in the bundle.
Builder/viewer code uses the repository's code license; Three.js keeps its MIT
notice. Record the source's 2024-catalog/2025-README discrepancy. Import no private
flight logs, local paths or credentials into this public bundle.
