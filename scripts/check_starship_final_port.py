#!/usr/bin/env python3
"""Run selected port contracts without any old editable-checkout fallback.

This is a code/contract check, not a mission, provider call or certificate.
Vendor libraries come from the invoking environment; .pth files are not run.
"""
from hashlib import sha256
import argparse
import json
from pathlib import Path
import subprocess
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]

BOOTSTRAP = r'''
import json,pathlib,sys
root=pathlib.Path(sys.argv[1]).resolve()
vendors=pathlib.Path(sys.argv[2]).resolve()
report=pathlib.Path(sys.argv[3])
paths=json.loads(sys.argv[4])
sys.path[:0]=[str(root),str(root/'packages/missionos-core/src'),
    str(root/'packages/missionos-cli/src'),str(root/'packages/missionos-gateway/src')]
sys.path.append(str(vendors))
import pytest
code=pytest.main([*paths,'-q','--tb=short'])
foreign=[]
for name,module in list(sys.modules.items()):
    if name.split('.')[0] not in {'src','scripts','tests','missionos_core','missionos_cli','missionos_gateway'}:
        continue
    filename=getattr(module,'__file__',None)
    if filename and not pathlib.Path(filename).resolve().is_relative_to(root):
        foreign.append(name)
value={'schema':'missionos.starship_final_port_contract_check.v1',
    'pytest_returncode':code,'foreign_local_imports':foreign,
    'python':sys.version,'selected_test_paths':paths,'editable_pth_files_processed':False,
    'registered_mission_executed':False,'provider_calls':0,'qualification_refreshed':False,
    'passed':code==0 and not foreign}
with report.open('x') as stream: json.dump(value,stream,indent=2,sort_keys=True);stream.write('\n')
raise SystemExit(0 if value['passed'] else 1)
'''


def selected_paths():
    imported = json.loads((ROOT/"docs/agents/starship-develop-port-imports-v1.json").read_bytes())
    paths = {row["path"] for row in imported["paths"]
        if row["path"].startswith("tests/contract/test_") and row["path"].endswith(".py")}
    paths |= {str(path.relative_to(ROOT)) for path in (ROOT/"tests/contract").glob("test_starship_final_*.py")}
    if not paths or any(not (ROOT/path).is_file() for path in paths):
        raise ValueError("final_port_test_inventory_missing")
    return sorted(paths)


def source_inventory():
    names = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT).split(b"\0")
    result = {}
    for raw in sorted(set(names)-{b""}):
        name = raw.decode()
        path = ROOT/name
        if path.suffix not in {".py", ".json", ".toml", ".js", ".html", ".txt", ".csv"}:
            continue
        if not path.is_file() or path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError("source_inventory_missing_or_symlinked_file")
        result[name] = sha256(path.read_bytes()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, help="New private directory under output/")
    parser.add_argument("--full-tests", action="store_true", help="Run the complete maintained tests/ tree")
    args = parser.parse_args()
    relative = Path(args.output_dir)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "output":
        raise ValueError("new_repo_output_directory_required")
    output = ROOT/relative
    if output.exists() or output.resolve() != output.absolute():
        raise ValueError("exclusive_non_symlink_output_directory_required")
    output.mkdir(parents=True, mode=0o700)
    vendors = Path(sysconfig.get_path("purelib"))
    if not (vendors/"pytest").is_dir():
        raise ValueError("invoking_environment_pytest_required")
    paths = ["tests"] if args.full_tests else selected_paths()
    before = source_inventory()
    (output/"source-inventory.json").write_text(json.dumps(before, indent=2, sort_keys=True)+"\n")
    command = [sys.executable, "-I", "-S", "-c", BOOTSTRAP,
        str(ROOT), str(vendors), str(output/"result.json"), json.dumps(paths)]
    with (output/"contracts.log").open("xb") as log:
        process = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=900.)
    if not (output/"result.json").is_file():
        print(json.dumps({"passed": False, "returncode": process.returncode,
            "result_missing": True, "log": str(relative/"contracts.log")}))
        raise SystemExit(1)
    value = json.loads((output/"result.json").read_bytes())
    value["source_unchanged"] = source_inventory() == before
    value["source_inventory_sha256"] = sha256((output/"source-inventory.json").read_bytes()).hexdigest()
    value["passed"] = value["passed"] is True and value["source_unchanged"]
    value["log_sha256"] = sha256((output/"contracts.log").read_bytes()).hexdigest()
    value["result_path"] = str(relative/"result.json")
    (output/"verification.json").write_text(json.dumps(value, indent=2, sort_keys=True)+"\n")
    print(json.dumps(value, indent=2, sort_keys=True))
    raise SystemExit(0 if value["passed"] else 1)


if __name__ == "__main__":
    main()
