# Go2 Agent-supervised delivery report / Agent配送管制レポート

- [日本語レポート](report.md) · [動画付きHTML](index.html)
- [English report](report-en.md) · [HTML with videos](index-en.html)

Download this directory with its `media/` subdirectory, or serve it locally:

```sh
python3 -m http.server 18855 --bind 127.0.0.1 --directory docs/assets/go2-agent-delivery-20260925
```

Open `http://127.0.0.1:18855/index-en.html` (Japanese: `index.html`). GitHub source pages do not execute the HTML replay. Videos are unchanged MuJoCo recordings at approximately 3× playback speed, not predicted scenes.

Verification (standard-library Python; no model or simulator calls):

```sh
python3 docs/assets/go2-agent-delivery-20260925/verify_report.py
python3 docs/assets/go2-agent-delivery-20260925/verify_translation.py
```

To regenerate HTML after editing Markdown, use Python with `markdown-it-py` and run `render_report.py` and `render_report_en.py` from this directory. Regenerate `manifest.json` after reviewing any artifact changes. `provenance.json` records the tested revision and raw-evidence hashes; raw private records are not published. The verifier checks the curated records, not an independent simulator rerun.
