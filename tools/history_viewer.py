"""Browse a histories.jsonl (one dataset history per line) in the browser without loading the file into memory.

Indexes line offsets once at start (one sequential read), then serves one history per page, on 127.0.0.1 only.
questions.jsonl and gold.jsonl next to the file, if present, are shown with each history, and gold evidence sessions
are highlighted. Conversation text is HTML-escaped: it is data, never markup. Standard library only.

    python3 tools/history_viewer.py ~/Downloads/dev-large/histories.jsonl --port 8765
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HID = re.compile(rb'"history_id":\s*"([^"]+)"')
DATE = re.compile(rb'"date":\s*"([^"]+)"')
CSS = """
:root { --bg:#fbfaf7; --fg:#1f2328; --muted:#6b7079; --line:#e3e1db; --card:#ffffff; --user:#eef4ff;
        --asst:#f6f5f2; --ev:#fff4d6; --evline:#d9a400; --link:#0b5fd7; }
@media (prefers-color-scheme: dark) { :root { --bg:#16181c; --fg:#e6e6e6; --muted:#9aa0a8; --line:#2c3036;
        --card:#1d2025; --user:#1c2636; --asst:#20232a; --ev:#3a3016; --evline:#e0b030; --link:#7fb0ff; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 -apple-system, system-ui, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:16px; }
a { color:var(--link); text-decoration:none; } a:hover { text-decoration:underline; }
h1 { font-size:18px; margin:4px 0 12px; } .muted { color:var(--muted); } code { font-size:12px; }
.bar { position:sticky; top:0; background:var(--bg); padding:10px 0; border-bottom:1px solid var(--line);
       display:flex; gap:12px; align-items:center; flex-wrap:wrap; z-index:2; }
input[type=search] { flex:1; min-width:200px; padding:7px 10px; border:1px solid var(--line); border-radius:6px;
       background:var(--card); color:var(--fg); font-size:14px; }
table { width:100%; border-collapse:collapse; } th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line);
       vertical-align:top; } th { font-weight:600; color:var(--muted); font-size:12px; }
.q { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:10px 12px; margin:8px 0; }
.q .meta { font-size:12px; color:var(--muted); }
details.session { background:var(--card); border:1px solid var(--line); border-radius:8px; margin:6px 0; }
details.session.ev { border-left:4px solid var(--evline); background:var(--ev); }
summary { cursor:pointer; padding:8px 12px; font-size:13px; } summary b { font-weight:600; }
.turn { margin:0 12px 10px; padding:8px 10px; border-radius:6px; white-space:pre-wrap; overflow-wrap:anywhere; }
.turn.user { background:var(--user); } .turn.assistant { background:var(--asst); }
.role { font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin-bottom:2px; }
.tag { font-size:11px; padding:1px 6px; border-radius:10px; border:1px solid var(--evline); margin-left:6px; }
"""


def esc(x) -> str:
    return html.escape(str(x), quote=True)


def build_index(path: Path) -> list[dict]:
    """(offset, length, history_id, session count, first and last session date) per line, in one pass."""
    entries, offset = [], 0
    with open(path, "rb") as f:
        for line in f:
            if line.strip():
                m = HID.search(line, 0, 300)
                first = DATE.search(line)
                last_at = line.rfind(b'"date": "')
                last = DATE.match(line, last_at) if last_at >= 0 else None
                entries.append({"offset": offset, "length": len(line),
                                "history_id": m.group(1).decode() if m else f"line {len(entries) + 1}",
                                "sessions": line.count(b'"session_id"'),
                                "first": first.group(1).decode() if first else "",
                                "last": last.group(1).decode() if last else ""})
            offset += len(line)
    return entries


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class Viewer:
    def __init__(self, path: Path):
        t0 = time.time()
        self.path = path
        self.index = build_index(path)
        self.questions: dict[str, list[dict]] = {}
        for q in read_jsonl(path.parent / "questions.jsonl"):
            self.questions.setdefault(q.get("history_id"), []).append(q)
        self.gold = {g["question_id"]: g for g in read_jsonl(path.parent / "gold.jsonl")}
        self.seconds = time.time() - t0

    def history(self, i: int) -> dict:
        e = self.index[i]
        with open(self.path, "rb") as f:
            f.seek(e["offset"])
            return json.loads(f.read(e["length"]))

    def page(self, title: str, body: str) -> bytes:
        return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
                f"<meta name=viewport content='width=device-width, initial-scale=1'><title>{esc(title)}</title>"
                f"<style>{CSS}</style></head><body><main>{body}</main></body></html>").encode("utf-8")

    def index_page(self) -> bytes:
        rows = []
        for i, e in enumerate(self.index):
            qs = self.questions.get(e["history_id"], [])
            qtext = " · ".join(q.get("question", "") for q in qs)
            rows.append(f"<tr data-s='{esc((e['history_id'] + ' ' + qtext).lower())}'><td>{i + 1}</td>"
                        f"<td><a href='/h/{i}'><code>{esc(e['history_id'])}</code></a></td>"
                        f"<td>{e['sessions']}</td><td>{e['length'] / 1e6:.1f} MB</td>"
                        f"<td class=muted>{esc(e['first'])} → {esc(e['last'])}</td><td>{esc(qtext[:160])}</td></tr>")
        body = (f"<h1>{esc(self.path)}</h1><p class=muted>{len(self.index)} histories · "
                f"{sum(e['sessions'] for e in self.index):,} sessions · indexed in {self.seconds:.1f} s · "
                f"{'questions' if self.questions else 'no questions.jsonl'}"
                f"{' + gold' if self.gold else ''} found next to the file</p>"
                "<div class=bar><input type=search id=f placeholder='Filter by history id or question text'>"
                "<span class=muted id=n></span></div>"
                "<table><thead><tr><th>#</th><th>History</th><th>Sessions</th><th>Size</th><th>Dates</th>"
                f"<th>Question(s)</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
                "<script>const f=document.getElementById('f'),n=document.getElementById('n'),"
                "rows=[...document.querySelectorAll('tbody tr')];f.oninput=()=>{const t=f.value.toLowerCase();"
                "let k=0;for(const r of rows){const s=!t||r.dataset.s.includes(t);r.style.display=s?'':'none';k+=s}"
                "n.textContent=t?k+' shown':''};</script>")
        return self.page("Histories", body)

    def history_page(self, i: int) -> bytes:
        h = self.history(i)
        hid = h.get("history_id", "")
        qs = self.questions.get(hid, [])
        evidence: set[str] = set()
        qhtml = []
        for q in qs:
            g = self.gold.get(q.get("question_id"))
            meta = f"<code>{esc(q.get('question_id'))}</code> · asked {esc(q.get('question_date'))}"
            gold = ""
            if g:
                evidence |= set(g.get("evidence", []))
                links = ", ".join(f"<a href='#{esc(s)}'><code>{esc(s)}</code></a>" for s in g.get("evidence", []))
                gold = (f"<div class=meta>gold: {esc(g.get('question_type'))} · abstain={esc(g.get('abstain'))} · "
                        f"answer: <b>{esc(g.get('answer'))}</b>{' · evidence: ' + links if links else ''}</div>")
            qhtml.append(f"<div class=q><div>{esc(q.get('question'))}</div><div class=meta>{meta}</div>{gold}</div>")
        sessions = []
        for n, s in enumerate(h.get("sessions", []), 1):
            sid = s.get("session_id", "")
            turns = s.get("turns", [])
            chars = sum(len(t.get("content", "")) for t in turns)
            ev = sid in evidence
            body = "".join(f"<div class='turn {esc(t.get('role'))}'><div class=role>{esc(t.get('role'))}</div>"
                           f"{esc(t.get('content', ''))}</div>" for t in turns)
            sessions.append(f"<details class='session{' ev' if ev else ''}' id='{esc(sid)}'{' open' if ev else ''}>"
                            f"<summary><b>#{n}</b> · {esc(s.get('date'))} · <code>{esc(sid)}</code> · "
                            f"{len(turns)} turns · {chars:,} chars{'<span class=tag>evidence</span>' if ev else ''}"
                            f"</summary>{body}</details>")
        nav = (f"<a href='/'>← all histories</a>"
               + (f" · <a href='/h/{i - 1}'>‹ previous</a>" if i > 0 else "")
               + (f" · <a href='/h/{i + 1}'>next ›</a>" if i + 1 < len(self.index) else ""))
        body = (f"<p>{nav}</p><h1>History {i + 1}: <code>{esc(hid)}</code></h1>"
                f"<p class=muted>{len(sessions)} sessions · {self.index[i]['length'] / 1e6:.1f} MB · "
                f"{esc(self.index[i]['first'])} → {esc(self.index[i]['last'])}</p>{''.join(qhtml)}"
                "<div class=bar><input type=search id=f placeholder='Find sessions containing… (opens matches)'>"
                "<label><input type=checkbox id=e> evidence only</label><span class=muted id=n></span></div>"
                f"{''.join(sessions)}"
                "<script>const f=document.getElementById('f'),e=document.getElementById('e'),"
                "n=document.getElementById('n'),ss=[...document.querySelectorAll('details.session')];let tm;"
                "function run(){const t=f.value.trim().toLowerCase();let k=0;for(const s of ss){"
                "if(t&&s._t===undefined)s._t=s.textContent.toLowerCase();"
                "const ok=(!e.checked||s.classList.contains('ev'))&&(!t||s._t.includes(t));"
                "s.style.display=ok?'':'none';if(ok&&t)s.open=true;k+=ok}"
                "n.textContent=(t||e.checked)?k+' of '+ss.length+' sessions':''}"
                "f.oninput=()=>{clearTimeout(tm);tm=setTimeout(run,250)};e.onchange=run;</script>")
        return self.page(f"History {i + 1}", body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("histories", type=Path)
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    print(f"indexing {args.histories} …", file=sys.stderr, flush=True)
    viewer = Viewer(args.histories)
    print(f"{len(viewer.index)} histories indexed in {viewer.seconds:.1f} s", file=sys.stderr, flush=True)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = urlsplit(self.path).path
            try:
                if route == "/":
                    data, code = viewer.index_page(), 200
                elif m := re.fullmatch(r"/h/(\d+)", route):
                    i = int(m.group(1))
                    data, code = (viewer.history_page(i), 200) if i < len(viewer.index) else (b"not found", 404)
                else:
                    data, code = b"not found", 404
            except Exception as exc:  # show the error rather than a dropped connection
                data, code = esc(f"error: {exc}").encode(), 500
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"serving on http://127.0.0.1:{args.port}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
