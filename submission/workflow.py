"""Reproducible benchmark execution and strict submission export. Python 3.11+."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'reactor'))
import client
from predictions import strip_labels


def read_jsonl(path):
    rows=[]
    with Path(path).open(encoding='utf-8-sig') as f:
        for n,line in enumerate(f,1):
            if line.strip():
                try: row=json.loads(line)
                except ValueError as e: raise ValueError(f'{path}:{n}: invalid JSON') from e
                if not isinstance(row,dict): raise ValueError(f'{path}:{n}: expected object')
                rows.append(row)
    return rows


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(value,indent=2)+'\n');tmp.replace(path)


def dataset(questions_path,histories_path):
    questions=read_jsonl(questions_path);histories={};seen=set()
    for q in questions:
        for key in ('question_id','history_id','question','question_date'):
            if not isinstance(q.get(key),str) or not q[key]:raise ValueError(f'Question needs {key}')
        if q['question_id'] in seen:raise ValueError('Duplicate question ID: '+q['question_id'])
        seen.add(q['question_id']);client.parse_question_date(q['question_date'])
    if not questions:raise ValueError('Question file is empty')
    wanted={q['history_id'] for q in questions}
    with Path(histories_path).open(encoding='utf-8-sig') as f:
        for line in f:
            if not line.strip():continue
            h=json.loads(line);hid=h.get('history_id')
            if hid not in wanted:continue
            if hid in histories:raise ValueError('Duplicate history ID: '+hid)
            if not isinstance(h.get('sessions'),list):raise ValueError('History needs sessions')
            sids=set()
            for s in h['sessions']:
                if not isinstance(s.get('session_id'),str) or not s['session_id']:raise ValueError('Session needs session_id')
                if s['session_id'] in sids:raise ValueError('Duplicate session ID within '+hid)
                sids.add(s['session_id']);client.parse_question_date(s['date'])
                if not isinstance(s.get('turns'),list):raise ValueError('Session needs turns')
                for t in s['turns']:
                    if not isinstance(t.get('content'),str) or t.get('role') not in ('user','assistant','system'):raise ValueError('Invalid turn')
            histories[hid]=h
    missing=wanted-histories.keys()
    if missing:raise ValueError('Missing histories: '+', '.join(sorted(missing)))
    return questions,histories


STOP=set('a an the is are was were be been what when where who how why do did does i my me we our you your it its to of in on at and or for with as that this have has had can could would should tell please'.split())
def tokens(text):return [t for t in re.findall(r'\w+',text.lower()) if t not in STOP and len(t)>1]


class Retriever:
    """Small transparent lexical baseline; replace via the HTTP harness for final use."""
    def __init__(self,history):
        self.docs=[]
        for s in history['sessions']:
            turns=[]
            for turn in s['turns']:
                if turn['role'] not in ('user','assistant'):continue
                text=turn['content']
                for start in range(0,len(text),900):
                    turns.append(turn['role']+': '+text[start:start+1100])
            for i in range(0,len(turns),2):
                text='\n'.join(turns[max(0,i-1):i+2])
                counts=Counter(tokens(text))
                self.docs.append({'id':s['session_id'],'date':s['date'],'text':text,'counts':counts,'length':sum(counts.values())})
        self.df=Counter(t for d in self.docs for t in d['counts'])
        self.avg=sum(d['length'] for d in self.docs)/max(len(self.docs),1)
    def retrieve(self,question,max_chars=12000,top_k=8):
        query=set(tokens(question['question']));ranked=[];n=len(self.docs)
        when=client.parse_question_date(question['question_date'])
        for d in self.docs:
            # Do not retrieve future sessions, even if a dataset contains them by accident.
            if client.parse_question_date(d['date'])>when:continue
            score=0.0
            for t in query:
                tf=d['counts'][t]
                if tf:score+=math.log(1+(n-self.df[t]+.5)/(self.df[t]+.5))*tf*2.2/(tf+1.2*(.25+.75*d['length']/max(self.avg,1)))
            if score>0:ranked.append((score,client.parse_question_date(d['date']),d))
        ranked.sort(key=lambda item:(item[0],item[1]),reverse=True)
        evidence=[];per_session=Counter()
        for _,_,d in ranked:
            if len(evidence)>=top_k:break
            if per_session[d['id']]>=2:continue
            item={k:d[k] for k in ('id','date','text')}
            proposed=evidence+[item]
            if len(client.build_message(question['question'],proposed,'00000000',when))>max_chars:continue
            evidence=proposed;per_session[d['id']]+=1
        return evidence


class API:
    def __init__(self,base,token):
        self.base=base.rstrip('/');self.token=token
        if not token:raise ValueError('Set HALO_API_TOKEN to match the running dashboard server')
    def call(self,path,data=None):
        req=urllib.request.Request(self.base+path,data=None if data is None else json.dumps(data).encode(),
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=20) as r:return json.load(r)
        except urllib.error.HTTPError as e:
            try:message=json.load(e).get('error',str(e.code))
            except ValueError:message=str(e.code)
            raise RuntimeError(f'API {e.code}: {message}') from None
    def wait_run(self,path,timeout=500):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            run=self.call(path)
            if run['status'] in ('completed','failed','prepared'):return run
            time.sleep(2)
        raise RuntimeError('Run still pending; inspect '+path+' before resuming')
    def new_history(self):
        self.call('/api/v1/history',{})
        deadline=time.monotonic()+350
        while time.monotonic()<deadline:
            state=self.call('/api/state')
            if not state['busy']:
                if state.get('error'):raise RuntimeError(state['error'])
                return state['history_id']
            time.sleep(2)
        raise RuntimeError('History reset still pending')


def run_benchmark(args):
    questions,histories=dataset(args.questions,args.histories)
    api=API(args.api_url,os.environ.get('HALO_API_TOKEN',''))
    config=api.call('/api/v1/harness')
    if args.evidence_mode=='none' and not config['enabled']:raise ValueError('evidence-mode none requires an enabled final harness')
    fingerprint={'questions_sha256':digest(args.questions),'histories_sha256':digest(args.histories),
                 'evidence_mode':args.evidence_mode,'harness':config,'harness_version':args.harness_version,
                 'workflow_sha256':digest(__file__),'client_sha256':digest(ROOT/'reactor/client.py'),
                 'top_k':args.top_k,'max_chars':args.max_chars}
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    checkpoint_path=out/'checkpoint.json'
    checkpoint={'fingerprint':fingerprint,'completed_histories':{},'run_ids':{}}
    if checkpoint_path.exists():
        checkpoint=json.loads(checkpoint_path.read_text())
        if checkpoint['fingerprint']!=fingerprint:raise ValueError('Dataset or configuration changed. Use a new output directory.')
    groups=defaultdict(list)
    for q in questions:groups[q['history_id']].append(q)
    print(f'{len(questions)} questions, {len(groups)} histories. Only completed groups are resumed.',flush=True)
    for hid,group in groups.items():
        if hid in checkpoint['completed_histories']:continue
        # Interrupted groups restart in full: all accepted predictions share one Reactor history.
        reactor_history=api.new_history()
        index=Retriever(histories[hid]) if args.evidence_mode=='bm25' else None
        group_ids={}
        for q in group:
            evidence=index.retrieve(q,args.max_chars,args.top_k) if index else []
            accepted=api.call('/api/v1/runs',{'original_prompt':q['question'],'question_id':q['question_id'],
                'question_date':q['question_date'],'history_id':reactor_history,'evidence':evidence,
                'context':{'dataset_history_id':hid},'dry_run':False})
            run=api.wait_run(accepted['poll_url'])
            atomic_json(out/'runs'/(run['run_id']+'.json'),run)
            if run['status']!='completed':raise RuntimeError(f"{q['question_id']} failed; no prediction fabricated. Resume later to rerun this history.")
            group_ids[q['question_id']]=run['run_id']
            print(f"{q['question_id']}: {run['result']['status']} ({len(group_ids)}/{len(group)})",flush=True)
        checkpoint['run_ids'].update(group_ids)
        checkpoint['completed_histories'][hid]=reactor_history
        atomic_json(checkpoint_path,checkpoint)
    export_predictions(args.questions,args.histories,checkpoint_path,out/'predictions.jsonl',getattr(args,'citation_fallback',False))


def fallback_citations(answer,evidence,allowed,log):
    """Organiser-sanctioned (2026-10-30): when Reactor's answer has no usable [E#] labels, cite the sessions among the
    evidence it was sent whose text supports the answer; never the top-ranked one by default. Implemented in
    src/memory_challenge/reactor/citation_fallback.py (standard library only)."""
    src=str(ROOT/'src')
    if src not in sys.path:sys.path.insert(0,src)
    from memory_challenge.reactor.citation_fallback import choose
    chosen,record=choose(answer,{'evidence':evidence},allowed)
    log.append(record)
    return chosen


def make_predictions(questions,histories,runs,citation_fallback=False,fallback_log=None):
    by_id={}
    for run in runs:
        qid=run.get('input',{}).get('question_id')
        if qid in by_id:raise ValueError('Duplicate selected run for '+str(qid))
        by_id[qid]=run
    expected={q['question_id'] for q in questions}
    if set(by_id)!=expected:raise ValueError(f'Coverage mismatch: {len(expected-set(by_id))} missing, {len(set(by_id)-expected)} unknown questions')
    predictions=[];warnings=[];history_mapping={}
    for q in questions:
        run=by_id[q['question_id']];source=run['input'];result=run.get('result',{})
        if run.get('status')!='completed' or source.get('dry_run') or result.get('status')!='answered':raise ValueError('No completed Reactor answer for '+q['question_id'])
        if source.get('original_prompt')!=q['question'] or source.get('question_date')!=q['question_date']:
            raise ValueError('Dataset question/date mismatch: '+q['question_id'])
        if source.get('context',{}).get('dataset_history_id')!=q['history_id']:raise ValueError('Dataset history mapping missing or wrong: '+q['question_id'])
        history=source.get('history_id')
        if not history or result.get('history_id')!=history:raise ValueError('Reactor history missing or inconsistent')
        previous=history_mapping.setdefault(q['history_id'],history)
        if previous!=history:raise ValueError('Questions in one dataset history used different Reactor histories')
        if list(history_mapping.values()).count(history)>1:raise ValueError('Reactor history reused across different dataset histories')
        ref=run.get('ref')
        if not isinstance(ref,str) or not ref or client.final_answers(result.get('reply','')).get(ref)!=result.get('answer'):
            raise ValueError('Cannot verify final-answer provenance for '+q['question_id'])
        if result.get('ref')!=ref:raise ValueError('Result ref differs from run ref')
        allowed={s['session_id'] for s in histories[q['history_id']]['sessions']}
        prepared=run.get('prepared',{});evidence=prepared.get('evidence',[])
        if client.parse_question_date(prepared.get('question_date',''))!=client.parse_question_date(q['question_date']):raise ValueError('Harness changed the question date')
        sessions={s['session_id']:s for s in histories[q['history_id']]['sessions']}
        for e in evidence:
            if e.get('id') in sessions and client.parse_question_date(e.get('date',''))!=client.parse_question_date(sessions[e['id']]['date']):raise ValueError('Harness changed or omitted an evidence session date')
        normalized=client.load_evidence(json.dumps(evidence)) if evidence else []
        wire=client.build_message(prepared.get('prompt',''),normalized,ref,client.parse_question_date(prepared['question_date']))
        if run.get('outbound_message')!={'type':'message','text':wire}:raise ValueError('Outgoing message does not match recorded prepared input')
        if any(e.get('id') not in allowed for e in evidence):raise ValueError('Evidence from another history: '+q['question_id'])
        cited,unknown=client.citations(result['answer'],evidence)
        if unknown:raise ValueError('Unknown citation labels: '+q['question_id'])
        ids=list(dict.fromkeys(c['id'] for c in cited))
        if any(cid not in allowed for cid in ids):raise ValueError('Invalid session citation')
        abstain=bool(client._INSUFFICIENT.match(result['answer']))
        if abstain!=bool(result.get('abstained')):raise ValueError('Stored abstention decision inconsistent with Reactor answer')
        answer='' if abstain else strip_labels(result['answer'])
        if not abstain and not answer:raise ValueError('Empty non-abstained answer')
        if not abstain and not ids and citation_fallback:
            log=fallback_log if fallback_log is not None else []
            ids=fallback_citations(answer,evidence,allowed,log)
            log[-1]['question_id']=q['question_id']
            if ids:warnings.append(q['question_id']+': citations from supported evidence (fallback): '+', '.join(ids))
        if not abstain and not ids:warnings.append(q['question_id']+': no final-answer citations')
        predictions.append({'question_id':q['question_id'],'abstain':abstain,'answer':answer,'citations':ids})
    return predictions,warnings


def validate_predictions(predictions,questions,histories):
    expected={q['question_id']:q for q in questions};seen=set()
    for row in predictions:
        if set(row)!={'question_id','abstain','answer','citations'}:raise ValueError('Predictions must have exactly the four required fields')
        qid=row['question_id']
        if not isinstance(qid,str) or qid not in expected or qid in seen:raise ValueError('Unknown or duplicate question ID')
        seen.add(qid)
        if type(row['abstain']) is not bool or not isinstance(row['answer'],str) or not isinstance(row['citations'],list):raise ValueError('Invalid prediction field types')
        if any(not isinstance(c,str) for c in row['citations']):raise ValueError('Citation IDs must be strings')
        allowed={s['session_id'] for s in histories[expected[qid]['history_id']]['sessions']}
        if len(set(row['citations']))!=len(row['citations']) or not set(row['citations'])<=allowed:raise ValueError('Duplicate or wrong-history citations')
        if not row['abstain'] and not row['answer'].strip():raise ValueError('Non-abstained answer is empty')
    if seen!=set(expected):raise ValueError(f'{len(set(expected)-seen)} test questions are missing')
    return {'valid':True,'questions':len(questions),'predictions':len(predictions),'abstained':sum(p['abstain'] for p in predictions)}


def export_predictions(questions_path,histories_path,checkpoint_path,out,citation_fallback=False):
    questions,histories=dataset(questions_path,histories_path)
    checkpoint_path=Path(checkpoint_path);checkpoint=json.loads(checkpoint_path.read_text())
    for field,path in [('questions_sha256',questions_path),('histories_sha256',histories_path)]:
        if checkpoint['fingerprint'][field]!=digest(path):raise ValueError('Dataset changed since inference')
    runs=[]
    for qid,rid in checkpoint['run_ids'].items():
        if not isinstance(rid,str) or not re.fullmatch(r'run_[0-9a-f]{32}',rid):raise ValueError('Invalid run ID in checkpoint')
        run=json.loads((checkpoint_path.parent/'runs'/(rid+'.json')).read_text())
        if run.get('run_id')!=rid or run.get('input',{}).get('question_id')!=qid:raise ValueError('Checkpoint/run identity mismatch')
        runs.append(run)
    fallback_log=[]
    predictions,warnings=make_predictions(questions,histories,runs,citation_fallback,fallback_log)
    report=validate_predictions(predictions,questions,histories)
    out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
    temp=out.with_suffix('.tmp');temp.write_text(''.join(json.dumps(p,ensure_ascii=False)+'\n' for p in predictions),encoding='utf-8');temp.replace(out)
    report.update(warnings=warnings,predictions_sha256=digest(out),run_ids=checkpoint['run_ids'],dataset_fingerprint=checkpoint['fingerprint'],
                  citation_fallback={'enabled':citation_fallback,'decisions':fallback_log})
    atomic_json(out.with_suffix('.validation.json'),report)
    print(json.dumps(report,indent=2));return report


def package(args):
    questions,histories=dataset(args.questions,args.histories)
    predictions=read_jsonl(args.predictions);validate_predictions(predictions,questions,histories)
    # Reproduce the file from selected raw Reactor runs before including it in a submission.
    checkpoint=Path(args.checkpoint)
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        report=export_predictions(args.questions,args.histories,checkpoint,Path(folder)/'predictions.jsonl',getattr(args,'citation_fallback',False))
        if read_jsonl(Path(folder)/'predictions.jsonl')!=predictions:raise ValueError('Predictions differ from the reproducible Reactor export')
    output=Path(args.out);output.parent.mkdir(parents=True,exist_ok=True)
    paths=[]
    paths.append(ROOT/'score.py')
    for directory in ('dashboard','reactor','submission'):
        paths.extend(p for p in (ROOT/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix in ('.py','.js','.cjs','.css','.html','.md','.txt','.mjs'))
    paths.extend(p for p in (ROOT/'reactor/examples').glob('*-example.json*') if p.is_file())
    presentation=Path(args.presentation)
    if not presentation.is_file():raise ValueError('Presentation file is missing')
    extra=[]
    if args.harness_source:
        if not args.harness_source.is_dir():raise ValueError('Harness source directory is missing')
        for p in args.harness_source.rglob('*'):
            if p.is_file() and not any(part.startswith('.') or part in ('node_modules','__pycache__','results','venv') for part in p.relative_to(args.harness_source).parts) and p.suffix in ('.py','.js','.ts','.json','.md','.txt','.toml','.yaml','.yml'):
                extra.append(p)
        if not extra:raise ValueError('Harness source directory contains no supported source files')
    elif report['dataset_fingerprint']['harness']['enabled']:
        raise ValueError('Include your final harness code using --harness-source')
    for path in paths+extra+[checkpoint.parent/'runs'/(rid+'.json') for rid in report['run_ids'].values()]:
        if re.search(r'(?:rxk_|ghp_|github_pat_)[A-Za-z0-9_]{16,}',path.read_text()):raise ValueError('Potential secret in '+str(path))
    temporary=output.with_suffix('.tmp')
    with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.write(args.predictions,'predictions.jsonl')
        archive.write(ROOT/'submission/README.md','README.md')
        archive.write(presentation,'presentation'+presentation.suffix)
        archive.writestr('manifest.json',json.dumps({'schema_version':'halo.submission.v1','validation':report,
            'repository':'https://github.com/swladyk/memory-challenge/tree/front',
            'source_sha256':{str(p.relative_to(ROOT)):digest(p) for p in paths},
            'harness_sha256':{str(p.relative_to(args.harness_source)):digest(p) for p in extra}},indent=2))
        for path in extra:archive.write(path,'harness/'+str(path.relative_to(args.harness_source)))
        for path in paths:
            if re.search(r'(?:rxk_|ghp_|github_pat_)[A-Za-z0-9_]{16,}',path.read_text()):raise ValueError('Potential secret in '+str(path))
            archive.write(path,'code/'+str(path.relative_to(ROOT)))
        for rid in report['run_ids'].values():archive.write(checkpoint.parent/'runs'/(rid+'.json'),'reproducibility/runs/'+rid+'.json')
        archive.write(checkpoint,'reproducibility/checkpoint.json')
    temporary.replace(output);print('Submission archive: '+str(output))


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    for name in ('plan','run','export','validate','package'):
        p=sub.add_parser(name);p.add_argument('--questions',required=True,type=Path);p.add_argument('--histories',required=True,type=Path)
        if name=='run':
            p.add_argument('--api-url',default='http://127.0.0.1:8873');p.add_argument('--out',required=True,type=Path)
            p.add_argument('--harness-version',required=True,help='Version or commit of your final harness');p.add_argument('--evidence-mode',choices=['bm25','none'],default='bm25');p.add_argument('--top-k',type=int,default=8);p.add_argument('--max-chars',type=int,default=12000)
        if name in ('export','package'):p.add_argument('--checkpoint',required=True,type=Path);p.add_argument('--out',required=True,type=Path)
        if name in ('run','export','package'):p.add_argument('--citation-fallback',action='store_true',help='when an answer has no [E#] citation, cite only evidence sessions whose text supports it (organiser-sanctioned; off by default)')
        if name in ('validate','package'):p.add_argument('--predictions',required=True,type=Path)
        if name=='package':p.add_argument('--presentation',required=True,type=Path);p.add_argument('--harness-source',type=Path)
    args=parser.parse_args()
    try:
        if args.command=='plan':
            qs,hs=dataset(args.questions,args.histories);print(json.dumps({'questions':len(qs),'histories':len(hs),'sessions':sum(len(h['sessions']) for h in hs.values()),'reactor_calls':0,'status':'ready_for_final_harness'},indent=2))
        elif args.command=='run':
            if not 1<=args.top_k<=30 or not 2000<=args.max_chars<=14000:raise ValueError('Use top-k 1–30 and max-chars 2000–14000')
            run_benchmark(args)
        elif args.command=='export':export_predictions(args.questions,args.histories,args.checkpoint,args.out,args.citation_fallback)
        elif args.command=='validate':print(json.dumps(validate_predictions(read_jsonl(args.predictions),*dataset(args.questions,args.histories)),indent=2))
        elif args.command=='package':package(args)
    except (OSError,ValueError,RuntimeError,KeyError) as e:
        print('Submission blocked: '+str(e),file=sys.stderr);return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
