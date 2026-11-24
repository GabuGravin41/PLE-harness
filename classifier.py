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

"""Pre-task classifier for PLE engineering tasks.

Runs a lightweight 1-shot LLM call to determine:
  - discipline: structural | civil | electrical | mechanical
  - seismic_path: simplified | dynamic | none
  - risk_category: 1 | 2 | 3 | 4
  - primary_code: ASCE7 | IBC | NFPA | NEC | none
  - deliverable_type: calc_memo | ifc4_model | xlsx_report | pdf_memo | other

This avoids the most common failure mode: defaulting to the simplified seismic
path for Risk Category III/IV buildings, or applying IBC where ASCE 7 is needed.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

try:
    from openai import OpenAI
    _HAS_OPENAI = True
except ImportError:
    _HAS_OPENAI = False

CLASSIFY_PROMPT = """\
You are an expert engineering task classifier.
Read the task description below and output ONLY valid JSON (no markdown, no explanation).

Output schema:
{
  "discipline": "structural|civil|electrical|mechanical",
  "seismic_path": "simplified|dynamic|none",
  "risk_category": 1,
  "primary_code": "ASCE7|IBC|NFPA|NEC|none",
  "deliverable_type": "calc_memo|ifc4_model|xlsx_report|pdf_memo|other"
}

Rules:
- risk_category: 1=storage/non-occupied, 2=typical occupancy, 3=schools/hospitals, 4=critical infrastructure
- seismic_path: "simplified" only for single-story or SDC A/B with Risk Cat I/II; otherwise "dynamic"
- If the task involves IFC, Revit, or BIM → deliverable_type="ifc4_model"
- If the task produces an engineering calculation or report → deliverable_type="calc_memo"

Task description:
{instruction}
"""

_DEFAULT = {
    "discipline": "unknown",
    "seismic_path": "none",
    "risk_category": 2,
    "primary_code": "none",
    "deliverable_type": "calc_memo",
}

_JSON_RE = re.compile(r"\{[\s\S]+\}", re.MULTILINE)


def classify_task(instruction: str) -> dict[str, Any]:
    """Classify the engineering task using a quick LLM call.

    Falls back to keyword-based heuristics if the LLM call fails.

    Args:
        instruction: The raw task instruction string.

    Returns:
        A dictionary with discipline, seismic_path, risk_category,
        primary_code, and deliverable_type keys.
    """
    if _HAS_OPENAI:
        base_url = os.environ.get("MODEL_BASE_URL")
        api_key = os.environ.get("MODEL_API_KEY", "placeholder")
        model = os.environ.get("MODEL_NAME", "gemini-3.8-flash").split("/")[-1]
        if base_url:
            try:
                client = OpenAI(base_url=base_url, api_key=api_key)
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{
                        "role": "user",
                        "content": CLASSIFY_PROMPT.format(
                            instruction=instruction[:3000]
                        ),
                    }],
                    max_tokens=200,
                    temperature=0,
                )
                raw = resp.choices[0].message.content or ""
                m = _JSON_RE.search(raw)
                if m:
                    result = json.loads(m.group())
                    # Merge with defaults to ensure all keys are present
                    return {**_DEFAULT, **result}
            except Exception:  # pylint: disable=broad-exception-caught
                pass

    return _keyword_classify(instruction)


def _keyword_classify(instruction: str) -> dict[str, Any]:
    """Robust keyword-based fallback classifier with word boundaries and scoring."""
    text = instruction.lower()
    result = dict(_DEFAULT)

    def _matches(kw_list: tuple[str, ...]) -> int:
        count = 0
        for kw in kw_list:
            if re.search(rf"\b{re.escape(kw)}\b", text, re.IGNORECASE):
                count += 1
        return count

    # Discipline keyword dictionaries
    structural_kw = (
        "seismic", "shear wall", "beam", "column", "footing", "steel",
        "concrete", "structural", "ifc", "bim", "girder", "aisc", "lrfd",
        "truss", "slab", "rebar", "aci", "moment frame", "deflection"
    )
    civil_kw = (
        "settlement", "boring", "soil", "clay", "grading", "drainage",
        "pavement", "geotechnical", "civil", "earthwork", "slope", "retaining wall"
    )
    electrical_kw = (
        "electrical", "nec", "circuit", "panel", "load calc", "conduit",
        "voltage", "ampere", "ampacity", "switchboard", "feeder", "transformer",
        "voltage drop", "breaker", "busbar", "3-phase", "480v", "208v", "120v"
    )
    mechanical_kw = (
        "hvac", "cooling", "mechanical", "chiller", "duct", "heat",
        "thermal", "ventilation", "pump", "plumbing", "mep", "airflow", "cfm"
    )

    scores = {
        "structural": _matches(structural_kw),
        "civil": _matches(civil_kw),
        "electrical": _matches(electrical_kw),
        "mechanical": _matches(mechanical_kw),
    }

    best_discipline = max(scores, key=lambda k: scores[k])
    if scores[best_discipline] > 0:
        result["discipline"] = best_discipline

    # Seismic
    if _matches(("seismic", "asce 7", "asce7", "sdc", "spectral")):
        result["seismic_path"] = "dynamic"
        result["primary_code"] = "ASCE7"
        if _matches(("one-story", "single story", "sdc a", "sdc b")):
            if "hospital" not in text and "school" not in text:
                result["seismic_path"] = "simplified"

    # Risk category
    if _matches(("hospital", "healthcare", "emergency", "fire station")):
        result["risk_category"] = 3
    elif _matches(("nuclear", "power plant", "water supply", "dam")):
        result["risk_category"] = 4
    elif _matches(("storage", "agricultural", "warehouse", "uninhabited")):
        result["risk_category"] = 1

    # Code detection with word boundaries
    if _matches(("nec", "nfpa 70", "nfpa70")):
        result["primary_code"] = "NEC"
    elif _matches(("nfpa",)):
        result["primary_code"] = "NFPA"
    elif _matches(("ibc",)):
        result["primary_code"] = "IBC"
    elif _matches(("aisc",)):
        result["primary_code"] = "AISC"
    elif _matches(("aci",)):
        result["primary_code"] = "ACI"
    elif _matches(("asce", "asce 7", "asce7")):
        result["primary_code"] = "ASCE7"
    elif result["discipline"] == "electrical":
        result["primary_code"] = "NEC"
    elif result["discipline"] == "structural":
        result["primary_code"] = "AISC"

    # Deliverable
    if _matches(("ifc", "ifc4", "bim", "revit", "3d model")):
        result["deliverable_type"] = "ifc4_model"
    elif any(k in text for k in (".xlsx", "spreadsheet", "schedule")):
        result["deliverable_type"] = "xlsx_report"
    elif any(k in text for k in ("memo", "report", "letter", ".pdf")):
        result["deliverable_type"] = "pdf_memo"

    return result
