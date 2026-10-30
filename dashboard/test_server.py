"""End-to-end local API tests. All Reactor responses are mocked."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import server
import evaluation

class Harness(BaseHTTPRequestHandler):
    payloads=[]
    def log_message(self,*args): pass
    def do_POST(self):
        payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.payloads.append(payload)
        result={'contract_version':'halo.harness.v1','prompt':payload['original_prompt']+' Answer concisely.',
                'evidence':[{'id':'s_wedding','date':'2023-05-27','text':'The wedding moved to June.'}]}
        raw=json.dumps(result).encode(); self.send_response(200); self.end_headers(); self.wfile.write(raw)

class PipelineAPI(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.patches=[patch.object(server,'RESULTS',Path(self.temp.name)),
            patch.dict(os.environ,{'HALO_API_TOKEN':'test-api-token','REACTOR_ACCESS_KEY':'test-reactor-secret'}),
            patch.object(evaluation,'reactor_transport',side_effect=self.transport)]
        for p in self.patches:p.start()
        self.calls=[]; Harness.payloads=[]
        server.STATE.update(busy=False,history_id='test-history',error=None,active_run_id=None)
        server.HARNESS.clear();server.HARNESS.update(evaluation.validate_harness({}))
        self.harness=ThreadingHTTPServer(('127.0.0.1',0),Harness)
        self.http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        for s in (self.harness,self.http):threading.Thread(target=s.serve_forever,daemon=True).start()
        self.base=f'http://127.0.0.1:{self.http.server_port}'
    def tearDown(self):
        for _ in range(100):
            if not server.STATE['busy']: break
            time.sleep(.01)
        for s in (self.http,self.harness):s.shutdown();s.server_close()
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def transport(self,message,evidence,ref,history,key):
        self.calls.append((message,evidence,history,key))
        return {'status':'answered','answer':'June [E1].','reply':'June [E1].','citations':[{'id':'s_wedding','label':'E1'}],
                'abstained':False,'latency_s':.1,'retries':0}
    def request(self,path,data=None,auth=True):
        headers={'Content-Type':'application/json'}
        if auth:headers['Authorization']='Bearer test-api-token'
        req=urllib.request.Request(self.base+path,data=None if data is None else json.dumps(data).encode(),headers=headers)
        try:
            with urllib.request.urlopen(req) as r:return r.status,json.load(r)
        except urllib.error.HTTPError as e:return e.code,json.load(e)
    def wait(self,accepted):
        for _ in range(200):
            _,r=self.request(accepted['poll_url'])
            if r['status'] in ('completed','prepared','failed'):return r
            time.sleep(.01)
        self.fail('Run did not finish')
    def configure(self):
        return self.request('/api/v1/harness',{'enabled':True,'url':f'http://127.0.0.1:{self.harness.server_port}/prepare','token':'test-harness-secret'})
    def test_harness_transform_evaluation_and_reevaluation(self):
        self.assertEqual(self.configure()[0],200)
        ref={'answer':'June','abstain':False,'evidence':['s_wedding']}
        code,accepted=self.request('/api/v1/runs',{'original_prompt':'When is the wedding?','question_id':'q1','reference':ref})
        self.assertEqual(code,202)
        run=self.wait(accepted)
        self.assertEqual(run['status'],'completed')
        self.assertEqual(run['input']['original_prompt'],'When is the wedding?')
        self.assertIn('Answer concisely.',run['prepared']['prompt'])
        self.assertEqual(self.calls[0][0],run['outbound_message'])
        self.assertIn('[E1]',run['outbound_message']['text'])
        self.assertTrue(run['metrics']['exact_match']);self.assertEqual(run['metrics']['evidence_f1'],1)
        self.assertNotIn('reference',Harness.payloads[0]);self.assertNotIn('reactor_key',Harness.payloads[0])
        for path in Path(self.temp.name).rglob('*.json'):
            text=path.read_text();self.assertNotIn('test-reactor-secret',text);self.assertNotIn('test-harness-secret',text)
        code,r=self.request(accepted['poll_url']+'/evaluate',{'reference':{'answer':'July','abstain':False,'evidence':['other']}})
        self.assertEqual(code,200);self.assertFalse(r['metrics']['exact_match']);self.assertEqual(r['metrics']['evidence_f1'],0)
        self.assertEqual(len(self.calls),1)
        _,summary=self.request('/api/v1/evaluations');self.assertEqual(summary['run_count'],1)
        self.assertEqual(summary['exact_match']['value'],0)
    def test_dry_run_never_calls_reactor(self):
        self.configure()
        _,accepted=self.request('/api/v1/runs',{'original_prompt':'Hello','dry_run':True})
        run=self.wait(accepted)
        self.assertEqual(run['status'],'prepared');self.assertEqual(len(self.calls),0);self.assertIsNone(run['metrics'])
        self.assertIn('outbound_message',run)
    def test_auth_validation_busy_and_config_atomicity(self):
        self.assertEqual(self.request('/api/v1/runs',auth=False)[0],401)
        self.assertEqual(self.request('/api/v1/runs',{'original_prompt':''})[0],400)
        self.assertEqual(self.request('/api/v1/runs',{'original_prompt':'x','dry_run':'false'})[0],400)
        self.configure();before=dict(server.HARNESS)
        self.assertEqual(self.request('/api/v1/harness',{'url':'http://example.com/prepare'})[0],400)
        self.assertEqual(before,server.HARNESS)
        server.STATE['busy']=True
        self.assertEqual(self.request('/api/v1/runs',{'original_prompt':'x'})[0],409)
        server.STATE['busy']=False
    def test_harness_failure_does_not_fall_back_to_reactor(self):
        with patch.object(evaluation,'prepare',side_effect=ValueError('Harness failed')):
            _,accepted=self.request('/api/v1/runs',{'original_prompt':'Hello'})
            run=self.wait(accepted)
        self.assertEqual(run['status'],'failed');self.assertEqual(run['error']['stage'],'harness');self.assertEqual(len(self.calls),0)
    def test_passthrough_and_ui_compatibility(self):
        _,accepted=self.request('/api/run',{'action':'ask','question':'When?','date':'2023-05-30','evidence':[]})
        run=self.wait(accepted)
        self.assertEqual(run['prepared']['prompt'],'When?')
        self.assertIsNone(run['metrics']['exact_match'])
        _,state=self.request('/api/state');self.assertEqual(state['rows'][0]['question'],'When?')
        self.assertIn('pipeline',state['rows'][0])
    def test_restart_does_not_replay_and_scores_interruption(self):
        source=evaluation.validate_input({'original_prompt':'When?','history_id':'test-history','reference':{'answer':'June','abstain':False,'evidence':['s1']}})
        run={'run_id':'run_'+'a'*32,'status':'running','input':source,'stages':[],'harness':{},'created_at':time.time()}
        server.save_run(run);server.recover_interrupted()
        recovered=server.get_run(run['run_id'])
        self.assertEqual(recovered['status'],'failed');self.assertFalse(recovered['metrics']['exact_match']);self.assertFalse(self.calls)

    def test_message_limit_and_scoring(self):
        with patch.object(evaluation,'prepare',return_value={'prompt':'x'*16000,'evidence':[],'question_date':None}):
            _,accepted=self.request('/api/v1/runs',{'original_prompt':'Hello'})
            run=self.wait(accepted)
        self.assertEqual(run['error']['stage'],'compile');self.assertFalse(self.calls)
        scored=evaluation.score({'status':'timeout'},{'answer':'June','abstain':False,'evidence':['s1']})
        self.assertFalse(scored['exact_match']);self.assertFalse(scored['abstention_correct']);self.assertEqual(scored['evidence_f1'],0)
        scored=evaluation.score({'status':'answered','abstained':True},{'answer':'unknown','abstain':True})
        self.assertTrue(scored['abstention_correct']);self.assertIsNone(scored['exact_match']);self.assertIsNone(scored['evidence_f1'])

if __name__=='__main__':unittest.main()
