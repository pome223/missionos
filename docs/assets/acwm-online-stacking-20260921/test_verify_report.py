"""Negative fixtures: keep the manifest consistent while corrupting evidence."""
import copy
import csv
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('online_report_verifier', ROOT / 'verify_report.py')
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


@pytest.mark.parametrize('corruption', ['missing_game', 'false_label', 'missing_hold', 'early_inference'])
def test_individual_corruption_rejected(tmp_path, corruption):
    # Symlinks keep media immutable and avoid copying the same assets repeatedly.
    for source in ROOT.iterdir():
        if source.is_file() and source.name not in ('benchmark.json', 'decision-evidence.json.gz', 'decisions.csv', 'sha256.json'):
            (tmp_path / source.name).symlink_to(source)
    data = copy.deepcopy(json.loads((ROOT / 'benchmark.json').read_text()))
    data['decisions'] = json.loads(gzip.decompress((ROOT / 'decision-evidence.json.gz').read_bytes()))
    if corruption == 'missing_game':
        data['games'].pop()
    elif corruption == 'false_label':
        data['decisions'][0]['continue_collapsed'] = not data['decisions'][0]['continue_collapsed']
    elif corruption == 'missing_hold':
        game = next(g for g in data['games'] if g['technical_failure'] is None)
        game['terminal_hold_verified'] = False
    else:
        row = next(r for r in data['decisions'] if r['inference_receipts'])
        row['inference_receipts'][0]['request_started_at'] = row['decision_committed_at'] - 1
    with (ROOT / 'decisions.csv').open(newline='') as handle:
        keys = next(csv.reader(handle))
    with (tmp_path / 'decisions.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=keys, lineterminator="\n")
        writer.writeheader()
        writer.writerows({k: row.get(k) for k in keys} for row in data['decisions'])
    (tmp_path / 'decision-evidence.json.gz').write_bytes(gzip.compress(json.dumps(data['decisions']).encode(), mtime=0))
    contents = json.dumps({k:v for k,v in data.items() if k != 'decisions'}).encode()
    (tmp_path / 'benchmark.json').write_bytes(contents)
    manifest = json.loads((ROOT / 'sha256.json').read_text())
    for name in ('benchmark.json', 'decision-evidence.json.gz', 'decisions.csv'):
        manifest[name] = hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
    (tmp_path / 'sha256.json').write_text(json.dumps(manifest))
    with pytest.raises(AssertionError):
        VERIFIER.verify(tmp_path)
