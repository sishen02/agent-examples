"""Kubernetes provider for CockroachDB operational tooling."""

from datetime import timezone
from typing import Any


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "astimezone"):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


class NullKubernetesProvider:
    """Provider used when Kubernetes access is disabled or unavailable."""

    def __init__(self, reason: str = "Kubernetes provider is not configured"):
        self.reason = reason

    def status(self) -> dict[str, Any]:
        return {"error": self.reason, "pods": [], "statefulsets": [], "services": [], "events": []}

    def scale_statefulset(self, name: str, replicas: int) -> dict[str, Any]:
        return {"error": self.reason, "changed": False, "statefulset": name, "replicas": replicas}

    def restart_pod(self, pod_name: str) -> dict[str, Any]:
        return {"error": self.reason, "changed": False, "pod": pod_name}


class KubernetesAPIProvider:
    """Kubernetes API provider using the official Python client."""

    def __init__(self, namespace: str, label_selector: str):
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()

        self.namespace = namespace
        self.label_selector = label_selector
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()

    def status(self) -> dict[str, Any]:
        pods = self.core.list_namespaced_pod(self.namespace, label_selector=self.label_selector)
        statefulsets = self.apps.list_namespaced_stateful_set(self.namespace, label_selector=self.label_selector)
        services = self.core.list_namespaced_service(self.namespace, label_selector=self.label_selector)
        events = self.core.list_namespaced_event(self.namespace, limit=25)

        return {
            "namespace": self.namespace,
            "label_selector": self.label_selector,
            "pods": [
                {
                    "name": pod.metadata.name,
                    "phase": pod.status.phase,
                    "ready": all(condition.ready for condition in (pod.status.container_statuses or [])),
                    "node_name": pod.spec.node_name,
                    "restart_count": sum(condition.restart_count for condition in (pod.status.container_statuses or [])),
                    "created": _iso(pod.metadata.creation_timestamp),
                }
                for pod in pods.items
            ],
            "statefulsets": [
                {
                    "name": sts.metadata.name,
                    "replicas": sts.spec.replicas,
                    "ready_replicas": sts.status.ready_replicas or 0,
                    "current_replicas": sts.status.current_replicas or 0,
                    "updated_replicas": sts.status.updated_replicas or 0,
                }
                for sts in statefulsets.items
            ],
            "services": [
                {
                    "name": svc.metadata.name,
                    "type": svc.spec.type,
                    "cluster_ip": svc.spec.cluster_ip,
                    "ports": [port.port for port in svc.spec.ports or []],
                }
                for svc in services.items
            ],
            "events": [
                {
                    "name": event.metadata.name,
                    "type": event.type,
                    "reason": event.reason,
                    "message": event.message,
                    "involved_object": getattr(event.involved_object, "name", None),
                    "last_timestamp": _iso(event.last_timestamp),
                }
                for event in events.items[-25:]
            ],
        }

    def scale_statefulset(self, name: str, replicas: int) -> dict[str, Any]:
        body = {"spec": {"replicas": replicas}}
        self.apps.patch_namespaced_stateful_set_scale(name=name, namespace=self.namespace, body=body)
        return {"changed": True, "statefulset": name, "replicas": replicas}

    def restart_pod(self, pod_name: str) -> dict[str, Any]:
        self.core.delete_namespaced_pod(name=pod_name, namespace=self.namespace)
        return {"changed": True, "pod": pod_name}

