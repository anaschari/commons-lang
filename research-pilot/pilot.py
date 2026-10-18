#!/usr/bin/env python3
# Copyright 2026 Anas Chari
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Run a prospective class-level technical collector without controlling test order."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

HERE=Path(__file__).resolve().parent
REPO=HERE.parent
OUTPUT=REPO/'target/pilot'
UPSTREAM='c89cdf682fe21b0c39759cf58186ed5336a105d4'
ENVIRONMENT='ubuntu-24.04-temurin-21-python-3.12-single-junit-class-slot'
CONFIG={'schema_version':2,'project':'commons-lang','phase':'technical_calibration',
        'efficacy_eligible':False,'unit':'outermost JUnit ClassSource container',
        'upstream_base':UPSTREAM,'budgets':[.1,.2,.3],
        'full_suite':True,'jupiter_parallel':False,'heap':'-Xmx1G',
        'class_filter':'.*Test excluding org.researchpilot.*',
        'target':'raw JUnit failed class; no reproducibility diagnosis',
        'suite_complete':'every frozen class has a binary observed result; leaf coverage reported separately',
        'publication':'fsynced class payload followed by live publication marker and collector ingestion',
        'cost_scope':'discovery and freeze pipeline charged; install/compile/diagnostic probe separate'}
JAVA_FLAGS=['-Xmx1G','-XX:+EnableDynamicAgentLoading']+sum((['--add-opens','java.base/'+p+'=ALL-UNNAMED'] for p in ('java.lang.reflect','java.lang','java.util','java.time','java.time.chrono','java.util.concurrent.atomic')),[])

def require(ok,message):
    if not ok:raise ValueError(message)

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def digest(x):return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()

def write(p,x):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('w') as f:json.dump(x,f,indent=2,ensure_ascii=False,allow_nan=False);f.write('\n');f.flush();os.fsync(f.fileno())

def append(p,x):
    with Path(p).open('a') as f:f.write(json.dumps(x,ensure_ascii=False,allow_nan=False)+'\n');f.flush();os.fsync(f.fileno())

def run_text(args):return subprocess.check_output(args,cwd=REPO,text=True,stderr=subprocess.STDOUT).strip()

def protected(p):
    p=Path(p).resolve();require(p.is_relative_to((REPO/'target').resolve()) and p!=(REPO/'target').resolve(),'output must be dedicated under target');return p

def signature():
    paths=sorted([HERE/'pilot.py',HERE/'policies.py',HERE/'validate.py',HERE/'frozen_model.py',HERE/'requirements.txt',REPO/'.github/workflows/shadow-pilot.yml',HERE/'java/META-INF/services/org.junit.platform.launcher.TestExecutionListener']+list((HERE/'java').rglob('*java')))
    files={str(p.relative_to(REPO)):sha(p) for p in paths}
    return digest({'files':files,'configuration':CONFIG,'environment':ENVIRONMENT}),files

def prepare():
    require(os.environ.get('GITHUB_REPOSITORY')=='anaschari/commons-lang','run in the authorized fork only')
    require(not OUTPUT.exists(),'new run output must not overwrite a prior observation')
    OUTPUT.mkdir(parents=True)
    commit=run_text(['git','rev-parse','HEAD']);commitat=time.time()
    require(run_text(['git','rev-parse','--abbrev-ref','HEAD'])=='research/shadow-pilot','wrong research branch')
    sig,files=signature()
    env={'profile':ENVIRONMENT,'java':run_text(['java','-version']),'maven':run_text(['mvn','--version']),
         'python':sys.version,'platform':platform.platform(),'cpu_count':os.cpu_count(),'observed_load':list(os.getloadavg()) if hasattr(os,'getloadavg') else None,'ImageOS':os.environ.get('ImageOS'),'ImageVersion':os.environ.get('ImageVersion'),'RUNNER_ARCH':os.environ.get('RUNNER_ARCH'),
         'dependency_versions':run_text([sys.executable,'-m','pip','freeze']),
         'clock':'Java millisecond epoch anchor plus monotonic deltas; Python live wall ingestion; uncertainty in inventory.json',
         'instrumentation':'callback fsync overhead is included in observed class durations'}
    meta={'commit':commit,'commit_captured_at':commitat,'run_id':os.environ['GITHUB_RUN_ID']+'-attempt-'+os.environ.get('GITHUB_RUN_ATTEMPT','1'),
          'workflow_run_id':int(os.environ['GITHUB_RUN_ID']),'workflow_run_attempt':int(os.environ.get('GITHUB_RUN_ATTEMPT','1')),
          'configuration':CONFIG,'signature':sig,'files':files,'environment':env,
          'github_run_url':os.environ['GITHUB_SERVER_URL']+'/'+os.environ['GITHUB_REPOSITORY']+'/actions/runs/'+os.environ['GITHUB_RUN_ID']}
    write(OUTPUT/'metadata.json',meta)
    compile_pom(REPO)
    print('Prepared prospective technical collector at',commit)

def compile_pom(repo):
    # A separate POM adds only launcher API to the test classpath. Tracked source/pom stay untouched.
    ns='http://maven.apache.org/POM/4.0.0';ET.register_namespace('',ns)
    tree=ET.parse(repo/'pom.xml');deps=tree.getroot().find('{'+ns+'}dependencies')
    dep=ET.SubElement(deps,'{'+ns+'}dependency')
    for k,v in [('groupId','org.junit.platform'),('artifactId','junit-platform-launcher'),('scope','test')]:ET.SubElement(dep,'{'+ns+'}'+k).text=v
    rat=next(p for p in tree.getroot().findall('.//{'+ns+'}plugin') if p.findtext('{'+ns+'}artifactId')=='apache-rat-plugin')
    configuration=rat.find('{'+ns+'}configuration')
    excludes=configuration.find('{'+ns+'}inputExcludes')
    ET.SubElement(excludes,'{'+ns+'}inputExclude').text='research-pilot/synthetic-example.jsonl'
    # ElementTree omits comments before the root: preserve the original Apache license.
    original=(repo/'pom.xml').read_text();license_comment=original[original.index('<!--'):original.index('-->')+3]
    (repo/'.pilot-pom.xml').write_text('<?xml version="1.0" encoding="UTF-8"?>\n'+license_comment+'\n'+ET.tostring(tree.getroot(),encoding='unicode'))

def restore_history(meta):
    # Run conclusions change on rerun. Select immutable collection artifacts instead.
    import re
    repo='anaschari/commons-lang'
    pages=json.loads(run_text(['gh','api','--paginate','--slurp',f'repos/{repo}/actions/artifacts?per_page=100']))
    artifacts=sorted((a for page in pages for a in page['artifacts']),key=lambda a:a['id'],reverse=True)
    prior=[];skipped=[];provenance={'status':'no_prior_valid_collection'}
    for artifact in artifacts:
        match=re.fullmatch(r'shadow-pilot-state-([1-9][0-9]*)',artifact['name'])
        if match is None:continue
        source=artifact['workflow_run'];rid=source['id'];attempt=int(match.group(1))
        if source['head_branch']!='research/shadow-pilot':continue
        if rid==meta['workflow_run_id'] and attempt>=meta['workflow_run_attempt']:continue
        run=json.loads(run_text(['gh','api',f'repos/{repo}/actions/runs/{rid}/attempts/{attempt}']))
        if run['path']!='.github/workflows/shadow-pilot.yml':continue
        require(run['repository']['full_name']==repo and run['head_branch']=='research/shadow-pilot'
                and run['id']==rid and run['run_attempt']==attempt and run['status']=='completed',
                'prior workflow attempt identity/status mismatch')
        require(source['repository_id']==source['head_repository_id']==run['repository']['id']
                and source['head_sha']==run['head_sha'],'prior artifact source identity mismatch')
        require(not artifact['expired'],'prior collection artifact expired: explicit dataset decision required')
        dest=OUTPUT/'prior-artifact.zip'
        with dest.open('wb') as f:
            subprocess.run(['gh','api',f'repos/{repo}/actions/artifacts/{artifact["id"]}/zip'],stdout=f,cwd=REPO,check=True)
        require(artifact.get('digest')=='sha256:'+sha(dest),'prior artifact digest mismatch or unavailable')
        with zipfile.ZipFile(dest) as z:
            names=z.namelist();require(len(names)==len(set(names)),'duplicate prior archive member')
            if 'summary.json' not in names:
                skipped.append({'artifact_id':artifact['id'],'source_run_id':rid,'source_run_attempt':attempt,
                                'reason':'no committed collection summary; raw/pre-test attempt retained separately'})
                dest.unlink();continue
            summary=json.loads(z.read('summary.json'))
            if summary.get('collection_valid') is not True:
                skipped.append({'artifact_id':artifact['id'],'source_run_id':rid,'source_run_attempt':attempt,
                                'reason':'collection explicitly invalid'})
                dest.unlink();continue
            require(all(name in names for name in ('manifest.json','state.json','metadata.json','events.jsonl')),
                    'valid prior collection lacks manifest/state/metadata/events')
            manifest=json.loads(z.read('manifest.json'))['files']
            require(all(name in manifest for name in ('state.json','summary.json','metadata.json','events.jsonl')),
                    'prior manifest omits required collection evidence')
            for name,item in manifest.items():
                require(Path(name).name==name and name in names,'prior manifest member mismatch')
                require(z.getinfo(name).file_size==item['bytes'],'prior manifest size mismatch')
                with z.open(name) as f:
                    hashed=hashlib.sha256()
                    for block in iter(lambda:f.read(1_048_576),b''):hashed.update(block)
                require(hashed.hexdigest()==item['sha256'],'prior manifest hash mismatch')
            # ponytail: cumulative JSON stops at 1GB; version the archive design if it reaches this ceiling.
            require(z.getinfo('state.json').file_size<1_000_000_000,'history export too large for pilot')
            state=json.loads(z.read('state.json'));metadata=json.loads(z.read('metadata.json'))
        require(state['source_run_id']==metadata['workflow_run_id']==rid
                and state['source_run_attempt']==metadata['workflow_run_attempt']==attempt
                and state['source_head_sha']==metadata['commit']==summary['commit']==run['head_sha']
                and metadata['run_id']==f'{rid}-attempt-{attempt}','prior collection source identity mismatch')
        require(state['signature']==metadata['signature']==meta['signature'],
                'collector/configuration changed: explicit separate dataset decision required')
        subprocess.run(['git','merge-base','--is-ancestor',run['head_sha'],meta['commit']],cwd=REPO,check=True)
        prior=state['events'];require(all(r['synthetic'] is False for r in prior),'synthetic history is inadmissible')
        from validate import validate
        validate(prior)
        decisions=[r for r in prior if r['type']=='decision']
        require(decisions[-1]['run_id']==metadata['run_id'] and decisions[-1]['commit']==run['head_sha']
                and all(r['frozen_hashes']['collector']==meta['signature'] for r in decisions),
                'prior history does not end at declared source or mixes collector signatures')
        provenance={'status':'restored','source_run_id':rid,'source_run_attempt':attempt,'head_sha':run['head_sha'],
                    'artifact_id':artifact['id'],'archive_sha256':sha(dest),'event_count':len(prior)}
        dest.unlink();break
    provenance['skipped_artifacts']=skipped
    write(OUTPUT/'history.json',prior);write(OUTPUT/'history-provenance.json',provenance)

def freeze(inventory_path,output):
    output=protected(output);meta=json.loads((output/'metadata.json').read_text())
    inv=json.loads(Path(inventory_path).read_text());inventory=sorted(x['class_id'] for x in inv['classes'])
    require(inventory and len(set(inventory))==len(inventory),'empty/duplicate discovered class inventory')
    prior=json.loads((output/'history.json').read_text());require(all(r['synthetic'] is False for r in prior),'synthetic history')
    start=json.loads((output/'run-start.json').read_text())['preparation_started_at']
    # Import/acquisition startup is included in the measured acquisition phase.
    from policies import score
    cutoff=time.time();index=sum(r['type']=='decision' for r in prior);did='commons-lang:'+meta['run_id']
    result=score(inventory,prior,index,decision_id=did,cutoff_at=cutoff,project='commons-lang',environment=ENVIRONMENT,acquisition_s=cutoff-start)
    final_start=time.perf_counter()
    decision={'schema_version':2,'synthetic':False,'type':'decision','id':did,'run_id':meta['run_id'],'commit':meta['commit'],
       'project':'commons-lang','environment':ENVIRONMENT,'mode':'shadow','ci_order_changed':False,
       'technical_calibration':True,'efficacy_eligible':False,'phase':'technical_calibration',
       'decision_index':index,'history_cutoff_at':cutoff,'preparation_started_at':start,
       'inventory_captured_at':inv['inventory_at_epoch_ns']/1e9,'commit_captured_at':meta['commit_captured_at'],
       'features_frozen_at':result['features_frozen_at'],'inventory':inventory,'inventory_sha256':digest(inventory),
       'inventory_evidence':'inventory.json from JUnit TestPlan before any tests','commit_evidence':'metadata.json git rev-parse HEAD before suite',
       'chosen_ci_order':None,'chosen_ci_order_missing_reason':'JUnit discovery inventory is not a promised execution order; observed starts are recorded separately',
       'resources':{'runner_slots':1,'dependencies':[],'description':'one JUnit launcher, parallel execution disabled; implicit dependencies unknown'},
       'frozen_hashes':{'collector':meta['signature'],'config':digest(CONFIG),'environment':digest(meta['environment'])},
       'feature_target':'raw_failed','features':result['features'],'features_sha256':result['features_sha256'],
       'policies':result['policies'],'model_state':result['model_state'],'policy_state':result['state']}
    # Serialize a predecision draft to charge batch finalization; the committed freeze receipt follows.
    write(output/'decision-draft.json',decision)
    overhead=time.perf_counter()-final_start
    for p in decision['policies']:
        p['costs_s']['finalization']+=overhead;p['costs_s']['total']+=overhead
        p['callback_tail_reserved_s']=1.
        p['preparation_charged_s']=p['costs_s']['total']+1.
    decision['decision_at']=time.time();decision['recorded_at']=time.time()
    write(output/'decision.json',decision)
    write(output/'freeze-receipt.json',{'decision_sha256':sha(output/'decision.json'),'persisted_at':time.time(),
          'tail_persistence_measured_seconds':time.perf_counter()-final_start-overhead,'callback_tail_guard_seconds':1.,
          'cost_note':'policy preparation charges draft finalization plus conservative one second callback tail reserve; actual callback tail checked before any efficacy use'})
    print('Froze',len(inventory),'classes and',len(decision['policies']),'policy snapshots before execution')

def result_event(decision,data,pub,ingested):
    require(data['class_id']==pub['class_id'] and data['sequence']==pub['data_sequence'],'publication payload identity mismatch')
    finished=data['finished_at_epoch_ns']/1e9;published=pub['published_at_epoch_ns']/1e9
    require(math.isfinite(ingested) and finished<=published<=ingested,'publication/ingestion clock inversion')
    began=data['started_at_epoch_ns'];duration=data['duration_ns']
    outcome={'PASSED':'passed','FAILED':'failed','ABORTED':'aborted','SKIPPED':'skipped'}[data['outcome']]
    return {'schema_version':2,'synthetic':False,'technical_calibration':True,'efficacy_eligible':False,
      'type':'result','id':decision['id']+':result:'+data['class_id'],'decision_id':decision['id'],'run_id':decision['run_id'],'commit':decision['commit'],
      'recorded_at':ingested,'test_id':data['class_id'],'started_at':began/1e9 if began is not None else None,
      'finished_at':finished,'duration_s':duration/1e9 if duration is not None else None,'slot':1 if began is not None else None,
      'raw_outcome':outcome,'junit_class_observation':data,'published_at':published,'ingested_at':ingested,'available_at':ingested,
      'publication_evidence':'live-raw.jsonl#sequence='+str(pub['sequence'])+'; after class payload fsync',
      'ingestion_evidence':'events.jsonl#'+decision['id']+':result:'+data['class_id'],
      'result_evidence':'live-raw.jsonl#sequence='+str(data['sequence'])}

def collect():
    meta=json.loads((OUTPUT/'metadata.json').read_text());write(OUTPUT/'run-start.json',{'preparation_started_at':time.time()})
    restore_history(meta)
    os.environ.pop('GH_TOKEN',None)
    cp='target/test-classes:target/classes:'+(REPO/'target/pilot-classpath.txt').read_text().strip()
    command=['java']+JAVA_FLAGS+['-cp',cp,'org.researchpilot.PilotLauncher','target/test-classes']
    env=dict(os.environ,PILOT_OUTPUT_DIR=str(OUTPUT),PILOT_BRIDGE_SCRIPT=str(HERE/'pilot.py'),PILOT_PYTHON=sys.executable)
    write(OUTPUT/'execution-command.json',{'argv':command,'actual_environment':meta['environment']})
    process=subprocess.Popen(command,cwd=REPO,env=env)
    raw=None;decision=None;rows=[];payloads={};rawrows=[];seen=set();sequence=0
    try:
        while True:
            if raw is None and (OUTPUT/'live-raw.jsonl').exists():raw=(OUTPUT/'live-raw.jsonl').open()
            position=raw.tell() if raw else None
            line=raw.readline() if raw else ''
            if line:
                if not line.endswith('\n'):
                    require(process.poll() is None,'truncated final raw event');raw.seek(position);time.sleep(.01);continue
                event=json.loads(line);sequence+=1;require(event['sequence']==sequence,'noncontiguous raw sequence');rawrows.append(event)
                kind=event['event']
                if kind=='collection_error':raise ValueError('listener collection error: '+json.dumps(event))
                if kind=='class_finished':payloads[event['sequence']]=event
                if kind=='class_published':
                    if decision is None:
                        decision=json.loads((OUTPUT/'decision.json').read_text());rows.append(decision);append(OUTPUT/'events.jsonl',decision)
                    require(event['class_id'] not in seen,'duplicate primary class result');seen.add(event['class_id'])
                    result=result_event(decision,payloads[event['data_sequence']],event,time.time())
                    rows.append(result);append(OUTPUT/'events.jsonl',result)
                continue
            if process.poll() is not None:break
            time.sleep(.005)
        exit_code=process.wait()
    finally:
        # A collection error never cancels or kills the underlying complete test suite.
        if process.poll() is None:process.wait()
        if raw:raw.close()
    require(decision is not None,'no frozen decision/results from JUnit plan')
    require(any(r['event']=='freeze_completed' for r in rawrows),'freeze callback did not complete')
    end=next((r for r in reversed(rawrows) if r['event']=='plan_finished'),None)
    require(end is not None,'full JUnit plan lacks end marker')
    receipt=json.loads((OUTPUT/'freeze-receipt.json').read_text())
    first=min((r['started_at'] for r in rows if r['type']=='result' and r['started_at'] is not None),default=None)
    require(first is None or receipt['persisted_at']<=first,'decision file was not durably frozen before first class')
    freeze_end=next(r['observed_at_epoch_ns']/1e9 for r in rawrows if r['event']=='freeze_completed')
    tail=freeze_end-decision['decision_at']
    require(0<=tail<=receipt['callback_tail_guard_seconds'],'freeze callback exceeded charged tail reserve')
    observed=[r for r in rows if r['type']=='result'];complete=set(seen)==set(decision['inventory']) and all(r['raw_outcome'] in ('passed','failed') for r in observed)
    executed=[r for r in observed if r['started_at'] is not None]
    leaf_complete=complete and all(sum(r['junit_class_observation'][k] for k in ('tests_aborted','tests_skipped','tests_not_executed'))==0 for r in observed)
    suite={'schema_version':2,'synthetic':False,'technical_calibration':True,'efficacy_eligible':False,
       'type':'suite_end','id':decision['id']+':end','decision_id':decision['id'],'run_id':decision['run_id'],'commit':decision['commit'],
       'recorded_at':time.time(),'finished_at':end['observed_at_epoch_ns']/1e9,'status':'complete' if complete else 'incomplete',
       'incomplete_reason':None if complete else 'missing, entirely skipped or aborted primary class; retained explicitly',
       'normal_ci_full_suite':True,'execution_harness':'instrumented full JUnit class suite; not untouched upstream Maven CI',
       'full_known_leaf_coverage':leaf_complete,'result_ids':[r['id'] for r in observed],
       'actual_start_order':[r['test_id'] for r in sorted(executed,key=lambda r:(r['started_at'],r['test_id']))],
       'execution_deviations':'JUnit controls class order; original planned order unexposed; callback fsync overhead included',
       'test_process_exit_code':exit_code}
    rows.append(suite);append(OUTPUT/'events.jsonl',suite)
    prior=json.loads((OUTPUT/'history.json').read_text())
    from validate import validate
    checks=validate(prior+rows)
    state={'signature':meta['signature'],'source_run_id':meta['workflow_run_id'],'source_run_attempt':meta['workflow_run_attempt'],
           'source_head_sha':meta['commit'],'events':prior+rows}
    write(OUTPUT/'state.json',state)
    counts={k:sum(r['raw_outcome']==k for r in observed) for k in ('passed','failed','aborted','skipped','error','timed_out')}
    report={'collection_valid':True,'phase':'technical_calibration','efficacy_eligible':False,'commit':meta['commit'],'upstream_base':UPSTREAM,
            'run_url':meta['github_run_url'],'classes_frozen':len(decision['inventory']),'classes_observed':len(observed),
            'class_outcomes':counts,'primary_inventory_complete':complete,'full_known_leaf_coverage':leaf_complete,
            'junit_test_counts':{k:sum(r['junit_class_observation'][k] for r in observed) for k in ('tests_succeeded','tests_failed','tests_aborted','tests_skipped','tests_not_executed','tests_dynamic_registered')},
            'policy_snapshots':len(decision['policies']),'policy_state':decision['policy_state'],'validation':checks,
            'test_process_exit_code':exit_code,'actual_freeze_callback_tail_s':tail,'charged_callback_tail_guard_s':receipt['callback_tail_guard_seconds'],'measured_full_collector_preparation_s':freeze_end-decision['preparation_started_at'],'cost_note':CONFIG['cost_scope'],'limitations':['technical calibration only; no efficacy comparison','observer overhead included in class durations','unknown planned engine order','class inventory known; dynamic leaf count initially unknown','no reproduced-failure diagnosis']}
    write(OUTPUT/'summary.json',report)
    write(OUTPUT/'manifest.json',{'files':{p.name:{'sha256':sha(p),'bytes':p.stat().st_size} for p in sorted(OUTPUT.iterdir()) if p.is_file() and p.name!='manifest.json'}})
    print(json.dumps(report,indent=2))
    summary=os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary,'a') as f:f.write('## Technical shadow pilot\n\nCollection contract passed. Test outcomes are data; workflow success does not mean all tests passed.\n\n```json\n'+json.dumps(report,indent=2)+'\n```\n')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('prepare');sub.add_parser('run')
    f=sub.add_parser('freeze');f.add_argument('--inventory',required=True);f.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        if args.command=='prepare':prepare()
        elif args.command=='freeze':freeze(args.inventory,args.output)
        else:collect()
    except Exception as error:
        if OUTPUT.exists():
            write(OUTPUT/('freeze-error.json' if args.command=='freeze' else 'collection-status.json'),{'collection_valid':False,'incomplete':True,'phase':args.command,'error':type(error).__name__+': '+str(error),'recorded_at':time.time(),'efficacy_eligible':False})
        raise
