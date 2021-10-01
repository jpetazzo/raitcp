#!/usr/bin/env python3

import random
import select
import socket
import string
import sys
import time

# Left side = accepts client connection, connects to multiple outs
# Right side = accepts multiple ins, connects to server
LEFT = "LEFT"
RIGHT = "RIGHT"

RECV_CHUNK_SIZE = 65536
SEND_CHUNK_SIZE = 65536


def other(side):
    return {LEFT: RIGHT, RIGHT: LEFT}[side]


from config import left_config, right_config


def setup(config):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", config["listen_port"]))
    s.listen(4)
    listeners.append(Listener(s, config["side"], config["connect_from_to_port"]))


class Listener(object):
    def __init__(self, socket, side, connect_from_to_port):
        self.socket = socket
        self.side = side
        self.connect_from_to_port = connect_from_to_port

    def fileno(self):
        return self.socket.fileno()

    def when_readable(self):
        s, addr_info = self.socket.accept()
        print(f"Accepted connection from {addr_info}.")
        if self.side == LEFT:
            # Create new connection; this generates the cid.
            connection = Connection(None, self.connect_from_to_port, other(self.side))
            print(f"Generated new connection id {connection.cid}.")
            connections[connection.cid] = connection
            peer = Peer(s, self.side, addr_info, connection)
            connection.peers[peer.side].append(peer)
        else:
            # We need to read the connection id, but we must do it in a non-blocking way.
            # So we create a connection-less Peer and put in in the special queue.
            peer = Peer(s, self.side, addr_info, None)
            peer.connect_from_to_port = self.connect_from_to_port
            newpeers.append(peer)


class Peer(object):
    def __init__(self, socket, side, desc, connection):
        self.cid_buffer = b""
        self.socket = socket
        self.side = side
        self.desc = desc
        self.connection = connection
        self.bytes_received = 0
        self.output_buffer = b""
        # Timestamp of the last time we got new data on this connection
        self.was_leader_at = 0
        # Total byte count for new data on this connection
        self.was_source_for = 0

    def fileno(self):
        return self.socket.fileno()

    def when_readable(self):
        if self.connection:
            self.when_readable_with_connection()
        else:
            self.when_readable_without_connection()

    def when_readable_without_connection(self):
        # We only read 1 byte at a time. It's a bit inefficient, but we only
        # do it 4 times, so who cares.
        self.cid_buffer += self.socket.recv(1)
        if len(self.cid_buffer) == 4:
            cid = self.cid_buffer
            if cid not in connections:
                connection = Connection(
                    cid, self.connect_from_to_port, other(self.side)
                )
                connections[cid] = connection
            self.connection = connections[cid]
            self.connection.peers[self.side].append(self)
            newpeers.remove(self)

    def when_readable_with_connection(self):
        writers = self.connection.peers[other(self.side)]
        # Normally, we should have at least one writer available,
        # since we connect on demand.
        assert writers
        # Read the data available on the socket.
        data = self.socket.recv(RECV_CHUNK_SIZE)
        if len(data) == 0:
            # EOF
            print(f"Got EOF on {self}, closing peer sockets.")
            self.socket.close()
            for writer in writers:
                writer.socket.close()
            self.connection.open = False
            return
        # Smoke test: check that we're not past the connection reading point.
        assert self.bytes_received <= self.connection.bytes_received[self.side]
        # First, easy case where we are exactly at the reading point.
        if self.bytes_received == self.connection.bytes_received[self.side]:
            # Keep the entire data packet.
            new_data = data
        # Now, the less easy case.
        # We are receiving stale data. How stale exactly?
        else:
            lag = self.connection.bytes_received[self.side] - self.bytes_received
            new_bytes = len(data) - lag
            if new_bytes > 0:
                # There is at least a bit of new data, keep it.
                new_data = data[-new_bytes:]
            else:
                # Only stale data.
                new_data = b""
        # OK, update our counters.
        self.bytes_received += len(data)
        # If we have data, pipe it to output buffers.
        if new_data:
            self.was_leader_at = time.time()
            self.was_source_for += len(new_data)
            for writer in writers:
                writer.output_buffer += new_data
            self.connection.bytes_received[self.side] += len(new_data)

    def when_writable(self):
        bytes_sent = self.socket.send(self.output_buffer[:SEND_CHUNK_SIZE])
        self.output_buffer = self.output_buffer[bytes_sent:]


class Connection(object):
    def __init__(self, cid, connect_from_to_port, remote_side):
        if cid is None:
            cid = ""
            for i in range(4):
                cid += random.choice(string.ascii_letters)
            cid = cid.encode("ascii")
        self.cid = cid
        self.connect_from_to_port = connect_from_to_port
        self.peers = {LEFT: [], RIGHT: []}
        self.bytes_received = {LEFT: 0, RIGHT: 0}
        self.open = True

        for (localaddr, remoteaddr, remoteport) in connect_from_to_port:
            print(f"Connecting from {localaddr} to {remoteaddr}:{remoteport}.")
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind((localaddr, 0))
            s.connect((remoteaddr, remoteport))
            if remote_side == RIGHT:
                s.send(self.cid)
            peer = Peer(s, remote_side, (localaddr, remoteaddr, remoteport), self)
            self.peers[peer.side].append(peer)


listeners = []
connections = {}
newpeers = []


side = sys.argv[1]
if side == "left":
    setup(left_config)
elif side == "right":
    setup(right_config)
else:
    print(f"Invalid side {side!r}, should be left or right.")
    sys.exit(1)

next_stat_time = time.time()
while True:
    if time.time() > next_stat_time:
        # Clear screen
        print("\x1b[H\x1b[2J\x1b[3J")
        print(time.strftime("%H:%M:%S"))
        print(f"{len(connections)} connections.")
        for connection in connections.values():
            status = "OPEN" if connection.open else "CLOSED"
            print(
                f"Connection {connection.cid}: {status}, {connection.bytes_received[LEFT]}/{connection.bytes_received[RIGHT]} bytes received."
            )
            for side in (LEFT, RIGHT):
                for peer in connection.peers[side]:
                    print(
                        f"- {side}, {peer.desc}, {peer.bytes_received} bytes received, {peer.was_source_for} new bytes, output buffer has {len(peer.output_buffer)} bytes."
                    )
        next_stat_time = time.time() + 1  # Change this for stat interval
    reader_sockets = listeners + newpeers
    writer_sockets = []
    for connection in connections.values():
        if connection.open:
            peers = connection.peers[LEFT] + connection.peers[RIGHT]
            reader_sockets = reader_sockets + peers
            writer_sockets = writer_sockets + [p for p in peers if p.output_buffer]
    readable_sockets, writable_sockets, error_sockets = select.select(
        reader_sockets, writer_sockets, [], 1
    )
    for s in readable_sockets:
        s.when_readable()
    for s in writable_sockets:
        s.when_writable()
