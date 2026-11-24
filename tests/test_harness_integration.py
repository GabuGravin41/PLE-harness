#!/usr/bin/env python3
"""Integration and unit smoke test for PLE-harness components.
Empirical verification of classifier, stage contracts, constraint verifiers, and checkpointing.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Add ple-harness root to path
HARNESS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HARNESS_DIR))

import classifier
import constraint_verifier
import stage_contract
import checkpoints


class TestPLEHarness(unittest.TestCase):
    
    def test_01_classifier_heuristics(self):
        """Verify keyword/LLM fallback domain and code-path classification."""
        # Electrical task
        e_task = "Calculate feeder sizing, conduit fill, and voltage drop for a 480V 3-phase chiller under NEC 2023."
        res_e = classifier.classify_task(e_task)
        self.assertEqual(res_e["discipline"], "electrical")
        self.assertEqual(res_e["primary_code"], "NEC")

        # Structural task
        s_task = "Analyze W24x68 composite girder under AISC 360 LRFD with live load deflection limit L/360."
        res_s = classifier.classify_task(s_task)
        self.assertEqual(res_s["discipline"], "structural")

        # Civil / BIM MEP task
        m_task = "Coordinate MEP cable trays and piping in IFC4 model with 50mm clearance and zero clashes."
        res_m = classifier.classify_task(m_task)
        self.assertEqual(res_m["deliverable_type"], "ifc4_model")

    def test_02_constraint_verifier_physics(self):
        """Verify physics & engineering rule checking against tool outputs."""
        verifier = constraint_verifier.ConstraintVerifier()

        # 1. Structural girder check: DCR within bounds
        good_output = {"output": "AISC 360 LRFD calculation complete: dcr = 0.85, moment_knm = 450.0"}
        res_pass = verifier.check("run_python", good_output)
        self.assertFalse(res_pass.get("__retry", False))

        # 2. Structural girder check: DCR exceeds 1.0 (unfeasible failure)
        bad_output = {"output": "AISC 360 calculation: dcr = 1.45 (FAILURE)"}
        res_fail = verifier.check("run_python", bad_output)
        self.assertTrue(res_fail.get("__retry", False))
        self.assertTrue(any("dcr" in v for v in res_fail.get("__constraint_violations", [])))

        # 3. Electrical voltage drop: drop exceeds 3.0% limit (Article 215.2)
        bad_vd = {"output": "Feeder analysis: voltage_drop_pct = 4.8% on 480V circuit"}
        res_vd_fail = verifier.check("run_python", bad_vd)
        self.assertTrue(res_vd_fail.get("__retry", False))
        self.assertTrue(any("voltage_drop_pct" in v for v in res_vd_fail.get("__constraint_violations", [])))

        # 4. Mixed unit error check: kip-ft and kN-m mixed in the same output
        bad_units = {"output": "Applied moment is 350 kip*ft which corresponds to 120 kN*m in the section"}
        res_units_fail = verifier.check("run_python", bad_units)
        self.assertTrue(res_units_fail.get("__retry", False))

    def test_03_stage_contract_engine(self):
        """Verify Stage Contract Engine, dependency invalidation, and G=1 mandatory gating."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            state_file = tf.name

        try:
            engine = stage_contract.StageContractEngine(state_file=state_file)

            # Record Evidence into ledger
            f1 = engine.record_evidence(
                fact_name="beam_span",
                value=30.0,
                unit="ft",
                source_file="structural_drawings.pdf",
                source_locator="Sheet S-101",
                confidence="CONFIRMED"
            )
            self.assertTrue(f1.startswith("fact_001"))

            # Register 3 staged contracts: 1 (Ingestion) -> 2 (Calc) -> 3 (Final Deliverable)
            engine.register_contract(
                contract_id="contract_1",
                stage_name="Document Ingestion",
                inputs=["structural_drawings.pdf"],
                outputs=["evidence_ledger.json"],
                mandatory_checks=["sheet_s101_verified"],
            )
            engine.register_contract(
                contract_id="contract_2",
                stage_name="Girder Calculation",
                inputs=["evidence_ledger.json"],
                outputs=["girder_calc.xlsx"],
                depends_on=["contract_1"],
                invalidates=["contract_3"],
                mandatory_checks=["dcr_le_1", "deflection_le_L360"],
            )
            engine.register_contract(
                contract_id="contract_3",
                stage_name="Report Generation",
                inputs=["girder_calc.xlsx"],
                outputs=["structural_report.docx"],
                depends_on=["contract_2"],
                mandatory_checks=["docx_exists"],
            )

            # Evaluate contract 1 -> PASS mandatory
            eval1 = engine.evaluate_contract("contract_1", {"sheet_s101_verified": True})
            self.assertTrue(eval1["gate_passed"])
            self.assertEqual(engine.contracts["contract_1"].status, "ACCEPTED")

            # Evaluate contract 2 -> FAILS mandatory (DCR > 1.0)
            eval2_fail = engine.evaluate_contract("contract_2", {"dcr_le_1": False, "deflection_le_L360": True})
            self.assertFalse(eval2_fail["gate_passed"])
            self.assertEqual(engine.contracts["contract_2"].status, "BLOCKED")

            # Re-evaluate contract 2 after redesign -> PASSES
            eval2_pass = engine.evaluate_contract("contract_2", {"dcr_le_1": True, "deflection_le_L360": True})
            self.assertTrue(eval2_pass["gate_passed"])
            self.assertEqual(engine.contracts["contract_2"].status, "ACCEPTED")

            # Invalidate contract 2 and verify contract 3 is invalidated via invalidates cascade
            engine.invalidate_contract("contract_2", reason="New load combination added")
            self.assertEqual(engine.contracts["contract_2"].status, "BLOCKED")
            self.assertEqual(engine.contracts["contract_3"].status, "PENDING")

        finally:
            if os.path.exists(state_file):
                os.remove(state_file)

    def test_04_checkpoints_manager(self):
        """Verify progressive checkpointing and emergency promotion."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ckpt_dir = Path(tmpdir) / ".checkpoints"
            deliverable_dir = Path(tmpdir) / "deliverables"
            deliverable_dir.mkdir()

            mgr = checkpoints.CheckpointManager(checkpoint_dir=str(ckpt_dir))

            # Create dummy initial deliverable file
            target_file = deliverable_dir / "girder_calc.xlsx"
            target_file.write_text("V1 INITIAL CALCULATION")

            # Checkpoint V1 with gate_passed=True
            ckpt1 = mgr.save_checkpoint("contract_1", str(target_file), gate_passed=True)
            self.assertIsNotNone(ckpt1)

            # Modify file to corrupt/incomplete state V2 with gate_passed=False
            target_file.write_text("V2 CORRUPTED BROKEN CALCULATION")
            ckpt2 = mgr.save_checkpoint("contract_2", str(target_file), gate_passed=False)
            self.assertIsNotNone(ckpt2)

            # Best validated checkpoint must be V1 (gate_passed=True)
            best = mgr.get_best_checkpoint("girder_calc.xlsx")
            self.assertIsNotNone(best)
            self.assertEqual(best["version"], 1)

            # Simulate emergency timeout: restore best validated checkpoint
            ok = mgr.promote_best_checkpoint(str(target_file))
            self.assertTrue(ok)
            self.assertEqual(target_file.read_text(), "V1 INITIAL CALCULATION")


if __name__ == "__main__":
    unittest.main()
