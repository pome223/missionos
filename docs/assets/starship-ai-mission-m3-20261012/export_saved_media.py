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


def choice(row,phase):
    response=row.get('response') or {}
    dispatch=row.get('dispatch') or {}
    when=row.get('later_time_s',row.get('time_s',dispatch.get('time_s')))
    action=row.get('accepted_action',dispatch.get('action',row.get('action')))
    fallback=bool(row.get('fallback') or dispatch.get('response_invalid'))
    return {'time_s':when,'phase':phase,'provider':'DeepSeek' if (response.get('llm_invocation') or {}).get('model_inference_invoked') else 'Jev',
            'action':action,'adopted':bool(not fallback and action==response.get('action')),
            'fallback':fallback,'later_observation_time_s':(row.get('later_observation') or {}).get('time_s',row.get('later_time_s')),
            'rejection':dispatch.get('rejection')}


def comparison_material(value):
    """Copy only stable public numeric/claim fields, never arbitrary metadata."""
    selected={k:value[k] for k in ('schema','basis','candidate_sha256','notice_sha256','return_time_s',
              'reserve_after_loss_kg','contact_samples_available','limitations')}
    selected['waiting_at_burn']={k:value['waiting_at_burn'][k] for k in
        ('elapsed_wait_s','energy_remaining_kwh','temperature_upper_k','propellant_loss_upper_kg','valid_until_s')}
    selected['sampled_contact_ranges']={k:value['sampled_contact_ranges'][k] for k in
        ('time_s','speed_mps','tilt_deg','body_rate_rad_s','reserve_kg')}
    selected['area_service_estimates']={area:{k:entry[k] for k in
        ('service_delay_after_contact_s','estimated_completion_time_s')}
        for area,entry in value['area_service_estimates'].items() if area in ('east','west')}
    return selected


def build(study_path,summary_path):
    raw=study_path.read_bytes();summary_raw=summary_path.read_bytes()
    outer=json.loads(raw); audit=json.loads(summary_raw); role=audit['roles']['scope_amendment']
    if not (audit['bounded_m3_completed'] and role['record_verified'] and role['case_accepted']
            and role['study_sha256']==digest(raw) and role['scope_chain']['complete']):
        raise ValueError('verified_completed_same_flight_record_required')
    s=outer['m2_study'];prefix=s['concurrent_prefix'];launch=s['deployment_run'];returning=s['return_study'];executed=returning['execution']
    joint=('time_s','r_eci_m','v_eci_mps','q_body_to_eci','omega_body_rad_s','propellant_kg','engine_states','flap_angles_rad')
    if any(launch['samples'][-1][k]!=executed['samples'][0][k] for k in joint):
        raise ValueError('exact_execution_handoff_required')
    decisions=[choice(r,'ascent') for r in prefix['ascent']['decisions']]
    decisions += [choice(r,'booster') for r in prefix['prefix_run']['booster_mission_director']['records']]
    decisions += [choice(r,'deployment') for r in launch['mission_director']['records']]
    decisions += [choice(r,'return') for r in returning['decisions']]
    ds=next(r for r in returning['decisions'] if r['response'].get('decision_basis'))
    basis=ds['response']['decision_basis']
    # Pure deterministic reference lookup; never read provider reasoning_content.
    sys.path.insert(0,str(ROOT))
    from src.runtime.starship_reference_ids import catalog_for
    catalog={e['reference_id']:e for e in catalog_for(ds['request'])['entries']}
    references=[]
    for i,identifier in enumerate(basis['reference_ids']):
        item=catalog[identifier]
        references.append({'label':f'E{i+1:02d}',**{k:item[k] for k in ('candidate_id','field_path','value','unit')}})
    metrics=role['metrics'];chain=role['scope_chain']
    booster=launch['booster_run'];last=executed['samples'][-1]['time_s']
    segments=[
      (0.,135.6,12,'LAUNCH / AI MONITORING','ground','ship'),
      (135.6,480.7,7,'STAGE SEPARATION / ORBIT INSERTION','chase','ship'),
      (480.7,590.,5,'DEPLOYMENT / GUARDED CONTINUATION','payload','ship'),
      (590.,619.6551636219351,5,'SAME FLIGHT / BOOSTER WATER ENTRY','chase','booster'),
      (619.6551636219351,900.7,9,'26 RECORDED PAYLOAD RELEASES','payload','ship'),
      (900.7,1350.,4,'ORBIT / INITIAL RETURN RESERVATION','orbit','ship'),
      (1350.,1567.7,12,'CLEARANCE UPDATE / COMPARE AND DECIDE','orbit','ship'),
      (1567.7,1571.,10,'SCOPE EXTENSION / APPROVAL / LATER OBSERVATION','chase','ship'),
      (1571.,7731.45,14,'NEXT ORBIT / PERIODIC AI MONITORING','orbit','ship'),
      (7731.45,9900.,8,'APPROVED RETURN / EXECUTION','chase','ship'),
      (9900.,10340.,8,'ATMOSPHERIC RETURN / RECORDED ATTITUDE','orbit','ship'),
      (10340.,last,14,'TERMINAL DESCENT / MEASURED CONTACT','chase','ship'),
      (last,last,4,'VERIFIED RECORD / BOUNDED M3 COMPLETE','orbit','ship')]
    data={'schema':'missionos.starship_ai_saved_media.v1','title':'AI mission control. One complete flight.',
      'flight_label':'M3 / authority extension','frames':pack(launch['samples']+executed['samples']),
      'booster':pack(booster['samples']),
      'satellites':[{'label':f'Payload {i+1:02d}','release_s':b['samples'][0]['time_s'],'frames':pack(b['samples'])} for i,b in enumerate(launch['satellites'])],
      'events':[{'time_s':e['time_s'],'event':e['event']} for e in launch['events']+executed['events']],
      'decisions':sorted(decisions,key=lambda r:(r['time_s'],r['phase'])),
      'scope_timeline':[{k:e[k] for k in ('event','actor','time_s')} for e in chain['timeline']],
      'deepseek':{'decision_time_s':ds['later_time_s'],'request_time_s':ds['request']['time_s'],
          'external_decision_summary':basis['summary'],'reference_count':len(references),'references':references,
          'candidate_material':{key:{'return_time_s':v['return_time_s'],'constraint_rejections':v['constraint_rejections'],
              'comparison_material':comparison_material(v['comparison_material'])} for key,v in ds['request']['candidates'].items()},
          'free_prose_verified':False,'numeric_references_verified':True},
      'segments':[dict(zip(('start_s','end_s','video_s','label','camera','focus'),x)) for x in segments],
      'result':{'ship':{k:metrics['ship']['contact'][k] for k in ('time_s','speed_mps','tilt_deg','body_rate_rad_s','reserve_kg','latitude_deg','longitude_deg')},'booster_speed_mps':metrics['booster']['contact_speed_mps'],
          'released':26,'retained':0,'counts':{k:metrics['counts'][k] for k in ('jev_attempts','jev_valid','jev_adopted','deepseek_attempts','deepseek_valid','deepseek_adopted','reference_ids_submitted','reference_ids_resolved','invalid_reference_responses','fallback_decisions')},'bounded_m3_completed':True,
          'operator_kind':'authenticated scripted test operator','human_review_verified':False,
          'reference_checks':len(references),'model_failures_retained':1,'physical_execution':False},
      'presentation':{'sample_interpolation':'display-only linear position and quaternion slerp',
          'full_saved_time_range_s':[0.,last],'new_physics_states':False,'new_model_calls':0,
          'illustrative_geometry':True,'recorded_attitude':True,'satellite_service_verified':False,
          'terminal_display_hold_s':4.,'hold_grants_simulation_time':False},
      'source':{'source_commit':audit['source_commit'],'study_sha256':digest(raw),'reviewed_summary_sha256':digest(summary_raw)}}
    manifest={'schema':'missionos.starship_ai_saved_media_manifest.v1','source':data['source'],
      'same_flight_handoff':{k:True for k in joint},'independently_reviewed_record':True,
      'public_source_authentication_claimed':False,'private_raw_logs_included':False,
      'export_allowlist':'recorded numerical display fields, model actions, external decision summary, resolved numeric references, observed approval-chain events',
      'excluded':['raw provider exchanges','reasoning_content','credentials','private paths','session identifiers','signatures'],
      'claim':'One bounded M3 simulator-control record, including scripted operator scope approval; no physical spacecraft or flight-qualified recovery.',
      'media_maximum_mp4_bytes':20*1024*1024,'new_model_calls':0,'new_physics_steps':0,'media':[]}
    validate(data)
    return data,manifest


def validate(value):
    def visit(x):
        if isinstance(x,dict):
            if DENIED.intersection(x):raise ValueError('private_field_in_public_export')
            for v in x.values():visit(v)
        elif isinstance(x,list):
            for v in x:visit(v)
        elif isinstance(x,float) and not math.isfinite(x):raise ValueError('nonfinite_export')
        elif isinstance(x,str) and re.search(r'/Users/|/private/|Bearer\s|sk-[A-Za-z0-9]{12,}|session_01',x):
            raise ValueError('private_value_in_public_export')
    visit(value)
    for frames in [value['frames'],value['booster'],*(x['frames'] for x in value['satellites'])]:
        times=[r[0] for r in frames['rows']]
        if not times or any(b<=a for a,b in zip(times,times[1:])):raise ValueError('strict_saved_time_required')
        for row in frames['rows']:
            q=row[FIELDS.index('q_body_to_eci')]
            if abs(math.sqrt(sum(v*v for v in q))-1)>1e-6:raise ValueError('recorded_unit_quaternion_required')
    if sum(s['video_s'] for s in value['segments'])!=112:raise ValueError('fixed_movie_duration')
    if value['result']['reference_checks']!=9:raise ValueError('reviewed_reference_count')
    for item in value['deepseek']['references']:
        actual=value['deepseek']['candidate_material'][item['candidate_id']]['comparison_material']
        for key in item['field_path'].split('/')[1:]:
            actual=actual[int(key)] if isinstance(actual,list) else actual[key]
        if actual!=item['value']:raise ValueError('reference_value_mismatch')
    counts=value['result']['counts'];decisions=value['decisions']
    for name,prefix in [('Jev','jev'),('DeepSeek','deepseek')]:
        rows=[x for x in decisions if x['provider']==name]
        if len(rows)!=counts[prefix+'_attempts'] or sum(x['adopted'] for x in rows)!=counts[prefix+'_adopted']:
            raise ValueError('displayed_model_counts_mismatch')
    if sum(x['fallback'] for x in decisions)!=counts['fallback_decisions']:
        raise ValueError('displayed_fallback_count_mismatch')


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
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-n','-i',str(webm),'-an','-c:v','libx264','-preset','medium','-crf','24','-maxrate','1200k','-bufsize','2400k','-pix_fmt','yuv420p','-r','24','-t','112','-movflags','+faststart',str(mp4)],check=True)
    if mp4.stat().st_size>manifest['media_maximum_mp4_bytes']:raise ValueError('mp4_exceeds_budget')
    poster=HERE/'mission-poster.jpg'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-n','-ss','60','-i',str(mp4),'-frames:v','1','-q:v','2',str(poster)],check=True)
    gif=HERE/'mission-preview.gif'
    filters="[0:v]trim=start=0:end=10,setpts=PTS/3.333[a];[0:v]trim=start=40:end=64,setpts=(PTS-STARTPTS)/3[b];[0:v]trim=start=98:end=112,setpts=(PTS-STARTPTS)/2[c];[a][b][c]concat=n=3:v=1:a=0,fps=6,scale=768:-1:flags=lanczos,split[x][y];[x]palettegen=max_colors=96[p];[y][p]paletteuse=dither=sierra2_4a"
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
    manifest['preview_segments_video_s']=[[0,10],[40,64],[98,112]]
    manifest['preview_is_excerpt']=True


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--study',type=Path);p.add_argument('--summary',type=Path);p.add_argument('--rebuild-page',action='store_true');p.add_argument('--check',action='store_true');p.add_argument('--serve',action='store_true');p.add_argument('--port',type=int,default=18880);p.add_argument('--make-media',action='store_true');a=p.parse_args()
    if a.study:
        if not a.summary:p.error('--study requires reviewed --summary')
        if (HERE/'data.json').exists():p.error('curated dataset already exists; preserve it')
        data,manifest=build(a.study,a.summary);(HERE/'data.json').write_bytes(encoded(data));(HERE/'index.html').write_text(page(data));update_manifest(manifest,data)
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
        print(json.dumps({'passed':True,'ship_samples':len(data['frames']['rows']),'movie_seconds':112,'references':data['result']['reference_checks'],'new_physics_or_model_calls':0}))
    if a.serve:serve(a.port)


if __name__=='__main__':main()
