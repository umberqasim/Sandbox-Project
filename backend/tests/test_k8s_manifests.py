"""Static checks of k8s/*.yaml for the defects found in review (no cluster needed)."""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

K8S_DIR = Path(__file__).resolve().parents[2] / "k8s"
NOT_RESOURCES = {"kustomization.yaml", "secrets.example.yaml"}

pytestmark = pytest.mark.skipif(not K8S_DIR.is_dir(), reason="k8s/ folder not found next to backend/")


def _load():
    docs = []
    for path in sorted(K8S_DIR.glob("*.yaml")):
        if path.name in NOT_RESOURCES:
            continue
        docs += [(path.name, d) for d in yaml.safe_load_all(path.read_text()) if d]
    return docs


def _workloads(docs):
    return [(f, d) for f, d in docs if d["kind"] in ("Deployment", "StatefulSet")]


def _containers(workload):
    return workload["spec"]["template"]["spec"]["containers"]


def test_env_variables_are_defined_before_they_are_referenced():
    for _, w in _workloads(_load()):
        for c in _containers(w):
            defined = set()
            for env in c.get("env", []):
                for ref in re.findall(r"\$\(([A-Za-z0-9_]+)\)", env.get("value", "")):
                    assert ref in defined, f"{w['metadata']['name']}: ${ref} used before it is defined"
                defined.add(env["name"])


def test_hpa_targets_have_cpu_requests():
    docs = _load()
    workloads = {(d["kind"], d["metadata"]["name"]): d for _, d in _workloads(docs)}
    hpas = [d for _, d in docs if d["kind"] == "HorizontalPodAutoscaler"]
    assert hpas
    for hpa in hpas:
        target = workloads[(hpa["spec"]["scaleTargetRef"]["kind"], hpa["spec"]["scaleTargetRef"]["name"])]
        for c in _containers(target):
            assert "cpu" in c.get("resources", {}).get("requests", {}), f"{c['name']} has no cpu request"


def test_services_select_existing_pods():
    docs = _load()
    labels = [w["spec"]["template"]["metadata"]["labels"] for _, w in _workloads(docs)]
    for _, svc in [(f, d) for f, d in docs if d["kind"] == "Service"]:
        assert any(svc["spec"]["selector"].items() <= lab.items() for lab in labels), svc["metadata"]["name"]


def test_only_the_worker_mounts_the_docker_socket_and_only_on_docker_nodes():
    for _, w in _workloads(_load()):
        volumes = w["spec"]["template"]["spec"].get("volumes", [])
        sockets = [v for v in volumes if v.get("hostPath", {}).get("path") == "/var/run/docker.sock"]
        if w["metadata"]["name"] == "sandbox-worker":
            assert sockets and sockets[0]["hostPath"]["type"] == "Socket"
            assert w["spec"]["template"]["spec"]["nodeSelector"] == {"sandbox-docker": "true"}
        else:
            assert not sockets, f"{w['metadata']['name']} must not mount the Docker socket"


def test_worker_id_is_the_stable_pod_name():
    worker = next(w for _, w in _workloads(_load()) if w["metadata"]["name"] == "sandbox-worker")
    assert worker["kind"] == "StatefulSet"  # stable names => crash recovery can find the old pod's evaluations
    env = {e["name"]: e for e in _containers(worker)[0]["env"]}
    assert env["SANDBOX_WORKER_ID"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.name"


def test_local_images_are_not_always_pulled():
    for _, w in _workloads(_load()):
        for c in _containers(w):
            if c["image"].endswith(":latest"):
                assert c.get("imagePullPolicy") == "IfNotPresent", c["image"]


def test_uploads_volume_is_shared_between_api_and_worker():
    docs = _load()
    pvc = next(d for _, d in docs if d["metadata"]["name"] == "sandbox-uploads-pvc")
    assert pvc["spec"]["accessModes"] == ["ReadWriteMany"]
    for name in ("sandbox-backend", "sandbox-worker"):
        w = next(w for _, w in _workloads(docs) if w["metadata"]["name"] == name)
        assert any(m["mountPath"] == "/tmp/sandbox-runs" for m in _containers(w)[0]["volumeMounts"])


def test_kustomization_lists_every_resource_file_but_not_the_secret_example():
    kustomization = yaml.safe_load((K8S_DIR / "kustomization.yaml").read_text())
    listed = set(kustomization["resources"])
    on_disk = {p.name for p in K8S_DIR.glob("*.yaml")} - NOT_RESOURCES
    assert listed == on_disk
