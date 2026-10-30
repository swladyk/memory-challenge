import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import workflow as w

DATE='2023/05/30 (Tue) 08:08'
SESSION_DATE='2023/05/27 (Sat) 10:05'
H={'h1':{'history_id':'h1','sessions':[{'session_id':'s1','date':SESSION_DATE,'turns':[{'role':'user','content':'The wedding moved to June.'},{'role':'assistant','content':'June is the updated wedding month.'}]}]}}
Q=[{'question_id':'q1','history_id':'h1','question':'When is the wedding?','question_date':DATE}]
def run(q=Q[0],answer='June [E1].',abstained=False):
    evidence=[{'id':'s1','date':SESSION_DATE,'text':'user: The wedding moved to June.'}];ref='1234abcd'
    prepared={'prompt':q['question'],'question_date':DATE,'evidence':evidence}
    return {'run_id':'run_'+'a'*32,'status':'completed','ref':ref,
       'input':{'question_id':q['question_id'],'original_prompt':q['question'],'question_date':DATE,'context':{'dataset_history_id':'h1'},'history_id':'reactor1','dry_run':False},
       'prepared':prepared,'outbound_message':{'type':'message','text':w.client.build_message(q['question'],evidence,ref,w.client.parse_question_date(DATE))},
       'result':{'status':'answered','ref':ref,'history_id':'reactor1','answer':answer,'reply':f'# FINAL ANSWER [ref:{ref}]: '+answer,'abstained':abstained}}

class SubmissionTests(unittest.TestCase):
    def test_answer_and_abstention_export(self):
        rows,_=w.make_predictions(Q,H,[run()]);self.assertEqual(rows,[{'question_id':'q1','abstain':False,'answer':'June.','citations':['s1']}])
        rows,_=w.make_predictions(Q,H,[run(answer='INSUFFICIENT_EVIDENCE',abstained=True)]);self.assertEqual(rows[0]['answer'],'');self.assertTrue(rows[0]['abstain'])
    def test_missing_duplicate_failed_and_edited_answers_rejected(self):
        for runs in ([],[run(),run()]):
            with self.assertRaises(ValueError):w.make_predictions(Q,H,runs)
        for change in ('failed','edited','wrong_history','wire','date'):
            r=run()
            if change=='failed':r['status']='failed'
            if change=='edited':r['result']['answer']='July'
            if change=='wrong_history':r['input']['context']['dataset_history_id']='h2'
            if change=='wire':r['outbound_message']['text']='different'
            if change=='date':r['prepared']['question_date']='2024-01-01'
            with self.assertRaises(ValueError,msg=change):w.make_predictions(Q,H,[r])
    def test_unknown_labels_and_wrong_history_citations_rejected(self):
        with self.assertRaises(ValueError):w.make_predictions(Q,H,[run(answer='June [E9].')])
        r=run();r['prepared']['evidence'][0]['id']='other'
        with self.assertRaises(ValueError):w.make_predictions(Q,H,[r])
    def test_validator_checks_per_history_and_types(self):
        rows,_=w.make_predictions(Q,H,[run()]);self.assertTrue(w.validate_predictions(rows,Q,H)['valid'])
        for field,value in [('abstain','false'),('citations',['other']),('question_id','unknown')]:
            bad=copy.deepcopy(rows);bad[0][field]=value
            with self.assertRaises(ValueError):w.validate_predictions(bad,Q,H)
    def test_retrieval_includes_roles_dates_budget_and_excludes_future(self):
        h=copy.deepcopy(H['h1']);h['sessions'].append({'session_id':'future','date':'2024-01-01','turns':[{'role':'user','content':'wedding wedding wedding'}]})
        evidence=w.Retriever(h).retrieve(Q[0],max_chars=2000)
        self.assertTrue(evidence);self.assertTrue(all(e['id']=='s1' for e in evidence));self.assertIn('assistant:',evidence[0]['text'])
        self.assertEqual(evidence[0]['date'],SESSION_DATE)
        self.assertLessEqual(len(w.client.build_message(Q[0]['question'],evidence,'00000000',w.client.parse_question_date(DATE))),2000)
    def test_atomic_export_and_dataset_fingerprint(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'runs').mkdir();(p/'q.jsonl').write_text(json.dumps(Q[0])+'\n');(p/'h.jsonl').write_text(json.dumps(H['h1'])+'\n')
            r=run();(p/'runs'/(r['run_id']+'.json')).write_text(json.dumps(r))
            checkpoint={'fingerprint':{'questions_sha256':w.digest(p/'q.jsonl'),'histories_sha256':w.digest(p/'h.jsonl')},'run_ids':{'q1':r['run_id']}}
            w.atomic_json(p/'checkpoint.json',checkpoint)
            report=w.export_predictions(p/'q.jsonl',p/'h.jsonl',p/'checkpoint.json',p/'predictions.jsonl');self.assertTrue(report['valid'])
            before=(p/'predictions.jsonl').read_text();(p/'q.jsonl').write_text(json.dumps({**Q[0],'question':'Changed'})+'\n')
            with self.assertRaises(ValueError):w.export_predictions(p/'q.jsonl',p/'h.jsonl',p/'checkpoint.json',p/'predictions.jsonl')
            self.assertEqual(before,(p/'predictions.jsonl').read_text())
    def test_completed_history_resume_skips_model(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'q.jsonl').write_text(json.dumps(Q[0])+'\n');(p/'h.jsonl').write_text(json.dumps(H['h1'])+'\n')
            args=SimpleNamespace(questions=p/'q.jsonl',histories=p/'h.jsonl',api_url='http://localhost',out=p/'out',evidence_mode='bm25',top_k=8,max_chars=12000,harness_version='test')
            class API:
                calls=0
                def __init__(self,*args):pass
                def call(self,path,data=None):
                    if path=='/api/v1/harness':return {'enabled':False}
                    self.calls+=1;return {'poll_url':'/run'}
                def new_history(self):return 'reactor1'
                def wait_run(self,path):return run()
            api=API()
            with patch.object(w,'API',return_value=api):
                w.run_benchmark(args);w.run_benchmark(args)
            self.assertEqual(api.calls,1)

if __name__=='__main__':unittest.main()
