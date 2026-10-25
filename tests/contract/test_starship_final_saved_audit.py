"""Historical saved audits are diagnostics, never current runtime readiness."""
from copy import deepcopy
from hashlib import sha256
import json
import socket
import subprocess
import sys
from types import ModuleType

import pytest

from scripts import audit_starship_final_saved as audit
from src.runtime import starship_final_verifier as final
from src.runtime import starship_final_shared as shared


def digest(raw):
    return sha256(raw).hexdigest()


@pytest.fixture
def audit_case(tmp_path, monkeypatch):
    root = tmp_path/'checkout'
    root.mkdir()
    record = root/'output'/'old'
    record.mkdir(parents=True)
    old = b'historical checker source\n'
    new = b'current reviewed checker source\n'
    manifest = {}
    for name in audit.REQUIRED_CHECKERS:
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(old)
        if name != audit.SCRIPT:
            manifest[name] = digest(old)
    changed = 'src/runtime/starship_final_verifier.py'
    (root/changed).write_bytes(new)
    plan = {'source_commit':'a'*40, 'source_sha256':manifest, 'input_sha256':{}, 'backend':{'test':'backend'}}
    plan['plan_sha256'] = shared.digest(plan)
    (record/'plan.json').write_text(json.dumps(plan))
    for name, raw in [('record.json.gz',b'original packed bytes'), ('outer-receipt.json',b'{}'),
                      ('verification.json',b'{"passed":false}')]:
        (record/name).write_bytes(raw)
    policy = {'schema':audit.POLICY_SCHEMA,'source_commit':plan['source_commit'],'plan_sha256':plan['plan_sha256'],
        'plan_file_sha256':audit.file_hash(record/'plan.json'),
        'record_gzip_sha256':audit.file_hash(record/'record.json.gz'),
        'original_verification_sha256':audit.file_hash(record/'verification.json'),
        'outer_receipt_sha256':audit.file_hash(record/'outer-receipt.json'),
        'allowed_source_drift':{changed:digest(new)},'additional_runtime_sources':{},
        'checker_sha256':{name:audit.file_hash(root/name) for name in audit.REQUIRED_CHECKERS}}
    monkeypatch.setattr(audit, 'ROOT', root)
    monkeypatch.setattr(audit.shared, 'ROOT', root)
    monkeypatch.setattr(audit.runner, 'ROOT', root)
    monkeypatch.setattr(audit.shared, 'environment', lambda:plan['backend'])
    monkeypatch.setattr(audit, 'historical_git_manifest', lambda *_args:{'historical_git_blob_bytes_match':True})
    monkeypatch.setattr(audit, 'loaded_project_modules', lambda _root:{'src.fixture':'src/fixture.py'})
    projection = {'frozen':'projection'}
    monkeypatch.setattr(audit.runner.planning, 'verification_inputs', lambda _plan:deepcopy(projection))
    return root,record,plan,policy,projection


def test_offline_audit_keeps_record_verdict_and_current_binding_separate(audit_case,monkeypatch):
    root,folder,plan,policy,projection=audit_case
    before=audit.tree_manifest(folder)
    original=final.verify_current_binding
    def saved(_plan,_folder):
        binding=final.verify_current_binding(projection)
        assert binding['historical_git_blob_bytes_match'] is True
        assert binding['current_source_bytes_match_historical'] is False
        assert binding['runtime_admission'] is False
        return {'schema':final.VERIFICATION_SCHEMA,'passed':True,'checks':{'current_binding':binding},
            'work':{'all_native_role_work_observed':False}}
    monkeypatch.setattr(audit.runner,'verify_saved',saved)
    result=audit.audit_saved(plan,folder,policy,root=root)
    assert result['schema']==audit.SCHEMA != final.VERIFICATION_SCHEMA
    assert result['audit_completed'] and result['consistency_passed']
    assert result['new_live_success_established'] is False
    assert result['execution_code_matches_current'] is False
    assert result['runtime_admission'] is result['execution_authority_granted'] is False
    assert result['qualification_refreshed'] is result['new_flight'] is False
    assert result['model_calls']==result['plant_steps']==0
    assert audit.tree_manifest(folder)==before
    assert final.verify_current_binding is original
    assert json.loads((folder/'verification.json').read_bytes())=={'passed':False}


@pytest.mark.parametrize('operation', ['socket','subprocess','physics','provider'])
def test_historical_audit_never_allows_flight_network_or_new_process(audit_case,monkeypatch,operation):
    root,folder,plan,policy,projection=audit_case
    from src.runtime import starship_sixdof as dyn
    from src.intelligence.starship_m2_broker import DeadlineTransport
    def saved(_plan,_folder):
        final.verify_current_binding(projection)
        with pytest.raises(ValueError):
            if operation=='socket':
                socket.create_connection(('example.invalid',443))
            elif operation=='subprocess':
                subprocess.Popen(['never-executed'])
            elif operation=='physics':
                dyn.step(None,None,None,None)
            else:
                DeadlineTransport._invoke(None,None)
        return {'passed':False,'issues':[{'reason':'synthetic_stop'}]}
    monkeypatch.setattr(audit.runner,'verify_saved',saved)
    result=audit.audit_saved(plan,folder,policy,root=root)
    assert result['audit_completed'] and not result['consistency_passed']
    assert result['consistency_result']['issues'][0]['reason']=='synthetic_stop'


@pytest.mark.parametrize('change', ['unlisted','changed_again','extra_runtime','missing_source','checker','unneeded_allowance'])
def test_current_source_drift_is_an_exact_reviewed_set(audit_case,change):
    root,_,plan,policy,_=audit_case
    if change=='unlisted':
        policy['allowed_source_drift']={}
    elif change=='changed_again':
        (root/'src/runtime/starship_final_verifier.py').write_text('another version')
    elif change=='extra_runtime':
        (root/'src/runtime/unreviewed.py').write_text('new unreviewed module')
    elif change=='missing_source':
        (root/'src/runtime/starship_final_shared.py').unlink()
    elif change=='checker':
        policy['checker_sha256'][audit.SCRIPT]='0'*64
    else:
        policy['allowed_source_drift']['src/runtime/starship_final_shared.py']=plan['source_sha256']['src/runtime/starship_final_shared.py']
    with pytest.raises(ValueError):
        audit.current_sources(root,plan['source_sha256'],policy)


def test_explicitly_pinned_new_runtime_module_is_recorded_separately(audit_case):
    root,_,plan,policy,_=audit_case
    (root/'src/runtime/reviewed.py').write_text('reviewed module')
    policy['additional_runtime_sources']={'src/runtime/reviewed.py':digest(b'reviewed module')}
    result=audit.current_sources(root,plan['source_sha256'],policy)
    assert result['additional_runtime_sources']==policy['additional_runtime_sources']
    assert 'src/runtime/reviewed.py' not in plan['source_sha256']


@pytest.mark.parametrize('name', ['plan.json','verification.json','outer-receipt.json','record.json.gz'])
def test_original_pin_change_stops_before_any_saved_checker(audit_case,monkeypatch,name):
    root,folder,plan,policy,_=audit_case
    (folder/name).write_bytes(b'changed')
    monkeypatch.setattr(audit.runner,'verify_saved',lambda *_a:pytest.fail('must not check changed record'))
    with pytest.raises(ValueError,match='audit_original_record_pin'):
        audit.audit_saved(plan,folder,policy,root=root)


def test_projection_cannot_be_swapped_for_one_from_the_new_code(audit_case,monkeypatch):
    root,folder,plan,policy,_=audit_case
    original=final.verify_current_binding
    monkeypatch.setattr(audit.runner,'verify_saved',lambda *_a:final.verify_current_binding({'changed':'projection'}))
    with pytest.raises(ValueError,match='audit_projection_rebound'):
        audit.audit_saved(plan,folder,policy,root=root)
    assert final.verify_current_binding is original


def test_checker_source_change_during_verification_is_detected(audit_case,monkeypatch):
    root,folder,plan,policy,projection=audit_case
    def saved(*_a):
        final.verify_current_binding(projection)
        (root/'src/runtime/starship_final_verifier.py').write_bytes(b'changed during check')
        return {'passed':True}
    monkeypatch.setattr(audit.runner,'verify_saved',saved)
    with pytest.raises(ValueError,match='audit_unreviewed_source_drift'):
        audit.audit_saved(plan,folder,policy,root=root)


def test_original_record_change_during_verification_cannot_become_a_pass(audit_case,monkeypatch):
    root,folder,plan,policy,projection=audit_case
    def saved(*_a):
        final.verify_current_binding(projection)
        (folder/'record.json.gz').write_bytes(b'tampered')
        return {'passed':True}
    monkeypatch.setattr(audit.runner,'verify_saved',saved)
    with pytest.raises(ValueError,match='audit_original_tree_changed'):
        audit.audit_saved(plan,folder,policy,root=root)


def test_standard_current_binding_still_refuses_historical_source_drift(monkeypatch):
    # Exercise the actual default checker, not the audit replacement.
    manifest=shared.source_manifest()
    manifest['src/runtime/starship_final_verifier.py']='0'*64
    inputs={'source_sha256':manifest,'input_sha256':{},'backend':{}}
    with pytest.raises(ValueError,match='final_source_or_input_changed'):
        final.verify_current_binding(inputs)


def test_real_git_read_proves_blob_bytes_not_just_commit_label(tmp_path):
    root=tmp_path/'git'
    root.mkdir()
    def git(*args):
        return subprocess.check_output(['git',*args],cwd=root,stderr=subprocess.DEVNULL).decode().strip()
    git('init','-q')
    (root/'source.py').write_bytes(b'old source\n')
    git('add','source.py')
    git('-c','user.name=Audit Test','-c','user.email=audit@example.invalid','commit','-qm','fixture')
    commit=git('rev-parse','HEAD')
    manifest={'source.py':digest(b'old source\n')}
    (root/'source.py').write_text('new uncommitted source')
    result=audit.historical_git_manifest(root,commit,manifest)
    assert result['files']==1 and result['historical_git_blob_bytes_match']
    with pytest.raises(ValueError,match='audit_historical_blob_mismatch'):
        audit.historical_git_manifest(root,commit,{'source.py':'0'*64})
    with pytest.raises(ValueError,match='audit_git_blob_missing'):
        audit.historical_git_manifest(root,commit,{'absent.py':'0'*64})


@pytest.mark.parametrize('name',['../source.py','/source.py','src/../source.py','src\\source.py','src/a\n.py','src/a:b.py'])
def test_git_requests_do_not_allow_path_or_protocol_injection(name):
    with pytest.raises(ValueError,match='audit_plain_relative_name'):
        audit.relative_name(name)


@pytest.mark.parametrize('placement', ['foreign_file', 'foreign_namespace', 'wrong_local_file', 'foreign_package_path'])
def test_loaded_project_modules_reject_foreign_or_misnamed_imports(tmp_path, monkeypatch, placement):
    root = tmp_path/'checkout'
    (root/'src').mkdir(parents=True)
    (root/'src/checked.py').write_text('')
    (root/'src/other.py').write_text('')
    package = root/'src/checked'
    package.mkdir()
    (package/'__init__.py').write_text('')
    module = ModuleType('src.checked')
    if placement == 'foreign_namespace':
        module.__path__ = [str(tmp_path/'foreign')]
    elif placement == 'foreign_file':
        module.__file__ = str(tmp_path/'foreign.py')
    elif placement == 'foreign_package_path':
        module.__file__ = str(package/'__init__.py')
        module.__path__ = [str(tmp_path/'foreign')]
    else:
        module.__file__ = str(root/'src/other.py')
    monkeypatch.setattr(sys, 'modules', {'src.checked': module})
    with pytest.raises(ValueError, match='audit_foreign_loaded_module'):
        audit.loaded_project_modules(root)


def test_loaded_project_modules_accept_this_checkout_and_namespace_paths(monkeypatch):
    # Exercise real checkout package files, independently of
    # unrelated imports collected by the full suite (including intentional
    # compatibility aliases). Production still inspects its complete module set;
    # foreign/misnamed modules remain rejected by the separate negative cases.
    names = ('src', 'src.runtime', 'src.runtime.starship_final_verifier',
             'scripts', 'scripts.run_starship_final_mission')
    modules = {name: sys.modules[name] for name in names}
    monkeypatch.setattr(sys, 'modules', modules)
    loaded = audit.loaded_project_modules(audit.ROOT)
    assert set(loaded) == set(names)
    assert loaded['src.runtime.starship_final_verifier'] == 'src/runtime/starship_final_verifier.py'
    assert loaded['scripts.run_starship_final_mission'] == 'scripts/run_starship_final_mission.py'
    assert loaded['scripts'] == 'scripts/__init__.py'
    assert all(not name.startswith('/') for name in loaded.values())


def test_loaded_module_set_cannot_change_during_saved_verification(audit_case, monkeypatch):
    root, folder, plan, policy, projection = audit_case
    observations = iter([{'src.fixture': 'src/fixture.py'},
                         {'src.fixture': 'src/fixture.py'},
                         {'src.other': 'src/other.py'}])
    monkeypatch.setattr(audit, 'loaded_project_modules', lambda _root: next(observations))
    def saved(*_a):
        final.verify_current_binding(projection)
        return {'passed': True}
    monkeypatch.setattr(audit.runner, 'verify_saved', saved)
    with pytest.raises(ValueError, match='audit_loaded_module_changed_during_read'):
        audit.audit_saved(plan, folder, policy, root=root)
