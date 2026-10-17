import json
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
    if get_config("whale:docker_use_ssl", False):
        tls_config = docker.tls.TLSConfig(
            verify=True,
            ca_cert=get_config("whale:docker_ssl_ca_cert") or None,
            client_cert=(
                get_config("whale:docker_ssl_client_cert"),
                get_config("whale:docker_ssl_client_key")
            ),
        )
        return docker.DockerClient(
            base_url=get_config("whale:docker_api_url"),
            tls=tls_config,
        )
    else:
        return docker.DockerClient(base_url=get_config("whale:docker_api_url"))


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
        if container.challenge.docker_image.startswith("{"):
            DockerUtils._create_grouped_container(DockerUtils.client, container)
        else:
            DockerUtils._create_standalone_container(DockerUtils.client, container)

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
        range_prefix = CacheProvider(app=current_app).get_available_network_range()

        ipam_pool = docker.types.IPAMPool(subnet=range_prefix)
        ipam_config = docker.types.IPAMConfig(
            driver='default', pool_configs=[ipam_pool])
        network_name = f'{container.user_id}-{container.uuid}'
        network = client.networks.create(
            network_name, internal=True,
            ipam=ipam_config, attachable=True,
            labels={'prefix': range_prefix},
            driver="overlay", scope="swarm"
        )

        dns = []
        containers = get_config("whale:docker_auto_connect_containers", "").split(",")
        for c in containers:
            if not c:
                continue
            network.connect(c)
            if "dns" in c:
                network.reload()
                for name in network.attrs['Containers']:
                    if network.attrs['Containers'][name]['Name'] == c:
                        dns.append(network.attrs['Containers'][name]['IPv4Address'].split('/')[0])

        has_processed_main = False
        try:
            images = json.loads(
                container.challenge.docker_image,
                object_pairs_hook=OrderedDict
            )
        except json.JSONDecodeError:
            raise WhaleError(
                "Challenge Image Parse Error\n"
                "plase check the challenge image string"
            )
        for name, image in images.items():
            if has_processed_main:
                container_name = f'{container.user_id}-{uuid.uuid4()}'
            else:
                container_name = f'{container.user_id}-{container.uuid}'
                node = DockerUtils.choose_node(image, get_config("whale:docker_swarm_nodes", "").split(","))
                has_processed_main = True
            client.services.create(
                image=image, name=container_name, networks=[
                    docker.types.NetworkAttachmentConfig(network_name, aliases=[name])
                ],
                env={'FLAG': container.flag},
                dns_config=docker.types.DNSConfig(nameservers=dns),
                resources=docker.types.Resources(
                    mem_limit=DockerUtils.convert_readable_text(
                        container.challenge.memory_limit
                    ),
                    cpu_limit=int(container.challenge.cpu_limit * 1e9)),
                labels={
                    'whale_id': f'{container.user_id}-{container.uuid}'
                },  # for container deletion
                hostname=name, constraints=['node.labels.name==' + node],
                endpoint_spec=docker.types.EndpointSpec(mode='dnsrr', ports={})
            )

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
                for container in auto_containers:
                    try:
                        network.disconnect(container, force=True)
                    except Exception:
                        pass
                redis_util.add_available_network_range(network.attrs['Labels']['prefix'])
                network.remove()

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
                    per_service_states.append("starting")
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
            any_unknown = any(state == "unknown" for state in per_service_states)
            has_missing_services = found < expected

            if any_unknown:
                state = "unknown"
                detail = "At least one Swarm service did not return a recognizable task state."
            elif any_error or any_degraded:
                state = (
                    "degraded"
                    if any_running or any_starting or any_degraded or has_missing_services
                    else "error"
                )
                detail = "One or more Swarm services have failed tasks or are not yet available."
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
