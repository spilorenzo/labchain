import json
from enum import Enum
import socket
import socketserver
from threading import Thread
import logging
from queue import Queue
from binascii import unhexlify, hexlify

from .block import Block
from .transaction import Transaction

__all__ = ['Protocol', 'PeerConnection']

MAX_PEERS = 10
HELLO_MSG = b"bl0ckch41n"

socket.setdefaulttimeout(30)

class PeerConnection:
    """
    Handles the low-level socket connection to one other peer.
    :ivar peer_addr: The self-reported address one can use to connect to this peer.
    :ivar _sock_addr: The address our socket is or will be connected to.
    :ivar socket: The socket object we use to communicate with our peer.
    :ivar proto: The Protocol instance this peer connection belongs to.
    :ivar is_connected: A boolean indicating the current connection status.
    :ivar outgoing_msgs: A queue of messages we want to send to this peer.
    """

    def __init__(self, peer_addr, proto, socket=None):
        self.peer_addr = None
        self._sock_addr = peer_addr
        self.socket = socket
        self.proto = proto
        self.is_connected = False
        self.outgoing_msgs = Queue()

        Thread(target=self.run, daemon=True).start()

    def send_peers(self):
        """ Sends all known peers to this peer. """
        for peer in self.proto.peers:
            if peer.peer_addr is not None:
                self.send_msg("peer", list(peer.peer_addr))

    def run(self):
        """
        Creates a connection, handles the handshake, then hands off to the reader and writer threads.

        Does not return until the writer thread does.
        """
        if self.socket is None:
            self.socket = socket.create_connection(self._sock_addr)
        self.socket.sendall(HELLO_MSG)
        if self.socket.recv(len(HELLO_MSG)) != HELLO_MSG:
            return
        self.is_connected = True

        self.send_msg("myport", self.proto.server.server_address[1])
        self.send_msg("block", self.proto._primary_block)
        self.send_peers()

        # TODO: broadcast this new peer to our current peers, under certain circumstances

        Thread(target=self.reader_thread, daemon=True).start()
        self.writer_thread()

    def close_on_error(fn):
        """ A decorator that closes both threads if one dies. """

        def wrapper(self, *args, **kwargs):
            try:
                fn(self, *args, **kwargs)
            except Exception:
                logging.exception("exception in reader/writer thread")

            while not self.outgoing_msgs.empty():
                self.outgoing_msgs.get_nowait()
            self.outgoing_msgs.put(None)
            self.is_connected = False
            if self in self.proto.peers:
                self.proto.peers.remove(self)
            self.socket.close()
        return wrapper

    def send_msg(self, msg_type, msg_param):
        """
        Sends a message to this peer.

        :msg_type: The type of message.
        :msg_param: the JSON-compatible parameter of this message
        """

        if not self.is_connected:
            return
        self.outgoing_msgs.put({'msg_type': msg_type, 'msg_param': msg_param})

    @close_on_error
    def writer_thread(self):
        """ The writer thread takes messages from our message queue and sends them to the peer. """
        while True:
            item = self.outgoing_msgs.get()
            if item is None:
                break
            #print(repr(item))
            data = json.dumps(item, 4).encode()
            self.socket.sendall(str(len(data)).encode() + b"\n")
            self.socket.sendall(data)
            self.outgoing_msgs.task_done()

    @close_on_error
    def reader_thread(self):
        """ The reader thread reads messages from the socket and passes them to the protocol to handle. """
        while True:
            buf = b""
            while not buf or buf[-1] != '\n':
                tmp = self.socket.recv(1)
                if not tmp:
                    return
                buf += tmp
            length = int(buf)
            buf = bytearray(length)
            read = 0
            while length > read:
                tmp = self.socket.recv_into(buf[read:])
                if not tmp:
                    return
                read += tmp

            obj = json.loads(buf.decode())
            msg_type = obj['msg_type']
            msg_param = obj['msg_params']

            if msg_type == 'myport':
                self.peer_addr = (self._sock_addr,) + (int(msg_param),) + self._sock_addr[2:]
            else:
                self.proto.received(msg_type, msg_param, self)


class SocketServer(socketserver.TCPServer):
    allow_reuse_address = True
    def serve_forever_bg(self):
        Thread(target=self.serve_forever, daemon=True).start()

    def close_request(self, request):
        pass

    def shutdown_request(self, request):
        pass

class Protocol:
    """
    Manages connections to our peers. Allows sending messages to them and has event handlers
    for handling messages from other peers.
    """

    def __init__(self, bootstrap_peer, primary_block, listen_port=0):
        """
        :param bootstrap_peer: the network address of the peer where we bootstrap the P2P network from
        :param primary_block: the head of the primary block chain
        :param listen_port: the port where other peers should be able to reach us
        """

        self.block_receive_handlers = []
        self.trans_receive_handlers = []
        self.block_request_handlers = []
        self._primary_block = primary_block.to_json_compatible()
        self.peers = []

        class IncomingHandler(socketserver.BaseRequestHandler):
            """ Handler for incoming P2P connections. """
            proto = self
            def handle(self):
                if len(self.proto.peers) > MAX_PEERS:
                    # TODO: separate limits for incoming and outgoing connections
                    return

                conn = PeerConnection(self.client_address, self.proto, self.request)
                self.proto.peers.append(conn)
        self.server = SocketServer(("", listen_port), IncomingHandler)
        self.server.serve_forever_bg()

        # we want to do this only after we opened our listening socket
        self.peers.append(PeerConnection(bootstrap_peer, self))

    def broadcast_primary_block(self, block):
        """ Notifies all peers and local listeners of a new primary block. """
        self._primary_block = block.to_json_compatible()
        for peer in self.peers:
            peer.send_msg("block", self._primary_block)
        self.received_block(self._primary_block, None)

    def received(self, msg_type, msg_param, peer):
        """ Called by a PeerConnection when a new message was received. """
        getattr(self, 'received_' + msg_type)(msg_param, peer)

    def received_peer(self, peer_addr, _):
        """ Information about a peer has been received. """

        peer_addr = tuple(peer_addr)
        if len(self.peers) >= MAX_PEERS:
            return

        for peer in self.peers:
            if peer.peer_addr == peer_addr:
                return

        # TODO: if the other peer also just learned of us, we can end up with two connections (one from each direction)
        self.peers.append(PeerConnection(peer_addr, self))

    def received_getblock(self, block_hash, peer):
        """ We received a request for a new block from a certain peer. """
        for handler in self.block_request_handlers:
            block = handler(unhexlify(block_hash))
            if block is not None:
                peer.send_msg("block", block.to_json_compatible())
                break

    def received_block(self, block, _):
        """ Someone sent us a block. """
        for handler in self.block_receive_handlers:
            handler(Block.from_json_compatible(block))

    def received_transaction(self, transaction, _):
        """ Someone sent us a transaction. """
        for handler in self.trans_receive_handlers:
            handler(Transaction.from_json_compatible(block))

    def send_block_request(self, block_hash: bytes):
        """ Sends a request for a block to all our peers. """
        for peer in self.peers:
            peer.send_msg("getblock", hexlify(block_hash).decode())
