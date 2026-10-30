"""Local UI + versioned evaluation/harness API. Bind only to loopback."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from copy import deepcopy
import json, os, re, secrets, subprocess, sys, threading, time
import certifi
import evaluation

ROOT = Path(__file__).resolve().parent.parent
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.RLock()
STATE = {'busy': False, 'history_id': '', 'error': None, 'active_run_id': None}
RESULTS = ROOT / 'results'
RESULTS.mkdir(exist_ok=True)
HARNESS = evaluation.validate_harness({'enabled':bool(os.environ.get('HARNESS_URL')),
    'url':os.environ.get('HARNESS_URL',''),'token':os.environ.get('HARNESS_TOKEN','')})
try:
    STATE['history_id'] = json.loads((RESULTS / 'history.json').read_text())['history_id']
except (OSError, ValueError, KeyError): pass


def public_harness():
    return {k:v for k,v in HARNESS.items() if k != 'token'} | {'has_token': bool(HARNESS.get('token')),
                                                            'contract_version': evaluation.CONTRACT}


def save_run(run):
    with LOCK:
        folder = RESULTS / 'runs'; folder.mkdir(exist_ok=True)
        path = folder / (run['run_id'] + '.json')
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(run, indent=2))
        temp.replace(path)


def all_runs():
    with LOCK:
        runs = []
        for path in (RESULTS / 'runs').glob('run_*.json'):
            try: runs.append(json.loads(path.read_text()))
            except (ValueError, OSError): pass
        return sorted(runs, key=lambda r:r.get('created_at',0))


def get_run(run_id):
    if not re.fullmatch(r'run_[0-9a-f]{32}', run_id): return None
    with LOCK:
        try: return json.loads((RESULTS/'runs'/f'{run_id}.json').read_text())
        except (ValueError, OSError): return None


def records():
    rows = []
    for name in ('reactor-smoke.jsonl', 'dashboard.jsonl'):
        path = RESULTS / name
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    row = json.loads(line)
                    if isinstance(row, dict): rows.append(row)
                except ValueError: pass
    for run in all_runs():
        if run.get('result') and not run['input']['dry_run']:
            rows.append({**run['result'], 'evaluation':run.get('metrics'), 'reference':run['input'].get('reference'),
                         'pipeline': {'stages':run['stages'], 'outbound_message':run.get('outbound_message'),
                                      'prepared':run.get('prepared'), 'harness':run['harness'], 'duration_s':run.get('duration_s')}})
    return rows


def run_job(data):
    """Existing history-reset UI action; asking goes through the evaluation pipeline."""
    try:
        env = os.environ.copy()
        env['REACTOR_ACCESS_KEY'] = data.get('key') or os.environ.get('REACTOR_ACCESS_KEY','')
        env['SSL_CERT_FILE'] = certifi.where()
        proc = subprocess.run([sys.executable,str(ROOT/'reactor/client.py'),'--base-url',evaluation.BASE_URL,
                               '--agent-id',evaluation.AGENT_ID,'--new-history'],env=env,capture_output=True,text=True,timeout=330)
        row = json.loads(proc.stdout)
        if row.get('status') != 'history': raise ValueError(row.get('error','History creation failed'))
        with LOCK: STATE['history_id'] = row['history_id']
        (RESULTS/'history.json').write_text(json.dumps(row))
    except Exception:
        with LOCK: STATE['error'] = 'History creation failed. Check credentials and agent availability.'
    finally:
        with LOCK: STATE['busy'] = False


def pipeline_job(run, source, config, key):
    def persist(value):
        # Never persist either service credential, including accidental echoes in errors.
        raw = json.dumps(value)
        for secret in (key, config.get('token')):
            if secret: raw = raw.replace(json.dumps(secret)[1:-1], '[REDACTED]')
        save_run(json.loads(raw))
    try:
        result = evaluation.execute(run, source, config, key, persist)
        if result.get('error'):
            with LOCK: STATE['error'] = result['error']['message']
    finally:
        with LOCK: STATE.update(busy=False, active_run_id=None)


def submit(data):
    source = evaluation.validate_input(data)
    key = data.get('reactor_key', data.get('key')) or os.environ.get('REACTOR_ACCESS_KEY','')
    if not isinstance(key, str): raise ValueError('reactor_key must be a string')
    if not source['dry_run'] and not key: raise ValueError('Set REACTOR_ACCESS_KEY or supply reactor_key')
    with LOCK:
        if STATE['busy']: return None
        source['history_id'] = source['history_id'] or STATE['history_id']
        if not source['dry_run'] and not source['history_id']: raise ValueError('Create a Reactor history first')
        run_id = 'run_' + secrets.token_hex(16)
        config = deepcopy(HARNESS)
        run = {'run_id':run_id,'schema_version':'halo.run.v1','status':'queued','created_at':time.time(),
               'input':deepcopy(source),'harness':public_harness(),'stages':[]}
        save_run(run)
        STATE.update(busy=True, error=None, active_run_id=run_id)
        threading.Thread(target=pipeline_job,args=(run,source,config,key),daemon=True).start()
        return {'run_id':run_id,'status':'queued','poll_url':'/api/v1/runs/'+run_id}


def summary():
    runs = [r for r in all_runs() if not r['input']['dry_run'] and r.get('metrics')]
    def mean(field):
        values = [r['metrics'][field] for r in runs if r['metrics'].get(field) is not None]
        return {'value':sum(values)/len(values) if values else None,'count':len(values)}
    return {'run_count':len(runs),'exact_match':mean('exact_match'),'evidence_f1':mean('evidence_f1'),
            'abstention_accuracy':mean('abstention_correct'),'completion_rate':mean('completed'),
            'note':'Per-run metrics; repeated question IDs are separate attempts. Exact match is not an LLM judge.'}


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def authorized(self):
        browser = self.headers.get('X-Reactor-Token','')
        bearer = self.headers.get('Authorization','')
        configured = os.environ.get('HALO_API_TOKEN','')
        return secrets.compare_digest(browser,TOKEN) or bool(configured and secrets.compare_digest(bearer,'Bearer '+configured))
    def reply(self,code,data,kind='application/json'):
        body = (json.dumps(data) if kind=='application/json' else data).encode()
        self.send_response(code)
        self.send_header('Content-Type',kind+'; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path.startswith('/api/v1/'):
            if not self.authorized(): return self.reply(401,{'error':'Use Authorization: Bearer HALO_API_TOKEN or the browser session token'})
            if self.path=='/api/v1/harness': return self.reply(200,public_harness())
            if self.path=='/api/v1/evaluations': return self.reply(200,summary())
            if self.path=='/api/v1/runs': return self.reply(200,{'runs':all_runs()})
            if self.path.startswith('/api/v1/runs/'):
                run = get_run(self.path.removeprefix('/api/v1/runs/'))
                return self.reply(200,run) if run else self.reply(404,{'error':'Run not found'})
            return self.reply(404,{'error':'Not found'})
        if self.path=='/api/state':
            with LOCK: data={**STATE,'rows':records(),'harness':public_harness(),'evaluation_summary':summary()}
            data['has_server_key']=bool(os.environ.get('REACTOR_ACCESS_KEY'))
            data['example_evidence']=json.loads((ROOT/'reactor/examples/excerpts-example.json').read_text())
            self.reply(200,data)
        elif self.path in ('/styles.css','/app.js'):
            self.reply(200,(ROOT/'dashboard'/self.path[1:]).read_text(),'text/css' if self.path.endswith('.css') else 'application/javascript')
        elif self.path=='/': self.reply(200,(ROOT/'dashboard/index.html').read_text().replace('__TOKEN__',TOKEN),'text/html')
        else: self.reply(404,{'error':'Not found'})
    def do_POST(self):
        if not self.authorized(): return self.reply(401,{'error':'Missing or invalid API/session token'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=1000000: raise ValueError('Request must be between 1 byte and 1 MB')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict): raise ValueError('Request must be an object')
            if self.path=='/api/v1/harness':
                with LOCK:
                    if STATE['busy']: return self.reply(409,{'error':'Wait for the active request before changing harness configuration'})
                    config={**HARNESS,**data}
                    validated=evaluation.validate_harness(config)
                    HARNESS.clear(); HARNESS.update(validated)
                return self.reply(200,public_harness())
            if self.path in ('/api/v1/runs','/api/run') and data.get('action','ask')=='ask':
                accepted=submit(data)
                return self.reply(202,accepted) if accepted else self.reply(409,{'error':'One request is already active on this agent'})
            if self.path=='/api/v1/history' or self.path=='/api/run' and data.get('action')=='history':
                key=data.get('reactor_key',data.get('key')) or os.environ.get('REACTOR_ACCESS_KEY','')
                if not isinstance(key,str) or not key: raise ValueError('Reactor key required')
                with LOCK:
                    if STATE['busy']: return self.reply(409,{'error':'Agent is busy'})
                    STATE.update(busy=True,error=None)
                threading.Thread(target=run_job,args=({'key':key},),daemon=True).start()
                return self.reply(202,{'status':'pending','poll_url':'/api/state'})
            match=re.fullmatch(r'/api/v1/runs/(run_[0-9a-f]{32})/evaluate',self.path)
            if match:
                with LOCK:
                    run=get_run(match[1])
                    if not run: return self.reply(404,{'error':'Run not found'})
                    if run['status'] not in ('completed','failed') or not run.get('result') or run['input']['dry_run']:
                        return self.reply(409,{'error':'Only finished, executed runs can be evaluated'})
                    reference=data.get('reference'); evaluation.validate_reference(reference)
                    run['input']['reference']=reference
                    run['metrics']=evaluation.score(run['result'],reference)
                    run['evaluated_at']=time.time(); save_run(run)
                return self.reply(200,{'run_id':run['run_id'],'metrics':run['metrics']})
            self.reply(404,{'error':'Not found'})
        except (ValueError,TypeError,KeyError) as e: self.reply(400,{'error':str(e)[:500]})


def recover_interrupted():
    for run in all_runs():
        if run['status'] in ('queued','preparing','running'):
            message='Server restarted; request was not replayed'
            source=run['input']
            run.update(status='failed',error={'stage':'server','message':message},finished_at=time.time())
            run['stages'].append({'stage':'server','status':'interrupted'})
            if not source['dry_run']:
                run['result']={'status':'error','error':message,'question':source['original_prompt'],
                               'question_id':source['question_id'],'history_id':source['history_id'],
                               'question_date':source['question_date'],'evidence':source['evidence'],
                               'run_id':run['run_id'],'recorded_at':time.time()}
                run['metrics']=evaluation.score(run['result'],source['reference'])
            save_run(run)

if __name__=='__main__':
    recover_interrupted()
    server=ThreadingHTTPServer(('127.0.0.1',8873),Handler)
    print('Reactor dashboard + evaluation API: http://127.0.0.1:8873',flush=True)
    server.serve_forever()
