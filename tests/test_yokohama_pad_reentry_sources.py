"""Frozen reentry reproduction and current-Rules replay stay separate checks."""

import hashlib
import json
from pathlib import Path

import pytest

from scripts import check_yokohama_pad_reentry_sources as sources

BUNDLE = Path(__file__).resolve().parents[1] / "docs/examples/yokohama-pad-reentry-learning"

STUB = """
import hashlib
from pathlib import Path
REPO = Path(__file__).resolve().parents[1]
def check(bundle):
    body = (REPO / "src/runtime/rules.py").read_bytes()
    return dict(status="passed", rules_sha256=hashlib.sha256(body).hexdigest())
"""


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def repo(tmp_path, live="live = 2\n", frozen="live = 1\n"):
    root, store, bundle = tmp_path / "repo", tmp_path / "frozen", tmp_path / "bundle"
    for path, text in {
        root / "scripts/check_yokohama_pad_reentry.py": STUB,
        root / "src/runtime/rules.py": live,
        store / "src/runtime/rules.py": frozen,
    }.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (root / "docs").mkdir()
    bundle.mkdir()
    pins = {"src/runtime/rules.py": digest("live = 1\n")}
    (bundle / "source-sha256.json").write_text(json.dumps(pins))
    return root, store, bundle


def test_changed_pinned_source_runs_from_matching_frozen_copy(tmp_path):
    root, store, bundle = repo(tmp_path)
    result = sources.frozen_reproduction(bundle, root, store)
    assert result["restored_sources"] == ["src/runtime/rules.py"]
    assert result["rules_sha256"] == digest("live = 1\n")
    assert (root / "src/runtime/rules.py").read_text() == "live = 2\n"


def test_unchanged_source_needs_no_frozen_copy(tmp_path):
    root, store, bundle = repo(tmp_path, live="live = 1\n", frozen="unused\n")
    assert sources.frozen_reproduction(bundle, root, store)["restored_sources"] == []


def test_frozen_copy_with_another_hash_is_refused(tmp_path):
    root, store, bundle = repo(tmp_path, frozen="live = 3\n")
    with pytest.raises(ValueError, match="Frozen experiment source unavailable"):
        sources.frozen_reproduction(bundle, root, store)


def test_pins_outside_code_are_refused(tmp_path):
    root, store, bundle = repo(tmp_path)
    (bundle / "source-sha256.json").write_text(json.dumps({"../escape.py": "0" * 64}))
    with pytest.raises(ValueError, match="outside scripts/ or src/"):
        sources.frozen_reproduction(bundle, root, store)


def test_current_rules_reproduce_the_recorded_judgments():
    result = sources.current_replay(BUNDLE)
    assert result == dict(judgments=903, unchanged=True)


def test_a_changed_current_rule_is_detected(monkeypatch):
    monkeypatch.setattr(sources.queue, "require_response", lambda *args: "enter_delivery_approach")
    with pytest.raises(ValueError, match="Current Rules changed a recorded judgment"):
        sources.current_replay(BUNDLE)
