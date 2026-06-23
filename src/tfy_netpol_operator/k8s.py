"""Thin Kubernetes client helpers: idempotent apply, delete, and namespace reads."""

from __future__ import annotations

import kubernetes
from kubernetes.client import ApiClient, ApiException, CoreV1Api, NetworkingV1Api

from .policies import MANAGED_BY_SELECTOR

_serializer = ApiClient()


def load_kube_config() -> None:
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()


def _net() -> NetworkingV1Api:
    return NetworkingV1Api()


def _core() -> CoreV1Api:
    return CoreV1Api()


def read_namespace_annotations(namespace: str) -> dict | None:
    """Return the namespace's annotations, or None if the namespace is gone."""
    try:
        ns = _core().read_namespace(namespace)
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise
    return ns.metadata.annotations or {}


def apply_network_policy(body: dict, logger) -> None:
    """Create or update a NetworkPolicy idempotently.

    Skips the write when the live spec already matches the desired spec (prevents
    reconcile loops triggered by the operator's own writes).
    """
    net = _net()
    name = body["metadata"]["name"]
    namespace = body["metadata"]["namespace"]
    try:
        existing = net.read_namespaced_network_policy(name, namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
        net.create_namespaced_network_policy(namespace, body)
        logger.info(f"created networkpolicy {namespace}/{name}")
        return

    existing_spec = _serializer.sanitize_for_serialization(existing.spec)
    if existing_spec == body["spec"]:
        return
    body.setdefault("metadata", {})["resourceVersion"] = existing.metadata.resource_version
    net.replace_namespaced_network_policy(name, namespace, body)
    logger.info(f"updated networkpolicy {namespace}/{name}")


def delete_managed_policies(namespace: str, logger) -> None:
    """Delete every operator-managed NetworkPolicy in the namespace."""
    net = _net()
    try:
        listing = net.list_namespaced_network_policy(
            namespace, label_selector=MANAGED_BY_SELECTOR
        )
    except ApiException as exc:
        if exc.status == 404:
            return
        raise
    for item in listing.items:
        try:
            net.delete_namespaced_network_policy(item.metadata.name, namespace)
            logger.info(f"deleted networkpolicy {namespace}/{item.metadata.name}")
        except ApiException as exc:
            if exc.status != 404:
                raise
