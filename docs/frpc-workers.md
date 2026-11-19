# FRPC on every worker

CTFd-Whale supports two routing modes:

- **Legacy / single agent:** when there is no global FRPC Swarm service, routes are sent to the configured `whale:frp_api_url`.
- **Worker-aware / global agents:** one FRPC task runs on every eligible worker. Whale detects the primary Swarm task for each challenge and sends its routes only to the FRPC agent on the same node.

## Networks

This deployment deliberately has **no dedicated FRP overlay**:

- FRPS uses Docker's built-in `host` network on the VM named `frps`, as in `docker service create --network host`.
- FRPC uses the existing `challenges` overlay. It needs that overlay to resolve and reach the Whale challenge services by their Docker DNS names.
- FRPC connects outward to `frps.cybnsrv.rezo:7000`. The overlay network still has outbound connectivity through the Docker host gateway (`docker_gwbridge)); FRPC does not need to join the host network to reach the VM network.

Do not switch FRPC to `network_mode: host`: it would no longer be attached to the `challenges` overlay used to reach the player containers.

## Select workers

Swarm placement constraints do not support hostname globs such as `node.hostname == worker*`. The helper script labels only worker nodes whose hostname starts with `worker`:

```sh
chmod +x scripts/label-frpc-workers.sh
./scripts/label-frpc-workers.sh
```

It adds the plugin-owned label `ctfd_whale_frpc=true` to matching workers and removes that label from non-matching workers. Run it again after adding, removing, renaming, or reclassifying nodes.

## Deploy on the existing services

FRPS is already correctly deployed on your `frps` VM with `--network host` and `frps.cybnsrv.rezo`. **Keep this service unchanged.** Only FRPC needs to become a global service.

Set the same token you already use, then run on the Swarm manager:

```sh
export FRP_TOKEN='your-existing-token'

# Label all worker* nodes for FRPC placement.
./scripts/label-frpc-workers.sh

# Recreate FRPC in global mode. Its image, command, challenges network
# and host-published admin port remain the same as in your working service.
docker service rm frpc

docker service create \
  --name frpc \
  --mode global \
  --constraint 'node.role==worker' \
  --constraint 'node.labels.ctfd_whale_frpc==true' \
  --network challenges \
  --publish published=7400,target=7400,protocol=tcp,mode=host \
  --restart-condition any \
  frankli0324/frp:frpc \
  --server_addr=frps.cybnsrv.rezo \
  --server_port=7000 \
  --token="$FRP_TOKEN" \
  --admin_addr=0.0.0.0 \
  --admin_port=7400 \
  --admin_user= \
  --admin_pwd=
```

This is intentionally a replacement rather than `docker service update`: Docker's service-update command does not expose a mode switch from replicated to global. The existing replicated service must be removed and recreated with `--mode global`.

Check the result:

```sh
docker service ps frpc
docker service ls
```

There should be one running FRPC task on each worker whose hostname begins with `worker`. FRPS is not recreated and keeps listening on the VM host network.

## Optional clean deployment with a stack file

`docker-stack.frp.example.yml` contains the same host/overlay split for a fresh deployment. It creates stack-prefixed services (`frp_frps` and `frp_frpc`), so do not deploy it on top of your existing `frps` and `frpc` services without removing or renaming those services first.

The stack uses:

- FRPS: Docker `host` network, pinned to `node.hostname == frps`.
- FRPC: the existing external `challenges` overlay, global mode, and host-published TCP/7400.
- FRPC server address: `frps.cybnsrv.rezo:7000`.

For a fresh stack deployment, make sure the local `frps-frps:latest` image exists on the `frps` node, set `FRP_TOKEN`, and use:

```sh
./scripts/label-frpc-workers.sh
docker stack deploy --resolve-image never -c docker-stack.frp.example.yml frp
```

## Firewall and routing

The FRPS service binds directly to the host VM network on TCP/7000 and TCP/8000. Since it uses host networking, there is no Docker port-publishing section for FRPS. Open those ports in the VM firewall, plus the direct TCP port range configured in CTFd-Whale (normally 10000-10100).

FRPC remains on the `challenges` overlay and publishes its admin API on TCP/7400 in `mode: host`. This creates a listener on each worker that runs FRPC. CTFd queries the Docker Swarm API and calls `http://<node.Status.Addr>:7400`; ensure that address is the worker's reachable private VM address and allow TCP/7400 **only from the CTFd manager**. The example FRPC admin API has no authentication configured, so do not expose it publicly.

The `challenges` network must already exist as a Swarm overlay and must be the same network used by Whale's challenge services. Workers must also have outbound connectivity and DNS resolution for `frps.cybnsrv.rezo` on TCP/7000.

## FRPC admin API authentication

When `--admin_user` and `--admin_pwd` are configured on FRPC, enter the same credentials in CTFd-Whale under **Settings → Routing → FRPC Admin Username / Password**. Leave both fields empty when FRPC admin authentication is disabled. Whale uses HTTP Basic Auth for status checks, config reads/writes, and reloads on each worker.

## Configure FRPC admin credentials through CTFd Docker Compose

The CTFd plugin accepts two optional environment variables:

- `CTFD_WHALE_FRP_ADMIN_USER`
- `CTFD_WHALE_FRP_ADMIN_PASSWORD`

Add them to the **CTFd application service**, not only to the FRPC service. Example:

```yaml
services:
  ctfd:
    environment:
      - CTFD_WHALE_FRP_ADMIN_USER=${CTFD_WHALE_FRP_ADMIN_USER:-}
      - CTFD_WHALE_FRP_ADMIN_PASSWORD=${CTFD_WHALE_FRP_ADMIN_PASSWORD:-}
```

Put the values in the Compose `.env` file beside the compose YAML:

```dotenv
CTFD_WHALE_FRP_ADMIN_USER=your-admin-user
CTFD_WHALE_FRP_ADMIN_PASSWORD='your-admin-password'
```

Use the exact same username/password in the FRPC process arguments (`--admin_user` and `--admin_pwd`) on **every worker**. Compose substitutes `.env` variables when it renders the YAML; CTFd then reads the environment values when the FRP router starts.

Environment values take precedence over **Whale → Settings → Routing** values in the database. If a variable is absent, the plugin falls back to the corresponding UI setting. If it is explicitly set to an empty string, the plugin treats that as an intentional empty value. Restart/recreate the CTFd service after changing the variables so its processes receive the new environment.

For a Compose deployment, update the service with:

```sh
docker compose up -d --force-recreate ctfd
```

Do not commit the real `.env` file or publish the FRPC admin password.

## Worker-aware routing

For each routable Whale instance, the plugin resolves the Swarm task of its primary service (`<user_id>-<uuid>`) and reads its current `NodeID`. It sends the complete FRP rule set only to the FRPC agent on that node. The other agents receive configurations without those rules. When an instance is removed, the plugin refreshes each live agent so stale routes are removed.

The monitoring page lists workers whose hostnames start with `worker`, Swarm connectivity, FRPC task state, and FRPS state. Service logs are available from the same page.

If an instance is assigned to a worker without a running FRPC task, route registration fails explicitly instead of sending the route to a different agent. Check the node label, FRPC task state, TCP/7400 reachability, and Docker service logs.

## Rollback

To return to the previous single-agent mode, remove the global FRPC service and deploy one regular FRPC service reachable at `whale:frp_api_url`. When no global FRPC service is detected, Whale automatically keeps using the legacy endpoint.
