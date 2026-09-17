#!/usr/bin/env python3
"""Static cross-validator for the k8s/ manifests.

Catches the class of bug that only shows up at deploy time:
selectors that match nothing, Services pointing at missing ports,
env vars referencing Secret keys that don't exist, Ingress backends
that don't resolve, probe ports that aren't exposed.
Covers Deployments, CronJobs, and standalone Pods.
Requires only PyYAML.  Exit code 0 = all checks pass.
"""
import glob
import sys

import yaml

fails, warns, passes = [], [], []

docs = []
for f in sorted(glob.glob("k8s/*.yaml")):
    if f.endswith(".example.yaml"):
        continue
    for d in yaml.safe_load_all(open(f)):
        if d:
            docs.append((f, d))

secrets, deployments, services, ingresses, workloads = {}, [], {}, [], []
pod_sec = {}
for f, d in docs:
    kind = d.get("kind")
    ns = d.get("metadata", {}).get("namespace", "default")
    name = d.get("metadata", {}).get("name", "?")
    if kind == "Secret":
        keys = set(d.get("stringData", {}) or {}) | set(d.get("data", {}) or {})
        secrets[(ns, name)] = keys
    elif kind == "Deployment":
        deployments.append((f, ns, name, d))
        pod = d["spec"]["template"]["spec"]
        workloads.append((kind, ns, name, pod.get("containers", [])))
        pod_sec[(kind, ns, name)] = pod.get("securityContext") or {}
    elif kind == "CronJob":
        pod = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        workloads.append((kind, ns, name, pod.get("containers", [])))
    elif kind == "Pod":
        workloads.append((kind, ns, name, d["spec"].get("containers", [])))
    elif kind == "Service":
        services[(ns, name)] = (f, d)
    elif kind == "Ingress":
        ingresses.append((f, ns, name, d))

def container_ports(dep):
    out = set()
    for c in dep["spec"]["template"]["spec"].get("containers", []):
        for p in c.get("ports", []) or []:
            out.add(p.get("containerPort"))
    return out

# ---- checks that apply to every pod-bearing workload ----
for kind, ns, name, containers in workloads:
    ident = f"{kind} {ns}/{name}"
    for c in containers:
        for e in c.get("env", []) or []:
            ref = (e.get("valueFrom") or {}).get("secretKeyRef")
            if ref:
                key = (ns, ref["name"])
                if key not in secrets:
                    fails.append(f"{ident}: env {e['name']} -> Secret "
                                 f"{ref['name']} not defined in repo")
                elif ref["key"] not in secrets[key]:
                    fails.append(f"{ident}: env {e['name']} -> key "
                                 f"'{ref['key']}' missing in Secret {ref['name']}")
                else:
                    passes.append(f"{ident}: env {e['name']} resolves to "
                                  f"Secret {ref['name']}/{ref['key']}")
        for er in c.get("envFrom", []) or []:
            sr = er.get("secretRef")
            if sr and (ns, sr["name"]) not in secrets:
                fails.append(f"{ident}: envFrom Secret {sr['name']} "
                             f"not defined in repo")
        img = c.get("image", "")
        if img.endswith(":latest") or ":" not in img.rsplit("/", 1)[-1]:
            warns.append(f"{ident}: image '{img}' is not pinned")
        if not c.get("resources"):
            warns.append(f"{ident}/{c['name']}: no resources set")
        # container hardening (first-party images only; third-party
        # charts and the postgres entrypoint need root, see UPGRADE.md)
        if "192.168.1.210:5000/" in img:
            sc = c.get("securityContext") or {}
            if sc.get("allowPrivilegeEscalation") is not False:
                warns.append(f"{ident}/{c['name']}: allowPrivilegeEscalation not false")
            if sc.get("readOnlyRootFilesystem") is not True:
                warns.append(f"{ident}/{c['name']}: root filesystem is writable")
            if "ALL" not in ((sc.get("capabilities") or {}).get("drop") or []):
                warns.append(f"{ident}/{c['name']}: capabilities not dropped")
            pod_sc = pod_sec.get((kind, ns, name), {})
            if pod_sc.get("runAsNonRoot") is not True:
                warns.append(f"{ident}: pod securityContext missing runAsNonRoot")
            if not pod_sc.get("runAsUser"):
                warns.append(f"{ident}: pod securityContext missing runAsUser")

# ---- Deployment-specific checks ----
for f, ns, name, d in deployments:
    sel = d["spec"].get("selector", {}).get("matchLabels", {})
    labels = d["spec"]["template"]["metadata"].get("labels", {})
    if all(labels.get(k) == v for k, v in sel.items()):
        passes.append(f"Deployment {ns}/{name}: selector matches pod labels")
    else:
        fails.append(f"Deployment {ns}/{name}: selector {sel} != labels {labels}")
    ports = container_ports(d)
    for c in d["spec"]["template"]["spec"].get("containers", []):
        for probe in ("readinessProbe", "livenessProbe"):
            pr = c.get(probe)
            if pr and "httpGet" in pr:
                port = pr["httpGet"].get("port")
                if isinstance(port, int) and port not in ports:
                    fails.append(f"Deployment {ns}/{name}: {probe} port {port} "
                                 f"not in containerPorts {sorted(ports)}")
                else:
                    passes.append(f"Deployment {ns}/{name}: {probe} port ok")

# ---- Service checks ----
for (ns, name), (f, s) in services.items():
    sel = s["spec"].get("selector")
    if not sel:
        continue
    match = None
    for _, dns, dname, d in deployments:
        if dns != ns:
            continue
        labels = d["spec"]["template"]["metadata"].get("labels", {})
        if all(labels.get(k) == v for k, v in sel.items()):
            match = d
            break
    if not match:
        fails.append(f"Service {ns}/{name}: selector {sel} matches no Deployment")
        continue
    passes.append(f"Service {ns}/{name}: selector matches Deployment pods")
    ports = container_ports(match)
    for p in s["spec"].get("ports", []):
        tp = p.get("targetPort", p.get("port"))
        if isinstance(tp, int) and tp not in ports:
            fails.append(f"Service {ns}/{name}: targetPort {tp} not exposed "
                         f"by matched pod (has {sorted(ports)})")
        else:
            passes.append(f"Service {ns}/{name}: targetPort {tp} ok")

# ---- Ingress checks ----
for f, ns, name, d in ingresses:
    for rule in d["spec"].get("rules", []):
        for path in rule.get("http", {}).get("paths", []):
            be = path["backend"]["service"]
            key = (ns, be["name"])
            if key not in services:
                fails.append(f"Ingress {ns}/{name}: backend Service "
                             f"{be['name']} not defined in repo")
                continue
            svc_ports = {p.get("port") for p in services[key][1]["spec"].get("ports", [])}
            want = be.get("port", {}).get("number")
            if want and want not in svc_ports:
                fails.append(f"Ingress {ns}/{name}: backend port {want} not in "
                             f"Service {be['name']} ports {sorted(svc_ports)}")
            else:
                passes.append(f"Ingress {ns}/{name}: backend "
                              f"{be['name']}:{want} resolves")

print(f"PASS  ({len(passes)} checks)")
for w in warns:
    print(f"WARN  {w}")
for x in fails:
    print(f"FAIL  {x}")
print(f"\n{len(passes)} passed, {len(warns)} warnings, {len(fails)} failures")
sys.exit(1 if fails else 0)
