"""Kubernetes provider for CockroachDB operational tooling."""

import ssl
import time
from datetime import timezone
from typing import Any
from urllib.request import urlopen


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

    def start_config(self, pod_name: str | None = None, statefulset_name: str | None = None) -> dict[str, Any]:
        return {"error": self.reason, "pod": pod_name, "statefulset": statefulset_name}

    def membership_evidence(self, statefulset_name: str, desired_replicas: int) -> dict[str, Any]:
        return {"error": self.reason, "statefulset": statefulset_name, "desired_replicas": desired_replicas}

    def rollout_evidence(
        self,
        statefulset_name: str,
        partition: int | None = None,
        desired_image: str | None = None,
    ) -> dict[str, Any]:
        return {"error": self.reason, "statefulset": statefulset_name}

    def cleanup_restart_annotation(
        self,
        resource_name: str,
        annotation_key: str,
        expected_value: str | None = None,
    ) -> dict[str, Any]:
        return {"error": self.reason, "changed": False, "resource": resource_name}

    def sync_ingress_host(
        self,
        name: str,
        host: str,
        service_name: str,
        service_port: int,
        tls_secret_name: str | None = None,
        path: str = "/",
    ) -> dict[str, Any]:
        return {"error": self.reason, "changed": False, "ingress": name, "host": host}

    def delete_ingress(self, name: str) -> dict[str, Any]:
        return {"error": self.reason, "changed": False, "ingress": name}

    def version_check_job(
        self,
        job_name: str,
        image: str,
        expected_version: str | None = None,
        timeout_seconds: int = 120,
        delete_after: bool = True,
    ) -> dict[str, Any]:
        return {"error": self.reason, "changed": False, "job": job_name, "image": image}


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
        self.batch = client.BatchV1Api()
        self.networking = client.NetworkingV1Api()

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
        from kubernetes.stream import stream

        command = ["cockroach", *args]
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
        stdout = []
        stderr = []
        while response.is_open():
            response.update(timeout=1)
            if response.peek_stdout():
                stdout.append(response.read_stdout())
            if response.peek_stderr():
                stderr.append(response.read_stderr())
        return_code = response.returncode
        response.close()
        return {
            "pod": pod_name,
            "container": container,
            "command": command,
            "stdout": "".join(stdout),
            "stderr": "".join(stderr),
            "exit_code": return_code,
            "changed": _command_changes_state(args),
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

    def start_config(self, pod_name: str | None = None, statefulset_name: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"namespace": self.namespace}
        if pod_name:
            pod = self.core.read_namespaced_pod(name=pod_name, namespace=self.namespace)
            result["pod"] = _pod_config(pod)
        if statefulset_name:
            sts = self.apps.read_namespaced_stateful_set(name=statefulset_name, namespace=self.namespace)
            result["statefulset"] = _statefulset_config(sts)
        return result

    def membership_evidence(self, statefulset_name: str, desired_replicas: int) -> dict[str, Any]:
        sts = self.apps.read_namespaced_stateful_set(name=statefulset_name, namespace=self.namespace)
        pods = [
            self.core.read_namespaced_pod(name=f"{statefulset_name}-{index}", namespace=self.namespace)
            for index in range(max(0, desired_replicas))
        ]
        return {
            "statefulset": _statefulset_status(sts),
            "desired_replicas": desired_replicas,
            "pods": [_pod_status(pod) for pod in pods],
        }

    def rollout_evidence(
        self,
        statefulset_name: str,
        partition: int | None = None,
        desired_image: str | None = None,
    ) -> dict[str, Any]:
        sts = self.apps.read_namespaced_stateful_set(name=statefulset_name, namespace=self.namespace)
        pod_names = [f"{statefulset_name}-{partition}"] if partition is not None else self._pod_names_for_statefulset(
            statefulset_name,
            sts.spec.replicas,
        )
        pods = [self.core.read_namespaced_pod(name=name, namespace=self.namespace) for name in pod_names]
        return {
            "statefulset": _statefulset_status(sts),
            "partition": partition,
            "desired_image": desired_image,
            "pods": [_pod_rollout(pod, desired_image) for pod in pods],
        }

    def cleanup_restart_annotation(
        self,
        resource_name: str,
        annotation_key: str,
        expected_value: str | None = None,
    ) -> dict[str, Any]:
        sts = self.apps.read_namespaced_stateful_set(name=resource_name, namespace=self.namespace)
        annotations = dict(sts.metadata.annotations or {})
        current_value = annotations.get(annotation_key)
        if expected_value is not None and current_value != expected_value:
            return {
                "changed": False,
                "resource": resource_name,
                "annotation_key": annotation_key,
                "current_value": current_value,
                "error": "annotation value did not match expected_value",
            }
        body = {"metadata": {"annotations": {annotation_key: None}}}
        self.apps.patch_namespaced_stateful_set(name=resource_name, namespace=self.namespace, body=body)
        return {
            "changed": True,
            "resource": resource_name,
            "annotation_key": annotation_key,
            "previous_value": current_value,
        }

    def sync_ingress_host(
        self,
        name: str,
        host: str,
        service_name: str,
        service_port: int,
        tls_secret_name: str | None = None,
        path: str = "/",
    ) -> dict[str, Any]:
        from kubernetes import client

        ingress = client.V1Ingress(
            metadata=client.V1ObjectMeta(name=name, namespace=self.namespace),
            spec=client.V1IngressSpec(
                rules=[
                    client.V1IngressRule(
                        host=host,
                        http=client.V1HTTPIngressRuleValue(
                            paths=[
                                client.V1HTTPIngressPath(
                                    path=path,
                                    path_type="Prefix",
                                    backend=client.V1IngressBackend(
                                        service=client.V1IngressServiceBackend(
                                            name=service_name,
                                            port=client.V1ServiceBackendPort(number=service_port),
                                        )
                                    ),
                                )
                            ]
                        ),
                    )
                ],
                tls=[client.V1IngressTLS(hosts=[host], secret_name=tls_secret_name)] if tls_secret_name else None,
            ),
        )
        try:
            self.networking.patch_namespaced_ingress(name=name, namespace=self.namespace, body=ingress)
            action = "patched"
        except Exception:
            self.networking.create_namespaced_ingress(namespace=self.namespace, body=ingress)
            action = "created"
        return {
            "changed": True,
            "action": action,
            "ingress": name,
            "host": host,
            "service_name": service_name,
            "service_port": service_port,
        }

    def delete_ingress(self, name: str) -> dict[str, Any]:
        self.networking.delete_namespaced_ingress(name=name, namespace=self.namespace)
        return {"changed": True, "ingress": name, "deleted": True}

    def version_check_job(
        self,
        job_name: str,
        image: str,
        expected_version: str | None = None,
        timeout_seconds: int = 120,
        delete_after: bool = True,
    ) -> dict[str, Any]:
        from kubernetes import client

        command = ["/bin/sh", "-c", "cockroach version 2>&1"]
        job = client.V1Job(
            metadata=client.V1ObjectMeta(name=job_name, namespace=self.namespace),
            spec=client.V1JobSpec(
                backoff_limit=0,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels={"app.kubernetes.io/name": job_name}),
                    spec=client.V1PodSpec(
                        restart_policy="Never",
                        containers=[client.V1Container(name="version-check", image=image, command=command)],
                    ),
                ),
            ),
        )
        self.batch.create_namespaced_job(namespace=self.namespace, body=job)
        pod_name = self._wait_for_job_pod(job_name, timeout_seconds)
        log = self.core.read_namespaced_pod_log(name=pod_name, namespace=self.namespace, container="version-check")
        job_obj = self.batch.read_namespaced_job(name=job_name, namespace=self.namespace)
        if delete_after:
            self.batch.delete_namespaced_job(
                name=job_name,
                namespace=self.namespace,
                propagation_policy="Background",
            )
        return {
            "changed": True,
            "job": job_name,
            "pod": pod_name,
            "image": image,
            "expected_version": expected_version,
            "log": log,
            "succeeded": bool(job_obj.status.succeeded),
            "failed": bool(job_obj.status.failed),
            "deleted": delete_after,
        }

    def _pod_names_for_statefulset(self, statefulset_name: str | None, desired_replicas: int | None) -> list[str]:
        if not statefulset_name:
            pods = self.core.list_namespaced_pod(self.namespace, label_selector=self.label_selector)
            return [pod.metadata.name for pod in pods.items]
        if desired_replicas is None:
            sts = self.apps.read_namespaced_stateful_set(name=statefulset_name, namespace=self.namespace)
            desired_replicas = sts.spec.replicas or 0
        return [f"{statefulset_name}-{index}" for index in range(max(0, desired_replicas))]

    def _wait_for_job_pod(self, job_name: str, timeout_seconds: int) -> str:
        deadline = time.time() + timeout_seconds
        selector = f"job-name={job_name}"
        while time.time() < deadline:
            pods = self.core.list_namespaced_pod(namespace=self.namespace, label_selector=selector)
            if pods.items:
                return pods.items[0].metadata.name
            time.sleep(1)
        raise TimeoutError(f"timed out waiting for pod for job {job_name}")


def _command_changes_state(args: list[str]) -> bool:
    return bool(args and args[:2] == ["node", "decommission"]) or bool(args and args[0] == "init")


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


def _container_config(container: Any) -> dict[str, Any]:
    return {
        "name": container.name,
        "image": container.image,
        "command": container.command or [],
        "args": container.args or [],
        "ports": [{"name": port.name, "container_port": port.container_port} for port in (container.ports or [])],
        "env": [
            {"name": env.name, "value": env.value, "value_from": bool(env.value_from)}
            for env in (container.env or [])
        ],
        "volume_mounts": [
            {"name": mount.name, "mount_path": mount.mount_path}
            for mount in (container.volume_mounts or [])
        ],
    }


def _pod_config(pod: Any) -> dict[str, Any]:
    return {
        "name": pod.metadata.name,
        "uid": pod.metadata.uid,
        "containers": [_container_config(container) for container in (pod.spec.containers or [])],
        "init_containers": [_container_config(container) for container in (pod.spec.init_containers or [])],
        "volumes": [volume.name for volume in (pod.spec.volumes or [])],
    }


def _statefulset_config(sts: Any) -> dict[str, Any]:
    template = sts.spec.template
    return {
        "name": sts.metadata.name,
        "uid": sts.metadata.uid,
        "replicas": sts.spec.replicas,
        "service_name": sts.spec.service_name,
        "update_strategy": getattr(sts.spec.update_strategy, "type", None),
        "containers": [_container_config(container) for container in (template.spec.containers or [])],
        "init_containers": [_container_config(container) for container in (template.spec.init_containers or [])],
    }


def _pod_status(pod: Any) -> dict[str, Any]:
    return {
        "name": pod.metadata.name,
        "uid": pod.metadata.uid,
        "phase": pod.status.phase,
        "pod_ip": pod.status.pod_ip,
        "ready": all(status.ready for status in (pod.status.container_statuses or [])),
        "containers": [
            {"name": status.name, "image": status.image, "ready": status.ready, "restart_count": status.restart_count}
            for status in (pod.status.container_statuses or [])
        ],
    }


def _pod_rollout(pod: Any, desired_image: str | None) -> dict[str, Any]:
    status = _pod_status(pod)
    status["desired_image"] = desired_image
    status["image_matches"] = (
        all(container["image"] == desired_image for container in status["containers"])
        if desired_image
        else None
    )
    return status


def _statefulset_status(sts: Any) -> dict[str, Any]:
    return {
        "name": sts.metadata.name,
        "uid": sts.metadata.uid,
        "generation": sts.metadata.generation,
        "observed_generation": sts.status.observed_generation,
        "replicas": sts.spec.replicas,
        "ready_replicas": sts.status.ready_replicas or 0,
        "current_replicas": sts.status.current_replicas or 0,
        "updated_replicas": sts.status.updated_replicas or 0,
        "current_revision": sts.status.current_revision,
        "update_revision": sts.status.update_revision,
    }
