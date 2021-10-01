# RAITCP

Redundant Array of Independent TCP streams


```
                          Link A
                     /------>-------\
                    /                \
                   /                  \
client ---> raitcp                      raitcp ---> server
            "left" \                  / "right"
                    \                /
                     \------>-------/
                          Link B
```

This assumes the following:

- you have two internet connections
- they are directly connected to `client`
  (i.e. `client` has a way to pick which one to use
  when establishing a connection to `server`)
- you want to secure a connection from `client` to `server`
  ("secure" against jitter, packet loss, etc; we're not
  talking privacy/MITM/etc)

You can use `raitcp` as such:

- configure `raitcp` (create `config.py`, see `config-example.py`)
- run `raitcp left` on the client
- run `raitcp right` on the server
- `client` connects to `raitcp left`
- `raitcp left` will establish two connections to `raitcp right`
- `raitcp right` will connect to `server`

Principle of operation: `raitcp` duplicates the data that
it receives from `client` and `server` on both links.
When it receives data from the other `raitcp` instance,
if it's "fresh" data it forwards it to the `client` (or
`server`), and if it's "old" data (that it already received
from the other link) it just discards it.

If one link becomes congested, `raitcp` will buffer data going
to that link. It doesn't support disconnections or reconnections
at the moment.


## Useful tips and tricks

If you have a secondary network interface, you can enable source
routing like this:

```bash
SECONDARY_GATEWAY=192.168.2.1
SECONDARY_SUBNET=192.168.2.0/24
sudo ip route add 0/0 via $SECONDARY_GATEWAY table 2
sudo ip rule add from $SECONDARY_SUBNET table 2
```

If you want to test that it's working correctly, you can do:
```bash
PRIMARY_SUBNET=10.0.0.0/24
SECONDARY_SUBNET=192.168.2.0/24
REMOTE_SERVER=remote.server.io
REMOTE_PORT=1935
sudo iptables -I OUTPUT -s $PRIMARY_SUBNET -d $REMOTE_SERVER -p tcp --dport $REMOTE_PORT -j DROP
sleep 10
sudo iptables -D OUTPUT -s $PRIMARY_SUBNET -d $REMOTE_SERVER -p tcp --dport $REMOTE_PORT -j DROP
```

While traffic is being dropped, you will see the output buffers
(reported by `raitcp`) increase, then drain once the traffic is
enabled again.
