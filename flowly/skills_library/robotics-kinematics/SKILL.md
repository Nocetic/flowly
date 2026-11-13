---
name: robotics-kinematics
description: "Solve robot-arm kinematics — forward kinematics (joint angles → end-effector pose) via Denavit-Hartenberg parameters, inverse kinematics (target pose → joint angles, with closed-form for 2-link planar arms), the Jacobian (joint↔tip velocity, singularities), workspace/reach, and joint-limit checks. Includes a stdlib calculator. Use when the user asks where a robot arm's tip ends up, what joint angles reach a point, FK/IK, DH parameters, Jacobian, reachability, or singularities."
metadata: {"flowly":{"emoji":"🦾","tags":["engineering","robotics","kinematics","forward-kinematics","inverse-kinematics","dh-parameters","jacobian"],"requires":{"bins":["python3"]},"category":"engineering","related_skills":["mechanical-engineering","control-systems","engineering-units","gcode-cnc"]}}
---

# Robotics Kinematics — Where Does the Arm Go, and How Do I Get It There?

Kinematics is the geometry of motion without forces. Two directions: **forward** (given joint angles, where's the tip? — always one answer) and **inverse** (given a target, what joint angles? — often zero, multiple, or infinite answers). The discipline is being explicit about conventions (angle units, frame definitions, DH parameters) because a sign or frame error silently sends the arm to the wrong place.

## What this skill produces

**Chat-first.** Default: the computed pose or joint angles with the reasoning, plus reachability/limit/singularity checks. The `kinematics.py` helper does 2-link planar FK/IK and general DH-chain forward kinematics. Offer a fuller writeup for 6-DOF arms (where numerical IK or a library like `ikpy`/`roboticstoolbox` is the right tool).

## When to use

- "Where does the end-effector end up for these joint angles?" (FK)
- "What joint angles reach point (x, y)?" (IK)
- "Compute forward kinematics from these DH parameters."
- "What's the Jacobian / is this a singularity?"
- "Can the arm reach this point?" (workspace/reach)
- "Are these joint angles within limits?"

## Forward kinematics (FK) — always solvable

Chain transforms from base to tip. Each joint contributes a 4×4 homogeneous transform; multiply them in order: **T = T₁·T₂·…·Tₙ**. The result's translation column is the tip position; the rotation block is its orientation.

**Denavit-Hartenberg (DH)** is the standard parameterization — 4 numbers per joint (θ, d, a, α):
- θ — joint angle (the variable for a revolute joint), about z.
- d — link offset along z (the variable for a prismatic joint).
- a — link length along x.
- α — link twist about x.

Each row builds one transform; the product is FK. (Two conventions exist — *standard* vs *modified* DH; state which you're using, they differ in transform order.)

**2-link planar arm** (the teaching case), link lengths L₁, L₂, angles θ₁, θ₂:
- x = L₁·cos θ₁ + L₂·cos(θ₁+θ₂)
- y = L₁·sin θ₁ + L₂·sin(θ₁+θ₂)

## Inverse kinematics (IK) — the hard direction

Given a target pose, find joint angles. Unlike FK, IK can have:
- **No solution** (target outside the workspace — unreachable).
- **Multiple solutions** (e.g. "elbow-up" vs "elbow-down" for a 2-link arm).
- **Infinite solutions** (redundant arms, >6 DOF for a 6-DOF task).

**2-link planar closed form** (target x, y):
- r² = x² + y²; reachable iff |L₁−L₂| ≤ r ≤ L₁+L₂.
- cos θ₂ = (r² − L₁² − L₂²)/(2 L₁ L₂); θ₂ = ±acos(...) (the ± is elbow-up/down).
- θ₁ = atan2(y, x) − atan2(L₂ sin θ₂, L₁ + L₂ cos θ₂).

For 6-DOF arms: closed-form IK exists for special geometries (spherical wrist), otherwise use **numerical IK** (Jacobian-based iteration / a library). Always check the returned angles against **joint limits** and pick the solution that respects them and avoids obstacles.

## The Jacobian — velocities and singularities

The Jacobian **J** maps joint velocities to end-effector velocity: **ẋ = J·q̇**. It's the local linearization of FK.
- Used for velocity control, force mapping (τ = Jᵀ·F), and **detecting singularities**.
- **Singularity:** where J loses rank (det J → 0 for a square J). There the arm loses a DOF, IK blows up (huge joint speeds for small tip motion), and control degrades. Stay away from them. For a 2-link arm the singularity is when it's fully stretched or folded (θ₂ = 0 or π).

## Workspace & limits

- **Reach:** max reach = Σ link lengths; for a 2-link arm the workspace is an annulus between |L₁−L₂| and L₁+L₂.
- **Joint limits:** every solution must lie within each joint's min/max; an FK that's geometrically fine but exceeds a limit is invalid.

## The calculator

`scripts/kinematics.py` (stdlib; angles in **degrees** by default, `--rad` for radians):
```bash
python3 scripts/kinematics.py fk2 --l1 1 --l2 0.5 --t1 30 --t2 45    # 2-link tip pose
python3 scripts/kinematics.py ik2 --l1 1 --l2 0.5 --x 1.2 --y 0.5    # both elbow solutions
python3 scripts/kinematics.py jacobian2 --l1 1 --l2 0.5 --t1 30 --t2 45  # J + singularity check
python3 scripts/kinematics.py dh --rows "theta,d,a,alpha" ...        # general FK from DH table
```
DH table is passed as `--row "theta d a alpha"` (one per joint, degrees for angles); the tool multiplies the chain and prints the tip position + orientation.

## Chat output format

```
**2-link IK — target (1.2, 0.5), L1=1.0, L2=0.5**

r = 1.30 m · reachable range [0.50, 1.50] → reachable ✅
Two solutions (each FK-verified back to the target):
  elbow-down: θ1 = 2.4°,  θ2 = 63.9°
  elbow-up:   θ1 = 42.8°, θ2 = −63.9°
Pick by joint limits / obstacle avoidance. Near full stretch? r/reach = 87% — fine.
```

## Workflow

1. **Pin conventions:** DOF, link lengths, DH parameters (and which DH convention), angle units, joint limits.
2. **FK** for "where does it go" (`fk2`/`dh`) — multiply the chain.
3. **IK** for "how to reach" (`ik2` closed form for 2-link; numerical/library for 6-DOF) — **check reachability first**, return all solution branches.
4. **Validate:** joint limits, singularity proximity (`jacobian2`), obstacle/self-collision (note if out of scope).
5. **Deliver** angles/pose + checks; suggest `ikpy`/`roboticstoolbox` for full 6-DOF numerical IK; route actuator torque/sizing to `mechanical-engineering`/`power-sizing`, motion control to `control-systems`.

## Key pitfalls

- **Degrees vs radians.** The #1 silent error — trig functions take radians; state and convert. (The helper defaults to degrees and converts internally.)
- **Forgetting multiple IK solutions.** Returning only "elbow-down" can violate limits or hit an obstacle the other branch avoids — give all branches.
- **Unreachable targets.** Check |L₁−L₂| ≤ r ≤ ΣL *before* solving; acos of out-of-range = error.
- **DH convention mix-up.** Standard vs modified DH differ — using the wrong transform order misplaces the tip. State which.
- **Ignoring singularities.** Near-singular configurations demand huge joint speeds and wreck control — check det(J).
- **Skipping joint limits.** A mathematically valid solution that exceeds a joint's range is not a real solution.
- **Hand-rolling 6-DOF numerical IK.** For general arms use a tested library; closed-form only for special geometries.

## Quick reference

- FK: T = ΠTᵢ (multiply DH transforms base→tip); 2-link: x=L₁cosθ₁+L₂cos(θ₁+θ₂), y=L₁sinθ₁+L₂sin(θ₁+θ₂).
- DH per joint: (θ, d, a, α). State standard vs modified.
- 2-link IK: reachable iff |L₁−L₂|≤r≤L₁+L₂; cosθ₂=(r²−L₁²−L₂²)/(2L₁L₂); θ₂=±acos → elbow up/down.
- Jacobian: ẋ = J·q̇; τ = JᵀF; singularity when det J → 0 (2-link: θ₂ = 0 or π).
- Always: radians in trig, check reach, return all IK branches, respect joint limits.
- 6-DOF numerical IK → ikpy / roboticstoolbox; torque/sizing → mechanical-engineering/power-sizing.
