# FRPC on every worker

CTFd-Whale supports two routing modes:

- **Legacy / single agent:** no global FRPC Swarm service is present; all routes are sent to `whale:frp_api_url` as before.
- **Worker-aware / global agents:** a global FRPC service runs on each eligible worker. Whale resolves the primary Swarm task for each challenge and sends that challenge's routes only to the FRPC agent on that node.

## Why the node label is required

Docker Swarm placement constraints support exact hostname matches, not wildcard expressions such as `node.hostname == worker*`. This deployment uses the plugin-owned node label `ctfd_whale_frpc=true` instead.

Run the synchronizer on the Swarm manager (the CTFd leader), before deploying the FRP stack:

```sh
chmod +x scripts/label-frpc-workers.sh
./scripts/label-frpc-workers.sh
```

The default prefix is `worker`; pass another prefix as the first argument when needed. Run the script again whenever workers are added, removed, renamed, or reclassified.

## Deploy the FRP stack

Keep the existing FRPS image, FRPC image, token, ports, and domain values. The example stack now deploys FRPC in `global` mode with the placement constraint `node.labels.ctfd_whale_frpc == true`:

```sh
docker stack deploy -c docker-stack.frp.example.yml whale-frp
```

The FRPC admin API must stay on target port `7400` and be published with `mode: host`. This gives every worker its own listener on its own node address. CTFd discovers each running task through the Docker API and calls `http://<node.Status.Addr>:7400`; therefore the CTFd manager must be able to reach every worker's Swarm node address on TCP/7400.

**Firewall:** the FRPC admin API in the example has no authentication configured, matching the existing plugin behavior. Do not expose TCP/7400 publicly. Allow it only from the private address of the CTFd manager, and keep the FRPC service on the private FRP/containers networks.

The FRPC container must remain attached to the external overlay network used by the challenge services (`CTFD_CONTAINERS_NETWORK`). Its connection to FRPS continues to use the existing `frp` overlay network.

## How routes are assigned

For each routable Whale instance, the plugin resolves the task of its primary service (`<user_id>-<uuid>`) and reads its current `NodeID`. It then sends that instance's complete FRP rule set to the matching worker FRPC. The other worker agents receive configurations without those rules. When an instance is removed, a full configuration refresh also clears its rules from every available worker agent.

The monitoring page lists hostnames beginning with `worker`, Swarm connectivity, FRPC task state, and FRPS state. Service logs remain available from the same page.

If an instance is assigned to a worker without a running FRPC task, route registration fails with an explicit error instead of silently sending the routes to another agent. Check the worker label, FRPC task state, TCP/7400 reachability, and the service logs.

## Rollback

To return to the previous single-agent behavior:

1. Deploy FRPC as a regular replicated service on the node reachable at the configured `whale:frp_api_url`.
2. Remove the global FRPC service (or change it back to replicated mode).
3. Keep the existing `whale:frp_api_url` setting.

Whale automatically uses the legacy endpoint when no global FRPC service is detected.
