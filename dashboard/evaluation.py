"""Versioned harness contract, deterministic scoring, and Reactor transport."""
from copy import deepcopy
from pathlib import Path
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import ssl
import certifi

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'reactor'))
import client

CONTRACT = 'halo.harness.v1'
BASE_URL = 'https://t05.api.reactor-hackathon.rxai.dev'
AGENT_ID = 'agt_afcb0e0cb62f62db'
os.environ.setdefault('SSL_CERT_FILE', certifi.where())


def validate_input(data):
    if not isinstance(data, dict): raise ValueError('Request must be a JSON object')
    prompt = data.get('original_prompt', data.get('question'))
    if not isinstance(prompt, str) or not prompt.strip(): raise ValueError('original_prompt is required')
    if len(prompt) > 16000: raise ValueError('Original prompt exceeds 16,000 characters')
    question_id = data.get('question_id') or 'q_ui_' + secrets.token_hex(6)
    if not isinstance(question_id, str) or not question_id.strip(): raise ValueError('question_id must be a non-empty string')
    when = data.get('question_date', data.get('date')) or None
    if when is not None:
        if not isinstance(when, str): raise ValueError('question_date must be a string')
        client.parse_question_date(when)
    evidence = validate_evidence(data.get('evidence', []))
    context = data.get('context', {})
    if not isinstance(context, dict): raise ValueError('context must be an object')
    if 'dry_run' in data and not isinstance(data['dry_run'], bool): raise ValueError('dry_run must be a boolean')
    reference = data.get('reference')
    if reference is not None: validate_reference(reference)
    history = data.get('history_id')
    if history is not None and (not isinstance(history, str) or not history): raise ValueError('history_id must be a non-empty string')
    return {'original_prompt': prompt, 'question_id': question_id, 'question_date': when,
            'evidence': evidence, 'context': deepcopy(context), 'reference': deepcopy(reference),
            'history_id': history, 'dry_run': data.get('dry_run', False)}


def validate_evidence(value):
    if not isinstance(value, list): raise ValueError('evidence must be an array')
    for e in value:
        if not isinstance(e, dict) or not all(isinstance(e.get(k), str) and e[k].strip() for k in ('id', 'text')):
            raise ValueError('Each evidence item needs non-empty string id and text')
        if e.get('date') is not None and (not isinstance(e['date'], str) or '\n' in e['date']):
            raise ValueError('Evidence dates must be single-line strings')
    return [{k: e[k] for k in ('id', 'text', 'date') if k in e} for e in value]


def validate_reference(ref):
    if not isinstance(ref, dict) or not isinstance(ref.get('answer'), str) or type(ref.get('abstain')) is not bool:
        raise ValueError('reference needs answer (string) and abstain (boolean)')
    for k in ('evidence', 'aliases'):
        if k in ref and (not isinstance(ref[k], list) or any(not isinstance(s, str) for s in ref[k])):
            raise ValueError(f'reference.{k} must be an array of strings')


def validate_harness(config):
    if not isinstance(config, dict): raise ValueError('Harness configuration must be an object')
    enabled = config.get('enabled', False)
    if type(enabled) is not bool: raise ValueError('enabled must be a boolean')
    url = config.get('url', '')
    if not isinstance(url, str): raise ValueError('url must be a string')
    parsed = urllib.parse.urlsplit(url)
    if url and (parsed.username or parsed.password or parsed.fragment or parsed.query):
        raise ValueError('Harness URL must not contain credentials, query parameters, or fragments')
    if url and not (parsed.scheme == 'https' and parsed.hostname or parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
        raise ValueError('Use HTTPS, or HTTP on localhost, for the harness URL')
    if enabled and not url: raise ValueError('Enabled harness requires a URL')
    timeout = config.get('timeout_s', 30)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
        raise ValueError('timeout_s must be between 1 and 120')
    token = config.get('token', '')
    if not isinstance(token, str) or '\n' in token or '\r' in token: raise ValueError('Invalid harness token')
    return {'enabled': enabled, 'url': url, 'timeout_s': timeout, 'token': token}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args): return None


def prepare(source, config, run_id):
    payload = {'contract_version': CONTRACT, 'run_id': run_id, 'question_id': source['question_id'],
               'original_prompt': source['original_prompt'], 'question_date': source['question_date'],
               'evidence': deepcopy(source['evidence']), 'context': deepcopy(source['context'])}
    if not config['enabled']:
        return {'prompt': source['original_prompt'], 'question_date': source['question_date'],
                'evidence': deepcopy(source['evidence'])}
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    if config.get('token'): headers['Authorization'] = 'Bearer ' + config['token']
    request = urllib.request.Request(config['url'], data=json.dumps(payload).encode(), headers=headers, method='POST')
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())))
    try:
        with opener.open(request, timeout=config['timeout_s']) as response:
            raw = response.read(100001)
    except urllib.error.HTTPError as e:
        raise ValueError(f'Harness returned HTTP {e.code}; Reactor was not called') from None
    except (OSError, TimeoutError):
        raise ValueError('Harness connection failed or timed out; Reactor was not called') from None
    if len(raw) > 100000: raise ValueError('Harness response exceeds 100 KB')
    try: output = json.loads(raw)
    except ValueError: raise ValueError('Harness returned invalid JSON') from None
    if not isinstance(output, dict) or output.get('contract_version') != CONTRACT:
        raise ValueError('Harness response must declare contract_version halo.harness.v1')
    if not isinstance(output.get('prompt'), str) or not output['prompt'].strip():
        raise ValueError('Harness response needs a non-empty prompt')
    when = output.get('question_date', source['question_date'])
    if when is not None:
        if not isinstance(when, str): raise ValueError('Harness question_date must be a string')
        client.parse_question_date(when)
    return {'prompt': output['prompt'], 'question_date': when,
            'evidence': validate_evidence(output.get('evidence', source['evidence']))}


def compile_message(prepared, ref):
    when = client.parse_question_date(prepared['question_date']) if prepared['question_date'] else None
    evidence = client.load_evidence(json.dumps(prepared['evidence'])) if prepared['evidence'] else []
    text = client.build_message(prepared['prompt'], evidence, ref, when)
    if len(text) > 16000: raise ValueError('Prepared Reactor message exceeds 16,000 characters')
    return {'type': 'message', 'text': text}, evidence


def reactor_transport(message, evidence, ref, history_id, access_key):
    try:
        result, retries = client.ask_retrying(BASE_URL, AGENT_ID, access_key, message['text'], timeout=300, ref=ref, history=history_id)
        citations, unknown = client.citations(result['answer'], evidence)
        return {'status': 'answered', **result, 'retries': retries, 'citations': citations,
                'unknown_citations': unknown, 'uncited': bool(evidence) and not citations,
                'abstained': bool(client._INSUFFICIENT.match(result['answer']))}
    except client.NoAnswer as e:
        status = 'refused' if isinstance(e, client.Refused) else 'timeout' if isinstance(e, client.TimedOut) else 'closed'
        return {'status': status, 'error': str(e), 'code': getattr(e, 'code', None),
                'retries': getattr(e, 'retries', 0), 'other_replies': e.other_replies, 'late_replies': e.late_replies}
    except (OSError, ValueError) as e:
        return {'status': 'error', 'error': str(e)}


def normalize(text):
    text = re.sub(r'\[E\d+\]', '', str(text)).lower()
    return ' '.join(''.join(c if c.isalnum() or c.isspace() else ' ' for c in text).split())


def score(result, reference):
    completed = result.get('status') == 'answered'
    predicted = {c if isinstance(c, str) else c.get('id') for c in result.get('citations', [])} - {None}
    metrics = {'completed': completed, 'abstained': bool(result.get('abstained')) if completed else None,
               'citation_count': len(predicted), 'exact_match': None, 'evidence_f1': None, 'abstention_correct': None,
               'reference_available': reference is not None, 'scorer_version': 'deterministic-v1'}
    if reference is None: return metrics
    validate_reference(reference)
    metrics['abstention_correct'] = completed and bool(result.get('abstained')) == reference['abstain']
    if not reference['abstain']:
        metrics['exact_match'] = completed and not result.get('abstained', False) and normalize(result.get('answer', '')) in {normalize(a) for a in [reference['answer'], *reference.get('aliases', [])]}
        if 'evidence' in reference:
            truth = set(reference['evidence'])
            metrics['evidence_f1'] = 2 * len(predicted & truth) / (len(predicted) + len(truth)) if predicted or truth else 0.0
    return metrics


def execute(run, source, config, access_key, persist, transport=None):
    transport = transport or reactor_transport
    started = time.monotonic()
    stage = 'harness'
    run.update(status='preparing', stages=[])
    persist(run)
    try:
        t = time.monotonic()
        prepared = prepare(source, config, run['run_id'])
        run['prepared'] = prepared
        run['stages'].append({'stage': 'harness', 'status': 'completed', 'duration_s': round(time.monotonic()-t, 4), 'mode': 'http' if config['enabled'] else 'passthrough'})
        stage = 'compile'
        ref = secrets.token_hex(4)
        message, evidence = compile_message(prepared, ref)
        run['outbound_message'] = message
        run['ref'] = ref
        run['stages'].append({'stage': 'compile', 'status': 'completed', 'message_chars': len(message['text'])})
        if source['dry_run']:
            run.update(status='prepared', metrics=None)
        else:
            stage = 'reactor'
            run['status'] = 'running'; persist(run)
            t = time.monotonic()
            result = transport(message, evidence, ref, source['history_id'], access_key)
            run['stages'].append({'stage': 'reactor', 'status': result['status'], 'duration_s': round(time.monotonic()-t,4)})
            run['result'] = {**result, 'question': source['original_prompt'], 'prepared_prompt': prepared['prompt'],
                             'question_id': source['question_id'], 'history_id': source['history_id'],
                             'question_date': prepared['question_date'], 'evidence': evidence, 'ref': ref,
                             'run_id': run['run_id'], 'recorded_at': time.time()}
            stage = 'evaluation'
            run['metrics'] = score(result, source['reference'])
            run['status'] = 'completed' if result['status'] == 'answered' else 'failed'
            run['stages'].append({'stage': 'evaluation', 'status': 'completed'})
    except Exception as e:
        # External exception strings may contain credentials; never serialize unredacted secrets.
        message = str(e)
        for secret in (access_key, config.get('token')):
            if secret: message = message.replace(secret, '[REDACTED]')
        run.update(status='failed', error={'stage':stage,'message':message[:500]})
        run['stages'].append({'stage':stage,'status':'failed'})
        run['result'] = {'status':'error','error':message[:500], 'question':source['original_prompt'],
                         'question_id':source['question_id'],'history_id':source['history_id'],
                         'question_date':source['question_date'],'evidence':source['evidence'],
                         'run_id':run['run_id'],'recorded_at':time.time()}
        run['metrics'] = score(run['result'], source['reference'])
    finally:
        run['duration_s'] = round(time.monotonic()-started, 4)
        run['finished_at'] = time.time()
        persist(run)
    return run
