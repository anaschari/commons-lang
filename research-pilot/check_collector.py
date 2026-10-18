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

"""Small real-boundary checks; synthetic inputs are never study observations."""
import copy
from pilot import result_event

decision={'id':'d','run_id':'r','commit':'c'}
payload={'class_id':'suite.A','sequence':5,'started_at_epoch_ns':100000000000,'finished_at_epoch_ns':101000000000,'duration_ns':1000000000,'outcome':'PASSED','tests_succeeded':1,'tests_failed':0,'tests_aborted':0,'tests_skipped':0,'tests_not_executed':0}
publication={'data_sequence':5,'class_id':'suite.A','sequence':6,'published_at_epoch_ns':102000000000}
x=result_event(decision,payload,publication,103.)
assert x['duration_s']==1 and x['available_at']==103 and x['published_at']==102
bad=copy.deepcopy(publication);bad['class_id']='suite.B'
try:result_event(decision,payload,bad,103.)
except ValueError:pass
else:raise AssertionError('mismatched publication accepted')
skipped=copy.deepcopy(payload);skipped.update(started_at_epoch_ns=None,duration_ns=None,outcome='SKIPPED')
x=result_event(decision,skipped,publication,103.)
assert x['duration_s'] is None and x['started_at'] is None and x['raw_outcome']=='skipped'
print('PASS collector publication identity, real availability and unknown skipped duration')

# The overlay must preserve current RAT input exclusions rather than override them.
from pilot import compile_pom, REPO
from pathlib import Path
import tempfile,xml.etree.ElementTree as ET
with tempfile.TemporaryDirectory() as temp:
 p=Path(temp);p.joinpath('pom.xml').write_bytes((REPO/'pom.xml').read_bytes())
 compile_pom(p)
 text=(p/'.pilot-pom.xml').read_text()
 ns={'m':'http://maven.apache.org/POM/4.0.0'};tree=ET.fromstring(text)
 plugin=next(x for x in tree.findall('.//m:plugin',ns) if x.findtext('m:artifactId',namespaces=ns)=='apache-rat-plugin')
 excludes=[x.text for x in plugin.findall('.//m:inputExclude',ns)]
 assert 'src/test/resources/lang-708-input.txt' in excludes
 assert 'src/site/resources/release-notes/RELEASE-NOTES-*.txt' in excludes
 assert 'research-pilot/synthetic-example.jsonl' in excludes
 assert not tree.findall('.//m:plugin[m:artifactId="apache-rat-plugin"]/m:configuration/m:excludes',ns)
 assert '<!--' in text and 'Licensed to the Apache Software Foundation' in text
print('PASS compile POM preserves current RAT exclusions and license notice')

# A GitHub run conclusion changes on rerun; immutable collection artifacts must not vanish.
import hashlib,io,json,os,zipfile
from unittest.mock import patch
import pilot


def history_checks():
 fixture=[json.loads(line) for line in (pilot.HERE/'synthetic-example.jsonl').read_text().splitlines()[:4]]
 def archive(rid,attempt,signature='a'*64,complete=True,corrupt_manifest=False):
  rows=copy.deepcopy(fixture)
  for row in rows:
   row.update(synthetic=False,run_id=f'{rid}-attempt-{attempt}',commit='b'*40)
   if row['type']=='decision':row['frozen_hashes']['collector']=signature
  source={'workflow_run_id':rid,'workflow_run_attempt':attempt,'run_id':f'{rid}-attempt-{attempt}','commit':'b'*40,'signature':signature}
  files={'metadata.json':json.dumps(source).encode()}
  if complete:
   files.update({'summary.json':json.dumps({'collection_valid':True,'commit':'b'*40}).encode(),
                 'state.json':json.dumps({'source_run_id':rid,'source_run_attempt':attempt,'source_head_sha':'b'*40,'signature':signature,'events':rows}).encode(),
                 'events.jsonl':''.join(json.dumps(row)+'\n' for row in rows).encode()})
   manifest={'files':{name:{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in files.items()}}
   files['manifest.json']=json.dumps(manifest).encode()
   if corrupt_manifest:files['summary.json']+=b' '
  output=io.BytesIO()
  with zipfile.ZipFile(output,'w') as z:
   for name,data in files.items():z.writestr(name,data)
  return output.getvalue()
 def artifact(ident,rid,attempt,data):
  return {'id':ident,'name':f'shadow-pilot-state-{attempt}','expired':False,'digest':'sha256:'+hashlib.sha256(data).hexdigest(),
          'workflow_run':{'id':rid,'repository_id':1,'head_repository_id':1,'head_branch':'research/shadow-pilot','head_sha':'b'*40}}
 def exercise(items,blobs,current=(9,1)):
  pages=[{'artifacts':items[:1]},{'artifacts':items[1:]}] if items else [{'artifacts':[]}]
  def text(args):
   if args[:3]==['gh','run','list']:return '[]'  # Overall status excludes the old successful attempt.
   if args[:2]==['gh','api'] and '/actions/artifacts?' in args[-1]:
    assert '--paginate' in args and '--slurp' in args,'artifact search must not truncate at first page'
    return json.dumps(pages)
   if args[:2]==['gh','api'] and '/attempts/' in args[-1]:
    rid=int(args[-1].split('/runs/')[1].split('/')[0]);attempt=int(args[-1].rsplit('/',1)[1])
    return json.dumps({'id':rid,'run_attempt':attempt,'head_sha':'b'*40,'head_branch':'research/shadow-pilot','path':'.github/workflows/shadow-pilot.yml',
                       'status':'completed','conclusion':'failure' if attempt==2 else 'success','repository':{'id':1,'full_name':'anaschari/commons-lang'}})
   raise AssertionError('unexpected network command '+repr(args))
  def command(args,**kwargs):
   if args[:2]==['gh','api']:
    ident=int(args[-1].split('/artifacts/')[1].split('/')[0]);kwargs['stdout'].write(blobs[ident])
   elif args[:3]!=['git','merge-base','--is-ancestor']:raise AssertionError(args)
  with tempfile.TemporaryDirectory() as temp,patch.object(pilot,'OUTPUT',Path(temp)),patch.object(pilot,'run_text',text),patch.object(pilot.subprocess,'run',command):
   pilot.restore_history({'signature':'a'*64,'commit':'c'*40,'workflow_run_id':current[0],'workflow_run_attempt':current[1]})
   return json.loads((Path(temp)/'history.json').read_text()),json.loads((Path(temp)/'history-provenance.json').read_text())
 data=archive(7,1);a=artifact(2,7,1,data)
 rows,proof=exercise([a],{2:data},current=(7,2))
 assert len(rows)==4 and proof['source_run_id']==7 and proof['source_run_attempt']==1,'rerun lost prior admitted attempt'
 partial=archive(7,2,complete=False);later=artifact(3,7,2,partial)
 self_data=archive(7,2);self_artifact=artifact(8,7,2,self_data)
 rows,proof=exercise([self_artifact,a],{8:self_data,2:data},current=(7,2))
 assert proof['source_run_attempt']==1,'current attempt admitted its own artifact'
 rows,proof=exercise([later,a],{3:partial,2:data})
 assert len(rows)==4 and proof['artifact_id']==2,'failed latest attempt hid earlier valid collection'
 rows,proof=exercise([],{});assert rows==[] and proof['status']=='no_prior_valid_collection'
 rows,proof=exercise([later],{3:partial});assert rows==[] and proof['skipped_artifacts'][0]['artifact_id']==3
 for broken,blob in ((dict(a,digest='sha256:'+'0'*64),data),
                     (artifact(4,7,1,archive(7,1,signature='d'*64)),archive(7,1,signature='d'*64)),
                     (artifact(5,7,1,archive(7,1,corrupt_manifest=True)),archive(7,1,corrupt_manifest=True)),
                     (dict(a,id=6,expired=True),data),
                     (artifact(7,7,1,archive(7,2)),archive(7,2))):
  try:exercise([broken,a],{2:data,broken['id']:blob})
  except ValueError:pass
  else:raise AssertionError('corrupt/incompatible signed history silently became bootstrap')
 print('PASS artifact history: pagination, reruns, failed attempts, pre-test exports, digest/manifest/signature fail closed')

history_checks()
