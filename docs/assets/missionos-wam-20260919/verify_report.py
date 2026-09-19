"""Verify published plot data against report tables, links, arithmetic and hygiene."""

import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
D = json.loads((ROOT / "figure-data.json").read_text())
source = (REPO / "docs/agents/block-stacking-wam-physics-followup-20260918.md").read_text()
expected = {
    "v5": {"vla": 130, "current": 208, "new": 292, "width": 280},
    "v6": {"vla": 80, "current": 202, "new": 273, "old": 281, "width": 280},
    "v7": {"vla": 30, "current": 198, "new": 279, "old": 225, "width": 280},
}
for version, prefix in [("v5", "630"), ("v6", "650"), ("v7", "670")]:
    rows = D["cohorts"][version]
    assert len(rows) == 40 and len({r["seed"] for r in rows}) == 40
    assert sum(r["width_group"] == "wide" for r in rows) == 20
    fields = list(expected[version])
    raw = [
        [s.strip() for s in line.split("|")[1:-1]]
        for line in source.splitlines()
        if re.match(r"^\| " + prefix + r"\d\d \| (wide|narrow) \|", line)
    ]
    assert len(raw) == 40
    for row, values in zip(rows, raw):
        assert row["seed"] == int(values[0]) and row["width_group"] == values[1]
        assert [row[k] for k in fields] == list(map(int, values[2 : 2 + len(fields)]))
        assert all(0 <= row[k] <= 10 for k in fields)
    assert {k: sum(r[k] for r in rows) for k in fields} == expected[version]
rows = D["cohorts"]["v7"]
deltas = [r["new"] - r["width"] for r in rows]
assert (deltas.count(1), deltas.count(0), deltas.count(-8), sum(deltas)) == (15, 23, 2, -1)
for name, mean, lo, hi, p in D["v7_paired_statistics"]:
    a, b = name.lower().split(" - ")
    assert abs(sum(r[a] - r[b] for r in rows) / 40 - mean) < 1e-12
    assert lo <= mean <= hi and 0 <= p <= 1
assert [sum(D["e2e"][k]) for k in ["deterministic", "deepseek_r3", "deepseek_r4"]] == [17, 13, 16]
for c in D["proxy_cases"]:
    n = c["next_count"]
    bank = (n - 1) * (1 - c["bank_risk"])
    cont = n * (1 - c["continue_risk"])
    assert (cont > bank) == (c["seed"] == 67002)
report = REPO / "docs/agents/missionos-wam-system-technical-report-20260919.md"
for path in [report, ROOT / "README.md"]:
    text = path.read_text()
    for target in re.findall(r"\]\(([^)]+)\)", text):
        if not target.startswith(("https://", "http://", "#")):
            assert (path.parent / target.split("#")[0]).exists(), target
for path in [report, *ROOT.iterdir()]:
    if path.suffix in {".md", ".json", ".py", ".svg"}:
        text = path.read_text()
        for marker in [
            "/Users/",
            "/tmp/",
            "missionos-internal",
            "ghp_",
            "github_pat_",
            "BEGIN PRIVATE KEY",
        ]:
            # The verifier contains this list as literals; it is not publication data.
            if path.name != "verify_report.py":
                assert marker not in text, (path.name, marker)
assert len(list(ROOT.glob("*.png"))) == 6 and len(list(ROOT.glob("*.svg"))) == 6
print(
    "PASS: 120 source-matched rows, cohort totals, paired means, gain/loss counts, proxy arithmetic, figure count, links and publication scan"
)
