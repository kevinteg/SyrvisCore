# Networking & Request Flow

This page traces a request from the outside world all the way to a container, in both of
SyrvisCore's exposure modes, and explains the two pieces of networking that make it work on a
Synology NAS: the **macvlan network** (so Traefik can own ports 80/443) and the **shim interface**
(so Traefik can still reach the NAS host).

For how the *same hostname* resolves differently depending on where the client is, see
[Split DNS](04-split-dns.md). For the containers themselves, see [Primordial Substrate](02-primordial-substrate.md).

---

## The two networks

Every SyrvisCore deployment has exactly two Docker networks:

```mermaid
flowchart TB
    subgraph host["Synology NAS host — 192.168.8.3"]
        nginx["DSM's own nginx<br/>:80 / :443 (occupied)"]
        dsm["DSM / Photos / Drive<br/>:5000 :5001 :6690"]
        shim["syrvis-shim<br/>(macvlan, host side)<br/>SHIM_IP 192.168.8.5"]
    end

    subgraph macvlan["syrvis-macvlan (driver: macvlan, parent: ovs_eth0)"]
        traefik["traefik<br/>TRAEFIK_IP 192.168.8.4<br/>:80 :443 (its own IP)"]
    end

    subgraph proxy["proxy (driver: bridge)"]
        portainer["portainer :9000"]
        cloudflared["cloudflared<br/>metrics :20241"]
        dashboard["dashboard :8000"]
        l2["your Layer 2 services"]
    end

    traefik --- proxy
    traefik -. "reaches host via" .-> shim
    shim --- dsm
    lan(["LAN clients"]) --> traefik
```

- **`syrvis-macvlan`** — a `macvlan` network whose parent is the NAS's physical interface
  (`NETWORK_INTERFACE`, e.g. `ovs_eth0`). Traefik is given a **dedicated LAN IP** on it
  (`TRAEFIK_IP`, e.g. `192.168.8.4`). Because Traefik has its *own* IP, it can bind **ports 80 and
  443 without colliding with DSM's own nginx**, which already occupies those ports on the NAS's
  host IP. This is the crux of the whole design.
- **`proxy`** — an ordinary `bridge` network that every other container joins. Traefik is attached
  to it too, so it can forward to Portainer, the dashboard, Cloudflared, and your Layer 2 services
  by container name.

### Why the shim exists

A macvlan container **cannot talk to its own host** — that is a kernel limitation of macvlan, not a
SyrvisCore choice. So Traefik (on `syrvis-macvlan`) cannot reach services that live on the NAS
*host* (DSM, Synology Photos, Drive) at the host IP directly.

The fix is a **shim**: a second macvlan sub-interface created *on the host* (`syrvis-shim`) with its
own IP (`SHIM_IP`, conventionally `TRAEFIK_IP + 1`, e.g. `192.168.8.5`). Now the host is reachable
from the macvlan segment at `SHIM_IP`, and Traefik proxies Synology services to `https://SHIM_IP:5001`.
This works because DSM's system services bind `0.0.0.0`, so a packet arriving on the shim interface
is accepted. (`syrvis setup` creates the shim; a boot hook recreates it after reboot — see
[Primordial Substrate](02-primordial-substrate.md#boot-persistence).)

```mermaid
flowchart LR
    t["traefik<br/>(macvlan 192.168.8.4)"] -->|"https://192.168.8.5:5001"| shim["syrvis-shim<br/>192.168.8.5"]
    shim --> dsm["DSM / Photos / Drive<br/>bound on 0.0.0.0"]
```

### Traefik's entrypoints

```mermaid
flowchart LR
    web[":80 / web"] -->|https-redirect| websecure[":443 / websecure"]
    websecure -->|TLS terminate| router["Host-based router"]
    router --> svc["loadBalancer → container"]
    api[":8080 / API + dashboard + /ping<br/>(insecure, proxy-net only)"]
```

- **`web` (:80)** — every HTTP request is redirected to HTTPS by the `https-redirect` middleware.
- **`websecure` (:443)** — TLS is terminated here using a Let's Encrypt certificate, then the
  request is routed to a backend by its `Host(...)` rule.
- **`:8080`** — Traefik's API/dashboard and its `/ping` liveness endpoint. It is `insecure: true`
  and only reachable inside the `proxy` network (never published to the LAN); the SyrvisCore
  dashboard's health probe reads `/ping` and `/api/overview` here.

> **Static vs dynamic config.** `:8080`, the entrypoints, and the cert resolver live in Traefik's
> **static** config (`data/traefik/traefik.yml`), which Traefik reads **only at process start**.
> Per-service routing lives in the **dynamic** config (`data/traefik/config/`), which Traefik
> hot-reloads. This distinction matters: changing static config requires a Traefik **restart**, and
> SyrvisCore now does that automatically whenever it regenerates `traefik.yml` (see
> [Primordial Substrate → Traefik](02-primordial-substrate.md#traefik)).

---

## Request flow — `internal` exposure (LAN-only)

An `internal` service (the default) is reachable only from inside the network. The client resolves
the hostname to Traefik's LAN IP directly; **Cloudflare is not in the request path at all** (it is
used only to issue the certificate, via DNS-01).

```mermaid
sequenceDiagram
    autonumber
    participant C as LAN client
    participant DNS as LAN DNS resolver
    participant T as Traefik (192.168.8.4)
    participant S as Container (proxy net)

    C->>DNS: resolve photos.example.com
    DNS-->>C: A → 192.168.8.4  (TRAEFIK_IP)
    C->>T: HTTPS request (Host: photos.example.com)
    Note over T: Match Host() router,<br/>serve Let's Encrypt cert (DNS-01)
    T->>S: proxy over the proxy network
    S-->>C: response
```

The only external state an `internal` service needs is **one LAN DNS A record** pointing the
hostname at `TRAEFIK_IP`. `syrvis stack hostnames` reports exactly that record; home-tech creates it.

For a **Synology** service (DSM, Photos) the last hop goes through the shim instead of the proxy
network:

```mermaid
flowchart LR
    C(["LAN client"]) -->|"A → 192.168.8.4"| T["traefik"]
    T -->|"https://SHIM_IP:5001"| shim["syrvis-shim → NAS host"]
    shim --> P["Synology Photos :5001"]
```

---

## Request flow — `tunnel` exposure (remote via Cloudflare)

A `tunnel` service is reachable from anywhere, gated by Cloudflare Access. No ports are forwarded on
your router; instead **Cloudflared holds an outbound tunnel** to the Cloudflare edge, and the edge
delivers authenticated requests back through it.

```mermaid
sequenceDiagram
    autonumber
    participant U as Remote user
    participant CF as Cloudflare edge
    participant AC as Cloudflare Access
    participant CD as cloudflared (proxy net)
    participant T as Traefik
    participant S as Container

    U->>CF: HTTPS request to wiki.example.com
    Note over CF: public DNS is a proxied<br/>CNAME → the tunnel
    CF->>AC: enforce Access policy (SSO / login)
    AC-->>CF: allow (identity asserted)
    CF->>CD: deliver over the established tunnel
    CD->>T: forward on the proxy network (Host: wiki.example.com)
    Note over T: same Host() router + cert<br/>as the internal path
    T->>S: proxy to the container
    S-->>U: response (back through the tunnel)
```

Key points:

- **No inbound ports.** `cloudflared` dials *out* to Cloudflare (`TUNNEL_TOKEN`), so nothing on your
  router is exposed. This is why `tunnel` works even behind CGNAT.
- **Access is the front door.** Every tunnel request is authenticated by a Cloudflare Access policy
  before it ever reaches the NAS. The dashboard can even consume the `Cf-Access-Jwt-Assertion`
  header for SSO.
- **Traefik still routes it.** The tunnel's ingress (configured by home-tech, not SyrvisCore) points
  at Traefik, so a `tunnel` service is routed and TLS-served by the *same* `Host()` router as an
  `internal` one. **SyrvisCore routes both exposures identically** — the difference is purely the
  external record home-tech must create (a proxied CNAME to the tunnel, plus an Access policy).

The whole picture, both planes at once:

```mermaid
flowchart TB
    subgraph internet["Internet"]
        remote(["Remote user"])
        cfedge["Cloudflare edge + Access"]
    end
    subgraph lan["Home LAN"]
        localuser(["LAN user"])
        subgraph nasbox["Synology NAS"]
            cd["cloudflared"]
            traefik["traefik<br/>192.168.8.4"]
            wiki["wiki (L2)"]
            photos["Synology Photos<br/>(via shim)"]
        end
    end

    remote -->|"CNAME → tunnel"| cfedge
    cfedge -->|"outbound tunnel"| cd
    cd --> traefik
    localuser -->|"A → 192.168.8.4"| traefik
    traefik --> wiki
    traefik --> photos
```

---

## TLS / certificate issuance

Traefik issues Let's Encrypt certificates and stores them in `data/traefik/acme.json` (mode `0600`).
There are two challenge types, chosen automatically:

```mermaid
flowchart TD
    start{"CLOUDFLARE_DNS_API_TOKEN set<br/>(or TRAEFIK_ACME_CHALLENGE=dns)?"}
    start -->|yes| dns["DNS-01 challenge<br/>via Cloudflare"]
    start -->|no| http["HTTP-01 challenge<br/>via the :80 entrypoint"]
    dns --> ok["✅ Issues certs for private-IP /<br/>split-horizon names too"]
    http --> caveat["⚠️ Only works if the name is<br/>publicly reachable over :80"]
```

- **DNS-01 (recommended, and required for `internal`)** — Traefik proves domain control by writing a
  TXT record via the Cloudflare API (`CF_DNS_API_TOKEN`). This works **even when the hostname
  resolves to a private LAN IP**, which is exactly the split-horizon case for `internal` services.
  Set `CLOUDFLARE_DNS_API_TOKEN` in `.env` to enable it.
- **HTTP-01 (fallback)** — Let's Encrypt validates by fetching a token over the public Internet on
  port 80. For a private-IP name this **cannot succeed**, so without a DNS token an `internal`
  service will fall back to a default self-signed cert. If you route any `internal` host, set the
  DNS token.

---

## Quick reference — the `.env` knobs

| Variable | Meaning | Example |
|----------|---------|---------|
| `NETWORK_INTERFACE` | Physical parent for the macvlan | `ovs_eth0` |
| `NETWORK_SUBNET` | LAN subnet (CIDR) | `192.168.8.0/24` |
| `NETWORK_GATEWAY` | LAN gateway | `192.168.8.1` |
| `TRAEFIK_IP` | Traefik's dedicated LAN IP | `192.168.8.4` |
| `SHIM_IP` | Host shim IP (Traefik → host) | `192.168.8.5` |
| `NAS_IP` | The NAS's own host IP | `192.168.8.3` |
| `DOMAIN` | Base domain for all routes | `example.com` |
| `CLOUDFLARE_DNS_API_TOKEN` | Enables DNS-01 certs | *(secret)* |
| `CLOUDFLARE_TUNNEL_TOKEN` | Enables the Cloudflared tunnel | *(secret)* |

All of these are set by `syrvis setup` and consumed by the compose + Traefik config generators.
