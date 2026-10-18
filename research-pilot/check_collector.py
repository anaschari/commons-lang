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
