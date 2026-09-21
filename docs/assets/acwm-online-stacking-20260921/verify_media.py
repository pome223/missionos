"""Decode released PNG/MP4 assets; requires Pillow, ffmpeg and ffprobe."""
from pathlib import Path
import json
import subprocess
from PIL import Image

root = Path(__file__).resolve().parent
for path in sorted(root.glob('*.png')):
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        image.load()
        assert image.width > 0 and image.height > 0
for path in sorted(root.glob('*.mp4')):
    info = json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(path)
    ]))
    streams = [s for s in info['streams'] if s['codec_type'] == 'video']
    assert len(streams) == 1 and int(streams[0]['nb_frames']) == 37
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-f', 'null', '-'], check=True)
print('PASS: released PNGs loaded and all MP4 frames decoded')
