#!/usr/bin/env python3

import errno
import logging
import os
import random
import select
import socket
import string
import struct
import sys
import time
import yaml


# Left side = accepts client connection, connects to multiple outs
# Right side = accepts multiple ins, connects to server
LEFT = "LEFT"
RIGHT = "RIGHT"

RECV_CHUNK_SIZE = 65536
SEND_CHUNK_SIZE = 65536


def other(side):
    return {LEFT: RIGHT, RIGHT: LEFT}[side]


if os.environ.get("DEBUG", "")[:1].upper() in ["Y", "1"]:
    DEBUG = True
    logging.basicConfig(level=logging.DEBUG)
else:
    DEBUG = False
    logging.basicConfig()
log = logging


def setup(config, side):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", config[side]["bindport"]))
    s.listen(4)
    listeners.append(Listener(s, side, config[side]["endpoints"]))


def encode_u64(n):
    return struct.pack("!Q", n)


def decode_u64(b):
    return struct.unpack("!Q", b)[0]


class Listener(object):
    def __init__(self, socket, side, remote_endpoints):
        self.socket = socket
        self.remote_side = side
        self.remote_endpoints = remote_endpoints

    def fileno(self):
        return self.socket.fileno()

    def when_readable(self):
        s, remote_addr = self.socket.accept()
        log.info(f"Accepted connection from {remote_addr}.")
        if self.remote_side == LEFT:
            # "LEFT" side is client-side, so when we accept
            # a connection, we generate a connection ID,
            # establish multiple connections to the "RIGHT"
            # side, and send that connection ID on each of them.
            connection = Connection(other(self.remote_side), self.remote_endpoints)
            log.info(f"Generated new connection id {connection.cid}.")
            connections[connection.cid] = connection
            peer = Peer(self.remote_side, remote_addr, connection, s)
            connection.peers[peer.remote_side].append(peer)
            # Also don't read a bytes_received count on that side!
            peer.bytes_received = 0
        else:
            # "RIGHT" side is the server-side, so when we accept
            # a connection, we must figure out to which mirrored
            # connection it belongs.
            # We need to read the connection id, but we must do it
            # in a non-blocking way. So we create a connection-less 
            # Peer and put in in the special queue, where it will
            # fill its input buffer until we have the connection id.
            peer = Peer(self.remote_side, remote_addr, None, s)
            peer.remote_endpoints = self.remote_endpoints
            newpeers.append(peer)


class Peer(object):
    def __init__(self, remote_side, remote_addr, connection, socket=None):
        self.connector = False
        self.socket = socket
        self.remote_side = remote_side
        self.remote_addr = remote_addr
        self.connection = connection
        self.bytes_received = None
        self.input_buffer = b""
        self.output_buffer = b""
        # Timestamp of the last time we got new data on this connection
        self.was_leader_at = 0
        # Total byte count for new data on this connection
        self.was_source_for = 0

    def __str__(self):
        if self.connection:
            cid = self.connection.cid
        else:
            cid = None
        return f"Peer(connection={cid}, remote_side={self.remote_side}, remote_addr={self.remote_addr})"

    def fileno(self):
        return self.socket.fileno()

    def connect(self):
        log.debug(f"{self} connecting.")
        self.connector = True
        localaddr = self.remote_addr["bindaddr"]
        remoteaddr = self.remote_addr["connectaddr"]
        remoteport = self.remote_addr["connectport"]
        log.info(f"Connecting from {localaddr} to {remoteaddr}:{remoteport}.")
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind((localaddr, 0))
        self.socket.setblocking(False)
        try:
            self.socket.connect((remoteaddr, remoteport))
        except os.error as e:
            if e.errno == errno.EINPROGRESS:
                pass
        if self.remote_side == RIGHT:
            cid = self.connection.cid
            bytes_received = self.connection.bytes_received[other(self.remote_side)]
            log.debug(f"{self} sending connection id {cid} and byte position {bytes_received}")
            self.output_buffer += cid
            self.output_buffer += encode_u64(bytes_received)
        else:
            # Don't expect a bytes_received header from the server
            self.bytes_received = 0

    def when_readable(self):
        # In "prelude" state, we only read 1 byte at a time.
        # It's a bit inefficient, but we only read 12 bytes like this.
        if self.connection is None:
            self.input_buffer += self.socket.recv(1)
            if len(self.input_buffer) == 4:
                cid = self.input_buffer
                log.debug(f"{self} got connection id: {cid}")
                self.input_buffer = b''
                if cid not in connections:
                    connection = Connection(other(self.remote_side), self.remote_endpoints, cid)
                    connections[cid] = connection
                self.connection = connections[cid]
                self.connection.peers[self.remote_side].append(self)
                newpeers.remove(self)
                # And now that we know which connection this belongs to, send the current byte position.
                self.output_buffer += encode_u64(self.connection.bytes_received[other(self.remote_side)])
            return
        if self.bytes_received is None:
            self.input_buffer += self.socket.recv(1)
            if len(self.input_buffer) == 8:
                # FIXME: perhaps we should check that this is <= to the connection bytes received?
                self.bytes_received = decode_u64(self.input_buffer)
                log.debug(f"{self} got bytes_received position: {self.bytes_received} / {self.input_buffer}")
                self.input_buffer = b''
            return
        self.receive_and_send()

    def receive_and_send(self):
        writers = self.connection.peers[other(self.remote_side)]
        # Normally, we should have at least one writer available,
        # since we connect on demand.
        assert writers
        # Read the data available on the socket.
        data = self.socket.recv(RECV_CHUNK_SIZE)
        if len(data) == 0:
            # EOF
            log.warning(f"Got EOF on {self}, closing peer sockets.")
            self.socket.close()
            for writer in writers:
                writer.socket.close()
            self.connection.open = False
            return
        # Smoke test: check that we're not past the connection reading point.
        assert self.bytes_received <= self.connection.bytes_received[self.remote_side]
        # First, easy case where we are exactly at the reading point.
        if self.bytes_received == self.connection.bytes_received[self.remote_side]:
            # Keep the entire data packet.
            new_data = data
        # Now, the less easy case.
        # We are receiving stale data. How stale exactly?
        else:
            lag = self.connection.bytes_received[self.remote_side] - self.bytes_received
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
            self.connection.bytes_received[self.remote_side] += len(new_data)

    def when_writable(self):
        bytes_sent = self.socket.send(self.output_buffer[:SEND_CHUNK_SIZE])
        self.output_buffer = self.output_buffer[bytes_sent:]

# "Connection" represents one "mirrored" TCP connection.
# It will typically have one peer on the "left" side and
# multiple peers on the "right" side, or the other way
# around.
class Connection(object):
    def __init__(self, remote_side, remote_endpoints, cid=None):
        if cid is None:
            cid = ""
            for i in range(4):
                cid += random.choice(string.ascii_letters)
            cid = cid.encode("ascii")
        self.cid = cid
        self.remote_endpoints = remote_endpoints
        self.peers = {LEFT: [], RIGHT: []}
        self.bytes_received = {LEFT: 0, RIGHT: 0}
        self.open = True
        for remote_endpoint in remote_endpoints:
            peer = Peer(remote_side, remote_endpoint, self)
            self.peers[peer.remote_side].append(peer)
            peer.connect()


listeners = []
connections = {}
newpeers = []


config_file, side = sys.argv[1], sys.argv[2]
assert side in [LEFT, RIGHT]
config = yaml.safe_load(open(config_file))
setup(config, side)


next_stat_time = time.time()
while True:
    if time.time() > next_stat_time:
        # Show state of connections.
        # Clear screen.
        if not DEBUG:
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
                        f"- {side}, {peer.remote_addr}, {peer.bytes_received} bytes received, {peer.was_source_for} new bytes, output buffer has {len(peer.output_buffer)} bytes."
                    )
        next_stat_time = time.time() + 1  # Change this for stat interval
    # This is a classic select-based event loop.
    reader_sockets = listeners + newpeers
    writer_sockets = []
    for connection in connections.values():
        if connection.open:
            peers = connection.peers[LEFT] + connection.peers[RIGHT]
            reader_sockets = reader_sockets + peers
            writer_sockets = writer_sockets + [p for p in peers if p.output_buffer]
    # Remove closed sockets (their fileno() returns -1 apparently?)
    reader_sockets = [s for s in reader_sockets if s.fileno() >= 0]
    writer_sockets = [s for s in writer_sockets if s.fileno() >= 0]
    readable_sockets, writable_sockets, _ = select.select(
        reader_sockets, writer_sockets, [], 1
    )
    for p in readable_sockets:
        try:
            p.when_readable()
        except Exception as e:
            log.exception(f"Couldn't read data from {p}")
            p.socket.close()
            if p.connector and p.remote_side==RIGHT:
                log.info("Reconnecting")
                newpeer = Peer(p.remote_side, p.remote_addr, p.connection)
                p.connection.peers[p.remote_side].append(newpeer)
                newpeer.connect()
    for p in writable_sockets:
        try:
            p.when_writable()
        except Exception as e:
            log.exception(f"Couldn't write data to {p}")
            p.socket.close()
            if p.connector and p.remote_side==RIGHT:
                log.info("Reconnecting")
                newpeer = Peer(p.remote_side, p.remote_addr, p.connection)
                p.connection.peers[p.remote_side].append(newpeer)
                newpeer.connect()
