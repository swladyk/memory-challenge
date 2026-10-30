# Reactor connection test

The client and example files were retrieved from swladyk/memory-challenge.
Team: hAlo (t05). Agent: agt_afcb0e0cb62f62db.

From the workspace root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r reactor/requirements.txt
.venv/bin/python reactor/smoke_test.py
```

Enter the access key at the hidden prompt. It is passed to the official client
through its environment and is not saved. The certificate bundle is supplied
by certifi; TLS verification remains enabled.

The test starts a new Reactor history (resetting the agent's current memory),
then asks two questions sequentially using the supplied example evidence.
Do not run it while another teammate is using the same agent.
Results are appended to `results/reactor-smoke.jsonl`; the latest history
metadata is saved in `results/history.json`. Both are ignored by Git.

Use one Reactor history per dataset history in benchmark runs. Evidence IDs
must be session IDs; preserve session dates and the question date.
