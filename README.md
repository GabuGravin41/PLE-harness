# PLE EngAgent — Engineering-Aware Autonomous Harness

An autonomous engineering agent harness for the [Physical-Engineering's Last Exam (PLE)](https://ple-bench.com/) competition.

## Architecture

Built on top of the `agy-gemini` sample harness with three key additions:

1. **Pre-task Classifier** (`classifier.py`) — Determines discipline, seismic code path, risk category, and deliverable type *before* the ReAct loop starts. Fixes the most common failure mode: wrong seismic code path.

2. **Constraint Verifier** (`constraint_verifier.py`) — Physics and unit sanity checks on every tool result. Flags physically impossible outputs (negative capacity, mixed units, deprecated IFC4 entities) so the agent retries rather than accepting them.

3. **Engineering System Prompt** — Injected into agy's agent markdown with explicit rules for unit discipline, code-clause lookup, factor-of-safety requirements, IFC4 validation, and a midpoint audit obligation.

## Setup

```bash
# From the PLE kit root:
cp -r sample_submissions/agy-gemini ../ple-harness  # already done
cd ../ple-harness
git init -q && git add -A && git commit -m "init"

# Check (requires Docker):
cd ../ple
uv run python -m kit check ../ple-harness

# Run dev set:
uv run python eval/run.py ../ple-harness

# Verify artifacts:
uv run python -m kit artifacts ../ple-harness
```

## Model

**Stage 2 target:** `google-cloud/gemini-3.8-flash`

For local dev, set `MODEL_BASE_URL` to a local Ollama / LM Studio endpoint.

## Files

| File | Role |
|---|---|
| `submission.yaml` | Kit config: model, install, serve commands |
| `serve.py` | Main harness: agy loop + stall/rate-limit resume |
| `install.sh` | Venv + agy binary install |
| `classifier.py` | Pre-task LLM + keyword classifier |
| `constraint_verifier.py` | Physics / unit / IFC4 sanity checks |
| `task_mcp.py` | MCP server: shell, read_file, write_file, edit_file |
| `taskbridge.py` | WebSocket relay (from official sample) |
| `requirements.txt` | Python dependencies |
| `LICENSE` | Apache-2.0 |

## License

Apache-2.0
