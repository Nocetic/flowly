---
name: kubernetes
description: "Write and debug Kubernetes manifests and workloads — Deployments, Services, Ingress, ConfigMaps/Secrets, probes, resource requests/limits, rollouts, HPA, and namespaces — plus kubectl debugging of crashing/pending/unreachable pods. Covers the common failure modes (CrashLoopBackOff, ImagePullBackOff, Pending, OOMKilled) and how to diagnose them. Use when the user wants a k8s manifest, to deploy/scale/expose an app, to debug a pod/service, or help with kubectl."
metadata: {"flowly":{"emoji":"☸️","tags":["devops","kubernetes","k8s","manifests","kubectl","deployment","orchestration"],"requires":{"bins":["python3"]},"optional_bins":["kubectl"],"category":"devops","related_skills":["docker","bash-scripting","api-security-audit","sql-query"]}}
---

# Kubernetes — Declare the Desired State, Then Debug the Gap

Kubernetes is declarative: you describe the **desired state** in YAML and the control loop works to match it. So the two jobs are **write correct manifests** and, when reality diverges, **diagnose why** (almost always via `kubectl describe` + `logs` + `events`). Resist imperative one-off `kubectl` commands for anything you want to keep — put it in YAML under version control.

## What this skill produces

**Chat-first.** Default: the manifest YAML in a fenced block with the key fields explained, or a diagnosis (what's wrong + the `kubectl` commands to confirm + the fix). Running `kubectl` is the user's to do against their cluster (the skill works without it installed).

## When to use

- "Write a Deployment / Service / Ingress / ConfigMap for \<app\>."
- "Deploy / scale / expose / roll out \<app\>."
- "Why is my pod CrashLoopBackOff / Pending / ImagePullBackOff / OOMKilled?"
- "Debug this service — it's not reachable."
- "Set up probes / resource limits / autoscaling / rolling updates."

## The core objects (and how they relate)

- **Pod** — the smallest unit (one+ containers sharing network/storage). You rarely create these directly.
- **Deployment** — manages a ReplicaSet of identical pods; handles rolling updates and rollback. The default way to run a stateless app.
- **Service** — stable virtual IP + DNS name load-balancing across a set of pods (selected by labels). Pods are ephemeral; Services are how anything finds them. Types: **ClusterIP** (internal, default), **NodePort** (port on every node), **LoadBalancer** (cloud LB), and `Headless` (direct pod DNS).
- **Ingress** — HTTP(S) routing (host/path → Service) with TLS; needs an ingress controller installed.
- **ConfigMap / Secret** — config and credentials injected as env vars or mounted files (keep config out of the image).
- **Namespace** — scoping/isolation. **StatefulSet** for stateful apps (stable identity + storage); **DaemonSet** for one-per-node; **Job/CronJob** for batch.

**Labels & selectors are the glue:** a Service finds pods by label selector; a Deployment manages pods by labels. A selector that doesn't match the pod labels = a Service with no endpoints (silent failure).

## A minimal, correct Deployment + Service

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: {name: web, labels: {app: web}}
spec:
  replicas: 3
  selector: {matchLabels: {app: web}}     # MUST match template labels
  template:
    metadata: {labels: {app: web}}
    spec:
      containers:
        - name: web
          image: myrepo/web:1.4.2          # pin a tag, not :latest
          ports: [{containerPort: 8080}]
          resources:                        # always set requests/limits
            requests: {cpu: 100m, memory: 128Mi}
            limits:   {cpu: 500m, memory: 256Mi}
          readinessProbe:                   # gates traffic
            httpGet: {path: /healthz, port: 8080}
            initialDelaySeconds: 5
          livenessProbe:                    # restarts if hung
            httpGet: {path: /healthz, port: 8080}
            initialDelaySeconds: 15
---
apiVersion: v1
kind: Service
metadata: {name: web}
spec:
  selector: {app: web}                      # MUST match pod labels
  ports: [{port: 80, targetPort: 8080}]
  type: ClusterIP
```

## The things people forget (and pay for)

- **Resource requests/limits.** No requests → bad scheduling and noisy-neighbor problems; no limits → a pod can starve the node. Limits too low → **OOMKilled** / CPU throttling. Set both, sized to reality.
- **Probes.** **readiness** gates whether the pod receives traffic (use it so rollouts don't send traffic to a not-yet-ready pod); **liveness** restarts a hung pod; **startup** for slow boots. A missing readiness probe causes failed-deploy traffic errors; a too-aggressive liveness probe causes restart loops.
- **Rolling updates & rollback:** Deployments update gradually (`maxSurge`/`maxUnavailable`); `kubectl rollout status` / `kubectl rollout undo` are your friends. Pin image tags so a rollout is reproducible.
- **Config/secrets out of the image** via ConfigMap/Secret (Secrets are base64, *not* encrypted at rest by default — enable encryption / use a real secrets manager).
- **PodDisruptionBudget / anti-affinity / HPA** for availability and scaling.

## Debugging — the universal sequence

```bash
kubectl get pods                      # status + restarts
kubectl describe pod <name>           # EVENTS at the bottom — the #1 clue
kubectl logs <name> [-p] [-c <ctr>]   # app logs (-p = previous crashed container)
kubectl get events --sort-by=.lastTimestamp
kubectl get endpoints <svc>           # empty = selector/label mismatch
```

| Symptom | Usual cause | Check / fix |
|---|---|---|
| **ImagePullBackOff** | bad image name/tag, private registry, no pull secret | `describe` events; fix image / add imagePullSecret |
| **CrashLoopBackOff** | app exits/errors on start | `logs -p`; fix the app/config/command |
| **Pending** | no node fits (resources), unbound PVC, taints | `describe` events; lower requests / add capacity / fix PVC |
| **OOMKilled** | exceeded memory limit | raise memory limit or fix the leak |
| **Service unreachable** | selector ≠ pod labels, wrong targetPort | `get endpoints` (empty?), check labels & ports |
| **Readiness failing** | probe path/port wrong, app slow to start | check probe config / initialDelaySeconds |

## Chat output format

````
**Diagnosis: CrashLoopBackOff on `web`**

```bash
kubectl describe pod web-xxxx      # look at Events
kubectl logs web-xxxx -p           # logs from the crashed container
```
Most likely: the app errors on startup (bad env/config) or the liveness probe
is killing it before it's ready. If logs show a config error → fix the ConfigMap;
if it's slow to boot → add a startupProbe / raise initialDelaySeconds.
````

## Workflow

1. **Clarify** the app: stateless vs stateful, ports, config/secrets, replicas, exposure (internal vs external).
2. **Write manifests:** Deployment (with resources + probes + pinned image) + Service (+ Ingress if external) + ConfigMap/Secret. Verify **selector ↔ labels** match.
3. **For debugging:** `get pods` → `describe` (events) → `logs -p` → `get endpoints`. Map the symptom to the table.
4. **Apply & roll out:** `kubectl apply -f`, `rollout status`, `rollout undo` if it regresses.
5. **Deliver** YAML/diagnosis + the exact kubectl commands; route the image to `docker`, scripts to `bash-scripting`, app security/RBAC depth to `api-security-audit`.

## Key pitfalls

- **Selector ≠ labels.** A Service/Deployment whose selector doesn't match the pod labels silently has no endpoints — the #1 "it's not working" cause. `kubectl get endpoints` to confirm.
- **No resource requests/limits.** Causes bad scheduling, OOMKills, and noisy neighbors. Always set both.
- **Missing/wrong probes.** No readiness → traffic to unready pods during rollout; too-aggressive liveness → restart loops. Tune `initialDelaySeconds`.
- **`:latest` image tags.** Non-reproducible rollouts and no real rollback. Pin versions (→ `docker`).
- **Secrets treated as encrypted.** k8s Secrets are base64-encoded, not encrypted at rest by default — enable encryption or use an external manager; never commit them.
- **Imperative changes that drift.** `kubectl edit`/`scale` one-offs diverge from your YAML — keep manifests in git (GitOps).
- **Ignoring `describe` events.** The events section is where Kubernetes tells you exactly what's wrong — read it first.
- **One giant pod.** Don't cram multiple services into one container; one concern per container, orchestrate with separate workloads.

## Quick reference

- Objects: Pod < Deployment(ReplicaSet) · Service (ClusterIP/NodePort/LoadBalancer) · Ingress · ConfigMap/Secret · Namespace; StatefulSet/DaemonSet/Job for special cases.
- **Labels/selectors are the glue** — Service selector must match pod labels (else no endpoints).
- Always set resources (requests+limits) and probes (readiness gates traffic, liveness restarts).
- Debug: `get pods` → `describe pod` (events!) → `logs -p` → `get endpoints`.
- Symptoms: ImagePullBackOff (image), CrashLoopBackOff (app/logs), Pending (resources/PVC), OOMKilled (memory limit), no endpoints (selector).
- Pin image tags; keep config/secrets out of the image; manifests in git; `rollout undo` to revert.
