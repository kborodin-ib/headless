#### Option 1. Forward to localhost.

1. Obtain a VPS and have access to it's terminal via ssh
2. Download the cp-gateway from this link: https://download2.interactivebrokers.com/portal/clientportal.gw.zip and store it somewhere in your system. 4
3. 
   ```bash
   unzip clientportal.gw.zip
   ```
4. Startup the gateway
   ```bash
   ./bin/run.sh root/conf.yaml
   ```
5.  Tests if it's OK locally:
```bash
curl https://localhost:5000/v1/api/auth/status
```
At this point it returns 401 Unauthorized
6.  Switch back to your local machine and forward traffic to local host of the VPS
```bash
ssh -L 5000:127.0.0.1:5000 user@vps-remote-server
```
7. In browser navigate to https://localhost:5000 and authenticate
8. Verify whether session is authenticated by vising url:
```bash
https://localhost:5000/v1/api/iserver/auth/status
```
Should return something like:
```JSON
{
"authenticated": true,
"established": true,
"competing": false,
"connected": true,
"message": ""
}
```
9.  Switch back to  VPS and confirm whether same response is sent locally:
```
curl https://localhost:5000/v1/api/iserver/auth/status
```

Now, to call any endpoint it will be required to configure request with at least 'User-Agent' header and session 'cookies'. Those can be extracted from the browsers developer tools.

### Options 2. Allow IP address in conf.yaml and expose external interface


Mostly the same except for we now add ip address of our own machine to conf.yaml under allowed ip's and startup the tunnel with following command:

```bash
ssh -L 5000:VPS-IP:5000 user@vps-remote-host
```

The final hop here leaves loopback and comes back through the network interface. This only works if service is bound to this ip address and nothing(firewall) is blocking connections from external address.

By default if ip address of connecting machine is not allowed via conf.yaml - access will be denied(401).

Option 1 is considered to be more secure because no ports are exposed to outside network and only way to access the the cp-gateway is through the ssh-tunnel.

With this option live account fails might randomly fail to establish a session with 401 HTTP error. Session timeout might also be an issue.
