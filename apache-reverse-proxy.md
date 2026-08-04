---
title: Apache Reverse Proxy to IBKR Gateway on CentOS Stream 9
tags: [devops, apache, httpd, reverse-proxy, selinux, iptables, ibkr, centos]
created: 2026-08-04
---

# Apache Reverse Proxy to IBKR Gateway (CentOS Stream 9)

Expose an IBKR Client Portal Gateway (listening on `127.0.0.1:5000`, HTTPS with a
self-signed cert) through Apache `httpd` on an external port. Covers the port
collision, `mod_ssl`, both SELinux gotchas, and the HTTP-vs-HTTPS scheme mismatch.

> Consider whether you actually need this. An SSH tunnel
> (`ssh -L 5000:localhost:5000 boris@vps`) reaches the gateway from your own machine
> with zero public exposure — no new listener, no SELinux, no firewall changes. Only
> use a public reverse proxy when something other than your own machine must reach the
> gateway. If you do expose it publicly, add an IP allowlist and auth — anyone who can
> reach the port can hit your brokerage session.

## Overview

Three layers must all line up, or it silently fails:

1. **iptables** — open the external port (above the REJECT catch-all).
2. **SELinux** — two separate booleans/labels: one to *bind* the port, one to *reach out* as a proxy.
3. **Apache** — reverse proxy config, with correct HTTP/HTTPS scheme on each hop.

## Prerequisites

Confirm required modules are loaded:

```bash
httpd -M 2>/dev/null | grep -E 'proxy|ssl'
```

Need `proxy_module`, `proxy_http_module`, and `ssl_module`. If `ssl_module` is
missing, install `mod_ssl` (this is the cause of the `Invalid command
SSLProxyEngine` error — the directive comes from an unloaded module):

```bash
sudo dnf install -y mod_ssl
```

## Step 1 — Pick a port and avoid the collision

The gateway holds `127.0.0.1:5000`. Apache **cannot** bind `0.0.0.0:5000` on top of
that. Two options:

- **Different external port** (cleaner): listen on e.g. `5001` or `9000`.
- **Same port, external IP only**: bind Apache to `VPS_IP:5000` — a distinct socket
  from `127.0.0.1:5000`, so they coexist.

Prefer a port already in SELinux's `http_port_t` list (see Step 3) to skip a step:

```bash
sudo semanage port -l | grep http_port_t
# default: 80, 81, 443, 488, 8008, 8009, 8443, 9000
```

## Step 2 — Apache vhost

Create `/etc/httpd/conf.d/ibkr-proxy.conf` (replace `VPS_IP` and the port):

```apache
Listen VPS_IP:9000

<VirtualHost VPS_IP:9000>
    # --- Upstream hop: Apache -> gateway (HTTPS, self-signed) ---
    SSLProxyEngine On
    SSLProxyVerify none
    SSLProxyCheckPeerCN off
    SSLProxyCheckPeerName off
    SSLProxyCheckPeerExpire off

    ProxyPreserveHost On
    ProxyPass        / https://127.0.0.1:5000/
    ProxyPassReverse / https://127.0.0.1:5000/

    # --- Frontend hop: client -> Apache ---
    # Leave as plain HTTP (talk to it with http://), OR uncomment to serve HTTPS:
    # SSLEngine On
    # SSLCertificateFile    /etc/pki/tls/certs/localhost.crt
    # SSLCertificateKeyFile /etc/pki/tls/private/localhost.key
</VirtualHost>
```

Test syntax before every restart:

```bash
sudo httpd -t
sudo systemctl restart httpd
sudo systemctl enable httpd
```

### The two SSL directives — don't confuse them

| Directive          | Hop                  | Purpose                                    |
| ------------------ | -------------------- | ------------------------------------------ |
| `SSLProxyEngine`   | Apache → gateway     | Speak HTTPS **upstream** to the gateway    |
| `SSLEngine`        | client → Apache      | Serve HTTPS to the **client** (frontend)   |

## Step 3 — SELinux (two distinct fixes, both errno 13)

SELinux causes two different `Permission denied` failures here. Same errno, different
mechanism.

### 3a — Allow httpd to BIND the port

Symptom on restart:

```
(13)Permission denied: AH00072: make_sock: could not bind to address VPS_IP:5001
```

httpd may only bind ports in `http_port_t`. Add yours:

```bash
sudo semanage port -a -t http_port_t -p tcp 5001
```

If it errors `ValueError: Port tcp/5001 already defined`, the port already has a
different type label — **modify** instead of add:

```bash
sudo semanage port -m -t http_port_t -p tcp 5001
```

`-a` = add (port has no entry). `-m` = modify (port already labeled another type,
e.g. 5001 defaults to `commplex_link_t`). Verify:

```bash
sudo semanage port -l | grep http_port_t
```

### 3b — Allow httpd to CONNECT out (proxy hop)

Symptom (a 503 to the client, plus in `error_log`):

```
(13)Permission denied: AH00957: HTTPS: attempt to connect to 127.0.0.1:5000 (127.0.0.1) failed
```

Proxying means httpd opens an outbound connection, blocked by default:

```bash
sudo setsebool -P httpd_can_network_connect on
```

The `-P` makes it persist across reboots — without it the 503 returns after a
restart. Verify:

```bash
getsebool httpd_can_network_connect
```

## Step 4 — Firewall (iptables)

Insert the ACCEPT **above** the REJECT catch-all (append with `-A` lands after REJECT
and does nothing). Position 5 assumes the standard INPUT chain with REJECT last:

```bash
sudo iptables -I INPUT 5 -p tcp -m state --state NEW -m tcp --dport 9000 -j ACCEPT
sudo iptables -L INPUT -n -v --line-numbers   # confirm it sits just before REJECT
```

Persist (runtime rules are lost on reboot otherwise):

```bash
sudo dnf install -y iptables-services   # if needed
sudo service iptables save              # writes /etc/sysconfig/iptables
sudo systemctl enable iptables
```

## Step 5 — Verify end to end

```bash
# Both listeners present: gateway on loopback, httpd on external IP
sudo ss -tlnp | grep -E '5000|9000'

# Local upstream reachable (proves the gateway is alive)
curl -kI https://127.0.0.1:5000/v1/api/iserver/auth/status

# Through the proxy — use the scheme matching the frontend hop:
curl http://VPS_IP:9000/v1/api/iserver/auth/status      # plain-HTTP vhost
# curl -kI https://VPS_IP:9000/...                       # if SSLEngine On
```

Expect `401` until authenticated (that's success — the chain works), `200` once
logged in.

## Reverse-proxying the login flow (the hard part)

Static API calls proxy cleanly. The **interactive login/SSO chain** is what fights a
reverse proxy, because the gateway is built on a same-machine assumption: IBKR's docs
state browser authentication must happen on the machine running the gateway. Two
distinct failure modes show up.

### Failure 1 — redirects bounce to an unreachable URL

The gateway emits self-referential absolute URLs (the `/sso/Login` →
`/sso/Dispatcher` chain). Coming in via `VPS_IP:9000`, the browser gets a `Location:`
or JS/SSO callback pointing at `localhost:5000` / `127.0.0.1:5000` and can't follow
it.

**Gateway side (`conf.yaml`)** — set the base URL it uses to build links (empty by
default; poorly documented, so treat as first-try-not-guaranteed):

```yaml
portalBaseURL: "https://95.217.127.220:9000"
cors:
  origin.allowed: "*"
  allowCredentials: false
ips:
  allow:
    - 127.0.0.1          # Apache connects from loopback — must stay
    - 95.217.127.220
```

**Apache side** — preserve host, rewrite both upstream forms, and rewrite body +
cookies if localhost leaks into the HTML/JS or cookie domain:

```apache
ProxyPreserveHost On
ProxyPassReverse  / https://127.0.0.1:5000/
ProxyPassReverse  / https://localhost:5000/

# body rewrite (requires mod_substitute) — for localhost URLs leaked in HTML/JS
AddOutputFilterByType SUBSTITUTE text/html
Substitute "s|https://127.0.0.1:5000|https://95.217.127.220:9000|n"
Substitute "s|https://localhost:5000|https://95.217.127.220:9000|n"

# cookie domain/path rewrite
ProxyPassReverseCookieDomain 127.0.0.1 95.217.127.220
ProxyPassReverseCookiePath / /
```

### Failure 2 — login stalls mid-SSO, session never sticks (Secure cookie over HTTP)

Symptom: the SSO chain gets most of the way (Dispatcher returns 200, `/sso/report`
and bulletins succeed) then stalls around the GDPR step and never completes. The tell
is in the response headers:

```
Set-Cookie: x-sess-uuid=...; secure; HttpOnly
```

The session cookie is flagged **`Secure`**. Browsers refuse to store a `Secure`
cookie delivered over a plain-HTTP connection — so with an HTTP frontend on 9000, the
browser silently drops it, and the next request in the chain arrives with no session.
`curl -k https://localhost:5000` works because that hop is HTTPS; the browser over
`http://VPS_IP:9000` can't.

**Fix: the frontend hop must be HTTPS.** Add `SSLEngine On` + a cert to the vhost
(this is the `SSLEngine` vs `SSLProxyEngine` distinction — you need *both* legs
encrypted, not just upstream):

```apache
SSLEngine On
SSLCertificateFile    /etc/pki/tls/certs/localhost.crt
SSLCertificateKeyFile /etc/pki/tls/private/localhost.key
```

Then browse to `https://VPS_IP:9000` (accept the self-signed warning). The Secure
cookie now stores and the session persists past GDPR.

### Why no reverse proxy fully fixes the login flow

Fixing Failure 1 and Failure 2 individually can get you a working login, but it stays
fragile — and it's worth understanding *why* the failures keep appearing, because the
root cause is categorical, not an Apache/nginx detail. **A reverse proxy, by
definition, changes the origin.** The browser sees `https://VPS_IP:9000`; the gateway
believes it is `https://localhost:5000`. Everything below breaks the *same-origin
invariant* the gateway and IBKR's SSO chain are built on:

- **The origin is baked into the application layer, not the transport.** A proxy
  rewrites headers and (with `sub_filter`/`Substitute`) body text, but the gateway
  encodes its identity into absolute URLs, cookie domains, `Origin`/`Referer` checks,
  and WebSocket handshakes. You are reconstructing an origin the app treats as atomic —
  miss one spot and the chain breaks. It's whack-a-mole, not a fix.

- **The SSO handshake terminates on origins you don't control.** During login the
  gateway proxies out to IBKR's real infrastructure (`proxyRemoteHost:
  https://api.ibkr.com`) and the browser is bounced through `interactivebrokers.com`
  SSO/GDPR endpoints. Cookies and tokens set by *those* origins live on domains your
  proxy never sees and cannot rewrite. No amount of `proxy_redirect` / `sub_filter`
  reaches them.

- **`Secure` + `HttpOnly` + `SameSite` + HSTS compound it.** The session cookie is
  `Secure` (needs HTTPS frontend) and `HttpOnly` (JS can't touch it), the responses
  carry `Strict-Transport-Security`, and cross-origin cookies are increasingly
  `SameSite`-gated. Each is a browser-enforced rule keyed to origin — the proxy can't
  override the browser's security model.

- **WebSocket origin validation.** The streaming layer opens a WebSocket and the
  gateway validates its `Origin`. A rewritten HTTP origin and the WS origin must agree
  with what the gateway expects, which a proxy can't reliably guarantee across the
  whole handshake.

- **IBKR designed it same-machine on purpose.** The docs state browser authentication
  must happen on the machine running the gateway. That's a security choice, not an
  oversight — so fighting it means fighting the design.

**This applies to every reverse proxy** — Apache, nginx, Caddy, Traefik, HAProxy.
They all translate one origin to another, and none can make a browser treat two
origins as one, or rewrite content served to the browser directly from IBKR.

**Why the SSH tunnel is categorically different:** a tunnel is transport-transparent —
it does *not* change the origin. The browser genuinely connects to `localhost:5000`,
so the same-origin invariant holds end to end: redirects resolve, cookies are
same-origin HTTPS, `Origin`/`Referer`/WebSocket checks all pass, and there is nothing
to rewrite. A reverse proxy translates the origin; a tunnel preserves it. That single
distinction is why the login works through one and resists the other.

### The pragmatic hybrid: tunnel for login, proxy for API

The Secure-cookie problem is *intrinsic* to moving login off localhost, and other
origin/WebSocket checks pile on. The robust pattern:

1. **Authenticate through an SSH tunnel** — browser really is `localhost:5000`, so
   every localhost redirect resolves and every cookie is same-origin HTTPS:
   ```bash
   ssh -L 5000:localhost:5000 boris@95.217.127.220
   # log in at https://localhost:5000 on your own machine
   ```
2. **Use the Apache proxy only for authenticated API calls** — no redirects to
   rewrite once the session exists.

The redirect/cookie pain lives entirely in the interactive login step; route just
that through the tunnel and the proxy handles the rest.

## Alternatives to the SSH tunnel

Same organizing rule as above: to preserve the origin, the browser must either **run
on the VPS** (same-origin with the gateway) or you must **skip the interactive login
entirely**. Three categories, all of which beat fighting the reverse proxy.

### A — Browser on the VPS, automated (IBeam)

[IBeam](https://github.com/Voyz/ibeam) (`voyz/ibeam`) is the standard tool for exactly
this situation. It runs the CP Gateway headless with a virtual display buffer and
injects your IBKR credentials into the login page automatically, handling 2FA and
re-authentication. Because the automated browser runs *beside* the gateway in the
container, login is `localhost:5000` — same origin, so none of the proxy problems
exist. Run IBeam on the VPS, then front its `:5000` with your Apache/nginx proxy for
API calls (API has no redirects to rewrite).

```bash
docker run -d --env IBEAM_ACCOUNT=your_account --env IBEAM_PASSWORD=your_password \
  -p 5000:5000 voyz/ibeam
```

**Security cost — matters for a live account.** IBeam must store your credentials
somewhere; that's an inherent risk. Use Docker secrets (not plaintext env vars), lock
the host, and use paper-account credentials while testing. Weigh whether an always-on
stored-credential setup is worth it versus manual auth.

### A — Browser on the VPS, manual (VNC, no stored creds)

Run a real browser on the VPS under `Xvfb + x11vnc + noVNC` (the port-6080 supervisord
pattern), point it at `https://localhost:5000`, and drive it from your laptop over
noVNC in a browser tab. The browser is physically on the VPS → same-origin localhost →
login completes cleanly, 2FA and all. You authenticate manually when needed; **no
credentials stored anywhere.** Lowest-risk option; ideal for occasional session
refreshes rather than 24/7 automation.

### B — Eliminate the interactive login (Web API OAuth)

IBKR's Web API OAuth uses signed requests with your own RSA keys — no gateway, no
browser, no session cookie, nothing to reverse-proxy. It's the architecturally clean
path for headless server access. Caveat: individual-account availability and the OAuth
flavor (1.0a vs newer) have shifted over time — verify current terms against IBKR's
live docs before committing. Best choice for a durable server-side setup rather than
just making the gateway work.

### C — A non-SSH tunnel that still preserves origin

The excluded SSH tunnel is one member of a category — *origin-preserving transport*.
The key is that your machine's `localhost:5000` must forward to the gateway, so the
browser still sees `localhost:5000`:

- **WireGuard / Tailscale + a local forward.** VPN alone doesn't help — hitting the
  VPS's VPN IP (`10.x.x.x:5000`) is a *new* origin, same wall as a reverse proxy. Add a
  local listener over the VPN and origin is preserved again:
  ```bash
  # on your machine, forwarding over the VPN to the gateway
  socat TCP-LISTEN:5000,fork TCP:10.x.x.x:5000
  ```
- **stunnel / socat** over any network path — same principle, different tools.

**The distinction:** a tunnel terminating at *your* `localhost` preserves origin; a VPN
or proxy that exposes the gateway on *some other* address does not. Only the former
works for login.

### Which to pick

- **VNC-to-local-browser** — manual, zero stored credentials, reuses the existing Xvfb
  stack.
- **IBeam** — hands-off 24/7, accepting the credential-storage risk.
- **OAuth** — one-time investment for the cleanest long-term headless access.

## Troubleshooting matrix

| Symptom                                                        | Cause                                             | Fix                                                        |
| ------------------------------------------------------------- | ------------------------------------------------- | --------------------------------------------------------- |
| `Invalid command SSLProxyEngine`                              | `mod_ssl` not loaded                              | `dnf install -y mod_ssl`                                  |
| `(13)Permission denied ... make_sock: could not bind`         | SELinux: port not in `http_port_t`                | `semanage port -a` (or `-m` if already defined)           |
| `ValueError: Port tcp/N already defined`                      | Port labeled with another type                    | `semanage port -m -t http_port_t -p tcp N`                |
| `503 Service Unavailable`                                     | SELinux blocking outbound, or gateway down        | `setsebool -P httpd_can_network_connect on`; check upstream |
| `(13)Permission denied: AH00957 ... attempt to connect ... failed` | SELinux: httpd can't connect out            | `setsebool -P httpd_can_network_connect on`               |
| `curl: (35) SSL received a record that exceeded ... length`   | Sent `https://` to a plain-HTTP vhost             | Use `http://`, or add `SSLEngine On` + cert to the vhost  |
| Local curl works, external doesn't                            | iptables rule missing / not saved / below REJECT  | Insert ACCEPT above REJECT, `service iptables save`       |
| Login redirect bounces to `localhost:5000` / unreachable URL  | Gateway emits self-referential absolute URLs      | `portalBaseURL` in conf.yaml; `ProxyPassReverse` both forms; `mod_substitute` |
| Login stalls mid-SSO (after Dispatcher 200, around GDPR)      | `Secure` session cookie dropped over HTTP frontend | Make frontend HTTPS: add `SSLEngine On` + cert to vhost   |

## Key takeaways

- **errno 13 has two SELinux flavors here**: binding a port (`semanage port`) vs.
  connecting out (`setsebool httpd_can_network_connect`). Different mechanisms.
- **`-a` vs `-m`** in `semanage port`: add for unlabeled ports, modify for
  already-labeled ones.
- **Two SSL layers**: `SSLProxyEngine` (upstream) and `SSLEngine` (frontend) are
  independent — set each based on the scheme that hop uses.
- **Scheme mismatch** (`curl (35)`) means you spoke TLS to a cleartext listener, or
  vice versa.
- **iptables rule order**: ACCEPT must precede the REJECT catch-all.
- **`Secure` cookies need an HTTPS frontend**: the login SSO chain sets a `Secure`
  session cookie; a plain-HTTP frontend causes the browser to drop it and login stalls
  mid-flow. Encrypt *both* legs, not just upstream.
- **Login is the hard part, API is easy**: the redirect/cookie friction lives entirely
  in the interactive login step. Tunnel for login, proxy for authenticated API calls.
- **No reverse proxy fully fixes login — categorically.** A proxy changes the origin;
  the gateway + IBKR SSO chain enforce a same-origin invariant (absolute URLs, `Secure`
  cookies, `Origin`/WebSocket checks, cross-origin SSO on `interactivebrokers.com` you
  can't rewrite). True of Apache, nginx, Caddy, Traefik, HAProxy alike.
- **A tunnel preserves the origin; a proxy translates it.** The browser really is
  `localhost:5000` through a tunnel, so the invariant holds end to end. That one
  distinction is why login works through a tunnel and resists every reverse proxy.
