"""Read-only API over the framework's live run logs (run.json, events.jsonl, stats_history.jsonl).

Runs live under MEMRET_RUNS_DIR (default: <repo>/runs), up to three folders deep. A run id is its path relative to
that folder with '/' written as '~' (runs/abl/baseline -> abl~baseline). Ids are validated and resolved inside the
folder, never outside it. Lines are addressed by position: ?after=N returns lines N+1.. and 'next' is the offset to
ask for next time; ?tail=N returns only the last N lines (a feed opening on a long run). A last line without its
newline (still being written) is left for the next poll.
"""
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import json, os, re

ROOT = Path(__file__).resolve().parent.parent
RUN_ID = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9._-]*(?:~[A-Za-z0-9_-][A-Za-z0-9._-]*){0,2}')
SKIP = {'payloads', 'traces', 'evidence'}  # per-question files: never run folders, and large
MAX_LIMIT = 5000


def runs_dir():
    return Path(os.environ.get('MEMRET_RUNS_DIR', ROOT / 'runs')).resolve()


def run_folders(base, depth=3):
    if depth == 0 or not base.is_dir(): return
    for entry in sorted(os.scandir(base), key=lambda e: e.name):
        if entry.is_dir(follow_symlinks=False) and entry.name not in SKIP and not entry.name.startswith('.'):
            folder = Path(entry.path)
            if (folder / 'run.json').is_file(): yield folder
            yield from run_folders(folder, depth - 1)


def resolve(run_id):
    if not RUN_ID.fullmatch(run_id or '') or '..' in run_id or SKIP & set(run_id.split('~')): return None
    base = runs_dir()
    folder = base.joinpath(*run_id.split('~')).resolve()
    if base not in folder.parents or not (folder / 'run.json').is_file(): return None
    return folder


def parse(raw, n):
    try: return json.loads(raw)
    except ValueError: return {'unreadable_line': n}


def read_lines(path, after=0, limit=MAX_LIMIT, tail=None):
    """Complete JSON lines after the first ``after`` (at most ``limit``), or the last ``tail``; and the offset to
    resume from."""
    items, n = [], 0
    if path.exists():
        with open(path, 'rb') as f:
            for raw in f:
                if not raw.endswith(b'\n'): break  # partially written: next poll
                n += 1
                if tail is not None:
                    items.append((n, raw))
                    if len(items) > tail: items.pop(0)
                    continue
                if n <= after: continue
                if len(items) >= limit: n -= 1; break
                items.append(parse(raw, n))
    if tail is not None: return [parse(raw, i) for i, raw in items], n
    return items, max(n, after)


def last_line(path):
    """The last complete line, read from the end of the file (snapshots grow to a few MB on large runs)."""
    if not path.exists(): return None
    with open(path, 'rb') as f:
        f.seek(0, 2); end = f.tell(); size = min(end, 1 << 16)
        while True:
            f.seek(end - size); lines = f.read(size).split(b'\n')
            # lines[-1] has no newline yet; lines[0] may start mid-line unless the chunk is the whole file
            complete = [l for l in lines[(0 if size == end else 1):-1] if l.strip()]
            if complete: return json.loads(complete[-1])
            if size == end: return None
            size = min(end, size * 4)


def meta(folder):
    try: data = json.loads((folder / 'run.json').read_text())
    except (OSError, ValueError): return None
    data['id'] = '~'.join(folder.relative_to(runs_dir()).parts)
    return data


def run_summary(folder):
    data = meta(folder)
    if data is None: return None
    snap = last_line(folder / 'stats_history.jsonl')
    keep = ('id', 'run_id', 'kind', 'status', 'command', 'config', 'split', 'partition', 'backend', 'created',
            'finished', 'total', 'has_gold')
    return {k: data.get(k) for k in keep} | {'progress': (snap or {}).get('progress')}


def page(query):
    q = parse_qs(query)
    def num(key, default):
        try: return max(0, int(q[key][0])) if key in q else default
        except ValueError: return default
    tail = num('tail', None)
    return num('after', 0), min(num('limit', MAX_LIMIT) or MAX_LIMIT, MAX_LIMIT), \
        None if tail is None else min(tail, MAX_LIMIT)


def handle(path):
    """(status, body) for a GET under /api/v1/live/; the caller has already checked authorization."""
    url = urlsplit(path)
    parts = url.path.removeprefix('/api/v1/live/').strip('/').split('/')
    if parts == ['runs']:
        runs = [s for s in map(run_summary, run_folders(runs_dir())) if s]
        runs.sort(key=lambda r: r.get('created') or 0, reverse=True)
        return 200, {'runs': runs, 'runs_dir': runs_dir().name}
    if len(parts) not in (2, 3) or parts[0] != 'runs': return 404, {'error': 'Not found'}
    folder = resolve(parts[1])
    if folder is None: return 404, {'error': 'Run not found'}
    data = meta(folder)
    if len(parts) == 2:
        return 200, {'run': data, 'last': last_line(folder / 'stats_history.jsonl')}
    name = {'events': 'events.jsonl', 'stats': 'stats_history.jsonl'}.get(parts[2])
    if name is None: return 404, {'error': 'Not found'}
    after, limit, tail = page(url.query)
    items, offset = read_lines(folder / name, after, limit, tail)
    return 200, {'id': data['id'], 'status': data.get('status'), 'after': after, 'next': offset, 'items': items}
