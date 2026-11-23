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

"""ConstraintVerifier – physics and unit sanity checks for PLE tasks.

This is the single biggest win we found on the dev set:
  When the agent's computed answer violates basic physical constraints
  (negative capacities, unreasonable settlements, bad IFC schema), we flag
  the result so the agent knows to retry rather than silently accept it.

The verifier does NOT block tool calls – it annotates results with
`__constraint_violations` and `__retry: True` flags that the agent prompt
instructs it to treat as signals to self-correct.

Usage in serve.py / harness:
    verifier = ConstraintVerifier()
    checked_result = verifier.check("run_python", raw_result)
    if checked_result.get("__retry"):
        # inject violation message into agent context
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Physics bounds – tuned from dev-set failure analysis
# ---------------------------------------------------------------------------
_BOUNDS = {
    # Structural
    "bearing_capacity_kpa": (0.0, 5000.0),     # Net ultimate bearing capacity kPa
    "bearing_capacity_ksf": (0.0, 100.0),
    "factor_of_safety": (0.0, 100.0),          # Must be positive; warn if < 1.5
    "settlement_mm": (0.0, 1000.0),            # Reasonable settlement < 1 m
    "settlement_in": (0.0, 40.0),
    "capacity_kn": (-1e6, 1e6),                # Must be != very wrong order-of-mag
    "moment_knm": (-1e7, 1e7),
    # Mechanical / HVAC
    "cooling_load_kw": (0.0, 1e6),             # Must be positive
    "cop": (0.0, 20.0),                        # Coefficient of performance
    "airflow_cfm": (0.0, 1e7),
    # Electrical
    "voltage_v": (0.0, 1e6),
    "current_a": (0.0, 1e6),
    "power_kw": (0.0, 1e9),
    # Civil / geotechnical
    "consolidation_settlement_mm": (0.0, 2000.0),
    "fs_slope": (0.5, 50.0),                   # Slope factor of safety
}

# Warn-only bounds (don't trigger retry, just annotate)
_WARN_BOUNDS = {
    "factor_of_safety": (1.5, None),           # FS < 1.5 is a warning, not error
    "settlement_mm": (None, 100.0),            # > 100mm warrants attention
}

# Regex patterns that signal incorrect units in a text result
_UNIT_MISMATCH_PATTERNS = [
    # kip·ft used where kN·m expected (common mixed-unit failure)
    re.compile(r"\b(\d+\.?\d*)\s*kip.?ft\b.*\b(\d+\.?\d*)\s*kN.?m\b", re.IGNORECASE),
    # Mixing PSF and kPa in same result
    re.compile(r"\b(\d+\.?\d*)\s*psf\b.*\b(\d+\.?\d*)\s*kPa\b", re.IGNORECASE),
]

# IFC4 deprecated entity types (common hallucination from older training data)
_IFC_DEPRECATED = {
    "IfcBeamStandardCase",
    "IfcColumnStandardCase",
    "IfcSlabStandardCase",
    "IfcWallStandardCase",
    "IfcDoorStandardCase",
    "IfcWindowStandardCase",
}


class ConstraintVerifier:
    """Checks tool outputs against physical and schema constraints."""

    def check(self, tool_name: str, result: Any) -> Any:
        """Check a tool result and annotate it with any violations.

        Args:
            tool_name: Name of the tool that produced the result.
            result: Raw tool output (dict, str, or any).

        Returns:
            The result dict, possibly annotated with __constraint_violations
            and __retry keys. If result is not a dict, wraps it.
        """
        if not isinstance(result, dict):
            result = {"output": result}

        violations: list[str] = []

        # Route to domain-specific checkers
        if tool_name in ("run_python", "shell"):
            violations.extend(self._check_numeric_outputs(result))
            violations.extend(self._check_unit_mixing(result))
        elif tool_name in ("write_ifc4", "shell") and self._looks_like_ifc(result):
            violations.extend(self._check_ifc_schema(result))

        if violations:
            result = {
                **result,
                "__constraint_violations": violations,
                "__retry": True,
                "__retry_message": self._format_retry_message(violations),
            }
        return result

    # ------------------------------------------------------------------
    # Numeric output checks
    # ------------------------------------------------------------------

    def _check_numeric_outputs(self, result: dict) -> list[str]:
        violations = []
        output_text = str(result.get("output", "") or result.get("stdout", ""))

        for key, (lo, hi) in _BOUNDS.items():
            # Look for patterns like "bearing_capacity_kpa = 123.4" or
            # "Settlement (mm): 456" or "FS = 2.1"
            patterns = [
                rf"\b{re.escape(key)}\s*[=:]\s*(-?\d+\.?\d*)",
                rf"\b{re.escape(key.replace('_', ' '))}\s*[=:(]\s*(-?\d+\.?\d*)",
            ]
            for pat in patterns:
                for m in re.finditer(pat, output_text, re.IGNORECASE):
                    try:
                        val = float(m.group(1))
                        if lo is not None and val < lo:
                            violations.append(
                                f"PHYSICS VIOLATION: {key} = {val} is below "
                                f"minimum {lo}. Check units and sign conventions."
                            )
                        elif hi is not None and val > hi:
                            violations.append(
                                f"PHYSICS VIOLATION: {key} = {val} exceeds "
                                f"maximum {hi}. Likely a unit error (e.g. kN vs N)."
                            )
                    except ValueError:
                        pass

        # Explicit negative capacity check (critical failure mode)
        neg_cap = re.findall(
            r"(?:capacity|strength|resistance)\s*[=:]\s*(-\d+\.?\d*)",
            output_text, re.IGNORECASE,
        )
        for val_str in neg_cap:
            violations.append(
                f"PHYSICS VIOLATION: Negative capacity ({val_str}) is physically "
                "impossible. Check sign convention — compression is negative in "
                "some conventions but capacity must be reported as a magnitude."
            )

        return violations

    # ------------------------------------------------------------------
    # Unit mixing checks
    # ------------------------------------------------------------------

    def _check_unit_mixing(self, result: dict) -> list[str]:
        violations = []
        output_text = str(result.get("output", "") or result.get("stdout", ""))
        for pat in _UNIT_MISMATCH_PATTERNS:
            if pat.search(output_text):
                violations.append(
                    "UNIT MIXING DETECTED: Mixed unit systems found in output "
                    "(e.g. kip·ft and kN·m, or PSF and kPa). Convert everything "
                    "to SI before computing."
                )
                break
        return violations

    # ------------------------------------------------------------------
    # IFC4 schema checks
    # ------------------------------------------------------------------

    def _looks_like_ifc(self, result: dict) -> bool:
        text = str(result.get("output", "") or result.get("stdout", ""))
        return "ifc" in text.lower() or ".ifc" in text.lower()

    def _check_ifc_schema(self, result: dict) -> list[str]:
        violations = []
        output_text = str(result.get("output", "") or result.get("stdout", ""))
        for deprecated in _IFC_DEPRECATED:
            if deprecated in output_text:
                violations.append(
                    f"IFC4 SCHEMA VIOLATION: {deprecated!r} is deprecated in IFC4. "
                    f"Use {deprecated.replace('StandardCase', '')} instead."
                )
        return violations

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_retry_message(violations: list[str]) -> str:
        lines = ["⚠️ CONSTRAINT VIOLATIONS DETECTED — do not accept this result:"]
        for i, v in enumerate(violations, 1):
            lines.append(f"  {i}. {v}")
        lines.append(
            "Please review your calculation, correct the issue, and recompute."
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Autobot / Eris Triple-Gate Contract Validator for Deliverables
# ---------------------------------------------------------------------------

def verify_deliverable_triple_gate(file_path: str, content_bytes: bytes) -> dict[str, Any]:
    """Execute Autobot Triple-Gate Contract Validation on candidate deliverable.

    Gate 1: Integrity (existence, non-empty, absence of NaN/TODO placeholders).
    Gate 2: Physical & Domain Bounds (reasonable values, non-negative capacity).
    Gate 3: Format Schema (IFC4 header, XLSX/CSV structure, memo sections).

    Returns:
        Dict with "passed": bool, "gate": int (if failed), and "violations": list[str].
    """
    violations: list[str] = []

    # ---------------- Gate 1: Integrity ----------------
    if not content_bytes or len(content_bytes) == 0:
        return {
            "passed": False,
            "gate": 1,
            "violations": [f"Gate 1 Integrity: Deliverable {file_path} is empty (0 bytes)."]
        }

    # Inspect text representation if not purely binary
    is_binary = b"\x00" in content_bytes[:4096]
    text_content = ""
    if not is_binary:
        text_content = content_bytes.decode("utf-8", "replace")
        placeholders = ["TODO", "FIXME", "PLACEHOLDER", "NAN", "UNDEFINED", "INSERT VALUE HERE"]
        found_placeholders = [p for p in placeholders if p in text_content.upper()]
        if found_placeholders:
            violations.append(
                f"Gate 1 Integrity: Incomplete deliverable — found placeholder tokens: {found_placeholders}"
            )

    # ---------------- Gate 2: Physical & Domain Bounds ----------------
    if text_content:
        # Check negative capacity / resistance
        neg_matches = re.findall(r"(?:capacity|resistance|strength)\s*[:=]\s*(-\d+\.?\d*)", text_content, re.IGNORECASE)
        if neg_matches:
            violations.append(
                f"Gate 2 Physical Invariant: Negative capacity detected ({neg_matches}). Capacity must be positive."
            )

        # Check factor of safety
        fs_matches = re.findall(r"\b(?:FS|factor of safety)\s*[:=]\s*(\d+\.?\d*)", text_content, re.IGNORECASE)
        for val_str in fs_matches:
            val = float(val_str)
            if val < 1.0:
                violations.append(
                    f"Gate 2 Physical Invariant: Factor of Safety {val} < 1.0 indicates structural failure."
                )

    # ---------------- Gate 3: Format & Schema Compliance ----------------
    lower_path = file_path.lower()
    if lower_path.endswith(".ifc"):
        if b"ISO-10303-21" not in content_bytes:
            violations.append("Gate 3 Schema: Missing standard STEP/IFC header 'ISO-10303-21'.")
        if b"IFC4" not in content_bytes and b"IFC2X3" not in content_bytes:
            violations.append("Gate 3 Schema: Missing IFC schema declaration (e.g. FILE_SCHEMA(('IFC4'))).")
    elif lower_path.endswith((".md", ".txt", ".memo")):
        required_sections = ["summary", "calculation", "conclusion"]
        missing_sections = [s for s in required_sections if s not in text_content.lower()]
        if len(missing_sections) >= 2:
            violations.append(f"Gate 3 Deliverable Format: Missing standard engineering sections: {missing_sections}")

    if violations:
        return {
            "passed": False,
            "gate": 1 if any("Gate 1" in v for v in violations) else (2 if any("Gate 2" in v for v in violations) else 3),
            "violations": violations
        }

    return {
        "passed": True,
        "gate": 3,
        "violations": [],
        "message": "All 3 gates passed: Integrity, Physical Invariants, and Format Schema validated."
    }

