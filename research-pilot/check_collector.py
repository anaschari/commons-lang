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
