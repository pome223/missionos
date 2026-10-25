#!/usr/bin/env python3
"""Inert isolated sleepers prove owned cleanup; no mission, model or provider."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.runtime import starship_restored_process_owner as owner  # noqa: E402


def save(path,value):
    with Path(path).open("xb") as handle:
        handle.write(json.dumps(value,sort_keys=True,indent=2,allow_nan=False).encode()+b"\n")


def worker(output,*,normal=False):
    children = [subprocess.Popen([sys.executable,"-c","import time; time.sleep(.05)" if normal
        else "import time; time.sleep(60)"],start_new_session=True,stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) for _ in range(2)]
    save(output,{"worker_pid":os.getpid(),"worker_pgid":os.getpgid(0),
        "children":[{"pid":child.pid,"pgid":os.getpgid(child.pid)} for child in children],"inert_sleepers_only":True})
    if normal:
        for child in children:
            child.wait(timeout=5.)
    else:
        time.sleep(60.)


def source_pins():
    return {path.relative_to(ROOT).as_posix():sha256(path.read_bytes()).hexdigest() for path in
        (Path(__file__).resolve(),ROOT/"src/runtime/starship_restored_process_owner.py")}


def run_smoke(output):
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("restored_process_smoke_fresh_output_required")
    output.mkdir(parents=True)
    pins = source_pins()
    source = sha256(json.dumps(pins,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    plan = sha256(json.dumps({"source_sha256":source,"inert_children":2,"normal_and_timeout":True},sort_keys=True).encode()).hexdigest()
    env = {name:value for name,value in os.environ.items() if name in {"PATH","HOME","TMPDIR","LANG","LC_ALL"}}
    checks,records,processes = {},{},[]
    unrelated = subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],env=env,
        start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    processes.append(unrelated)
    try:
        for normal in (False,True):
            name = "normal" if normal else "timeout"
            marker = output/(name+"-children.json")
            command = [sys.executable,str(Path(__file__).resolve()),"--_worker",str(marker)]
            if normal:
                command.append("--normal")
            with (output/(name+"-worker.log")).open("xb") as log:
                process = subprocess.Popen(command,cwd=ROOT,env=env,start_new_session=True,
                    stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
                processes.append(process)
                until = time.monotonic()+5.
                while not marker.exists() and process.poll() is None and time.monotonic() < until:
                    time.sleep(.01)
                if not marker.exists():
                    raise ValueError("restored_process_smoke_children_not_ready")
                family = json.loads(marker.read_bytes())
                if normal:
                    process.wait(timeout=5.)
                receipt = owner.cleanup_restored_worker(process,plan_sha256=plan,source_sha256=source)
                save(output/(name+"-cleanup.json"),receipt)
                table = owner._table()
                targets = [family["worker_pid"],*(row["pid"] for row in family["children"])]
                all_absent = all(pid not in table and not any(row["pgid"] == pid for row in table.values()) for pid in targets)
                checks[name+"_all_original_ids_and_groups_absent"] = all_absent
                checks[name+"_unrelated_sibling_alive"] = unrelated.poll() is None
                checks[name+"_no_signal_to_unrelated_sibling"] = all(row["pid_or_pgid"] != unrelated.pid for row in receipt["signals"])
                if normal:
                    checks["normal_noop_no_signals_or_unproven_descendant_claim"] = receipt["status"] == "already_completed_no_signal" \
                        and receipt["signals"] == [] and not receipt["descendant_cleanup_verified"]
                else:
                    checks["timeout_root_reaped_and_all_children_cleaned"] = receipt["status"] == "cleaned" \
                        and receipt["worker_reaped_by_host"] and receipt["all_owned_processes_absent"] \
                        and receipt["children_dead_before_worker_termination"]
                    checks["timeout_no_child_signals_after_root_reap"] = receipt["no_child_signals_after_worker_reap"]
                records[name] = {"cleanup_sha256":sha256((output/(name+"-cleanup.json")).read_bytes()).hexdigest(),
                    "family_sha256":sha256(marker.read_bytes()).hexdigest(),"returncode":process.returncode}
        checks["source_unchanged"] = source_pins() == pins
        result = {"schema":"missionos.starship_restored_process_owner_smoke.v1","passed":all(checks.values()),
            "plan_sha256":plan,"source_sha256":pins,"checks":checks,"records":records,
            "unrelated_sibling_was_preserved":unrelated.poll() is None,"plant_steps":0,"model_calls":0,
            "only_inert_sleep_processes_used":True,"mission_authority_granted":False}
        save(output/"summary.json",result)
        if not result["passed"]:
            raise ValueError("restored_process_owner_smoke_failed")
        return result
    except BaseException as error:
        if not (output/"stopped.json").exists():
            save(output/"stopped.json",{"error_type":type(error).__name__,"reason":str(error)[:200],
                "automatic_retry":False,"plant_steps":0,"model_calls":0,
                "ownership_receipt":getattr(error,"receipt",None)})
        raise
    finally:
        # These Popen objects were created by this smoke, never looked up by PID.
        for process in reversed(processes):
            if process.poll() is None:
                try:
                    owner.cleanup_restored_worker(process,plan_sha256=plan,source_sha256=source)
                except (ValueError,OSError,subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=5.)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",action="store_true")
    parser.add_argument("--output-dir",type=Path)
    parser.add_argument("--_worker",type=Path,help=argparse.SUPPRESS)
    parser.add_argument("--normal",action="store_true",help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._worker is not None:
        if args.run or args.output_dir is not None:
            parser.error("private worker accepts only its marker and normal flag")
        return worker(args._worker,normal=args.normal)
    if not args.run or args.output_dir is None or args.normal:
        parser.error("explicit --run and fresh --output-dir required")
    result = run_smoke(args.output_dir)
    print(json.dumps({"passed":result["passed"],"plant_steps":0,"model_calls":0}))


if __name__ == "__main__":
    main()
