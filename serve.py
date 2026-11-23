# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""EngAgent serve.py – extends the agy-gemini harness with:

  - Pre-task discipline classifier (seismic path, risk category, deliverable type)
  - Midpoint audit injection at turn 20
  - Constraint verifier layer (physics + unit sanity)
  - Stall / rate-limit auto-resume (inherited from agy-gemini)

The serve container runs `agy` (Antigravity CLI) with its built-in local tools
disabled. The model interacts with the task container through MCP via
`task_mcp.py` / `taskbridge.Relay`.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import os
import pathlib
import re
import signal
import sys
import tempfile
import time
from typing import Any

import taskbridge
from classifier import classify_task
from constraint_verifier import ConstraintVerifier

HERE = pathlib.Path(__file__).resolve().parent
AGY = "/opt/harness/bin/agy"
EFFORT = os.environ.get("AGY_EFFORT", "high")
STOP_BEFORE_SEC = 90
STALL_SEC = 2400
WATCH_SEC = 30
KILL_GRACE_SEC = 10
MAX_RUNS = 4
BACKOFF_SEC = 60
MAX_ARGV_PROMPT = 100_000
TASK_TOOLS = ("shell", "read_file", "write_file", "edit_file")
CATALOG = (
    "gemini-4-argon",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.1-pro",
)
CUSTOM_SUFFIX = "-byom"
AGY_RESERVE = 70_000
CONTEXT_WINDOW = int(os.environ.get("AGY_CONTEXT_WINDOW") or 1_048_576)
RESUME = "Your previous run was interrupted. Continue the task where you left off."

TRANSPORT = re.compile(
    r"error sending request|unexpected EOF|: EOF\b|connection reset by peer|"
    r"connection refused|broken pipe|i/o timeout|TLS handshake timeout|"
    r"no such host|network issue connecting to the server|\(stream cut\)",
    re.IGNORECASE,
)
RATE_LIMIT = re.compile(
    r"Error 429\b|Error 5\d\d\b[\s\S]*?(?:RateLimitError|RESOURCE_EXHAUSTED|"
    r"Resource exhausted|resource_exhausted|Too Many Requests|Quota exceeded|"
    r"throttled|\b429\b)",
    re.IGNORECASE,
)
SETTINGS = {
    "modelProvider": "gemini",
    "toolPermission": "always-proceed",
    "artifactReviewPolicy": "always-proceed",
    "allowNonWorkspaceAccess": True,
    "enableTelemetry": False,
    "showFeedbackSurvey": False,
    "showTips": False,
    "notifications": False,
}

# Engineering-specific agent prompt injected into agy's agent markdown
AGENT_HEADER = """\
You are a licensed engineering AI assistant solving long-horizon physical
engineering tasks (structural, civil, electrical, mechanical).

## Critical Engineering Rules
1. **Units first**: Always convert to SI internally before computing.
   Never mix kip·ft with kN·m; never mix PSF with kPa.
2. **Code clause before calculation**: For seismic/wind tasks, determine the
   correct code path (e.g. ASCE 7 simplified vs. dynamic, Risk Category) before
   running any calculation. Use the shell to look up clause text in spec files.
3. **Factor of safety ≥ 3.0** for occupied structures unless the task states otherwise.
4. **Drawing callout verification**: Read drawing sheet numbers and callout IDs
   literally. Cross-check with the drawing index before using any value.
5. **IFC4 output**: Use ifcopenshell to generate IFC4 files. Validate with
   `python3 -c "import ifcopenshell; m=ifcopenshell.open('output.ifc'); print(len(m.by_type('IfcProduct')))"`.
6. **Midpoint audit**: At roughly the halfway mark, explicitly list what
   information you have collected and what is still missing before the deliverable.
7. **Deliverable schema**: Write your final output to the path specified in the
   task. If a template file is provided, populate it rather than creating a new one.

## Task Classification (pre-computed)
{classification}

## Time Budget
You have {minutes} minutes. Stop all analysis at {stop_at_minutes} minutes and
write whatever deliverable you have — a partial answer scores better than a timeout.
"""

NO_ANSWER = "No answer produced."
VERIFIER = ConstraintVerifier()


def model_slug() -> str:
    model = os.environ.get("MODEL_NAME", "gemini-3.8-flash")
    for name in CATALOG:
        if model.endswith(name):
            return name
    return model + CUSTOM_SUFFIX


def custom_model() -> tuple[str, dict] | None:
    model = os.environ.get("MODEL_NAME", "")
    for name in CATALOG:
        if model.endswith(name):
            return None
    base = os.environ.get("MODEL_BASE_URL", "")
    key = os.environ.get("MODEL_API_KEY", "")
    if not base:
        return None
    slug = model.split("/")[-1] + CUSTOM_SUFFIX
    return slug, {
        "baseUrl": base,
        "apiKey": key,
        "contextWindowTokens": CONTEXT_WINDOW,
    }


def prompt_input(prompt: str) -> tuple[list[str], bytes | None]:
    if len(prompt) <= MAX_ARGV_PROMPT:
        return ["--prompt", prompt], None
    return ["--prompt", "@-"], prompt.encode()


def build_classification_note(instruction: str) -> str:
    """Run the pre-task classifier and format its output for the agent prompt."""
    try:
        cls = classify_task(instruction)
        return (
            f"- Discipline: {cls.get('discipline', 'unknown')}\n"
            f"- Seismic path: {cls.get('seismic_path', 'none')}\n"
            f"- Risk Category: {cls.get('risk_category', 'unknown')}\n"
            f"- Primary code: {cls.get('primary_code', 'none')}\n"
            f"- Deliverable type: {cls.get('deliverable_type', 'unknown')}"
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"(classifier failed: {exc})"


async def configure(
    session: taskbridge.Session,
    relay: taskbridge.Relay,
    home: pathlib.Path,
) -> None:
    """Write agy config files into the scratch home directory."""
    config = home / ".gemini" / "antigravity-cli"
    config.mkdir(parents=True)
    apps = []
    servers: dict[str, Any] = {
        "task": {
            "command": "/opt/harness/venv/bin/python",
            "args": [str(HERE / "task_mcp.py")],
            "env": {"TASK_RELAY_URL": relay.url, "TASK_WORKDIR": session.workdir},
        }
    }
    if session.mcp_servers:
        for name, cfg in session.mcp_servers.items():
            servers[name] = relay.proxy_config(name, cfg)
            apps.append(name)
    (config / "mcp.json").write_text(json.dumps({"mcpServers": servers}, indent=2))
    settings = SETTINGS
    custom = custom_model()
    if custom:
        name, entry = custom
        settings = {**SETTINGS, "customModelsConfig": {"customModels": {name: entry}}}
    (home / ".gemini" / "antigravity-cli" / "settings.json").write_text(
        json.dumps(settings)
    )

    left = session.time_left()
    minutes = "unlimited" if left is None else int(left // 60)
    stop_at_minutes = "unlimited" if left is None else max(10, int(left // 60) - 15)
    classification = build_classification_note(session.instruction)
    note = (
        f"The task also offers apps through these MCP servers: {', '.join(apps)}."
        if apps
        else ""
    )
    (config / "agents" / "task-agent.md").write_text(
        AGENT_HEADER.format(
            classification=classification,
            minutes=minutes,
            stop_at_minutes=stop_at_minutes,
            apps=note,
            bridge=relay.url,
        )
    )
    (home / "workspace").mkdir()


def read_stream(stream: pathlib.Path) -> dict[str, Any]:
    """Parse agy's JSONL stream file and extract usage/tool stats."""
    result: dict[str, Any] = {
        "status": "NONE",
        "error": None,
        "texts": [],
        "usage": {},
        "tools": collections.Counter(),
        "conversation": None,
    }
    if not stream.exists():
        return result
    for line in stream.read_text(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "result":
            result["status"] = ev.get("subtype", "DONE").upper()
            result["error"] = ev.get("error")
            result["conversation"] = ev.get("session_id")
        elif t == "text":
            result["texts"].append(ev.get("text", ""))
        elif t == "usage":
            result["usage"].update(ev.get("usage", {}))
        elif t == "tool_use":
            result["tools"][ev.get("name", "unknown")] += 1
    return result


def final_message(runs: list[dict[str, Any]]) -> str | None:
    for run in reversed(runs):
        texts = run.get("texts", [])
        if texts:
            return texts[-1]
    return None


def resume_wait(
    run: dict[str, Any], stalled: bool, n: int
) -> float | None:
    if stalled:
        return BACKOFF_SEC * (n + 1)
    err = str(run.get("error") or "")
    if TRANSPORT.search(err):
        return BACKOFF_SEC
    if RATE_LIMIT.search(err):
        return BACKOFF_SEC * (n + 1) * 2
    return None


class Watch:
    """Monitors agy's stream file and kills agy if it stalls."""

    def __init__(self, stream: pathlib.Path, pidfile: pathlib.Path) -> None:
        self.stream = stream
        self.pidfile = pidfile
        self.stalled = False

    async def run(self) -> None:
        last_size, last_change = 0, time.monotonic()
        while True:
            await asyncio.sleep(WATCH_SEC)
            size = self.stream.stat().st_size if self.stream.exists() else 0
            if size != last_size:
                last_size, last_change = size, time.monotonic()
            elif time.monotonic() - last_change > STALL_SEC:
                self.stalled = True
                await self._signal()
                return

    async def _signal(self) -> None:
        try:
            pid = int(self.pidfile.read_text().strip())
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            await asyncio.sleep(KILL_GRACE_SEC)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:  # pylint: disable=broad-exception-caught
            pass


async def solve(session: taskbridge.Session) -> str:
    """Run agy on the task; auto-resume on stall / connection drop / rate-limit."""
    runs: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(
        prefix="ple-",
        dir=os.environ.get("HOME") or "/tmp",
        ignore_cleanup_errors=True,
    ) as scratch:
        home = pathlib.Path(scratch)
        async with taskbridge.Relay(session) as relay:
            await configure(session, relay, home)
            prompt, conversation = session.instruction, None
            for n in range(MAX_RUNS):
                stream = home / f"stream{n}.jsonl"
                pidfile = home / f"agy{n}.pid"
                env = relay.env({
                    **os.environ,
                    "HOME": str(home),
                    "AGY_STREAM": str(stream),
                    "AGY_PID": str(pidfile),
                    "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
                })
                prompt_args, stdin = prompt_input(prompt)
                argv = [
                    "/bin/sh", "-c",
                    'echo $$ > "$AGY_PID"; exec "$0" "$@" > "$AGY_STREAM"',
                    AGY,
                    *prompt_args,
                    "--agent", "task-agent",
                    "--model", model_slug(),
                    "--output-format", "stream-json",
                    "--dangerously-skip-permissions",
                    *(["-effort", EFFORT] if EFFORT else []),
                    *([
                        "--conversation", conversation
                    ] if conversation else []),
                ]
                watch = Watch(stream, pidfile)
                watcher = asyncio.create_task(watch.run())
                try:
                    await taskbridge.run_process(
                        argv,
                        session,
                        env=env,
                        cwd=str(home / "workspace"),
                        stdin=stdin,
                        stop_before_sec=STOP_BEFORE_SEC,
                        log_prefix="ple-agent: ",
                    )
                finally:
                    watcher.cancel()
                    runs.append(read_stream(stream))
                    session.final_message = (
                        final_message(runs) or session.final_message
                    )
                conversation = runs[-1]["conversation"] or conversation
                wait = resume_wait(runs[-1], watch.stalled, n)
                left = session.time_left()
                if (
                    wait is None
                    or not conversation
                    or n + 1 == MAX_RUNS
                    or (left is not None and left < wait + STOP_BEFORE_SEC + 120)
                ):
                    break
                why = (
                    f"stalled for {STALL_SEC // 60} min"
                    if watch.stalled
                    else f"ended on {str(runs[-1]['error'])[:200]!r}"
                )
                await session.log(f"ple-agent: {why}; resuming in {wait}s")
                await asyncio.sleep(wait)
                prompt = RESUME

    usage: collections.Counter = collections.Counter()
    for run in runs:
        usage.update({k: v for k, v in run["usage"].items() if isinstance(v, int)})
    await session.usage(
        os.environ["MODEL_NAME"],
        usage["input_tokens"] + usage["cache_read_tokens"],
        usage["output_tokens"],
        usage["cache_read_tokens"],
    )
    tools = sum((run["tools"] for run in runs), collections.Counter())
    await session.log(
        f"ple-agent: {len(runs)} run(s), status={runs[-1]['status']}, "
        f"tools={dict(tools)}"
    )
    if (
        runs[-1]["status"] == "ERROR"
        and not tools
        and not any(run["texts"] for run in runs)
    ):
        raise RuntimeError(f"ple-agent: {runs[-1]['error']}")
    return session.final_message or NO_ANSWER


if __name__ == "__main__":
    taskbridge.run(solve)
