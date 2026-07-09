"""Kubernetes provider for CockroachDB operational tooling."""

import ssl
from datetime import timezone
from typing import Any
from urllib.request import urlopen

from kubernetes.stream import stream


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

    def exec_cockroach(self, pod_name: str, container: str, args: list[str]) -> dict[str, Any]:
        return {"error": self.reason, "pod": pod_name, "container": container, "args": args, "changed": False}

    def metrics_health(
        self,
        pod_names: list[str] | None = None,
        statefulset_name: str | None = None,
        desired_replicas: int | None = None,
        http_port: int = 8080,
        secure: bool = False,
        timeout_seconds: int = 10,
    ) -> dict[str, Any]:
        return {"error": self.reason, "pods": []}


class KubernetesAPIProvider:
    """Kubernetes API provider using the official Python client."""

    LEGACY_LABEL_SELECTORS = ("app=cockroachdb",)

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
        label_selector = self.label_selector
        pods = self.core.list_namespaced_pod(self.namespace, label_selector=label_selector)
        statefulsets = self.apps.list_namespaced_stateful_set(self.namespace, label_selector=label_selector)
        if not pods.items and not statefulsets.items:
            for fallback in self.LEGACY_LABEL_SELECTORS:
                fallback_pods = self.core.list_namespaced_pod(self.namespace, label_selector=fallback)
                fallback_statefulsets = self.apps.list_namespaced_stateful_set(self.namespace, label_selector=fallback)
                if fallback_pods.items or fallback_statefulsets.items:
                    label_selector = fallback
                    pods = fallback_pods
                    statefulsets = fallback_statefulsets
                    break
        services = self._services_for_selector(label_selector)
        events = self.core.list_namespaced_event(self.namespace, limit=25)

        return {
            "namespace": self.namespace,
            "label_selector": label_selector,
            "configured_label_selector": self.label_selector,
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
                for svc in services
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

    def _services_for_selector(self, label_selector: str):
        metadata_matches = self.core.list_namespaced_service(self.namespace, label_selector=label_selector)
        services_by_name = {service.metadata.name: service for service in metadata_matches.items}
        selector_labels = _parse_simple_label_selector(label_selector)
        if selector_labels:
            all_services = self.core.list_namespaced_service(self.namespace)
            for service in all_services.items:
                service_selector = service.spec.selector or {}
                if all(service_selector.get(key) == value for key, value in selector_labels.items()):
                    services_by_name[service.metadata.name] = service
        return list(services_by_name.values())

    def scale_statefulset(self, name: str, replicas: int) -> dict[str, Any]:
        body = {"spec": {"replicas": replicas}}
        self.apps.patch_namespaced_stateful_set_scale(name=name, namespace=self.namespace, body=body)
        return {"changed": True, "statefulset": name, "replicas": replicas}

    def restart_pod(self, pod_name: str) -> dict[str, Any]:
        self.core.delete_namespaced_pod(name=pod_name, namespace=self.namespace)
        return {"changed": True, "pod": pod_name}

    def exec_cockroach(self, pod_name: str, container: str, args: list[str]) -> dict[str, Any]:
        command = ["cockroach", *args]
        try:
            response = stream(
                self.core.connect_get_namespaced_pod_exec,
                pod_name,
                self.namespace,
                container=container,
                command=command,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
            )
        except Exception as exc:
            return _exec_error(pod_name, container, command, exc)
        stdout = []
        stderr = []
        try:
            while response.is_open():
                response.update(timeout=1)
                if response.peek_stdout():
                    stdout.append(response.read_stdout() or "")
                if response.peek_stderr():
                    stderr.append(response.read_stderr() or "")
            return_code = response.returncode
        except Exception as exc:
            response.close()
            return _exec_error(pod_name, container, command, exc, stdout=stdout, stderr=stderr)
        response.close()
        return {
            "pod": pod_name,
            "container": container,
            "command": command,
            "stdout": "".join(stdout),
            "stderr": "".join(stderr),
            "exit_code": return_code,
            "changed": return_code == 0 and _command_changes_state(args),
        }

    def metrics_health(
        self,
        pod_names: list[str] | None = None,
        statefulset_name: str | None = None,
        desired_replicas: int | None = None,
        http_port: int = 8080,
        secure: bool = False,
        timeout_seconds: int = 10,
    ) -> dict[str, Any]:
        names = pod_names or self._pod_names_for_statefulset(statefulset_name, desired_replicas)
        scheme = "https" if secure else "http"
        context = ssl._create_unverified_context() if secure else None
        results = []
        for name in names:
            pod = self.core.read_namespaced_pod(name=name, namespace=self.namespace)
            pod_ip = pod.status.pod_ip
            if not pod_ip:
                results.append({"pod": name, "ok": False, "error": "pod has no pod_ip"})
                continue
            url = f"{scheme}://{pod_ip}:{http_port}/_status/vars"
            try:
                with urlopen(url, timeout=timeout_seconds, context=context) as response:
                    body = response.read().decode("utf-8", errors="replace")
                    samples = _parse_underreplicated_samples(body)
                    results.append(
                        {
                            "pod": name,
                            "pod_uid": pod.metadata.uid,
                            "url": url,
                            "status": response.status,
                            "ok": response.status == 200 and bool(samples),
                            "ranges_underreplicated": samples,
                        }
                    )
            except Exception as exc:
                results.append({"pod": name, "url": url, "ok": False, "error": str(exc)})
        return {"pods": results, "all_ok": all(item.get("ok") for item in results) and bool(results)}

    def _pod_names_for_statefulset(self, statefulset_name: str | None, desired_replicas: int | None) -> list[str]:
        if not statefulset_name:
            pods = self.core.list_namespaced_pod(self.namespace, label_selector=self.label_selector)
            return [pod.metadata.name for pod in pods.items]
        if desired_replicas is None:
            sts = self.apps.read_namespaced_stateful_set(name=statefulset_name, namespace=self.namespace)
            desired_replicas = sts.spec.replicas or 0
        return [f"{statefulset_name}-{index}" for index in range(max(0, desired_replicas))]


def _command_changes_state(args: list[str]) -> bool:
    return bool(args and args[:2] == ["node", "decommission"]) or bool(args and args[0] == "init")


def _exec_error(
    pod_name: str,
    container: str,
    command: list[str],
    exc: Exception,
    stdout: list[str] | None = None,
    stderr: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "pod": pod_name,
        "container": container,
        "command": command,
        "stdout": "".join(stdout or []),
        "stderr": "".join(stderr or []),
        "exit_code": None,
        "changed": False,
        "error": str(exc),
        "error_type": type(exc).__name__,
    }


def _parse_simple_label_selector(label_selector: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for raw_part in label_selector.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "==" in part:
            key, value = part.split("==", 1)
        elif "=" in part and "!=" not in part:
            key, value = part.split("=", 1)
        else:
            return {}
        key = key.strip()
        value = value.strip()
        if not key or not value:
            return {}
        labels[key] = value
    return labels


def _parse_underreplicated_samples(body: str) -> list[dict[str, Any]]:
    samples = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, _, raw_value = stripped.partition(" ")
        if not name.startswith("ranges_underreplicated"):
            continue
        try:
            value = float(raw_value.split()[0])
        except (IndexError, ValueError):
            continue
        samples.append({"metric": name, "value": value})
    return samples
