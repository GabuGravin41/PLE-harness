#!/usr/bin/env python3
"""End-to-End Task Simulation Test for PLE-Harness.
Simulates realistic civil/structural/electrical task workflows without hardcoding.
Tests tool execution, constraint checking, evidence logging, and deliverable promotion.
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
import task_mcp


class TestTaskSimulation(unittest.TestCase):
    
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.test_dir.name)
        self.ckpt_dir = self.workspace / ".checkpoints"
        self.state_file = self.workspace / "stage_state.json"
        
        self.engine = stage_contract.StageContractEngine(state_file=str(self.state_file))
        self.ckpt_mgr = checkpoints.CheckpointManager(checkpoint_dir=str(self.ckpt_dir))
        self.verifier = constraint_verifier.ConstraintVerifier()

    def tearDown(self):
        self.test_dir.cleanup()

    def test_structural_girder_workflow_simulation(self):
        """Simulate an end-to-end structural girder calculation workflow."""
        instruction = (
            "Design an interior composite floor girder spanning 36 ft with 10 ft tributary width. "
            "Dead load = 65 psf, Live load = 100 psf. Design steel section under AISC 360-16 LRFD. "
            "Deliver girder_calc.xlsx and structural_summary.txt."
        )

        # Step 1: Pre-task classification
        classification = classifier.classify_task(instruction)
        self.assertEqual(classification["discipline"], "structural")
        self.assertEqual(classification["primary_code"], "AISC")

        # Step 2: Register Stage Contracts (Stage 1: Load Calcs -> Stage 2: Section Sizing -> Stage 3: Deliverable)
        self.engine.register_contract(
            contract_id="stage_loads",
            stage_name="Load & Moment Determination",
            inputs=["design_brief.txt"],
            outputs=["loads.json"],
            mandatory_checks=["moment_positive", "factored_load_valid"],
        )
        self.engine.register_contract(
            contract_id="stage_sizing",
            stage_name="AISC Section Selection",
            inputs=["loads.json"],
            outputs=["girder_calc.xlsx"],
            depends_on=["stage_loads"],
            invalidates=["stage_deliverable"],
            mandatory_checks=["dcr_le_1", "deflection_pass"],
        )
        self.engine.register_contract(
            contract_id="stage_deliverable",
            stage_name="Deliverable Finalization",
            inputs=["girder_calc.xlsx"],
            outputs=["structural_summary.txt"],
            depends_on=["stage_sizing"],
            mandatory_checks=["deliverables_exist"],
        )

        # Step 3: Execute Stage 1 (Loads)
        # Span = 36 ft, Trib = 10 ft
        # w_D = 0.065 * 10 = 0.65 kip/ft, w_L = 0.100 * 10 = 1.0 kip/ft
        # w_u = 1.2(0.65) + 1.6(1.0) = 0.78 + 1.60 = 2.38 kip/ft
        # M_u = w_u * L^2 / 8 = 2.38 * (36)^2 / 8 = 385.56 kip-ft
        fact_span = self.engine.record_evidence(
            fact_name="span_ft", value=36.0, unit="ft", source_file="instruction", confidence="CONFIRMED"
        )
        fact_mu = self.engine.record_evidence(
            fact_name="factored_moment_kip_ft", value=385.56, unit="kip-ft", source_file="load_engine", confidence="CONFIRMED"
        )
        self.assertTrue(fact_span.startswith("fact_001"))
        self.assertTrue(fact_mu.startswith("fact_002"))

        # Verify Stage 1
        res1 = self.engine.evaluate_contract("stage_loads", {"moment_positive": True, "factored_load_valid": True})
        self.assertTrue(res1["gate_passed"])
        self.assertEqual(self.engine.contracts["stage_loads"].status, "ACCEPTED")

        # Step 4: Execute Stage 2 (Trial Sizing: W21x44 -> phi*Mn = 358 kip-ft < 385.56 -> FAIL)
        trial_output = {"output": "Selected W21x44: phi_Mn = 358 kip-ft, Mu = 385.56 kip-ft, dcr = 1.08"}
        checked_trial = self.verifier.check("run_python", trial_output)
        self.assertTrue(checked_trial.get("__retry", False), "Verifier must detect DCR > 1.0 violation!")

        # Stage 2 evaluation fails initial trial
        res2_fail = self.engine.evaluate_contract("stage_sizing", {"dcr_le_1": False, "deflection_pass": True})
        self.assertFalse(res2_fail["gate_passed"])
        self.assertEqual(self.engine.contracts["stage_sizing"].status, "BLOCKED")

        # Redesign: Selected W24x55 -> phi*Mn = 503 kip-ft, dcr = 0.77 -> PASS
        redesign_output = {"output": "Selected W24x55: phi_Mn = 503 kip-ft, Mu = 385.56 kip-ft, dcr = 0.77, deflection = L/420"}
        checked_redesign = self.verifier.check("run_python", redesign_output)
        self.assertFalse(checked_redesign.get("__retry", False), "Verifier must pass compliant redesign!")

        # Write deliverable 1
        calc_path = self.workspace / "girder_calc.xlsx"
        calc_path.write_text("AISC 360-16 W24x55 Design Calculation Sheet")

        # Save checkpoint
        ckpt = self.ckpt_mgr.save_checkpoint("stage_sizing", str(calc_path), gate_passed=True)
        self.assertIsNotNone(ckpt)

        # Stage 2 evaluation passes
        res2_pass = self.engine.evaluate_contract("stage_sizing", {"dcr_le_1": True, "deflection_pass": True})
        self.assertTrue(res2_pass["gate_passed"])
        self.assertEqual(self.engine.contracts["stage_sizing"].status, "ACCEPTED")

        # Step 5: Execute Stage 3 (Final summary)
        summary_path = self.workspace / "structural_summary.txt"
        summary_path.write_text("Design Summary: W24x55 meets AISC 360-16 requirements with DCR=0.77.")

        res3 = self.engine.evaluate_contract("stage_deliverable", {"deliverables_exist": True})
        self.assertTrue(res3["gate_passed"])
        self.assertEqual(self.engine.contracts["stage_deliverable"].status, "ACCEPTED")

        # Step 6: Verify overall telemetry & checkpoint promotion
        summary = self.engine.get_summary()
        self.assertEqual(summary["accepted"], 3)
        self.assertEqual(summary["blocked"], 0)
        self.assertEqual(summary["pending"], 0)
        self.assertEqual(summary["evidence_facts"], 2)

        # Promote checkpoint and verify integrity
        best = self.ckpt_mgr.get_best_checkpoint("girder_calc.xlsx")
        self.assertIsNotNone(best)
        self.assertTrue(best["gate_passed"])

    def test_electrical_voltage_drop_simulation(self):
        """Simulate electrical feeder sizing under NEC 2023 with NOAA ambient derating."""
        instruction = (
            "Size a 480V 3-phase 150A continuous feeder routed across a commercial rooftop. "
            "Ambient design temperature is 46C. Check ampacity derating and ensure voltage drop <= 3.0%."
        )

        # Step 1: Pre-task classification
        classification = classifier.classify_task(instruction)
        self.assertEqual(classification["discipline"], "electrical")
        self.assertEqual(classification["primary_code"], "NEC")

        # Step 2: Test uncorrected ampacity failure detection
        # At 46C, Table 310.15(B)(1) requires 0.82 derating factor for 75C conductors.
        # Continuous load requires 125%: 150A * 1.25 = 187.5A.
        # Required unadjusted ampacity = 187.5 / 0.82 = 228.7A -> 4/0 AWG Cu required.
        # If agent tries 2/0 AWG (175A table rating): derated = 175 * 0.82 = 143.5A < 187.5A -> VIOLATION!
        bad_ampacity_output = {"output": "Conductor selection: 2/0 AWG THHN derated ampacity = 143.5A < 187.5A required"}
        # Check voltage drop exceeding 3%
        bad_vd_output = {"output": "Feeder run calculation: voltage_drop_pct = 3.6% on 480V circuit"}
        checked_vd = self.verifier.check("run_python", bad_vd_output)
        self.assertTrue(checked_vd.get("__retry", False))

        # Correct design: 4/0 AWG with voltage drop 1.8% -> PASS
        good_vd_output = {"output": "Feeder run calculation: 4/0 AWG THHN, voltage_drop_pct = 1.8%, dcr = 0.82"}
        checked_good = self.verifier.check("run_python", good_vd_output)
        self.assertFalse(checked_good.get("__retry", False))


if __name__ == "__main__":
    unittest.main()
