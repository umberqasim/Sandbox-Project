# Kubernetes Manifests

**Status: reference manifests. They are validated statically (`kubeconform --strict`, Kubernetes 1.29 schemas,
13 resources) and by unit tests, but they have not been applied to a running cluster** - a cluster does not fit
the 4 GB development machine. Kubernetes was optional in the case study; Docker Compose is the orchestration that
was actually run, tested and demonstrated.

## What maps to what

| File | Contents |
|---|---|
| `db-deployment.yaml` | PostgreSQL, PersistentVolumeClaim, Service |
| `redis-deployment.yaml` | Redis (Celery broker and result backend), Service |
| `uploads-pvc.yaml` | ReadWriteMany volume shared by the API and worker pods (ZIP uploads, MLflow store) |
| `backend-deployment.yaml` | FastAPI, 2 replicas, readiness probe, ClusterIP Service |
| `worker-deployment.yaml` | Celery worker **StatefulSet** + headless Service + HorizontalPodAutoscaler |
| `frontend-deployment.yaml` | React dashboard (Vite dev server), ClusterIP Service |
| `kustomization.yaml` | Lists the files above; used by `kubectl apply -k` |
| `secrets.example.yaml` | Documentation of the Secret's shape only (placeholders, not applied) |

## Requirements the cluster must meet

1. **Nodes that run Docker Engine.** The worker launches sandbox containers through the node's
   `/var/run/docker.sock`. Clusters that use containerd only (most managed clusters) have no such socket; the pod
   will not start there (`hostPath.type: Socket`). Label the Docker nodes:
   `kubectl label node <node> sandbox-docker=true`. The worker pod can control that node's Docker, so use a dedicated
   node pool.
2. **A ReadWriteMany storage class** for `uploads-pvc.yaml` (set `storageClassName`). The API pod saves an uploaded
   ZIP and a worker pod reads it, so both must see the same files.
3. **The images must exist on the nodes.** Compose builds images with its own names, so build these yourself:

   ```bash
   docker build -t sandbox-backend:latest ./backend      # used by the API AND the worker (different command)
   docker build -t sandbox-frontend:latest ./frontend
   # then make them available to the cluster: push to a registry and change the image names, or
   # minikube image load sandbox-backend:latest sandbox-frontend:latest
   ```

## Deploy

```bash
kubectl create secret generic sandbox-secrets \
  --from-literal=db-password='<url-safe-password>' --from-literal=api-key='<long-random-key>'
kubectl apply -k k8s/
```

Do not use `kubectl apply -f k8s/`: it would also try to apply `secrets.example.yaml` with its placeholder values.
The database password is placed inside `DATABASE_URL`, so use URL-safe characters. To enable the optional AI review add
`--from-literal=groq-api-key='<key>'` to the Secret; without that key the worker simply runs without it.

The dashboard calls the API at the same host on port 8000, so the Services are `ClusterIP`. For a demo:

```bash
kubectl port-forward svc/sandbox-backend 8000:8000 &
kubectl port-forward svc/sandbox-frontend 5173:5173
```

## Design notes (why the manifests look like this)

- **Worker = StatefulSet.** Crash recovery (interrupted evaluations are recorded, leftover images and containers are
  removed) is keyed by `SANDBOX_WORKER_ID`. Stable pod names give a re-created pod the same id. With a Deployment,
  every new pod would have a new random name and could never recover the old pod's evaluations.
- **Autoscaling.** The HPA scales on CPU and therefore needs `resources.requests.cpu`, which is set. It is still a weak
  signal here: the heavy work (`docker build`, sandbox containers) runs on the node's Docker daemon, outside the worker
  pod's cgroup, so the pod looks idle while evaluations are running. The right signal is the queue length. With KEDA,
  a `redis` trigger on the list `celery` of `sandbox-redis:6379` scales the StatefulSet by queue depth; this is
  described here but not included or tested.
- **Docker Compose scaling is not supported.** The worker has fixed container and worker IDs, so Compose's
  scale option cannot add worker replicas. Compose defaults to one worker with one evaluation slot; WORKER_CONCURRENCY
  can raise that on a larger host. Horizontal scaling is only described by these untested reference manifests.
- `kubectl scale statefulset sandbox-worker --replicas=N` is overridden by the HPA; change `minReplicas`/`maxReplicas`
  or delete the HPA to control the count by hand.
