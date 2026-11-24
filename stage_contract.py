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

"""Stage Contracts and Evidence Ledger for PLE Engineering Tasks.

Implements the Two-Level Feasibility -> Optimization Framework:
  1. Mandatory vs. Enhancing Criteria (NASA Decision Rule):
     - G = 1 only if all mandatory safety, code, and physical clearance checks pass.
     - Feasibility Search: agent cannot proceed to objective optimization until G = 1.
  2. Event-Driven Localized Invalidation:
     - Stage contracts track inputs, outputs, depends_on, and invalidates.
     - Changing an upstream assumption (e.g. beam elevation or soil layer) invalidates
       only dependent downstream contracts, preventing project-wide re-planning churn.
  3. Evidence Ledger:
     - Explicit provenance tracking for extracted engineering facts (CONFIRMED,
       ESTIMATED, or SUPERSEDED).
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import time
from typing import Any, Dict, List, Literal, Optional

ContractStatus = Literal[
    "PENDING",    # Prerequisites not yet met
    "READY",      # Inputs ready for execution
    "RUNNING",    # Actively being executed
    "VERIFYING",  # In independent verification
    "ACCEPTED",   # Contract verified; deliverables locked
    "BLOCKED",    # Mandatory check failed; needs targeted repair
]

EvidenceConfidence = Literal["CONFIRMED", "ESTIMATED", "SUPERSEDED"]


@dataclasses.dataclass
class EvidenceEntry:
    fact_id: str
    fact_name: str
    value: Any
    unit: str
    source_file: str
    source_locator: str  # Page, sheet, or grid callout
    confidence: EvidenceConfidence
    timestamp: float = dataclasses.field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class StageContract:
    contract_id: str
    stage_name: str
    version: int
    status: ContractStatus
    inputs: List[str]            # Required input files or fact_ids
    outputs: List[str]           # Expected deliverable paths
    depends_on: List[str]        # Upstream contract_ids
    invalidates: List[str]       # Downstream contract_ids to cascade upon failure
    mandatory_checks: List[str]  # Non-negotiable checks (G=1 gates)
    enhancing_checks: List[str]  # Quality/optimization criteria (p score)
    verification_results: Dict[str, Any] = dataclasses.field(default_factory=dict)
    retry_count: int = 0
    max_retries: int = 3
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


class StageContractEngine:
    """Manages the lifecycle of stage contracts and the evidence ledger."""

    def __init__(self, state_file: Optional[str] = None):
        self.contracts: Dict[str, StageContract] = {}
        self.evidence: Dict[str, EvidenceEntry] = {}
        self.state_file = state_file

    def record_evidence(
        self,
        fact_name: str,
        value: Any,
        unit: str,
        source_file: str,
        source_locator: str = "",
        confidence: EvidenceConfidence = "CONFIRMED",
    ) -> str:
        """Record an extracted engineering fact in the evidence ledger."""
        fact_id = f"fact_{len(self.evidence) + 1:03d}_{fact_name.lower().replace(' ', '_')[:24]}"
        entry = EvidenceEntry(
            fact_id=fact_id,
            fact_name=fact_name,
            value=value,
            unit=unit,
            source_file=source_file,
            source_locator=source_locator,
            confidence=confidence,
        )
        self.evidence[fact_id] = entry
        self._persist()
        return fact_id

    def register_contract(
        self,
        contract_id: str,
        stage_name: str,
        inputs: List[str],
        outputs: List[str],
        depends_on: Optional[List[str]] = None,
        invalidates: Optional[List[str]] = None,
        mandatory_checks: Optional[List[str]] = None,
        enhancing_checks: Optional[List[str]] = None,
    ) -> StageContract:
        """Register a new stage contract."""
        contract = StageContract(
            contract_id=contract_id,
            stage_name=stage_name,
            version=1,
            status="READY" if not depends_on else "PENDING",
            inputs=inputs,
            outputs=outputs,
            depends_on=depends_on or [],
            invalidates=invalidates or [],
            mandatory_checks=mandatory_checks or [],
            enhancing_checks=enhancing_checks or [],
        )
        self.contracts[contract_id] = contract
        self._persist()
        return contract

    def evaluate_contract(
        self,
        contract_id: str,
        check_results: Dict[str, bool],
    ) -> Dict[str, Any]:
        """Evaluate verification results against mandatory vs. enhancing criteria.

        NASA Decision Rule:
        - If ANY mandatory check fails: status becomes BLOCKED. G=0. Must repair.
        - If all mandatory pass: status becomes ACCEPTED. G=1. May optimize.
        """
        contract = self.contracts.get(contract_id)
        if not contract:
            return {"ok": False, "error": f"Unknown contract: {contract_id}"}

        contract.verification_results = check_results
        mandatory_failures = [
            chk for chk in contract.mandatory_checks
            if not check_results.get(chk, False)
        ]
        enhancing_failures = [
            chk for chk in contract.enhancing_checks
            if not check_results.get(chk, False)
        ]

        if mandatory_failures:
            contract.status = "BLOCKED"
            contract.retry_count += 1
            contract.notes = f"Mandatory gate G=0 violation: {mandatory_failures}"
            self._persist()
            return {
                "ok": True,
                "status": "BLOCKED",
                "gate_passed": False,
                "mandatory_failures": mandatory_failures,
                "enhancing_failures": enhancing_failures,
                "message": (
                    f"MANDATORY GATE FAILURE on {contract_id}: {mandatory_failures}. "
                    "You CANNOT proceed to optimization until this is resolved."
                ),
            }

        contract.status = "ACCEPTED"
        contract.notes = "All mandatory checks satisfied (G=1)."
        self._check_ready_dependents()
        self._persist()
        return {
            "ok": True,
            "status": "ACCEPTED",
            "gate_passed": True,
            "mandatory_failures": [],
            "enhancing_failures": enhancing_failures,
            "message": f"Contract {contract_id} ACCEPTED. Feasibility established (G=1).",
        }

    def invalidate_contract(self, contract_id: str, reason: str) -> List[str]:
        """Localized Invalidation: invalidate ONLY downstream dependents."""
        contract = self.contracts.get(contract_id)
        if not contract:
            return []

        invalidated_ids = [contract_id]
        contract.status = "BLOCKED"
        contract.version += 1
        contract.notes = f"Invalidated: {reason}"

        # Cascade invalidation to explicit downstream targets
        for downstream_id in contract.invalidates:
            if downstream_id in self.contracts:
                downstream = self.contracts[downstream_id]
                if downstream.status in ("ACCEPTED", "RUNNING", "VERIFYING", "READY"):
                    downstream.status = "PENDING"
                    downstream.notes = f"Upstream dependency {contract_id} invalidated."
                    invalidated_ids.append(downstream_id)

        self._persist()
        return invalidated_ids

    def _check_ready_dependents(self) -> None:
        """Promote PENDING contracts to READY when all prerequisites are ACCEPTED."""
        for contract in self.contracts.values():
            if contract.status == "PENDING":
                all_accepted = all(
                    self.contracts.get(dep, None) and self.contracts[dep].status == "ACCEPTED"
                    for dep in contract.depends_on
                )
                if all_accepted:
                    contract.status = "READY"

    def get_summary(self) -> Dict[str, Any]:
        """Return high-level contract and evidence telemetry."""
        return {
            "total_contracts": len(self.contracts),
            "accepted": sum(1 for c in self.contracts.values() if c.status == "ACCEPTED"),
            "blocked": sum(1 for c in self.contracts.values() if c.status == "BLOCKED"),
            "pending": sum(1 for c in self.contracts.values() if c.status == "PENDING"),
            "evidence_facts": len(self.evidence),
        }

    def _persist(self) -> None:
        if not self.state_file:
            return
        try:
            p = pathlib.Path(self.state_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "contracts": {k: v.to_dict() for k, v in self.contracts.items()},
                "evidence": {k: v.to_dict() for k, v in self.evidence.items()},
            }
            p.write_text(json.dumps(data, indent=2))
        except Exception:
            pass
