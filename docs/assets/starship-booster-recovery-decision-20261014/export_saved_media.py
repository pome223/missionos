#!/usr/bin/env python3
"""Curate a verified saved M3 flight; no model, simulator, or hardware call.

The published page and --check require only Python's standard library. External
curation accepts a private saved study plus its independently reviewed summary;
only an explicit numerical/action/external-summary allowlist is exported.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
FIELDS = ['time_s','phase','body_id','r_eci_m','v_eci_mps','q_body_to_eci','omega_body_rad_s',
          'propellant_kg','altitude_m','ground_speed_mps','com_z_m','applied_thrust_n','throttle',
          'engine_states','flap_angles_rad','main_engine_thrust_n','scene_index']
ENGINE_FIELDS = ['throttle','gimbal_x_rad','gimbal_y_rad','available']
BASENAME = 'mission'
SCRIPTS = ['starship_cg_models.js','starship_cg_renderer.js','replay.js']
DENIED = {'request_id','session_id','run_id','command_id','signature','token','api_key',
          'reasoning_content','reasoning','authorization','headers','raw_response','prompt'}


def encoded(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def pack(samples):
    ordered={s['time_s']:s for s in samples}
    rows,scenes=[],[]
    for t in sorted(ordered):
        s=ordered[t]
        scene={k:s.get(k,[]) for k in ('main_engine_anchors','aero_panel_names')}
        if scene not in scenes:scenes.append(scene)
        clean={k:s.get(k) for k in FIELDS}
        clean['scene_index']=scenes.index(scene)
        count=len(scene['main_engine_anchors'])
        clean['engine_states']=[[e[k] for k in ENGINE_FIELDS] for e in s.get('engine_states',[])[:count]]
        rows.append([clean[k] for k in FIELDS])
    return {'fields':FIELDS,'engine_fields':ENGINE_FIELDS,'scenes':scenes,'rows':rows}


def choice(row, phase):
    response = row.get('response') or {}; dispatch = row.get('dispatch') or {}
    deep = response.get('llm_invocation') or {}; jev = response.get('jev_invocation') or {}
    receipt = deep if deep.get('call_attempted') else jev
    action = row.get('accepted_action', dispatch.get('action', row.get('action')))
    fallback = bool(row.get('fallback') or dispatch.get('rejection'))
    return {'time_s': row.get('later_time_s', row.get('time_s', dispatch.get('time_s'))),
        'phase': phase, 'provider': 'DeepSeek' if deep.get('call_attempted') else 'Jev' if jev.get('call_attempted') else 'No model',
        'provider_attempts': {'Jev': int(jev.get('call_attempted') is True), 'DeepSeek': int(deep.get('call_attempted') is True)},
        'action': action, 'model_action': response.get('action'),
        'model_valid': receipt.get('model_inference_invoked') is True,
        'adopted': bool(not fallback and action == response.get('action')),
        'fallback': fallback, 'external_summary': response.get('decision_summary') or
            (response.get('decision_basis') or {}).get('summary'),
        'later_observation_time_s': (row.get('later_observation') or {}).get('time_s', row.get('later_time_s'))}


def build(study_path, verification_path, source_commit=None):
    raw = study_path.read_bytes(); verified_raw = verification_path.read_bytes()
    outer = json.loads(raw); verdict = json.loads(verified_raw)
    if (verdict.get('passed') is not True or verdict.get('booster_recovery_judgment_observed') is not True
            or verdict.get('booster_water_entry_met') is not True):
        raise ValueError('verified_adopted_booster_judgment_and_water_entry_required')
    s = outer['m2_study']; prefix = s['concurrent_prefix']; launch = s['deployment_run']
    returning = s['return_study']; executed = returning['execution']; booster = launch['booster_run']
    joint = ('time_s','r_eci_m','v_eci_mps','q_body_to_eci','omega_body_rad_s','propellant_kg','engine_states','flap_angles_rad')
    if any(launch['samples'][-1][k] != executed['samples'][0][k] for k in joint):
        raise ValueError('exact_same_flight_handoff_required')
    record = prefix['prefix_run']['booster_mission_director']['records'][0]
    response = record['response']; dispatch = record['dispatch']; receipt = response['llm_invocation']
    report = prefix['authority']['booster_recovery_report']
    if digest(json.dumps(report, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(',', ':')).encode()) != response['recovery_report_sha256']:
        raise ValueError('recovery_report_binding')
    decisions = [choice(r, 'ascent') for r in prefix['ascent']['decisions']]
    decisions += [choice(r, 'booster') for r in prefix['prefix_run']['booster_mission_director']['records']]
    decisions += [choice(r, 'deployment') for r in launch['mission_director']['records']]
    decisions += [choice(r, 'return') for r in returning['decisions']]
    last = executed['samples'][-1]; bfinal = booster['samples'][-1]
    burn = next(e['time_s'] for e in executed['events'] if e['event'] == 'return_requested')
    begin_terminal = max(burn, last['time_s'] - 190.)
    when = record['later_observation']['time_s']
    segments = [
        (0.,30.,12,'LAUNCH / TOWER SITE CONTEXT','ground','ship'),
        (30.,when,6,'ASCENT / LIVE AI MONITORING','chase','ship'),
        (when,when,12,'TOWER OR OCEAN / RECORDED RECOVERY JUDGMENT','orbit','tower'),
        (when,bfinal['time_s'],20,'PARALLEL BOOSTER BRANCH / CHECKED SEA RETURN','chase','booster'),
        (135.6,launch['samples'][-1]['time_s'],18,'SHIP / ORBIT INSERTION / 26 PAYLOAD RELEASES','payload','ship'),
        (launch['samples'][-1]['time_s'],burn,18,'OPERATIONAL UPDATE / AI RETURN DECISIONS','orbit','ship'),
        (burn,begin_terminal,16,'RESERVED RETURN / ATMOSPHERIC DESCENT','chase','ship'),
        (begin_terminal,last['time_s'],14,'TERMINAL DESCENT / RECORDED ATTITUDE','chase','ship'),
        (last['time_s'],last['time_s'],4,'OBSERVED CONTACT / BOUNDED RESULT','orbit','ship')]
    data = {'schema':'missionos.booster_report_saved_media.v1',
        'title':'Tower or ocean? Recorded MissionOS recovery judgment.',
        'frames':pack(launch['samples'] + executed['samples']), 'booster':pack(booster['samples']),
        'satellites':[{'label':f'Payload {i+1:02d}', 'release_s':b['samples'][0]['time_s'],
            'frames':pack(b['samples'])} for i,b in enumerate(launch['satellites'])],
        'decisions':sorted(decisions,key=lambda r:(r['time_s'],r['phase'])),
        'recovery_report':report,
        'booster_decision':{'time_s':when, 'model_action':response['action'],
            'model_valid':receipt['model_inference_invoked'], 'external_summary':response['decision_summary'],
            'executed_action':dispatch['action'], 'rules_accepted':dispatch['rules_accepted'],
            'fallback_used':dispatch['rejection'] is not None,
            'report_sha256':response['recovery_report_sha256'],
            'public_input_sha256':receipt['public_input_sha256'],
            'free_text_summary_verified':False},
        'segments':[dict(zip(('start_s','end_s','video_s','label','camera','focus'),x)) for x in segments],
        'result':{'ship_speed_mps':executed['outcome']['contact_receipt']['surface_relative_speed_mps'],
            'ship_cg_ground_speed_mps':last['ground_speed_mps'], 'booster_speed_mps':bfinal['ground_speed_mps'],
            'released':s['handoff']['released_count'], 'record_verified':True, 'case_accepted':verdict['case_accepted'],
            'case_issues':verdict['case_issues'],
            'provider_attempts': {k:sum(d['provider_attempts'][k] for d in decisions) for k in ('Jev','DeepSeek')}, 'booster_judgment_adopted':dispatch['rules_accepted'],
            'orbital_comparison_gate_passed':not bool(verdict['case_issues']),
            'bounded_four_role_m3_campaign_rerun':False, 'physical_execution':False},
        'presentation':{'full_saved_time_range_s':[0.,last['time_s']], 'new_physics_states':False,
            'new_model_calls':0, 'illustrative_geometry':True, 'tower_cutaway_is_executed_approach':False,
            'sample_interpolation':'display-only interpolation of saved states', 'hidden_reasoning_included':False},
        'source':{'study_sha256':digest(raw), 'verification_sha256':digest(verified_raw),
            'source_sha256':outer['source_sha256']}}
    if source_commit is not None:
        if re.fullmatch(r'[0-9a-f]{40}', source_commit) is None:
            raise ValueError('full_source_commit_required')
        for name, expected in outer['source_sha256'].items():
            content = subprocess.check_output(['git', 'show', source_commit + ':' + name], cwd=ROOT)
            if digest(content) != expected:
                raise ValueError('source_commit_binding')
        data['source']['source_commit'] = source_commit
    manifest = {'schema':'missionos.booster_report_media_manifest.v1','source':data['source'],
        'private_raw_logs_included':False, 'model_calls_during_render':0, 'physics_steps_during_render':0,
        'media_maximum_mp4_bytes':20*1024*1024,'media':[],
        'claim':'One verified local MissionOS/6DOF flight and recorded booster decision. Tower-only scene illustrates the alternative; no launch-derived catch or real spacecraft execution.'}
    if sum(data['result']['provider_attempts'].values()) != verdict['model_calls']:
        raise ValueError('attempts_not_fully_accounted_by_display_records')
    validate(data)
    return data,manifest


def validate(value):
    def visit(x):
        if isinstance(x,dict):
            if DENIED.intersection(x): raise ValueError('private_field_in_export')
            for v in x.values(): visit(v)
        elif isinstance(x,list):
            for v in x: visit(v)
        elif isinstance(x,float) and not math.isfinite(x): raise ValueError('nonfinite_export')
        elif isinstance(x,str) and re.search(r'/Users/|/private/|Bearer\s|sk-[A-Za-z0-9]{12,}|session_01',x):
            raise ValueError('private_value_in_export')
    visit(value)
    for f in [value['frames'],value['booster'],*(x['frames'] for x in value['satellites'])]:
        times=[r[0] for r in f['rows']]
        if not times or any(b<=a for a,b in zip(times,times[1:])): raise ValueError('strict_saved_time')
        for row in f['rows']:
            q=row[FIELDS.index('q_body_to_eci')]
            if abs(math.sqrt(sum(v*v for v in q))-1)>1e-6: raise ValueError('saved_unit_quaternion')
    if sum(s['video_s'] for s in value['segments'])!=120: raise ValueError('fixed_movie_duration')
    if value['presentation']['tower_cutaway_is_executed_approach'] is not False:
        raise ValueError('tower_cutaway_claim')


def page(data):
    return (HERE/'page-template.html').read_text().replace('__DATA__',encoded(data).decode().replace('<','\\u003c')).replace('__SCRIPTS__','\n'.join((HERE/f).read_text() for f in SCRIPTS))


def update_manifest(manifest,data):
    manifest['dataset_sha256']=digest(encoded(data))
    manifest['render_sources_sha256']={name:digest((HERE/name).read_bytes()) for name in [*SCRIPTS,'page-template.html','export_saved_media.py']}
    manifest['page_sha256']=digest((HERE/'index.html').read_bytes())
    (HERE/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')


def serve(port):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self,*a,**kw):super().__init__(*a,directory=str(HERE),**kw)
        def do_POST(self):
            name=self.path.removeprefix('/saved-media-export/');length=int(self.headers.get('Content-Length','0'))
            if (name not in {BASENAME+'.png',BASENAME+'.webm'} or not self.path.startswith('/saved-media-export/')
                or self.headers.get('Origin')!=f'http://127.0.0.1:{port}' or not 0<length<=64*1024*1024):
                self.send_error(400,'bounded local generated capture only');return
            raw=self.rfile.read(length);magic=b'\x89PNG\r\n\x1a\n' if name.endswith('.png') else b'\x1a\x45\xdf\xa3'
            if len(raw)!=length or not raw.startswith(magic):self.send_error(400,'invalid capture');return
            target=HERE/'captures'/name;target.parent.mkdir(exist_ok=True)
            try:
                with target.open('xb') as stream:stream.write(raw)
            except FileExistsError:self.send_error(409,'capture preserved; no overwrite');return
            payload=encoded({'saved':name,'bytes':length});self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    print(f'Saved CG export only: http://127.0.0.1:{port}/index.html',flush=True)
    ThreadingHTTPServer(('127.0.0.1',port),Handler).serve_forever()


def media(manifest):
    webm=HERE/'captures'/(BASENAME+'.webm');mp4=HERE/(BASENAME+'.mp4')
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-n','-i',str(webm),'-an','-c:v','libx264','-preset','medium','-crf','24','-maxrate','1200k','-bufsize','2400k','-pix_fmt','yuv420p','-r','24','-t','120','-movflags','+faststart',str(mp4)],check=True)
    if mp4.stat().st_size>manifest['media_maximum_mp4_bytes']:raise ValueError('mp4_exceeds_budget')
    poster=HERE/'mission-poster.jpg'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-n','-ss','23','-i',str(mp4),'-frames:v','1','-q:v','2',str(poster)],check=True)
    gif=HERE/'mission-preview.gif'
    filters="[0:v]trim=start=0:end=10,setpts=PTS/3.333[a];[0:v]trim=start=18:end=30,setpts=(PTS-STARTPTS)/3[b];[0:v]trim=start=106:end=120,setpts=(PTS-STARTPTS)/2[c];[a][b][c]concat=n=3:v=1:a=0,fps=6,scale=768:-1:flags=lanczos,split[x][y];[x]palettegen=max_colors=96[p];[y][p]paletteuse=dither=sierra2_4a"
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-n','-i',str(mp4),'-filter_complex',filters,'-an','-loop','0',str(gif)],check=True)
    if gif.stat().st_size>8*1024*1024:raise ValueError('preview_exceeds_budget')
    for p in (mp4,poster,gif):
        entry={'filename':p.name,'bytes':p.stat().st_size,'sha256':digest(p.read_bytes())}
        if p.suffix=='.mp4':
            probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(p)]));entry['video']={k:probe['streams'][0].get(k) for k in ('codec_name','width','height','pix_fmt','avg_frame_rate','nb_frames')};entry['duration_s']=float(probe['format']['duration'])
        manifest['media'].append(entry)
    manifest['capture_sha256']=digest(webm.read_bytes())
    manifest['capture_page_sha256']=digest((HERE/'index.html').read_bytes())
    manifest['capture_kind']='user-triggered browser canvas MediaRecorder; same saved page, no physics/model invocation'
    manifest['preview_segments_video_s']=[[0,10],[18,30],[106,120]]
    manifest['preview_is_excerpt']=True


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--study',type=Path);p.add_argument('--source-commit');p.add_argument('--refresh-display',action='store_true');p.add_argument('--summary',type=Path);p.add_argument('--rebuild-page',action='store_true');p.add_argument('--check',action='store_true');p.add_argument('--serve',action='store_true');p.add_argument('--port',type=int,default=18880);p.add_argument('--make-media',action='store_true');a=p.parse_args()
    if a.study:
        if not a.summary:p.error('--study requires saved verification --summary')
        previous = None
        if (HERE/'data.json').exists():
            if not a.refresh_display:p.error('curated dataset exists; explicit --refresh-display required')
            previous = json.loads((HERE/'data.json').read_bytes())
            if json.loads((HERE/'manifest.json').read_bytes())['media']:
                p.error('captured media is immutable; use a fresh artifact directory')
        data,manifest=build(a.study,a.summary,a.source_commit)
        if previous is not None:
            if (previous['source']['study_sha256'] != data['source']['study_sha256']
                    or any(previous[k] != data[k] for k in ('frames','booster','satellites','recovery_report'))):
                raise ValueError('display_refresh_cannot_change_flight_or_report')
            manifest['previous_display_dataset_sha256'] = digest(encoded(previous))
            manifest['display_refresh_preserved_all_flight_samples'] = True
        (HERE/'data.json').write_bytes(encoded(data));(HERE/'index.html').write_text(page(data));update_manifest(manifest,data)
    else:
        data=json.loads((HERE/'data.json').read_bytes());manifest=json.loads((HERE/'manifest.json').read_bytes());validate(data)
        if manifest['dataset_sha256']!=digest(encoded(data)):raise ValueError('dataset_digest_mismatch')
        if a.rebuild_page:(HERE/'index.html').write_text(page(data));update_manifest(manifest,data)
    if a.make_media:media(manifest);update_manifest(manifest,data)
    if a.check:
        if any(digest((HERE/name).read_bytes())!=h for name,h in manifest['render_sources_sha256'].items()):raise ValueError('renderer_digest_mismatch')
        if digest((HERE/'index.html').read_bytes())!=manifest['page_sha256']:raise ValueError('page_digest_mismatch')
        for x in manifest['media']:
            if digest((HERE/x['filename']).read_bytes())!=x['sha256']:raise ValueError('media_digest_mismatch')
        print(json.dumps({'passed':True,'ship_samples':len(data['frames']['rows']),'movie_seconds':120,'booster_model_valid':data['booster_decision']['model_valid'],'new_physics_or_model_calls':0}))
    if a.serve:serve(a.port)


if __name__=='__main__':main()
