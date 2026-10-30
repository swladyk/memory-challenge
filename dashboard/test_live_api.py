"""Live evaluation API: run listing, offsets, partial lines, auth and path safety. Uses temporary run folders."""
import json
import os
import re
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import server

def write_run(folder, kind, events, created, status='running', partial=False):
    folder.mkdir(parents=True)
    (folder/'run.json').write_text(json.dumps({'schema':'memret.live.v1','run_id':folder.name,'kind':kind,'status':status,
                                               'created':created,'total':events,'split':'dev-large'}))
    with open(folder/'events.jsonl','w') as f:
        for i in range(1,events+1): f.write(json.dumps({'seq':i,'stage':'graded','question_id':f'q_{i}'})+'\n')
        if partial: f.write('{"seq": 99, "stage"')
    with open(folder/'stats_history.jsonl','w') as f:
        for i in range(1,events+1): f.write(json.dumps({'ts':created+i,'progress':{'total':events,'processed':i,'evaluated':i,'graded':i}})+'\n')

class LiveAPI(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        runs=self.root/'runs'
        write_run(runs/'eval-a',  'retrieval',5,1000.0,'finished')
        write_run(runs/'abl'/'baseline','retrieval',3,2000.0,partial=True)
        write_run(runs/'smoke'/'reactor-live','reactor',2,3000.0)
        (runs/'eval-a'/'payloads').mkdir(); (runs/'eval-a'/'payloads'/'run.json').write_text('{}')  # never a run
        (self.root/'outside').mkdir(); (self.root/'outside'/'run.json').write_text('{"kind":"secret"}')
        self.patches=[patch.dict(os.environ,{'HALO_API_TOKEN':'test-api-token','MEMRET_RUNS_DIR':str(runs)})]
        for p in self.patches: p.start()
        self.httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        threading.Thread(target=self.httpd.serve_forever,daemon=True).start()
        self.base=f'http://127.0.0.1:{self.httpd.server_address[1]}'
    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        for p in self.patches: p.stop()
        self.tmp.cleanup()
    def get(self,path,auth=True):
        req=urllib.request.Request(self.base+path,headers={'Authorization':'Bearer test-api-token'} if auth else {})
        try:
            with urllib.request.urlopen(req) as r: return r.status,(json.load(r) if path.startswith('/api') else r.read().decode())
        except urllib.error.HTTPError as e: return e.code,json.load(e)

    def test_lists_nested_runs_newest_first_with_progress(self):
        code,data=self.get('/api/v1/live/runs')
        self.assertEqual(code,200)
        self.assertEqual([r['id'] for r in data['runs']],['smoke~reactor-live','abl~baseline','eval-a'])
        self.assertEqual(data['runs'][2]['progress']['processed'],5)
        self.assertEqual(data['runs'][0]['kind'],'reactor')
        code,data=self.get('/api/v1/live/runs/abl~baseline')
        self.assertEqual((code,data['run']['id'],data['last']['progress']['graded']),(200,'abl~baseline',3))

    def test_offsets_tail_and_partial_last_line(self):
        code,data=self.get('/api/v1/live/runs/eval-a/events?after=0&limit=2')
        self.assertEqual(([e['seq'] for e in data['items']],data['next']),([1,2],2))
        code,data=self.get('/api/v1/live/runs/eval-a/events?after=2')
        self.assertEqual(([e['seq'] for e in data['items']],data['next'],data['status']),([3,4,5],5,'finished'))
        code,data=self.get('/api/v1/live/runs/eval-a/events?after=5')
        self.assertEqual((data['items'],data['next']),([],5))
        code,data=self.get('/api/v1/live/runs/eval-a/events?tail=2')
        self.assertEqual(([e['seq'] for e in data['items']],data['next']),([4,5],5))
        code,data=self.get('/api/v1/live/runs/abl~baseline/events?after=0')  # the half-written line waits
        self.assertEqual(([e['seq'] for e in data['items']],data['next']),([1,2,3],3))
        code,data=self.get('/api/v1/live/runs/eval-a/stats?after=3&limit=bad')
        self.assertEqual([s['progress']['processed'] for s in data['items']],[4,5])

    def test_requires_auth_and_stays_inside_the_runs_folder(self):
        self.assertEqual(self.get('/api/v1/live/runs',auth=False)[0],401)
        for bad in ('..','..~outside','eval-a~payloads','%2e%2e','abl~..~..~outside','abl%2F..%2F..%2Foutside','eval-a/../..',
                    'missing','.hidden'):
            self.assertEqual(self.get(f'/api/v1/live/runs/{bad}')[0],404,bad)
        self.assertEqual(self.get('/api/v1/live/runs/eval-a/run.json')[0],404)
        self.assertEqual(self.get('/api/v1/live/other')[0],404)

    def test_live_script_shares_no_top_level_names_with_app(self):
        names=lambda f:set(re.findall(r'^(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)',(Path(server.ROOT)/'dashboard'/f).read_text(),re.M))
        self.assertEqual(names('live.js')&names('app.js'),set())  # a clash stops live.js from loading at all
    def test_serves_live_view(self):
        code,js=self.get('/live.js',auth=False)
        self.assertEqual(code,200); self.assertIn('/api/v1/live/runs',js)
        code,html=self.get('/',auth=False)
        self.assertIn('data-view="live"',html); self.assertIn('/live.js',html)

if __name__=='__main__':unittest.main()
