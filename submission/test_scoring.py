"""Regression checks for official scoring semantics and archive completeness."""
import importlib.util
from pathlib import Path
import unittest
import tempfile
import json
import zipfile
from types import SimpleNamespace
import workflow as w
from test_workflow import Q, H, run

spec=importlib.util.spec_from_file_location('official_score',w.ROOT/'score.py')
score=importlib.util.module_from_spec(spec);spec.loader.exec_module(score)

class ScoringTests(unittest.TestCase):
    def test_abstention_still_scores_evidence(self):
        gold={'question_id':'q1','question_type':'update','abstain':False,'answer':'June','evidence':['s1']}
        result=score.score_question({'abstain':True,'answer':'','citations':['s1']},gold)
        self.assertEqual(result['answer'],'wrong');self.assertEqual(result['evidence_f1'],1)
        rows,_=w.make_predictions(Q,H,[run(answer='INSUFFICIENT_EVIDENCE [E1]',abstained=True)])
        self.assertEqual(rows[0]['citations'],['s1'])
    def test_unjudged_and_abstention_breakdown(self):
        gold={'question_id':'q1','question_type':'update','abstain':False,'answer':'June','evidence':['s1']}
        r=score.score_question({'abstain':False,'answer':'summer','citations':[]},gold)
        self.assertEqual(r['answer'],'unjudged')
        a=score.score_question({'abstain':True,'answer':'','citations':[]},{**gold,'question_id':'q2','abstain':True})
        summary=score.summarize([r,a])
        self.assertEqual(summary['all']['unjudged'],.5)
        self.assertEqual(summary['all']['abstention_recall'],1)
        self.assertEqual(summary['all']['false_abstention'],0)
    def test_package_reproduces_predictions_and_requires_harness_source(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder);(p/'runs').mkdir()
            (p/'q.jsonl').write_text(json.dumps(Q[0])+'\n');(p/'h.jsonl').write_text(json.dumps(H['h1'])+'\n')
            r=run();w.atomic_json(p/'runs'/(r['run_id']+'.json'),r)
            cp={'fingerprint':{'questions_sha256':w.digest(p/'q.jsonl'),'histories_sha256':w.digest(p/'h.jsonl'),'harness':{'enabled':True}},'run_ids':{'q1':r['run_id']}}
            w.atomic_json(p/'checkpoint.json',cp)
            w.export_predictions(p/'q.jsonl',p/'h.jsonl',p/'checkpoint.json',p/'predictions.jsonl')
            args=SimpleNamespace(questions=p/'q.jsonl',histories=p/'h.jsonl',checkpoint=p/'checkpoint.json',predictions=p/'predictions.jsonl',presentation=w.ROOT/'submission/presentation.md',out=p/'submission.zip',harness_source=None)
            with self.assertRaises(ValueError):w.package(args)
            (p/'harness').mkdir();(p/'harness/main.py').write_text('# fixture harness\n')
            args.harness_source=p/'harness';w.package(args)
            with zipfile.ZipFile(args.out) as z:
                self.assertTrue({'predictions.jsonl','README.md','presentation.md','code/score.py','harness/main.py','manifest.json'}<=set(z.namelist()))
                self.assertTrue(json.loads(z.read('manifest.json'))['harness_sha256'])

if __name__=='__main__':unittest.main()
