# Saved Starship mission media

Open `index.html` directly for the offline CG replay. Its data and scripts are
embedded; no service, provider, simulator or private record is needed. The
three MP4s and posters are local companion files.

The nominal numerical flight covers launch, stage separation, 26 recorded
payload-separation states, orbital propagation and Ship return.
`nominal-flight.gif` previews the complete 36-second movie at twice playback
speed in 18 seconds, including the payload-phase counter from 0 to 26. It does
not claim that each payload or V3-specific deployment hardware is visually
resolved. The full MP4 and saved-state replay retain the original display.

These are historical numerical records. Nominal Ship contact is not a verified
landing or booster tower catch. The deployment intervention confirmed a skip
sequencer operation with zero payloads released. The isolated return did not
meet the handoff conditions and spent its support reserve. Physical execution,
mission completion, vehicle fidelity and model advantage remain false.

The manifest binds original-record digests, the allowlisted dataset, rendering
sources and exported files. Original private records are retained separately;
their digests are provenance references, not public source authentication.
The 30-second operator fixture is a separate test-operator runtime check, not
authenticated human identity or the full flight in these videos.

## Reproduce the portable page

Python's standard library is sufficient:

```sh
python export_saved_media.py --check
python export_saved_media.py --rebuild-page --check
```

External curation is opt-in. Pass your own saved numerical JSON (or JSON.gz)
using `--nominal`, `--supervision`, `--return-record` and their respective
`--nominal-verification`, `--supervision-verification`, `--return-verification`
arguments. An optional `--operator-receipt` binds that separate fixture. The
exporter imports no runtime model and copies only allowlisted display fields.

## Export video from saved states

```sh
python export_saved_media.py --serve --port 18766
```

Open the printed loopback URL in a browser, choose a saved case, and use
**Export poster** or **Export video**. This is display playback. The optional
same-origin writer exclusively creates six allowlisted PNG/WebM filenames
under ignored `captures/`, with a 12 MiB cap per file. It cannot overwrite an
existing capture or choose another path. Offline file playback offers normal
browser blob downloads instead. No camera, microphone or screen capture is
requested; only the page's own CG canvas is recorded.

Convert each generated WebM with an installed FFmpeg, for example:

```sh
ffmpeg -n -i captures/nominal-flight.webm -an -c:v libx264 -preset veryfast \
  -crf 24 -pix_fmt yuv420p -movflags +faststart -r 24 -t 40 nominal-flight.mp4
ffmpeg -n -i captures/nominal-flight.png -q:v 3 nominal-flight.jpg
```

Use the corresponding filenames for the other cases. Recording makes explicit
cuts across the declared nominal windows; seeking cannot interpolate through
the omitted orbit. All state interpolation is for display only. Do not use
these clips as dynamics, continuous collision, landing, service or hardware
verification. Geometry and plumes are illustrative, not engineering CAD or a
flow solution. Keep the final videos/posters below 30 MiB total.

To regenerate only the nominal animated preview from its existing MP4:

```sh
python export_saved_media.py --rebuild-page --make-previews --preview-case nominal-flight --check
```

This transform uses 640×360 at approximately 6 fps, with an embedded animated
preview label. It retains the previous GIF in ignored `captures/`, grants no
new model or flight invocation, and keeps the aggregate media cap at 30 MiB.
The nominal full-movie preview has a 6 MiB encoding cap; the other previews
retain their 3 MiB caps and existing bytes.
