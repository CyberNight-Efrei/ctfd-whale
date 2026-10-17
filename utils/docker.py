import json
import logging
import random
import uuid
from collections import OrderedDict

import docker
from flask import current_app

from CTFd.utils import get_config

from .cache import CacheProvider
from .exceptions import WhaleError
from .db import DBContainer


def get_docker_client():
    docker_api_url = get_config(
        "whale:docker_api_url", "unix:///var/run/docker.sock"
    )
    docker_use_ssl = get_config("whale:docker_use_ssl", False)
    if isinstance(docker_use_ssl, str):
        docker_use_ssl = docker_use_ssl.strip().lower() in ("1", "true", "yes", "on")

    if docker_use_ssl:
        tls_config = docker.tls.TLSConfig(
            verify=True,
            ca_cert=get_config("whale:docker_ssl_ca_cert") or None,
            client_cert=(
                get_config("whale:docker_ssl_client_cert"),
                get_config("whale:docker_ssl_client_key")
            ),
        )
        return docker.DockerClient(
            base_url=docker_api_url,
            tls=tls_config,
        )
    else:
        return docker.DockerClient(base_url=docker_api_url)


class DockerUtils:
    @staticmethod
    def init():
        try:
            DockerUtils.client = get_docker_client()
            # docker-py is thread safe: https://github.com/docker/docker-py/issues/619
        except Exception:
            raise WhaleError(
                'Docker Connection Error\n'
                'Please ensure the docker api url (first config item) is correct\n'
                'if you are using unix:///var/run/docker.sock, check if the socket is correctly mapped'
            )
        credentials = get_config("whale:docker_credentials")
        if credentials and credentials.count(':') == 1:
            try:
                DockerUtils.client.login(*credentials.split(':'))
            except Exception:
                raise WhaleError('docker.io failed to login, check your credentials')

    @staticmethod
    def add_container(container):
        try:
            if container.challenge.docker_image.lstrip().startswith("{"):
                DockerUtils._create_grouped_container(DockerUtils.client, container)
            else:
                DockerUtils._create_standalone_container(DockerUtils.client, container)
        except Exception:
            # Service creation can partially succeed (for example, when saving
            # the selected node fails after Docker has created the service).
            # Best-effort cleanup here is repeated by the lifecycle coordinator
            # if it cannot confirm that all resources were removed.
            try:
                DockerUtils.remove_container(container)
            except Exception:
                logging.exception(
                    "Docker cleanup after failed creation also failed for Whale instance %s-%s",
                    container.user_id,
                    container.uuid,
                )
            raise

    @staticmethod
    def _create_standalone_container(client, container):
        dns = get_config("whale:docker_dns", "").split(",")
        node = DockerUtils.choose_node(
            container.challenge.docker_image,
            get_config("whale:docker_swarm_nodes", "").split(",")
        )

        client.services.create(
            image=container.challenge.docker_image,
            name=f'{container.user_id}-{container.uuid}',
            env={'DYNAMIC_FLAG': container.flag}, dns_config=docker.types.DNSConfig(nameservers=dns),
            networks=[get_config("whale:docker_auto_connect_network", "ctfd_frp-containers")],
            resources=docker.types.Resources(
                mem_limit=DockerUtils.convert_readable_text(
                    container.challenge.memory_limit),
                cpu_limit=int(container.challenge.cpu_limit * 1e9)
            ),
            labels={
                'whale_id': f'{container.user_id}-{container.uuid}'
            },  # for container deletion
            constraints=['node.labels.name==' + node],
            endpoint_spec=docker.types.EndpointSpec(mode='dnsrr', ports={})
        )

        DBContainer.add_container_node(container, node)

    @staticmethod
    def _create_grouped_container(client, container):
        try:
            images = json.loads(
                container.challenge.docker_image,
                object_pairs_hook=OrderedDict,
            )
        except (TypeError, json.JSONDecodeError) as exc:
            raise WhaleError(
                "Challenge Image Parse Error\n"
                "Please check the challenge image JSON."
            ) from exc
        if not isinstance(images, dict) or not images:
            raise WhaleError("Grouped challenge images must be a non-empty JSON object.")

        cache_provider = CacheProvider(app=current_app)
        range_prefix = cache_provider.get_available_network_range()
        if not range_prefix:
            raise WhaleError(
                "No Docker subnet is available for a grouped challenge. "
                "Check the configured subnet range and existing Swarm networks."
            )

        whale_id = f"{container.user_id}-{container.uuid}"
        network_name = whale_id
        try:
            ipam_pool = docker.types.IPAMPool(subnet=range_prefix)
            ipam_config = docker.types.IPAMConfig(
                driver="default", pool_configs=[ipam_pool]
            )
            network = client.networks.create(
                network_name,
                internal=True,
                ipam=ipam_config,
                attachable=True,
                labels={"prefix": range_prefix, "whale_id": whale_id},
                driver="overlay",
                scope="swarm",
            )

            dns = []
            containers = get_config("whale:docker_auto_connect_containers", "").split(",")
            for external_container in containers:
                if not external_container:
                    continue
                network.connect(external_container)
                if "dns" in external_container:
                    network.reload()
                    for _, details in (network.attrs.get("Containers") or {}).items():
                        if details.get("Name") == external_container:
                            address = details.get("IPv4Address", "")
                            if address:
                                dns.append(address.split("/")[0])

            has_processed_main = False
            for name, image in images.items():
                if has_processed_main:
                    service_name = f"{container.user_id}-{uuid.uuid4()}"
                else:
                    service_name = whale_id
                    node = DockerUtils.choose_node(
                        image,
                        get_config("whale:docker_swarm_nodes", "").split(","),
                    )
                    has_processed_main = True

                client.services.create(
                    image=image,
                    name=service_name,
                    networks=[
                        docker.types.NetworkAttachmentConfig(
                            network_name, aliases=[name]
                        )
                    ],
                    env={"FLAG": container.flag},
                    dns_config=docker.types.DNSConfig(nameservers=dns),
                    resources=docker.types.Resources(
                        mem_limit=DockerUtils.convert_readable_text(
                            container.challenge.memory_limit
                        ),
                        cpu_limit=int(container.challenge.cpu_limit * 1e9),
                    ),
                    labels={"whale_id": whale_id},
                    hostname=name,
                    constraints=["node.labels.name==" + node],
                    endpoint_spec=docker.types.EndpointSpec(mode="dnsrr", ports={}),
                )

            # Keep the subnet reserved until the network and every service have
            # been created. The network label then becomes the durable record.
            cache_provider.commit_network_reservation(range_prefix)
        except Exception:
            cleanup_complete = True
            try:
                services = client.services.list(filters={"label": f"whale_id={whale_id}"})
                for service in services:
                    try:
                        service.remove()
                    except Exception:
                        cleanup_complete = False
                        logging.exception(
                            "Failed removing partially created Whale service %s",
                            getattr(service, "name", getattr(service, "id", "unknown")),
                        )
            except Exception:
                cleanup_complete = False
                logging.exception("Unable to enumerate partially created Whale services")

            try:
                networks = client.networks.list(names=[network_name])
                for partial_network in networks:
                    try:
                        partial_network.remove()
                    except Exception:
                        cleanup_complete = False
                        logging.exception(
                            "Failed removing partially created Whale network %s",
                            network_name,
                        )
                remaining = client.networks.list(names=[network_name])
                if remaining:
                    cleanup_complete = False
            except Exception:
                cleanup_complete = False
                logging.exception("Unable to verify cleanup of Whale network %s", network_name)

            if cleanup_complete:
                try:
                    cache_provider.add_available_network_range(range_prefix)
                except Exception:
                    logging.exception("Failed returning subnet %s to the pool", range_prefix)
            # If cleanup could not be confirmed, keep the allocation reserved.
            # Its lease expires eventually; reconciliation also checks network
            # labels before making a subnet available again.
            raise

    @staticmethod
    def remove_container(container):
        whale_id = f'{container.user_id}-{container.uuid}'

        for s in DockerUtils.client.services.list(filters={'label': f'whale_id={whale_id}'}):
            s.remove()

        networks = DockerUtils.client.networks.list(names=[whale_id])
        if len(networks) > 0:  # is grouped containers
            auto_containers = get_config("whale:docker_auto_connect_containers", "").split(",")
            redis_util = CacheProvider(app=current_app)
            for network in networks:
                for external_container in auto_containers:
                    if not external_container:
                        continue
                    try:
                        network.disconnect(external_container, force=True)
                    except Exception:
                        # The container may already have been disconnected.
                        pass
                prefix = (network.attrs.get("Labels") or {}).get("prefix")
                network.remove()
                # Never put a subnet back into the pool before Docker confirms
                # that its network has been removed.
                if prefix:
                    redis_util.add_available_network_range(prefix)

    @staticmethod
    def get_instance_statuses(containers):
        """Read Docker Swarm task state for instances shown in the admin UI.

        This method is read-only. It never changes a service, task, database
        record, or instance lifecycle. Status is based on Swarm task state,
        not on application-level health checks.
        """
        if not containers:
            return {}

        def status(state, detail, services_found=0, services_expected=1):
            labels = {
                "running": ("Running", "success"),
                "starting": ("Starting", "info"),
                "stopped": ("Stopped", "secondary"),
                "degraded": ("Degraded", "warning"),
                "error": ("Error", "danger"),
                "missing": ("Service missing", "danger"),
                "unknown": ("Unknown", "secondary"),
            }
            label, style = labels[state]
            return {
                "state": state,
                "label": label,
                "style": style,
                "detail": detail,
                "services_found": services_found,
                "services_expected": services_expected,
            }

        def expected_service_count(container):
            image_spec = container.challenge.docker_image or ""
            if isinstance(image_spec, str) and image_spec.lstrip().startswith("{"):
                try:
                    grouped_images = json.loads(image_spec)
                    if isinstance(grouped_images, dict) and grouped_images:
                        return len(grouped_images)
                except (TypeError, ValueError):
                    pass
            return 1

        instance_by_label = {
            f"{container.user_id}-{container.uuid}": container
            for container in containers
        }
        expected_counts = {
            label: expected_service_count(container)
            for label, container in instance_by_label.items()
        }

        unknown_detail = (
            "Docker Swarm could not be queried. The instance status is unknown."
        )
        unknown_statuses = {
            container.id: status("unknown", unknown_detail, 0, expected_counts[label])
            for label, container in instance_by_label.items()
        }

        try:
            client = getattr(DockerUtils, "client", None) or get_docker_client()
            # Services created by Whale carry this label. Filter on the label
            # key first, then match exact instance IDs locally.
            services = client.services.list(filters={"label": "whale_id"})
        except Exception:
            return unknown_statuses

        services_by_label = {label: [] for label in instance_by_label}
        service_by_id = {}
        service_objects = {}
        for service in services:
            attrs = getattr(service, "attrs", {}) or {}
            spec = attrs.get("Spec", {}) or {}
            labels = spec.get("Labels") or {}
            instance_label = labels.get("whale_id")
            if instance_label not in instance_by_label:
                continue
            service_id = getattr(service, "id", None) or attrs.get("ID")
            if not service_id:
                continue
            services_by_label[instance_label].append(service_id)
            service_by_id[service_id] = instance_label
            service_objects[service_id] = service

        relevant_service_ids = list(service_by_id)
        tasks_by_service = {service_id: [] for service_id in relevant_service_ids}
        if relevant_service_ids:
            try:
                # One batched Swarm API request is more efficient than making a
                # request for every service when the page contains many instances.
                tasks = client.api.tasks(filters={"service": relevant_service_ids})
            except Exception:
                return unknown_statuses
            for task in tasks:
                service_id = task.get("ServiceID")
                if service_id in tasks_by_service:
                    tasks_by_service[service_id].append(task)

        result = {}
        transitional_states = {
            "new", "allocated", "pending", "assigned", "accepted",
            "preparing", "ready", "starting",
        }
        failure_states = {"failed", "rejected", "orphaned"}
        for label, container in instance_by_label.items():
            service_ids = services_by_label.get(label, [])
            expected = expected_counts[label]
            found = len(service_ids)

            if found == 0:
                result[container.id] = status(
                    "missing",
                    "No Docker Swarm service with this instance's Whale label was found.",
                    0,
                    expected,
                )
                continue

            per_service_states = []
            for service_id in service_ids:
                service = service_objects[service_id]
                service_attrs = getattr(service, "attrs", {}) or {}
                service_spec = service_attrs.get("Spec", {}) or {}
                service_mode = service_spec.get("Mode", {}) or {}
                replicated = service_mode.get("Replicated", {}) or {}
                replicas = replicated.get("Replicas")
                if replicas == 0:
                    per_service_states.append("stopped")
                    continue

                service_tasks = tasks_by_service.get(service_id, [])
                desired_running = [
                    task for task in service_tasks
                    if str(task.get("DesiredState", "")).lower() == "running"
                ]
                task_states = [
                    str((task.get("Status") or {}).get("State", "")).lower()
                    for task in desired_running
                ]

                if not task_states:
                    # A new service can briefly exist before Swarm creates its
                    # first task. If tasks exist but none is desired-running,
                    # don't guess whether the service is starting or failed.
                    per_service_states.append("starting" if not service_tasks else "unknown")
                elif any(state in failure_states for state in task_states):
                    if any(state == "running" for state in task_states):
                        per_service_states.append("degraded")
                    else:
                        per_service_states.append("error")
                elif any(state in transitional_states for state in task_states):
                    per_service_states.append("starting")
                elif all(state == "running" for state in task_states):
                    per_service_states.append("running")
                else:
                    per_service_states.append("unknown")

            any_running = any(state == "running" for state in per_service_states)
            any_starting = any(state == "starting" for state in per_service_states)
            any_degraded = any(state == "degraded" for state in per_service_states)
            any_error = any(state == "error" for state in per_service_states)
            any_stopped = any(state == "stopped" for state in per_service_states)
            any_unknown = any(state == "unknown" for state in per_service_states)
            has_missing_services = found < expected

            if any_unknown:
                state = "unknown"
                detail = "At least one Swarm service has no current desired-running task, so its state cannot be confirmed."
            elif per_service_states and all(item == "stopped" for item in per_service_states) and not has_missing_services:
                state = "stopped"
                detail = "All observed Swarm services are configured with zero desired replicas."
            elif any_error or any_degraded or any_stopped:
                state = (
                    "degraded"
                    if any_running or any_starting or any_degraded or any_stopped
                    else "error"
                )
                detail = "One or more Swarm services have failed tasks, are stopped, or are not yet available."
            elif has_missing_services and not any_starting:
                state = "degraded"
                detail = (
                    f"Found {found} of {expected} expected Swarm services. "
                    "One or more services may be missing."
                )
            elif any_starting:
                state = "starting"
                if has_missing_services:
                    detail = (
                        f"Found {found} of {expected} expected Swarm services; "
                        "one or more services may still be starting."
                    )
                else:
                    detail = "At least one Swarm service is pending or starting."
            elif all(item == "running" for item in per_service_states) and not has_missing_services:
                state = "running"
                detail = f"All {found} expected Swarm service(s) have running tasks."
            elif has_missing_services:
                state = "degraded"
                detail = f"Found {found} of {expected} expected Swarm services."
            else:
                state = "unknown"
                detail = "The instance task state could not be determined."

            result[container.id] = status(state, detail, found, expected)

        return result

    @staticmethod
    def convert_readable_text(text):
        lower_text = text.lower()

        if lower_text.endswith("k"):
            return int(text[:-1]) * 1024

        if lower_text.endswith("m"):
            return int(text[:-1]) * 1024 * 1024

        if lower_text.endswith("g"):
            return int(text[:-1]) * 1024 * 1024 * 1024

        return 0

    @staticmethod
    def choose_node(image, nodes):
        win_nodes = []
        linux_nodes = []
        for node in nodes:
            if node.startswith("windows"):
                win_nodes.append(node)
            else:
                linux_nodes.append(node)
        try:
            tag = image.split(":")[1:]
            if len(tag) and tag[0].startswith("windows"):
                return random.choice(win_nodes)
            return random.choice(linux_nodes)
        except IndexError:
            raise WhaleError(
                'No Suitable Nodes.\n'
                'If you are using Whale for the first time, \n'
                'Please Setup Swarm Nodes Correctly and Lable Them with\n'
                'docker node update --label-add "name=linux-1" $(docker node ls -q)'
            )
