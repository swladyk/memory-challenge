"""Ask two example questions; prompt privately for the access key."""
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import certifi

ROOT = Path(__file__).resolve().parent
OUT = ROOT.parent / 'results'
OUT.mkdir(exist_ok=True)
env = os.environ.copy()
env['SSL_CERT_FILE'] = certifi.where()
env['REACTOR_ACCESS_KEY'] = env.get('REACTOR_ACCESS_KEY') or getpass.getpass('Reactor access key: ')
base = [sys.executable, str(ROOT / 'client.py'), '--base-url',
        'https://t05.api.reactor-hackathon.rxai.dev', '--agent-id', 'agt_afcb0e0cb62f62db']

def call(args):
    proc = subprocess.run(base + args, env=env, capture_output=True, text=True)
    result = json.loads(proc.stdout)
    print(json.dumps(result), flush=True)
    if proc.returncode:
        raise SystemExit(proc.returncode)
    return result

history = call(['--new-history'])
(OUT / 'history.json').write_text(json.dumps(history, indent=2))
with (OUT / 'reactor-smoke.jsonl').open('a') as output:
    for number, question in enumerate(['When is the wedding?', 'What colour was the cake?'], 1):
        result = call(['--history', history['history_id'], '--question', question,
                       '--question-date', '2023/05/30 (Tue) 08:08',
                       '--question-id', f'q_example{number:04d}',
                       '--evidence', str(ROOT / 'examples/excerpts-example.json')])
        output.write(json.dumps(result) + '\n')
        output.flush()
