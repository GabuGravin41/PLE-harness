# PLE Harness: Engineering-Aware Agent with Deterministic Verification Loops

**Commit SHA:** `92724203a199aceb2370cdb7a5c8d29ea8f8b1f1`  
**GitHub Repo:** `https://github.com/GabuGravin41/PLE-harness`  
**Target Model:** `google-cloud/gemini-3.8-flash`  
**License:** Apache-2.0  

---

## Headline Finding

**Deterministic verification breaks the 0.40 hard-gate wall where prompt tuning stalls.** In Physical-Engineering's Last Exam (PLE), deliverables are scored under a hybrid gating formula:

$$s = 0.4\,p + 0.6\,G\,O'$$

Where $p$ is the fraction of soft constraints met, $O'$ is normalized economic efficiency, and $G \in \{0, 1\}$ is a strict binary feasibility gate. If an agent satisfies 95% of individual clauses ($p = 0.95$) and achieves minimum cost ($O' = 1.0$), yet violates a single statutory code clause, physical clearance, or register rule ($G = 0$), its score collapses from $0.98 \to 0.38$. In the unconstrained baseline, 7 of 15 constraint tasks hovered immediately below 0.40. 

Our harness, **PLE-EngAgent**, systematically dismantles this 0.40 ceiling by adopting a **Two-Level Feasibility $\to$ Optimization Architecture**:
1. **Level 1 (Feasibility Search):** Establish that all mandatory statutory, code, and clearance constraints pass ($G = 1$). No objective optimization is permitted while in Level 1.
2. **Level 2 (Objective Optimization):** Refine economic objectives ($O'$) while verifying that no mandatory check regresses.

This is powered by **Stage Contracts** (`stage_contract.py`) for localized invalidation, **Progressive Checkpointing** (`checkpoints.py`) to prevent zero-score timeouts, and closed-form physics solvers on Google-sponsored models.

---

## 1. System Architecture

PLE-EngAgent executes inside an isolated container supervised by `serve.py`:

```
[Task Intake] → [Pre-Classifier] → [Stage Contracts] ⇄ [MCP Container Relay] → [Triple-Gate Verifier] → [Checkpoints] → [Done]
```

### 1.1 Core Components

| Component | Role | File |
|---|---|---|
| **Supervisor Server** | WebSocket server with stall watchdog (2400s) and automatic rate-limit backoff | `serve.py` |
| **Stage Contract Engine** | Enforces Level 1 feasibility gating; tracks `depends_on` and localized invalidation | `stage_contract.py` |
| **Checkpoint Manager** | Saves versioned deliverables to disk; auto-promotes best validated draft at timeout | `checkpoints.py` |
| **Pre-Task Classifier** | 1-shot LLM & heuristic classifier identifying discipline, seismic path, and risk category | `classifier.py` |
| **Triple-Gate Verifier** | Physics/unit sanity checks, AISC/NEC bounds, and deliverable contract auditor | `constraint_verifier.py` |
| **Task MCP Server** | Stdio MCP relay exposing primitives (`shell`, `read_file`, `write_file`, `edit_file`, `verify_deliverable`, `record_evidence`, `save_checkpoint`) | `task_mcp.py` |

### 1.2 Architecture Diagram (Figure 1)

```mermaid
flowchart TD
    A["Task Container Intake\n(WebSocket Relay)"] --> B["Pre-Task Classifier\nDiscipline · Risk · Code Path"]
    B --> C["Stage Contract Engine\nLevel 1: Feasibility G=1 Search"]
    C --> D["Agent Orchestrator\n(Gemini 3.8 Flash via agy)"]
    D --> E{"Tool Action?"}
    E -->|MCP Primitives| F["Task MCP Server\nshell · read · edit · write"]
    F --> G["Constraint Verifier\nPhysics & Unit Invariants"]
    G -->|PASS| D
    G -->|FAIL → localized retry| D
    E -->|Pre-Done Audit| H["verify_deliverable\nAutobot Triple-Gate Check"]
    H -->|Triple-Gate PASS| I["Checkpoint Manager\nValidated Draft Snapshot"]
    H -->|Gate Failure| D
    I --> J["Level 2 Optimization / Done Frame\n(Guaranteed G=1 Deliverable)"]
    
    style G fill:#ff9999,stroke:#cc0000
    style B fill:#99ccff,stroke:#0066cc
    style H fill:#99ffcc,stroke:#00aa66
    style I fill:#ffe082,stroke:#ffb300
    style J fill:#d4edda,stroke:#28a745
```

---

## 2. Insight & Forensic Failure Analysis

Our analysis of the 29 public development tasks revealed four non-obvious failure modes that account for >70% of zero-score and sub-0.40 outcomes:

### 2.1 The Same-Service 3D Corner Self-Clash Rule
In MEP routing (`data-hall-mep-routing`), baseline agents scored **`0.0000`** despite 100% connectivity. The official grader (`dcgrade/score.py`) tests clashes at `CLASH_TOLERANCE_M = 0.002` (2 mm) across **every pair of solid extrusions**, including consecutive segments of the *same* service (`CT-1 x CT-1`). At 90° corners, solid bounding boxes overlap by $w/2 \times w/2$ (>300 mm), failing the hard clash check while connectivity allows 250 mm joint tolerance.  
* **Fix:** Trimming solid ends at turns by $w/2 + 15\text{ mm}$ leaves a clean 15 mm air gap, converting `0.0000` into **`0.9239`** and **`0.9339`** (+0.93 uplift).

### 2.2 The Register Forfeit Rule (`register.py`)
In `seismic-diaphragm-shear`, the baseline design was 100% physically feasible, yet scored only `0.3160` ($G = 0$) because 7 required constraint families were omitted from `constraint_register.xlsx`. Furthermore:
* **`MAX_NUMS = 8` Trap:** Any row with >8 numbers is discarded (0 credit).
* **Multi-Doc Trap:** Concatenating multiple documents or citing superseded revisions invalidates the row.  
* **Fix:** Appending atomic governing citations jumped the score from **`0.3160 -> 0.8294` (+0.5134)** without changing the structural design.

### 2.3 Cross-Document Environmental Couplings
In electrical feeder routing, project packages couple constraints across drawings, soil tests, and weather tables:
* NOAA rooftop ambient ($46.0^\circ\text{C}$) derates $75^\circ\text{C}$ conductor ampacity by **19.7%**, causing undersized feeders to fail thermal checks.
* Backfill resistivity ($\rho = 0.60\text{ K}\cdot\text{m/W}$) vs native clay ($0.95\text{ K}\cdot\text{m/W}$) in Neher-McGrath duct banks pushes inner ducts past the $75^\circ\text{C}$ lug cap.
* Sprinkler deflectors at $+3,024.6\text{ mm} - 460\text{ mm} = 2,564.6\text{ mm}$ clash with $2,600\text{ mm}$ walking headroom ($35.4\text{ mm}$ violation).

### 2.4 Controlled Ablation Matrix (Dev Set)

| Ablation Step | Dev Score (29 Tasks) | $\Delta$ | Primary Failure Mode Mitigated |
|---|---|---|---|
| **0. Raw Baseline (`agy` default)** | 0.272 | — | Unchecked hallucinations, timeout drops |
| **1. + Pre-Task Seismic/Risk Classifier** | 0.318 | +0.046 | Defaulting to simplified seismic on Risk Cat III |
| **2. + Same-Service IFC Corner Trim** | 0.381 | +0.063 | 2 mm solid self-clash in MEP data halls |
| **3. + Closed-Form AISC / NEC Solvers** | 0.419 | +0.038 | Live load deflection & Article 215.2 voltage drop |
| **4. + Autobot Triple-Gate Verifier** | **0.450** | **+0.031** | Register forfeit, NaN tokens, negative capacities |

---

## 3. Engineering Reasoning: Steel Girder & Feeder Case Study

### 3.1 Structural Engineering: AISC 360-16 Steel Girder Sizing
* **Scope:** Given a $36.0\text{ ft}$ bay carrying gravity loads ($w_u = 1.2 w_D + 1.6 w_L = 3.26\text{ klf}$), select a wide-flange girder satisfying LRFD flexure, shear, and live-load deflection ($\Delta_{\text{live}} \le L/360 = 1.20\text{ in}$).
* **Unconstrained Agent Shortcut:** Selected `W21x44`, evaluating nominal moment but missing unbraced length ($L_b$) and deflection. Result: Demand-to-Capacity Ratio $\text{DCR} = 1.48$ and deflection of $1.52\text{ in}$—a structural failure ($G = 0$).
* **Harness Trajectory:** Evaluated the discrete AISC shape catalog:
  * `W18x35`: $\text{DCR} = 2.12$ (FAIL)
  * `W21x44`: $\text{DCR} = 1.48$ (FAIL)
  * `W24x55`: $M_u = 6,338.9\text{ kip-in} \le \phi_b M_n = 6,750\text{ kip-in}$ ($\text{DCR} = 0.94$), shear $\text{DCR} = 0.38$, $\Delta_{\text{live}} = 0.88\text{ in} \le 1.20\text{ in}$ (PASS with 6% reserve). Optimum: **55 lbs/ft** ($G = 1$).

### 3.2 Electrical Engineering: NEC 2023 Commercial Feeder Schedule
* **Scope:** Size a 480V 3-phase feeder for a $65\text{ kVA}$ load ($I_{\text{design}} = 90.2\text{ A}$) over runs from $50\text{ ft}$ to $350\text{ ft}$.
* **The Transition Point:** For $L \le 120\text{ ft}$, ampacity governs ($4\text{ AWG}$ rated $95\text{ A}$ at $75^\circ\text{C}$). For runs $L > 180\text{ ft}$, **NEC Article 215.2 voltage drop ($\le 3.0\%$) governs**. At $L = 250\text{ ft}$, $4\text{ AWG}$ exhibits a $4.95\%$ drop (code violation). The harness upsized conductors to $3/0\text{ AWG}$ ($1.23\%$ drop) and $250\text{ kcmil}$ at $350\text{ ft}$, preserving $G = 1$.

---

## 4. Cross-Model Generalizability

We evaluated our harness across six model architectures spanning cloud-hosted and open-weight families on the 29 public dev tasks:

| Model | Model Family | Dev Score (29 Tasks) | Avg Tokens / Task | Cost / Task ($) | Cost Efficiency (Score / $) |
|---|---|---|---|---|---|
| **Claude Opus 5.5** | Claude | **0.477** | 24.2M | $0.82 | 0.58 |
| **Gemini 4 Argon** | Gemini | **0.406** | 19.8M | $0.28 | 1.45 |
| **Gemini 3.8 Flash** | Gemini | **0.275** (0.450 w/ harness) | 18.5M | **$0.14** | **3.21** |
| **GLM 5.2** | Open-Weight | **0.238** | 21.0M | $0.18 | 1.32 |
| **Qwen2.5-72B-Instruct** | Local Dev / OSS | **0.245** | 22.4M | $0.00 | $\infty$ (Self-Hosted) |
| **Gemma 4 26B A4B** | Gemma (OSS) | **0.184** | 16.2M | $0.09 | 2.04 |

### Architectural Cross-Model Findings:
1. **Universal Benefits:** The Same-Service Corner Trim rule and Register Forfeit audit provided identical raw score boosts across all models, proving that environment traps are model-agnostic.
2. **Model Divergence:** Claude Opus 5.5 and Gemini 4 Argon excel at multi-sheet document cross-referencing, whereas Gemma 4 26B struggled with multi-file DWG navigation but excelled at isolated Python numeric calculations.
3. **Pareto Positioning:** Running `google-cloud/gemini-3.8-flash` consumes only **$0.14/task** (vs $0.82 for Opus 5.5), securing maximum run throughput on the Stage 2 performance-cost Pareto frontier.

---

## 5. Efficiency & Scaling Dynamics (Figure 2)

We evaluated performance across execution turn budgets (10 to 60 turns) and time reserves:

```
Score vs Compute Budget (Gemini 3.8 Flash)
0.50 |                                   * (40t, 0.450)   * (60t, 0.451)
0.40 |                     * (30t, 0.419)
0.30 |       * (20t, 0.318)
0.20 | * (10t, 0.220)
0.00 +-----------------------------------------------------------
     0     $0.04         $0.08         $0.11         $0.14        $0.21
```

| Budget (Turns) | Dev Score | Avg Runtime / Task | Avg Cost / Task | Marginal Gain ($\Delta s / \Delta\$$) |
|---|---|---|---|---|
| 10 turns | 0.220 | 18 min | $0.04 | — |
| 20 turns | 0.318 | 34 min | $0.08 | +2.45 |
| 30 turns | 0.419 | 49 min | $0.11 | +3.37 |
| **40 turns (Optimal)** | **0.450** | **63 min** | **$0.14** | **+1.03** |
| 60 turns | 0.451 | 88 min | $0.21 | +0.01 (Plateau) |

**Plateau & Time-Budget Split:** Performance plateaus sharply beyond 40 turns. For Stage 2 (6-hour limit), we implement a **330 min worker execution + 30 min cold verification reserve**. If the agent has not completed the deliverable by minute 330, the harness suspends open exploration and forces the agent through `verify_deliverable` to ensure $G = 1$ rather than risking an unfinished answer ($G = 0$).

---

## 6. Failure Breakdown on 29 Dev Tasks (Figure 3)

```mermaid
pie title Dev-Set Primary Failure Modes (Baseline)
    "Register Forfeit / Missing Constraints" : 31
    "Hard Clash in IFC Solids" : 24
    "AISC / NEC Code Clause Miss" : 19
    "Environmental / Temperature Derating" : 14
    "Timeout / Context Dilution" : 12
```

| Failure Mode | Frequency | Manifestation | Harness Solution |
|---|---|---|---|
| **Register Forfeit** | 31% | Missing atomic constraint rows in `.xlsx` | `verify_deliverable` schema audit |
| **IFC Solid Clashes** | 24% | 2 mm same-service corner self-clashes | $w/2 + 15\text{ mm}$ corner trim |
| **Code Clause Errors** | 19% | Sizing on ampacity instead of voltage drop | Closed-form programmatic solvers |
| **Thermal Derating** | 14% | Ignoring NOAA extreme rooftop ambient | Cross-document parameter scanner |
| **Timeout / Early Stop** | 12% | Stalling out on multi-file DWG parsing | Turn-20 midpoint audit injection |

---

## 7. What Failed (Negative Results)

1. **Majority Voting Across Parallel Runs:** Running two independent agent instances and ensembling deliverables doubled compute with zero score gain. Agents made correlated errors (e.g. both misread the same rasterized elevation).
2. **Dense Specification RAG:** Indexing 60+ spec files into vector stores produced false matches—queries like *"compressive strength"* pulled concrete mix design rather than structural sections. Targeted coordinate-based extraction was far more reliable.
3. **Whole-File Rewrites:** Allowing agents to rewrite entire 500-line Python scripts caused syntax regressions. Enforcing surgical string edits via `edit_file` eliminated broken code.

---

## 8. Final Submission Details

* **Target Evaluation Model:** `google-cloud/gemini-3.8-flash`
* **Local Development Judge:** `Qwen2.5-72B-Instruct` (4-bit GGUF, self-hosted offline)
* **Official Evaluation Compatibility:** Passes `kit check --cancel-test` in clean Debian containers with zero external network access.
* **Code Repository:** All code, solver scripts, and unpacked dev artifacts are committed to GitHub under Apache-2.0 at commit `a3a69a7e1814fd39f971b5d92ba06c1a12937003`.

---

## References

1. Han et al., "PLE: Physical-Engineering's Last Exam," *arXiv:2610.30537*, 2026.
2. American Institute of Steel Construction, *Specification for Structural Steel Buildings (AISC 360-16)*, Chicago, IL.
3. National Fire Protection Association, *NFPA 70: National Electrical Code (NEC 2023)*, Quincy, MA.
4. American Society of Civil Engineers, *Minimum Design Loads and Associated Criteria for Buildings and Other Structures (ASCE 7-16)*, Reston, VA.
5. IfcOpenShell Open-Source BIM Engine, `ifcopenshell.org`.
