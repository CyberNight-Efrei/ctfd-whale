"""Read-only monitoring of Docker Swarm workers and FRP workloads."""

from CTFd.utils import get_config

from .docker import DockerUtils, get_docker_client


class DockerClusterMonitor:
    """Collect FRP and worker state from the Swarm manager Docker API."""

    TRANSITIONAL = {
        "new", "allocated", "pending", "assigned", "accepted",
        "preparing", "ready", "starting",
    }
    FAILURES = {"failed", "rejected", "orphaned"}

    @staticmethod
    def _service_kind(service):
        attrs = getattr(service, "attrs", {}) or {}
        spec = attrs.get("Spec", {}) or {}
        task_template = spec.get("TaskTemplate", {}) or {}
        container_spec = task_template.get("ContainerSpec", {}) or {}
        name = spec.get("Name") or getattr(service, "name", "") or ""
        image = container_spec.get("Image") or ""
        text = (str(name) + " " + str(image)).lower()
        if "frpc" in text:
            return "frpc"
        if "frps" in text:
            return "frps"
        return None

    @staticmethod
    def _container_kind(container):
        attrs = getattr(container, "attrs", {}) or {}
        config = attrs.get("Config", {}) or {}
        name = getattr(container, "name", "") or attrs.get("Name", "")
        image = config.get("Image") or ""
        text = (str(name) + " " + str(image)).lower()
        if "frpc" in text:
            return "frpc"
        if "frps" in text:
            return "frps"
        return None

    @staticmethod
    def _decode_logs(output):
        if output is None:
            return ""
        if isinstance(output, bytes):
            return output.decode("utf-8", errors="replace")
        if isinstance(output, str):
            return output
        chunks = list(output)
        return "".join(
            item.decode("utf-8", errors="replace")
            if isinstance(item, bytes) else str(item)
            for item in chunks
        )

    @classmethod
    def _service_summary(cls, service, tasks):
        attrs = getattr(service, "attrs", {}) or {}
        spec = attrs.get("Spec", {}) or {}
        mode = spec.get("Mode", {}) or {}
        replicated = mode.get("Replicated", {}) or {}
        desired = replicated.get("Replicas")
        service_status = attrs.get("ServiceStatus", {}) or {}
        if desired is None:
            desired = service_status.get("DesiredTasks")

        desired_tasks = [
            task for task in tasks
            if str(task.get("DesiredState", "")).lower() == "running"
        ]
        states = [
            str((task.get("Status") or {}).get("State", "")).lower()
            for task in desired_tasks
        ]
        running = sum(1 for state in states if state == "running")
        failures = sum(1 for state in states if state in cls.FAILURES)
        starting = sum(1 for state in states if state in cls.TRANSITIONAL)

        if desired == 0:
            state = "stopped"
        elif desired is not None and running >= desired and desired > 0:
            state = "running"
        elif desired is None and running and not failures:
            state = "running"
        elif running and (failures or (desired is not None and running < desired)):
            state = "degraded"
        elif starting or not states:
            state = "starting"
        elif failures:
            state = "error"
        else:
            state = "unknown"

        last_error = next(
            (
                str((task.get("Status") or {}).get("Err"))
                for task in reversed(desired_tasks)
                if (task.get("Status") or {}).get("Err")
            ),
            "",
        )
        name = spec.get("Name") or getattr(service, "name", "") or "Docker service"
        container_spec = (
            (spec.get("TaskTemplate") or {}).get("ContainerSpec") or {}
        )
        return {
            "id": getattr(service, "id", None) or attrs.get("ID"),
            "source": "service",
            "name": str(name),
            "kind": cls._service_kind(service),
            "image": str(container_spec.get("Image") or ""),
            "mode": "global" if "Global" in mode else "replicated",
            "state": state,
            "running": running,
            "desired": desired,
            "last_error": last_error[:300],
        }

    @staticmethod
    def _container_summary(container):
        attrs = getattr(container, "attrs", {}) or {}
        state_attrs = attrs.get("State", {}) or {}
        config = attrs.get("Config", {}) or {}
        name = getattr(container, "name", "") or attrs.get("Name", "")
        if str(name).startswith("/"):
            name = str(name)[1:]
        is_running = bool(state_attrs.get("Running"))
        health = (state_attrs.get("Health") or {}).get("Status")
        if is_running and health in (None, "healthy"):
            state = "running"
        elif is_running:
            state = "degraded"
        else:
            state = "error" if state_attrs.get("Status") == "exited" else "stopped"
        return {
            "id": getattr(container, "id", None) or attrs.get("Id"),
            "source": "container",
            "name": str(name or "Docker container"),
            "kind": DockerClusterMonitor._container_kind(container),
            "image": str(config.get("Image") or ""),
            "mode": "standalone",
            "state": state,
            "running": 1 if is_running else 0,
            "desired": 1,
            "last_error": str(state_attrs.get("Error") or "")[:300],
        }

    @classmethod
    def snapshot(cls):
        """Return a serializable snapshot; all failures remain visible to admins."""
        client = getattr(DockerUtils, "client", None)
        try:
            client = client or get_docker_client()
            nodes = client.nodes.list()
            services = client.services.list()
            tasks = client.api.tasks()
        except Exception as exc:
            return {
                "available": False,
                "error": "Unable to query the Docker Swarm manager: " + str(exc),
                "workers": [],
                "frpc_entries": [],
                "frps_entries": [],
                "frpc_services": [],
                "summary": {"workers": 0, "online": 0, "frps": "unknown"},
            }

        tasks_by_service = {}
        tasks_by_node = {}
        for task in tasks:
            service_id = task.get("ServiceID")
            node_id = task.get("NodeID")
            if service_id:
                tasks_by_service.setdefault(service_id, []).append(task)
            if node_id:
                tasks_by_node.setdefault(node_id, []).append(task)

        frpc_services = []
        frps_entries = []
        for service in services:
            kind = cls._service_kind(service)
            if not kind:
                continue
            service_id = getattr(service, "id", None) or (
                (getattr(service, "attrs", {}) or {}).get("ID")
            )
            summary = cls._service_summary(
                service, tasks_by_service.get(service_id, [])
            )
            if not summary["id"]:
                continue
            if kind == "frpc":
                frpc_services.append(summary)
            else:
                frps_entries.append(summary)

        # The local daemon may also host standalone FRP containers (for example
        # the FRPS container beside CTFd). Swarm task containers are represented
        # by their service above and are skipped here to avoid duplicate cards.
        try:
            local_containers = client.containers.list(all=True)
        except Exception:
            local_containers = []
        local_frpc = []
        for container in local_containers:
            kind = cls._container_kind(container)
            if not kind:
                continue
            attrs = getattr(container, "attrs", {}) or {}
            labels = (attrs.get("Config", {}) or {}).get("Labels") or {}
            if labels.get("com.docker.swarm.service.id"):
                continue
            summary = cls._container_summary(container)
            if not summary["id"]:
                continue
            if kind == "frpc":
                local_frpc.append(summary)
            else:
                frps_entries.append(summary)

        # Include local FRPC containers in the list of services with log access,
        # while only Swarm FRPC services are used to assess worker assignments.
        frpc_entries = frpc_services + local_frpc

        workers = []
        # Worker names are an explicit infrastructure convention in this
        # deployment. Swarm placement constraints cannot use hostname globs,
        # so the same prefix is used by the FRPC label synchronizer.
        worker_hostname_prefix = str(
            get_config("whale:frp_worker_hostname_prefix", "worker") or "worker"
        ).lower()
        frpc_service_ids = {
            item["id"] for item in frpc_services if item.get("id")
        }
        has_global_frpc = any(
            item.get("mode") == "global" for item in frpc_services
        )
        for node in nodes:
            attrs = getattr(node, "attrs", {}) or {}
            spec = attrs.get("Spec", {}) or {}
            if spec.get("Role", "worker") != "worker":
                continue

            node_id = getattr(node, "id", None) or attrs.get("ID")
            description = attrs.get("Description", {}) or {}
            node_status = attrs.get("Status", {}) or {}
            state = str(node_status.get("State") or "unknown").lower()
            availability = str(spec.get("Availability") or "active").lower()
            hostname = description.get("Hostname") or getattr(node, "hostname", None) or node_id
            if not str(hostname).lower().startswith(worker_hostname_prefix):
                continue
            node_labels = spec.get("Labels") or {}
            node_label_text = ", ".join(
                f"{key}={value}" for key, value in sorted(node_labels.items())
            )
            node_tasks = [
                task for task in tasks_by_node.get(node_id, [])
                if task.get("ServiceID") in frpc_service_ids
                and str(task.get("DesiredState", "")).lower() == "running"
            ]
            task_states = [
                str((task.get("Status") or {}).get("State", "")).lower()
                for task in node_tasks
            ]
            running_tasks = [
                task for task in node_tasks
                if str((task.get("Status") or {}).get("State", "")).lower() == "running"
            ]
            if not frpc_services:
                frpc_state = "not_deployed"
                frpc_label = "Aucun agent FRPC déployé sur ce nœud"
            elif running_tasks:
                frpc_state = "running"
                frpc_label = "FRPC actif"
            elif any(value in cls.TRANSITIONAL for value in task_states):
                frpc_state = "starting"
                frpc_label = "FRPC en démarrage"
            elif any(value in cls.FAILURES for value in task_states):
                frpc_state = "error"
                frpc_label = "FRPC en erreur"
            elif has_global_frpc:
                frpc_state = "missing"
                frpc_label = "Tâche FRPC absente"
            else:
                frpc_state = "unassigned"
                frpc_label = "Aucune tâche FRPC affectée"

            # Swarm membership and FRPC process health are separate signals.
            # A worker remains connected when its node is Ready/Active even if
            # FRPC is a singleton service elsewhere in the cluster.
            if state != "ready":
                overall = "offline"
            elif availability != "active":
                overall = "paused"
            else:
                overall = "online"

            last_task_error = next(
                (
                    str((task.get("Status") or {}).get("Err"))
                    for task in reversed(node_tasks)
                    if (task.get("Status") or {}).get("Err")
                ),
                "",
            )
            workers.append({
                "id": node_id,
                "name": str(hostname),
                "state": state,
                "availability": availability,
                "node_labels": node_label_text,
                "frpc_state": frpc_state,
                "frpc_label": frpc_label,
                "status": overall,
                "status_label": {
                    "online": "Connecté au Swarm",
                    "offline": "Hors ligne",
                    "paused": "En pause / drain",
                    "unknown": "État Swarm inconnu",
                }.get(overall, "État inconnu"),
                "task_error": last_task_error[:300],
                "frpc_task_count": len(node_tasks),
            })

        workers.sort(key=lambda item: item["name"].lower())
        frpc_entries.sort(key=lambda item: item["name"].lower())
        frps_entries.sort(key=lambda item: item["name"].lower())

        online = sum(1 for worker in workers if worker["status"] == "online")
        if frps_entries and all(item["state"] == "running" for item in frps_entries):
            frps_state = "running"
        elif any(item["state"] in ("running", "degraded", "starting") for item in frps_entries):
            frps_state = "degraded"
        elif frps_entries:
            frps_state = "error"
        else:
            frps_state = "missing"
        return {
            "available": True,
            "error": "",
            "workers": workers,
            "frpc_entries": frpc_entries,
            "frps_entries": frps_entries,
            "frpc_services": frpc_services,
            "summary": {
                "workers": len(workers),
                "online": online,
                "frps": frps_state,
            },
        }

    @staticmethod
    def _node_identity(node):
        attrs = getattr(node, "attrs", {}) or {}
        description = attrs.get("Description", {}) or {}
        status = attrs.get("Status", {}) or {}
        spec = attrs.get("Spec", {}) or {}
        hostname = description.get("Hostname") or getattr(node, "hostname", None)
        node_id = getattr(node, "id", None) or attrs.get("ID")
        return node_id, str(hostname or node_id or "unknown"), str(status.get("Addr") or ""), spec

    @staticmethod
    def _published_admin_port(service, target_port):
        attrs = getattr(service, "attrs", {}) or {}
        spec = attrs.get("Spec", {}) or {}
        endpoint = spec.get("EndpointSpec", {}) or {}
        for published in endpoint.get("Ports", []) or []:
            try:
                target = int(published.get("TargetPort") or 0)
            except (TypeError, ValueError):
                continue
            if target != int(target_port):
                continue
            publish_mode = str(published.get("PublishMode") or "ingress").lower()
            if publish_mode != "host":
                raise ValueError(
                    "The global FRPC admin port must be published with mode=host "
                    "so CTFd reaches the agent on the matching worker."
                )
            port = published.get("PublishedPort")
            if port:
                return int(port)
        raise ValueError(
            "The global FRPC service must publish its admin port on the worker "
            f"(target port {target_port}, publish mode host)."
        )

    @classmethod
    def get_global_frpc_worker_targets(cls, admin_port=7400):
        """Return worker -> FRPC admin URLs when a global FRPC service exists.

        None means the deployment still uses the legacy single FRPC endpoint.
        An empty mapping means a global service exists but no worker agent is
        currently running.
        """
        client = getattr(DockerUtils, "client", None) or get_docker_client()
        nodes = client.nodes.list()
        node_by_id = {}
        prefix = str(
            get_config("whale:frp_worker_hostname_prefix", "worker") or "worker"
        ).lower()
        for node in nodes:
            node_id, hostname, address, spec = cls._node_identity(node)
            if not node_id or not hostname.lower().startswith(prefix):
                continue
            if str(spec.get("Role", "worker")).lower() != "worker":
                continue
            node_by_id[node_id] = {
                "node_id": node_id,
                "hostname": hostname,
                "address": address,
            }

        global_services = []
        for service in client.services.list():
            if cls._service_kind(service) != "frpc":
                continue
            attrs = getattr(service, "attrs", {}) or {}
            spec = attrs.get("Spec", {}) or {}
            mode = spec.get("Mode", {}) or {}
            if "Global" in mode:
                service_id = getattr(service, "id", None) or attrs.get("ID")
                if service_id:
                    published_port = cls._published_admin_port(service, admin_port)
                    global_services.append((service_id, published_port, spec.get("Name") or "FRPC"))

        if not global_services:
            return None

        service_ports = {
            service_id: (published_port, service_name)
            for service_id, published_port, service_name in global_services
        }
        tasks = client.api.tasks(
            filters={"service": list(service_ports.keys())}
        )
        targets = {}
        for task in tasks:
            service_id = task.get("ServiceID")
            node_id = task.get("NodeID")
            if service_id not in service_ports or node_id not in node_by_id:
                continue
            if str(task.get("DesiredState", "")).lower() != "running":
                continue
            if str((task.get("Status") or {}).get("State", "")).lower() != "running":
                continue
            node = node_by_id[node_id]
            address = node["address"]
            if not address:
                continue
            host = f"[{address}]" if ":" in address else address
            published_port, service_name = service_ports[service_id]
            targets[node_id] = {
                **node,
                "service_id": service_id,
                "service_name": str(service_name),
                "url": f"http://{host}:{published_port}",
            }
        return targets

    @classmethod
    def get_instance_node_assignments(cls, containers):
        """Resolve each Whale instance to the node running its primary service.

        The persisted node value is a placement label (not a hostname), so live
        Swarm task placement is preferred and the label is only a fallback while
        a task is being created or restarted.
        """
        if not containers:
            return {}

        client = getattr(DockerUtils, "client", None) or get_docker_client()
        node_ids_by_label = {}
        for node in client.nodes.list():
            node_id, _hostname, _address, spec = cls._node_identity(node)
            label = str((spec.get("Labels") or {}).get("name") or "").strip()
            if node_id and label:
                node_ids_by_label[label] = node_id

        whale_ids = {
            f"{container.user_id}-{container.uuid}": container
            for container in containers
        }
        primary_services = {}
        for service in client.services.list():
            attrs = getattr(service, "attrs", {}) or {}
            spec = attrs.get("Spec", {}) or {}
            labels = spec.get("Labels") or {}
            whale_id = labels.get("whale_id")
            service_name = spec.get("Name") or getattr(service, "name", None)
            service_id = getattr(service, "id", None) or attrs.get("ID")
            # For grouped challenges, only the primary service is constrained
            # to the selected challenge worker and owns the player-facing ports.
            if whale_id in whale_ids and service_name == whale_id and service_id:
                primary_services[service_id] = whale_id

        task_candidates = {}
        if primary_services:
            tasks = client.api.tasks(
                filters={"service": list(primary_services.keys())}
            )
            for task in tasks:
                whale_id = primary_services.get(task.get("ServiceID"))
                node_id = task.get("NodeID")
                if not whale_id or not node_id:
                    continue
                if str(task.get("DesiredState", "")).lower() != "running":
                    continue
                state = str((task.get("Status") or {}).get("State", "")).lower()
                if state == "running":
                    priority = 2
                elif state in cls.TRANSITIONAL:
                    priority = 1
                else:
                    continue
                updated = str(
                    task.get("UpdatedAt")
                    or task.get("CreatedAt")
                    or ""
                )
                candidate = (priority, updated, node_id)
                if whale_id not in task_candidates or candidate > task_candidates[whale_id]:
                    task_candidates[whale_id] = candidate

        assignments = {}
        for whale_id, container in whale_ids.items():
            candidate = task_candidates.get(whale_id)
            if candidate:
                assignments[whale_id] = candidate[2]
            else:
                label = str(getattr(container, "node", "") or "").strip()
                if label in node_ids_by_label:
                    assignments[whale_id] = node_ids_by_label[label]
        return assignments

    @classmethod
    def get_logs(cls, source, source_id, tail=200):
        """Read logs from one FRP Swarm service or local standalone container."""
        client = getattr(DockerUtils, "client", None) or get_docker_client()
        if source == "service":
            for service in client.services.list():
                service_id = getattr(service, "id", None) or (
                    (getattr(service, "attrs", {}) or {}).get("ID")
                )
                if service_id != source_id or not cls._service_kind(service):
                    continue
                output = service.logs(
                    stdout=True, stderr=True, timestamps=True,
                    tail=tail, follow=False,
                )
                return cls._decode_logs(output)
        elif source == "container":
            for container in client.containers.list(all=True):
                container_id = getattr(container, "id", None)
                if container_id != source_id or not cls._container_kind(container):
                    continue
                output = container.logs(
                    stdout=True, stderr=True, timestamps=True,
                    tail=tail, follow=False,
                )
                return cls._decode_logs(output)
        else:
            raise ValueError("Unknown Docker log source.")

        raise LookupError("FRPC/FRPS Docker service or container not found.")
