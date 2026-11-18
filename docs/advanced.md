# Advanced deployment

## Note

Please make sure that you have experienced the installation process on single node. This deployment method is *NOT* recommended on first try.

It would be easy for you to understand what we are going to do if you have some experience in using `docker` and `frp`.

## Goal

The goal of this advanced deployment is to deploy the CTFd and challenge containers on seperate machines for better experiences.

Overall, `ctfd-whale` can be decomposed into three compnents: `CTFd`, challenge containers along with frpc, and frps itself. The three components can be deployed seperately or together to satisfy different needs.

For example, if you're in a school or an organization that has a number of high-performance dedicated server *BUT* no public IP for public access, you can refer to this tutorial.

Here are some options:

* deploy frps on a server with public access
* deploy challenge containers on a seperate sever by joining the server into the swarm you created earlier
* deploy challenge containers on *rootless* docker
* deploy challenge containers on a remote server with public access, *securely*

You could achieve the first option with little effort by deploying the frps on the server and configure frpc with a different `server_addr`.  
In a swarm with multiple nodes, you can configure CTFd to start challenge containers on nodes you specifies randomly. Just make sure the node `whale` controlls is a `Leader`. This is not covered in this guide. You'll find it rather simple, even if you have zero experience on docker swarm.  
The [Docker docs](https://docs.docker.com/engine/security/rootless/) have a detailed introduction on how to set up a rootless docker, so it's also not covered in this guide.

In following paragraphs, the last option is introduced.

## Architecture

In this tutorial, we have 2 separate machines which we'll call them `web` and `target` server later. We will deploy CTFd on `web` and challenge containers (along with frp) on `target`.

This picture shows a brief glance.

![architecture](imgs/arch.png)

---

### Operate on `target` server

> root user is NOT recommended
> if you want to expose your docker deployment, you might also want to use [rootless docker](https://docs.docker.com/engine/security/rootless/)

Please read the [Docker docs](https://docs.docker.com/engine/security/protect-access/#use-tls-https-to-protect-the-docker-daemon-socket) thoroughly before continuing.

Setup docker swarm and clone this repo as described in [installation](./install.md), then follow the steps described in the Docker docs to sign your certificates.

> protect your certificates carefully
> one can take over the user running `dockerd` effortlessly with them
> and in most cases, the user is, unfortunately, root.

You can now create a network for your challenges by executing

```bash
docker network create --driver overlay --attachable challenges
```

Then setup frp on this machine. You might want to setup frps first:

```bash
# change to the version you prefer
wget https://github.com/fatedier/frp/releases/download/v0.37.0/frp_0.37.0_linux_amd64.tar.gz
tar xzvf frp_0.37.0_linux_amd64.tar.gz
cd frp_0.37.0_linux_amd64
mkdir /etc/frp
configure_frps frps.ini  # refer to [installation](./install.md)
cp systemd/frps.service /etc/systemd/system
systemctl daemon-reload
systemctl enable frps
systemctl start frps
```

Then frpc. Frpc should be running in the same network with the challenge containers, so make sure you connect frpc to the network you just created.

```bash
docker run -it --restart=always -d  --network challenges -p 7400:7400 frankli0324/frp:frpc \
    --server_addr=host_ip:host_port \
    --server_port=7000 \
    --admin_addr=7400 \
    --admin_port=7400 \
    --admin_user=username \
    --admin_pwd=password \
    --token=your_token
```

You could use `docker-compose` for better experience.

Here are some pitfalls or problems you might run into:

#### working with `systemd`

Copy the systemd service file to `/etc/systemd` in order to prevent it from being overwritten by future updates.

```bash
cp /lib/systemd/system/docker.service /etc/systemd/system/docker.service
```

Locate `ExecStart` in the file and change it into something like this:

```systemd
ExecStart=/usr/bin/dockerd \
    --tlsverify \
    --tlscacert=/etc/docker/certs/ca.pem \
    --tlscert=/etc/docker/certs/server-cert.pem \
    --tlskey=/etc/docker/certs/server-key.pem \
    -H tcp://0.0.0.0:2376 \
    -H unix:///var/run/docker.sock
```

Remember to reload `systemd` before restarting `docker.service`

```bash
systemctl daemon-reload
systemctl restart docker
```

#### cloud service providers

Most service providers provides you with a basic virus scanner in their system images, for example, AliCloud images comes with `YunDun`. You might want to disable it. The challenge containers often comes with backdoors, and is often accessed in a way cloud providers don't like (they are obviously attacks).

#### certificate security

Please follow the best practices when signing your certificates. If you gets used to signing both the client and server certicates on a single machine, you might run into troubles in the future.

If you feel inconvenient, at least sign them on your personal computer, and transfer only the needed files to client/server.

#### challenge networks and frpc

You could create an internal network for challenges, but you have to connect frpc to a different network *with* internet in order to map the ports so that CTFd can access the admin interface. Also, make sure frps is accessible by frpc.

### Operate on `web` server

Map your client certificates into docker. You might want to use `docker secrets`. Remember where the files are *inside the container*. In the case which you use `docker secrets`, the directory is `/run/secrets`.

You may also delete everything related to `frp` like `frp_network` since we are not going to run challenge containers on `web` server anymore. But if you just has one public IP for `web` server, you can leave `frps` service running.

Then recreate your containers:

```bash
docker-compose down  # needed for removing unwanted networks
docker-compose up -d
```

Now you can configure CTFd accordingly.
Sample configurations:

![whale-config1](imgs/whale-config1.png)
![whale-config2](imgs/whale-config2.png)
![whale-config3](imgs/whale-config3.png)

refer to [installation](./install.md) for explanations.

---

Now you can add a challenge to test it out.


## Deploy FRPC and FRPS as Docker Swarm services

The monitoring page reads Swarm nodes, services and tasks from the manager Docker API. A container started with \`docker compose up\` on a worker is a standalone container on that worker's local Docker daemon: it is not a Swarm service, and the manager cannot retrieve its logs through the Swarm service API.

The Compose file does **not** have to be copied to the workers. Run \`docker stack deploy\` on a manager with the stack file available locally. Swarm stores the service specifications and schedules their tasks. Bind-mounted files are different: they must exist on the node where the task is actually scheduled. See the [Docker stack deployment guide](https://docs.docker.com/engine/swarm/stack-deploy/).

### Configuration matching the CyberNight deployment

The provided topology has these Swarm node hostnames:

- \`ctfd\`: manager/leader, where the single FRPC service is pinned;
- \`frps\`: public FRP server, where the FRPS service is pinned;
- \`worker01\` through \`worker06\`: challenge execution nodes.

The existing FRPC Compose file uses the external network \`challenges\`. Reuse that exact network for the FRPC service and for Whale-created challenge services. Do not silently substitute \`ctfd_frp-containers\`.

Before deploying, inspect the network on the manager:

\`\`\`bash
docker network inspect challenges \
  --format 'driver={{.Driver}} scope={{.Scope}} attachable={{.Attachable}}'
\`\`\`

Expected: \`driver=overlay\`, \`scope=swarm\`, and preferably \`attachable=true\`. If it is a local bridge network, it is not the shared multi-node challenge network required by this design. Create/replace it only after checking what currently uses it.

Set the Whale configuration to match the existing FRP endpoints:

\`\`\`text
whale:docker_auto_connect_network = challenges
whale:frp_http_port = 8000
whale:frp_http_domain_suffix = challenges.cybn.fr
whale:frp_direct_port_minimum = 10000
whale:frp_direct_port_maximum = 10500
whale:frp_api_url = http://<private-IP-of-ctfd>:7400
whale:frp_direct_ip_address = <public-IP-or-DNS-of-frps>
\`\`\`

The DNS zone must also resolve \`*.challenges.cybn.fr\` to the public address of the FRPS node; the apex record alone is not sufficient for per-instance subdomains.

### Image requirement

The supplied FRPS Compose definition uses \`build:\`. **Docker Swarm's \`docker stack deploy\` does not build images**; it ignores the \`build\` option. Build the custom image and push it to a registry reachable by the Swarm nodes first. See Docker's [stack deployment documentation](https://docs.docker.com/engine/swarm/stack-deploy/).

Use a matching FRP release for FRPS and FRPC. The example stack requires \`FRPS_IMAGE\` so that the custom image is not silently replaced by the upstream example image.

### Deploy the stack

From the manager, first build and push the custom FRPS image from the machine/directory containing its Dockerfile (or copy that build context to a build host):

\`\`\`bash
docker build -t registry.example.org/frps:your-tag .
docker push registry.example.org/frps:your-tag
\`\`\`

Then, from the manager:

\`\`\`bash
export FRPS_IMAGE='registry.example.org/frps:your-tag'
export FRPC_IMAGE='frankli0324/frp:frpc'
export FRP_TOKEN='replace-with-a-long-random-token'
export FRP_SUBDOMAIN_HOST='challenges.cybn.fr'
export CTFD_CONTAINERS_NETWORK='challenges'
export FRP_SERVER_PORT='7000'
export FRP_VHOST_HTTP_PORT='8000'
export FRP_DIRECT_PORT_RANGE='10000-10500'

docker stack deploy -c docker-stack.frp.example.yml whale-frp
\`\`\`

The stack runs one FRPS replica on hostname \`frps\` and one FRPC replica on hostname \`ctfd\`. FRPC reaches FRPS using the private overlay DNS alias \`frps\`; the public name \`challenges.cybn.fr\` is the HTTP subdomain suffix, not the FRPC control-plane address.

The FRPC admin API is published on port 7400 in host mode on the \`ctfd\` node. Configure \`whale:frp_api_url\` with the private IP of that node, and verify reachability **from inside the CTFd container**. Restrict access to TCP/7400 with the host firewall. The current CTFd-Whale FRP router does not send HTTP Basic credentials to the FRPC API, so \`admin_user\` and \`admin_pwd\` must remain empty unless the plugin is also updated to authenticate.

Before starting the Swarm stack, stop the old standalone Compose containers: the existing FRPS on node frps already owns ports 7000, 8000 and 10000-10500, and an existing FRPC on node ctfd may already own port 7400. Stop any other standalone FRPC agents as well; this design intentionally replaces them with the single Swarm-managed FRPC on ctfd. Do not leave the old agents running alongside the new stack.

The FRPS published ports use the Swarm ingress routing mesh in this example. That means the published ports are reachable on every Swarm node, not only on the node hosting the FRPS task. Point public DNS to the FRPS node and configure firewalls so the FRP public ports are exposed only where intended. If you require host-only publication, use Swarm host publishing for the individual ports; keep in mind that a large direct-port range needs special handling because range publishing and host mode are not supported uniformly across Docker/Compose versions.

Finally, keep the direct port pool aligned: \`whale:frp_direct_port_minimum=10000\` and \`whale:frp_direct_port_maximum=10500\`. The HTTP listener must remain \`8000\` in FRPS and \`whale:frp_http_port\`. The server port remains \`7000\` in both agents.

Verify the deployment:

\`\`\`bash
docker service ls
docker service ps whale-frp_frpc
docker service ps whale-frp_frps
docker service inspect whale-frp_frpc --format '{{json .Endpoint.Spec.Ports}}'
\`\`\`

The challenge placement labels remain independent of FRPC placement. Confirm that the values under \`whale:docker_swarm_nodes\` match the \`name\` node label on each challenge worker:

\`\`\`bash
docker node inspect worker01 worker02 worker03 worker04 worker05 worker06 \
  --format '{{.Description.Hostname}} {{json .Spec.Labels}}'
\`\`\`

The current router sends every generated route configuration to one FRPC admin API. Do not run FRPC in global mode with this implementation: multiple independent clients would not receive the same updates.
