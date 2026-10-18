#!/usr/bin/env python3
"""Exploratory raw-failed-class replay. No claim of deployment-admissible availability."""
import os
for _k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','VECLIB_MAXIMUM_THREADS','NUMEXPR_NUM_THREADS','BLIS_NUM_THREADS'):
    os.environ[_k] = '1'
import argparse
import csv
from collections import Counter
from datetime import datetime, timezone
import hashlib
import gzip
import io
import json
import math
from pathlib import Path
import platform
import random
import resource
import statistics
import sys
import time
import warnings
import zipfile
import numpy as np
import scipy
from scipy.special import expit
import sklearn
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss
from threadpoolctl import threadpool_info, threadpool_limits
threadpool_limits(limits=1)
ROOT = Path(__file__).resolve().parent
FEATURES = ['last1','last5','last20','n','rate','sincefailure','transitions','decisionage','c_est','history_missing','lastfailure_missing','last_execution_missing','duration_missing']
CS = [.01,.1,1.,10.]
METHODS = ['B1','B2','B3','B4','B5','LOGIT']


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def dump(p, obj):
    Path(p).write_text(json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False) + '\n')


def write_csv(p, rows):
    if not rows:
        Path(p).write_text('')
        return
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with Path(p).open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


class Clock:
    def __init__(self):
        self.wall = time.perf_counter()
        self.cpu = time.process_time()
    def elapsed(self):
        return time.perf_counter()-self.wall, time.process_time()-self.cpu


def load_builds(metadata, data_dir, project):
    """Adapted from immutable pilot loader: positive-only historical costs fixed here."""
    with Path(metadata).open() as f:
        rows = [r for r in csv.DictReader(f) if r['project']==project]
    builds, sources = [], []
    for row in rows:
        if row['stage_id'] != 'single':
            raise ValueError('This implementation admits only single-stage builds')
        key = f"{row['pr_name']}_build{row['build_id']}"
        p = Path(data_dir) / f'{key}.csv.zip'
        with zipfile.ZipFile(p) as z:
            records = list(csv.DictReader(io.StringIO(z.read('test_class.csv').decode('utf-8'))))
        tests = {r['testclass']:(float(r['duration']),int(r['outcome'])) for r in records}
        if len(records)!=len(tests) or not tests:
            raise ValueError(f'duplicate or empty inventory {key}')
        if any(not math.isfinite(d) or d<0 or y not in (0,1) for d,y in tests.values()):
            raise ValueError(f'invalid oracle {key}')
        if len(tests)!=int(row['num_pass_class'])+int(row['num_fail_class']) or sum(y for d,y in tests.values())!=int(row['num_fail_class']):
            raise ValueError(f'count mismatch {key}')
        start, duration = float(row['build_timestamp']),float(row['build_duration'])
        if not math.isfinite(start+duration) or duration<=0:
            raise ValueError(f'invalid build metadata {key}')
        builds.append({'key':key,'start':start,'available':start+duration+2,'date':row['build_date'],'revision':row['build_head_sha'],'tests':tests})
        sources.append({'path':str(p),'sha256':sha(p),'bytes':p.stat().st_size})
    builds.sort(key=lambda b:(b['start'],b['key']))
    if len({b['key'] for b in builds})!=len(builds):
        raise ValueError('duplicate build key')
    for i,b in enumerate(builds):
        b['index']=i
    return builds,sources


def admit(history, build):
    """Only called after proxy availability; independent full logger, not selected feedback."""
    for name,(duration,y) in build['tests'].items():
        h=history.setdefault(name,{'ys':[],'sum':0.,'dn':0,'lastcost':None,'lastfail':None,'since':None,'trans':0,'r':0,'lastindex':None})
        if h['ys'] and y!=h['ys'][-1]: h['trans']+=1
        h['ys'].append(y)
        if duration>0:
            h['sum']+=duration; h['dn']+=1; h['lastcost']=duration
        if y:
            h['lastfail']=build['available']; h['since']=0; h['r']=1
        else:
            h['since']=None if h['since'] is None else h['since']+1
            h['r']+=1
        h['lastindex']=build['index']


def features(names, history, index):
    means=[h['sum']/h['dn'] for h in history.values() if h['dn']]
    fallback=statistics.median(means) if means else float('nan')
    x=[]; costs=[]; lastfails=[]; lrts=[]
    for name in names:
        h=history.get(name); ys=h['ys'] if h else []
        missing=not h or not h['dn']
        cost=h['sum']/h['dn'] if not missing else fallback
        x.append(([sum(ys[-k:]) for k in (1,5,20)] if ys else [np.nan]*3)+[len(ys),sum(ys)/len(ys) if ys else np.nan,h['since'] if h and h['since'] is not None else np.nan,h['trans'] if ys else np.nan,index-h['lastindex'] if ys else np.nan,cost,int(not ys),int(not h or h['lastfail'] is None),int(not ys),int(missing)])
        costs.append(cost)
        lastfails.append(h['lastfail'] if h and h['lastfail'] is not None else -math.inf)
        lrts.append(h['r']*(h['lastcost'] if h['lastcost'] is not None else cost) if h else 0.)
    return np.asarray(x,float),np.asarray(costs,float),lastfails,lrts


def rule_order(names, costs, lastfails, lrts, method, seed, key):
    if method=='B1': return list(range(len(names)))
    if method=='B2':
        order=list(range(len(names))); random.Random(f'{key}:{seed}').shuffle(order); return order
    if method=='B3': return sorted(range(len(names)),key=lambda i:(costs[i],names[i]))
    if method=='B4': return sorted(range(len(names)),key=lambda i:(-lastfails[i],costs[i],names[i]))
    if method=='B5': return sorted(range(len(names)),key=lambda i:(lrts[i],names[i]))
    raise ValueError(method)


def transform_fit(x):
    med=np.asarray([np.median(v[np.isfinite(v)]) if np.isfinite(v).any() else 0. for v in x.T])
    v=np.where(np.isfinite(x),x,med)
    mean=np.zeros(x.shape[1]); scale=np.ones(x.shape[1])
    mean[:9]=v[:,:9].mean(axis=0); scale[:9]=v[:,:9].std(axis=0)
    scale[scale==0]=1.
    return med,mean,scale


def transform(x, prep):
    med,mean,scale=prep
    v=(np.where(np.isfinite(x),x,med)-mean)/scale
    if not np.isfinite(v).all(): raise ValueError('nonfinite transformed features')
    return v


def refit(past, decision):
    clk=Clock(); timing={}; log={'decision':decision['key'],'decision_index':decision['index'],'cutoff_epoch':decision['start'],'past_available_builds':len(past),'positive_rows':sum(sum(y for d,y in b['tests'].values()) for b in past)}
    window=past[-200:]
    log['positive_rows']=sum(sum(y for d,y in b['tests'].values()) for b in window)
    log['window_builds']=[b['key'] for b in window]
    if len(past)<50 or log['positive_rows']<20:
        log['reason']='insufficient_past_builds' if len(past)<50 else 'insufficient_positive_rows'
        log['wall_seconds'],log['cpu_seconds']=clk.elapsed(); return None,log
    n=len(window); c1=int(.6*n); c2=c1+int(.2*n)
    blocks=[window[:c1],window[c1:c2],window[c2:]]
    log['partitions']={s:[b['key'] for b in bs] for s,bs in zip(('fit','select','calibrate'),blocks)}
    arrays=[]
    for bs in blocks:
        xx=np.vstack([b['x_asof'] for b in bs]); yy=np.concatenate([b['y'] for b in bs]); arrays.append((xx,yy))
    log['partition_rows']=[len(y) for x,y in arrays]; log['partition_positive_rows']=[int(y.sum()) for x,y in arrays]
    if any(len(np.unique(y))<2 for x,y in arrays):
        log['reason']='partition_lacks_both_classes'; log['wall_seconds'],log['cpu_seconds']=clk.elapsed(); return None,log
    try:
        t=Clock(); prep=transform_fit(arrays[0][0]); zz=[transform(x,prep) for x,y in arrays]; timing['transforms']=t.elapsed()
        fit_wall=fit_cpu=select_wall=select_cpu=0.; candidates=[]
        with warnings.catch_warnings():
            warnings.simplefilter('error',ConvergenceWarning)
            for c in CS:
                t=Clock(); model=LogisticRegression(C=c,solver='lbfgs',max_iter=1000,random_state=0).fit(zz[0],arrays[0][1]); w,cp=t.elapsed();fit_wall+=w;fit_cpu+=cp
                t=Clock(); probs=model.predict_proba(zz[1])[:,1]; losses=[]; offset=0
                for b in blocks[1]:
                    m=len(b['y']);losses.append(log_loss(b['y'],probs[offset:offset+m],labels=[0,1]));offset+=m
                loss=float(np.mean(losses)); w,cp=t.elapsed();select_wall+=w;select_cpu+=cp
                candidates.append((loss,c,model))
            loss,c,base=min(candidates,key=lambda q:(q[0],q[1]))
            t=Clock(); logits=base.decision_function(zz[2]); calib=LogisticRegression(C=np.inf,solver='lbfgs',max_iter=1000,random_state=0).fit(logits.reshape(-1,1),arrays[2][1]);timing['calibration']=t.elapsed()
        timing['four_base_fits']=(fit_wall,fit_cpu);timing['selection']=(select_wall,select_cpu)
        result={'prep':prep,'base':base,'calib':calib,'C':c,'refit_key':decision['key'],'refit_index':decision['index']}
        log.update(reason='fitted',selected_C=c,selection_logloss=loss,candidate_losses={str(cc):ll for ll,cc,_ in candidates},calibration_slope=float(calib.coef_[0,0]),calibration_intercept=float(calib.intercept_[0]),fit_base_rows=len(arrays[0][1]),base_coefficients=base.coef_[0].tolist(),base_intercept=float(base.intercept_[0]),imputation_medians=prep[0].tolist(),scaling_means=prep[1].tolist(),scaling_stds=prep[2].tolist())
    except (ValueError,FloatingPointError,ConvergenceWarning) as e:
        result=None;log['reason']='fit_failure:'+type(e).__name__+':'+str(e)
    log['components']={k:{'wall_seconds':v[0],'cpu_seconds':v[1]} for k,v in timing.items()}
    log['wall_seconds'],log['cpu_seconds']=clk.elapsed()
    return result,log


def probabilities(model,x):
    logits=model['base'].decision_function(transform(x,model['prep']))
    raw=expit(logits)
    cal=model['calib'].predict_proba(logits.reshape(-1,1))[:,1]
    if not np.isfinite(raw).all() or not np.isfinite(cal).all(): raise ValueError('nonfinite predictions')
    return raw,cal


def explain_adjacent(model,x,costs,order):
    """Full-score ordered replacement; batch all adjacent pairs before boundary known."""
    if len(order)<2: return [],0.
    target=x[order[:-1]].copy(); ref=x[order[1:]].copy()
    denominators=costs[order[1:]].copy()
    _,p=probabilities(model,ref); start=p/denominators; vals=[start]
    for cols in (list(range(8)),[8],list(range(9,13))):
        ref[:,cols]=target[:,cols]
        if cols==[8]: denominators=costs[order[:-1]].copy()
        _,p=probabilities(model,ref); vals.append(p/denominators)
    _,p=probabilities(model,target); expected=p/costs[order[:-1]]-start
    contributions=np.stack([vals[i+1]-vals[i] for i in range(3)],axis=1)
    residual=contributions.sum(axis=1)-expected
    if not np.allclose(residual,0.,rtol=1e-9,atol=1e-10):raise AssertionError('explanation completeness')
    return contributions,float(np.max(np.abs(residual)))


def estimated_prefix(order,costs,deadline,prep):
    allowance=max(0.,deadline-prep); elapsed=0.; prefix=[]
    for i in order:
        if elapsed+costs[i]>allowance:break
        prefix.append(i);elapsed+=costs[i]
    return prefix


def evaluate(prefix,actual,deadline,prep):
    total=sum(y for d,y in actual); elapsed=0.;completed=detected=started=late=late_fail=0;first=None
    for i in prefix:
        if prep+elapsed>=deadline:break
        started+=1;d,y=actual[i];elapsed+=d
        if prep+elapsed>deadline:
            late=1;late_fail=y;break
        completed+=1;detected+=y
        if y and first is None:first=prep+elapsed
    return {'estimated_prefix':len(prefix),'started':started,'completed':completed,'raw_failed_total':total,'raw_detected':detected,'raw_recall':detected/total if total else None,'any_detected':int(detected>0),'ttff_seconds_censored':first if first is not None else deadline,'ttff_censored':int(first is None),'observed_execution_seconds':elapsed,'overrun_seconds':max(0.,prep+elapsed-deadline),'late_results':late,'late_failures':late_fail,'actual_vs_estimated_boundary_differs':int(completed!=len(prefix))}


def rule_reasons(method,names,costs,lastfails,lrts,order,seed):
    # Materialized reasons, not an uncharged post hoc string for the selected boundary.
    if method=='B1':return [(names[i],names[i]) for i in order]
    if method=='B2':return [(names[i],f'seed={seed};position={j}') for j,i in enumerate(order)]
    if method=='B3':return [(names[i],float(costs[i])) for i in order]
    if method=='B4':return [(names[i],None if not math.isfinite(lastfails[i]) else lastfails[i],float(costs[i])) for i in order]
    return [(names[i],float(lrts[i])) for i in order]


def cal_stats(y,p):
    bins=[]
    for k in range(10):
        mask=(p>=k/10)&((p<(k+1)/10) if k<9 else (p<=1))
        bins.append({'bin':k,'n':int(mask.sum()),'positive':int(y[mask].sum()),'probability_sum':float(p[mask].sum())})
    return {'n':len(y),'positive':int(y.sum()),'squared_error_sum':float(((p-y)**2).sum()),'bins':bins}


def aggregate_cal(items):
    n=sum(x['n'] for x in items);pos=sum(x['positive'] for x in items)
    bins=[{'bin':k,'n':sum(x['bins'][k]['n'] for x in items),'positive':sum(x['bins'][k]['positive'] for x in items),'probability_sum':sum(x['bins'][k]['probability_sum'] for x in items)} for k in range(10)]
    for b in bins:
        b['mean_predicted']=b['probability_sum']/b['n'] if b['n'] else None;b['observed_rate']=b['positive']/b['n'] if b['n'] else None
    return {'n_test_predictions':n,'positive':pos,'base_rate':pos/n if n else None,'brier':sum(x['squared_error_sum'] for x in items)/n if n else None,'ece_10_equal_width':sum(abs(b['probability_sum']-b['positive']) for b in bins)/n if n else None,'reliability_bins':bins}


def summarize(results,audit,calibration,refits,metadata):
    summaries=[];comparisons=[]
    fitted={r['build'] for r in audit if r['evaluated'] and r['model_fitted']}
    for support in ('all_policy','common_fitted'):
        use=[r for r in results if support=='all_policy' or r['build'] in fitted]
        for frac in (.1,.2,.3):
            decisionmeans={}
            for method in METHODS:
                group=[r for r in use if r['budget_fraction']==frac and r['method']==method]
                by={}
                for r in group:by.setdefault(r['build'],[]).append(r)
                agg=[]
                for build,rows in by.items():
                    d={'build':build,'raw_failed_total':rows[0]['raw_failed_total']}
                    for k in ('raw_recall','any_detected','ttff_seconds_censored','ttff_censored','preparation_seconds','preparation_cpu_seconds','overrun_seconds','late_results','late_failures','estimated_prefix','completed'):
                        vals=[r[k] for r in rows if r[k] is not None];d[k]=statistics.mean(vals) if vals else None
                    agg.append(d)
                decisionmeans[method]={d['build']:d for d in agg}
                failed=[d for d in agg if d['raw_failed_total']]
                s={'support':support,'method':method,'budget_fraction':frac,'builds':len(agg),'failure_builds':len(failed),'passing_builds':len(agg)-len(failed),'nested_random_seeds':30 if method=='B2' else 1}
                for k in ('raw_recall','any_detected','ttff_seconds_censored','ttff_censored'):
                    s[k+'_mean_failure_builds']=statistics.mean(d[k] for d in failed) if failed else None
                for k in ('preparation_seconds','preparation_cpu_seconds','overrun_seconds','late_results','late_failures','estimated_prefix','completed'):
                    s[k+'_mean_all_builds']=statistics.mean(d[k] for d in agg) if agg else None
                s['preparation_seconds_max']=max((r['preparation_seconds'] for r in group),default=None)
                s['passing_preparation_seconds_total']=sum(d['preparation_seconds'] for d in agg if not d['raw_failed_total'])
                summaries.append(s)
            for rule in METHODS[:-1]:
                pairs=[(v,decisionmeans[rule][k]) for k,v in decisionmeans['LOGIT'].items() if k in decisionmeans[rule] and v['raw_failed_total']]
                diffs=[m['raw_recall']-b['raw_recall'] for m,b in pairs]
                comparisons.append({'support':support,'budget_fraction':frac,'model':'LOGIT','rule':rule,'paired_failure_builds':len(pairs),'raw_recall_difference_model_minus_rule':statistics.mean(diffs) if diffs else None,'positive_decisions':sum(d>1e-12 for d in diffs),'equal_decisions':sum(abs(d)<=1e-12 for d in diffs),'negative_decisions':sum(d< -1e-12 for d in diffs),'inference':'descriptive only; build observations are nested in one project'})
    return {**metadata,'status':'EXPLORATORY RETROSPECTIVE RAW-FAILED-CLASS REPLAY; NOT CONFIRMATORY','builds':len(audit),'evaluated_builds':sum(r['evaluated'] for r in audit),'fitted_decisions':sum(r['model_fitted'] and r['evaluated'] for r in audit),'fallback_decisions':sum(not r['model_fitted'] and r['evaluated'] for r in audit),'fallback_reasons':dict(Counter(r['fallback_reason'] for r in audit if r['evaluated'] and not r['model_fitted'])),'scheduled_refits':len(refits),'successful_refits':sum(r['reason']=='fitted' for r in refits),'scheduler_guard_changes_detected_rows':sum(r['guard_changes_detected'] for r in results),'scheduler_guard_changes_completed_rows':sum(r['guard_changes_completed'] for r in results),'selected_C_counts':dict(Counter(str(r['selected_C']) for r in refits if r['reason']=='fitted')),'calibration_all_fitted_test_predictions':{s:aggregate_cal([r[s] for r in calibration]) for s in ('raw','calibrated','past_constant')},'summaries':summaries,'paired_comparisons':comparisons,'limitations':json.loads((ROOT/'analysis_freeze.json').read_text())['deviations']}


def run(args):
    out=Path(args.output);out.mkdir(parents=True,exist_ok=False)
    start=Clock();load=Clock();builds,sources=load_builds(args.metadata,args.data_dir,args.project);loadtime=load.elapsed()
    if not builds: raise ValueError('no builds')
    dump(out/'source_manifest.json',{'metadata':str(args.metadata),'metadata_sha256':sha(args.metadata),'files':sources})
    release=sorted(builds,key=lambda b:(b['available'],b['start'],b['key']));release_index=0;history={};available=[]
    results=[];audit=[];refits=[];calibration=[];boundaries=[];model=None;last_refit=None
    prediction_stream=gzip.open(out/'model_orders_predictions.jsonl.gz','wt',encoding='utf-8')
    for i,b in enumerate(builds):
        common=Clock()
        while release_index<len(release) and release[release_index]['available']<=b['start']:
            prior=release[release_index]
            assert prior['index']<i
            admit(history,prior);available.append(prior);release_index+=1
        names=sorted(b['tests']);x,costs,lastfails,lrts=features(names,history,i)
        b['x_asof']=x;b['y']=np.asarray([b['tests'][n][1] for n in names],int)
        past_total=sum(len(q['tests']) for q in available)
        past_positive=sum(sum(yy for dd,yy in q['tests'].values()) for q in available)
        past_prevalence=past_positive/past_total if past_total else None
        commonwall,commoncpu=common.elapsed()
        valid=bool(np.isfinite(costs).all() and np.all(costs>0))
        fitwall=fitcpu=0.
        if i%10==0:
            past=sorted(available,key=lambda q:(q['start'],q['key']))
            model,last_refit=refit(past,b);refits.append(last_refit);fitwall=last_refit['wall_seconds'];fitcpu=last_refit['cpu_seconds']
        record={'build':b['key'],'date':b['date'],'decision_index':i,'start_epoch':b['start'],'available_epoch_proxy':b['available'],'classes':len(names),'raw_failures':int(b['y'].sum()),'zero_observed_durations':sum(d==0 for d,y in b['tests'].values()),'prior_available_builds':len(available),'overlapping_prior_builds_excluded':sum(q['index']<i and q['available']>b['start'] for q in builds),'new_test_classes':sum(n not in history for n in names),'duration_missing_classes':int(x[:,12].sum()),'estimated_suite_seconds':float(costs.sum()) if valid else None,'observed_suite_seconds':sum(d for d,y in b['tests'].values()),'past_available_class_executions':past_total,'past_available_positive_rows':past_positive,'past_available_prevalence':past_prevalence,'common_feature_seconds':commonwall,'common_feature_cpu_seconds':commoncpu,'evaluated':valid,'model_fitted':model is not None,'fallback_reason':None if model else last_refit['reason'],'scheduled_refit':int(i%10==0),'refit_seconds':fitwall,'refit_cpu_seconds':fitcpu,'active_refit':model['refit_key'] if model else None,'selected_C':model['C'] if model else None}
        audit.append(record)
        if not valid:continue
        actual=[b['tests'][n] for n in names];estimate=float(costs.sum())
        for method in METHODS:
            for seed in (range(30) if method=='B2' else [0]):
                inference=ranking=explanation=(0.,0.);residual=0.;contributions=[]
                if method=='LOGIT' and model is not None:
                    t=Clock();raw,cal=probabilities(model,x);scores=cal/costs;inference=t.elapsed()
                    t=Clock();order=sorted(range(len(names)),key=lambda j:(-scores[j],names[j]));ranking=t.elapsed()
                    t=Clock();contributions,residual=explain_adjacent(model,x,costs,order);explanation=t.elapsed()
                    calibration.append({'build':b['key'],'raw':cal_stats(b['y'],raw),'calibrated':cal_stats(b['y'],cal),'past_constant':cal_stats(b['y'],np.full(len(names),past_prevalence))})
                else:
                    effective='B4' if method=='LOGIT' else method
                    t=Clock();order=rule_order(names,costs,lastfails,lrts,effective,seed,b['key']);ranking=t.elapsed()
                    t=Clock();reasons=rule_reasons(effective,names,costs,lastfails,lrts,order,seed);explanation=t.elapsed()
                baseprep=commonwall+inference[0]+ranking[0]+explanation[0]+(fitwall if method=='LOGIT' else 0.)
                basecpu=commoncpu+inference[1]+ranking[1]+explanation[1]+(fitcpu if method=='LOGIT' else 0.)
                # ponytail: 1 ms scheduler reservation conservatively covers final allowance computation.
                reserve=.001;wastedwall=wastedcpu=0.
                while True:
                    t=Clock();prep=baseprep+wastedwall+reserve
                    prefixes=[estimated_prefix(order,costs,f*estimate,prep) for f in (.1,.2,.3)]
                    schedwall,schedcpu=t.elapsed()
                    if schedwall<=reserve:break
                    wastedwall+=schedwall;wastedcpu+=schedcpu;reserve=max(reserve*2,schedwall*2)
                prep_cpu=basecpu+wastedcpu+schedcpu
                if method=='LOGIT':
                    prediction_stream.write(json.dumps({'build':b['key'],'fitted':model is not None,'order_sorted_inventory_indices':order,'raw_probabilities':raw.tolist() if model else None,'calibrated_probabilities':cal.tolist() if model else None},separators=(',',':'))+'\n')
                for fraction,prefix in zip((.1,.2,.3),prefixes):
                    deadline=fraction*estimate
                    row={'project':args.project,'build':b['key'],'date':b['date'],'method':method,'seed':seed,'budget_fraction':fraction,'deadline_seconds':deadline,'preparation_seconds':prep,'preparation_measured_seconds':baseprep+wastedwall+schedwall,'scheduler_guard_extra_seconds':reserve-schedwall,'preparation_cpu_seconds':prep_cpu,'common_feature_seconds':commonwall,'common_feature_cpu_seconds':commoncpu,'fit_selection_calibration_seconds':fitwall if method=='LOGIT' else 0.,'fit_selection_calibration_cpu_seconds':fitcpu if method=='LOGIT' else 0.,'inference_seconds':inference[0],'inference_cpu_seconds':inference[1],'ranking_seconds':ranking[0],'ranking_cpu_seconds':ranking[1],'explanation_seconds':explanation[0],'explanation_cpu_seconds':explanation[1],'scheduler_reserved_seconds':reserve,'scheduler_measured_seconds':schedwall+wastedwall,'scheduler_cpu_seconds':schedcpu+wastedcpu,'model_fitted':model is not None if method=='LOGIT' else None,'fallback_reason':last_refit['reason'] if method=='LOGIT' and model is None else None,'order_sha256':hashlib.sha256('\n'.join(names[j] for j in order).encode()).hexdigest(),**evaluate(prefix,actual,deadline,prep)}
                    noguardprep=baseprep+wastedwall+schedwall
                    noguardprefix=estimated_prefix(order,costs,deadline,noguardprep)
                    noguard=evaluate(noguardprefix,actual,deadline,noguardprep)
                    row.update(guard_free_raw_recall=noguard['raw_recall'],guard_changes_detected=int(noguard['raw_detected']!=row['raw_detected']),guard_changes_completed=int(noguard['completed']!=row['completed']))
                    results.append(row)
                    if method=='LOGIT' and model is not None:
                        k=len(prefix)
                        boundary={'build':b['key'],'fraction':fraction,'included':names[order[k-1]] if k else None,'excluded':names[order[k]] if k<len(order) else None,'exists':0<k<len(order),'maximum_adjacent_completeness_residual':residual}
                        if 0<k<len(order):
                            boundary.update(history=float(contributions[k-1,0]),cost=float(contributions[k-1,1]),missingness=float(contributions[k-1,2]),score_contrast=float(scores[order[k-1]]-scores[order[k]]))
                        boundaries.append(boundary)
        if (i+1)%100==0:print(json.dumps({'project':args.project,'builds_processed':i+1,'fitted_decisions':sum(r['evaluated'] and r['model_fitted'] for r in audit),'elapsed_seconds':start.elapsed()[0]}),flush=True)
    prediction_stream.close()
    environment={'python':sys.version,'numpy':np.__version__,'scipy':scipy.__version__,'sklearn':sklearn.__version__,'platform':platform.platform(),'machine':platform.machine(),'processor':platform.processor(),'logical_cpu_count':os.cpu_count(),'threadpools':threadpool_info(),'thread_environment':{k:os.environ[k] for k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','VECLIB_MAXIMUM_THREADS','NUMEXPR_NUM_THREADS','BLIS_NUM_THREADS')},'max_rss_bytes_macos':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    metadata={'project':args.project,'period':[builds[0]['date'],builds[-1]['date']],'observed_class_executions':sum(len(b['tests']) for b in builds),'observed_raw_failed_classes':sum(r['raw_failures'] for r in audit),'observed_raw_failing_builds':sum(r['raw_failures']>0 for r in audit),'unique_class_names':len({n for b in builds for n in b['tests']}),'repeated_revision_groups':sum(v>1 for v in Counter(b['revision'] for b in builds).values()),'loading_research_wall_seconds':loadtime[0],'loading_research_cpu_seconds':loadtime[1],'acquisition_predecision_wall_seconds':None,'acquisition_status':'unmeasured: no end-to-end deployment cost conclusion','analysis_freeze_sha256':sha(ROOT/'analysis_freeze.json'),'analysis_amendment_sha256':sha(ROOT/'analysis_amendment_before_outputs.json'),'model_code_sha256':sha(__file__),'finished_utc':datetime.now(timezone.utc).isoformat(),'run_wall_seconds':start.elapsed()[0],'run_cpu_seconds':start.elapsed()[1],'environment':environment}
    summary=summarize(results,audit,calibration,refits,metadata)
    write_csv(out/'per_build_policy_seed.csv',results);write_csv(out/'build_audit.csv',audit);write_csv(out/'summary.csv',summary['summaries']);write_csv(out/'paired_comparisons.csv',summary['paired_comparisons']);dump(out/'refit_log.json',refits);dump(out/'boundary_explanations.json',boundaries);dump(out/'calibration_sufficient_statistics.json',calibration);dump(out/'summary.json',summary)
    print(json.dumps({k:v for k,v in summary.items() if k in ('project','builds','evaluated_builds','fitted_decisions','fallback_decisions','scheduled_refits','successful_refits','selected_C_counts','run_wall_seconds','run_cpu_seconds')}),flush=True)
    for r in summary['summaries']:
        if r['budget_fraction']==.2:print(json.dumps(r),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--metadata',required=True,type=Path);p.add_argument('--data-dir',required=True,type=Path);p.add_argument('--project',required=True);p.add_argument('--output',required=True,type=Path);run(p.parse_args())
