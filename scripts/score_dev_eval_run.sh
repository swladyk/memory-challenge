#!/usr/bin/env bash
# Score a run on the dev slice with the organisers' score.py: export (kit converter + supported-citation fallback),
# strict validation against the slice, then score.py against the slice's gold. Works on a partial run too: questions
# not answered yet count as wrong, exactly as score.py counts them. Contacts nothing.
#
#   bash scripts/score_dev_eval_run.sh [ours|kit] [RUN_DIR]   # default: ours, <slice>/reactor-live
set -u
cd "$(dirname "$0")/.." || exit 1
case "${1:-ours}" in
    ours) SLICE=runs/dev-live ;;
    kit) SLICE=runs/dev-live-kit ;;
    *) echo "usage: bash scripts/score_dev_eval_run.sh [ours|kit] [RUN_DIR]"; exit 2 ;;
esac
RUN_DIR="${2:-$SLICE/reactor-live}"
NAME=$(basename "$RUN_DIR")
OUT="$SLICE/predictions.$NAME.jsonl"
export PYTHONPATH=src

if [ ! -f "$RUN_DIR/results.jsonl" ]; then
    echo "No $RUN_DIR/results.jsonl yet: the run hasn't written results (they are written when it ends or stops)."
    exit 1
fi
echo "== export: $RUN_DIR -> $OUT"
.venv/bin/python -m memory_challenge export-predictions --run "$RUN_DIR" --payloads "$SLICE" \
    --histories dev-large/histories.jsonl --questions "$SLICE/questions.subset.jsonl" --out "$OUT" \
    > "$OUT.export.log" 2>&1 || { echo "export failed: see $OUT.export.log"; exit 1; }
.venv/bin/python - "$OUT.export.log" <<'EOF'
import json, sys
t, dec, i, objs = open(sys.argv[1]).read(), json.JSONDecoder(), 0, []
while (j := t.find("{", i)) >= 0:  # the export prints its report and the validation as JSON objects
    try:
        obj, i = dec.raw_decode(t, j)
        objs.append(obj)
    except ValueError:
        i = j + 1
rep = next((o for o in objs if "citation_fallback" in o), {})
fb = rep.get("citation_fallback") or {}
print(f"  {rep.get('summary')}\n  citation fallback: {fb.get('candidates')} uncited answer(s), {fb.get('filled')} filled")
EOF
echo "== strict validation against the slice"
.venv/bin/python -m memory_challenge validate-predictions --predictions "$OUT" --questions "$SLICE/questions.subset.jsonl" \
    --histories dev-large/histories.jsonl --gold "$SLICE/gold.subset.jsonl" > "$OUT.validate.log" 2>&1
.venv/bin/python - "$OUT.validate.log" <<'EOF'
import json, sys
t = open(sys.argv[1]).read()
d = json.loads(t[t.find("{"):t.rfind("}") + 1])
print("  " + ", ".join(f"{k}: {len(v) if isinstance(v, list) else v}" for k, v in d.items() if k in ("valid", "predictions", "questions", "missing", "errors")))
EOF
echo "== score.py (organisers' scorer; LLM-judged answers show as 'unjudged' here)"
python3 score.py "$SLICE/gold.subset.jsonl" "$OUT" --details "$OUT.scores.jsonl"
