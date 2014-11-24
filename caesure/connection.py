# -*- Mode: Python -*-

import re
import random
import struct
import time

import coro
from caesure.bitcoin import dhash
from caesure.ansi import *
from caesure.proto import VERSION, pack_inv, unpack_version

def make_nonce():
    return random.randint (0, 1 << 64)

ipv6_server_re = re.compile ('\[([A-Fa-f0-9:]+)\]:([0-9]+)')
ipv4_server_re = re.compile ('([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+):([0-9]+)')

def parse_addr_arg (addr):
    m = ipv4_server_re.match (addr)
    if not m:
        m = ipv6_server_re.match (addr)
        if not m:
            raise ValueError ("bad server address: %r" % (addr,))
    ip0, port0 = m.groups()
    port0 = int (port0)
    addr0 = (ip0, port0)
    return addr0

class BaseConnection:

    # Note: when you derive from this class you may want to tweak
    #  the protocol version depending on what features you will provide or expect.

    # protocol version
    version = 70001
    # software version
    version_string = '/caesure:201411118/'
    # relay flag (see bip37 for details...)
    relay = False

    # This class expects a 'GlobalState' object in __main__.G
    #  with the following attributes:
    #    args (an argparse instance)
    #    verbose (boolean)
    #    log (a logging method)

    def __init__ (self, my_addr, other_addr, conn=None):
        from __main__ import G
        self.G = G
        self.my_addr = my_addr
        self.other_addr = other_addr
        self.nonce = make_nonce()
        self.other_version = None
        self.send_mutex = coro.mutex()
        if conn is None:
            if ':' in other_addr[0]:
                self.conn = coro.tcp6_sock()
            else:
                self.conn = coro.tcp_sock()
        else:
            self.conn = conn
        self.packet_count = 0
        coro.spawn (self.go)

    def connect (self):
        self.G.log ('connect', self.other_addr)
        self.conn.connect (self.other_addr)

    def send_packet (self, command, payload):
        with self.send_mutex:
            lc = len(command)
            assert (lc < 12)
            cmd = command + ('\x00' * (12 - lc))
            h = dhash (payload)
            checksum, = struct.unpack ('<I', h[:4])
            self.conn.writev ([
                self.G.MAGIC,
                cmd,
                struct.pack ('<II', len(payload), checksum),
                payload
            ])
            if self.G.args.packet:
                self.G.log ('send', self.other_addr, command, payload)
            if self.G.verbose and command not in ('ping', 'pong'):
                WT (' ' + command)

    def get_our_block_height (self):
        return 0

    def send_version (self):
        v = VERSION()
        v.version = self.version
        v.services = 1
        v.timestamp = int(time.time())
        v.you_addr = (1, self.other_addr)
        v.me_addr = (1, self.my_addr)
        v.nonce = self.nonce
        v.sub_version_num = self.version_string
        start_height = self.get_our_block_height()
        if start_height < 0:
            start_height = 0
        v.start_height = start_height
        v.relay = self.relay
        self.send_packet ('version', v.pack())

    def getdata (self, items):
        "request (TX|BLOCK)+ from the other side"
        # note: pack_getdata == pack_inv
        self.send_packet ('getdata', pack_inv (items))

    def get_packet (self, timeout=1800):
        data = coro.with_timeout (timeout, self.conn.recv_exact, 24)
        if not data:
            self.G.log ('closed', self.other_addr)
            return None, None
        magic, command, length, checksum = struct.unpack ('<I12sII', data)
        command = command.strip ('\x00')
        if self.G.verbose and command not in ('ping', 'pong'):
            WF (' ' + command)
        self.packet_count += 1
        self.header = magic, command, length
        if length:
            payload = coro.with_timeout (30, self.conn.recv_exact, length)
        else:
            payload = ''
        if self.G.args.packet:
            self.G.log ('recv', self.other_addr, command, payload)
        return (command, payload)

    # please see server.py:Connection for a more complete version
    #   of incoming packet processing.

    def go (self):
        try:
            try:
                coro.with_timeout (30, self.connect)
                self.send_version()
                while 1:
                    command, payload = self.get_packet()
                    if command is None:
                        break
                    self.do_command (command, payload)
            except OSError:
                pass
            except coro.TimeoutError:
                pass
        finally:
            self.conn.close()

    def check_command_name (self, command):
        return re.match ('^[A-Za-z]+$', command) is not None

    def do_command (self, cmd, data):
        if self.check_command_name (cmd):
            method = getattr (self, 'cmd_%s' % cmd,)
            method (data)
        else:
            W ('connection: bad command %r\n' % (cmd,))

    def cmd_version (self, data):
        self.other_version = unpack_version (data)
        self.send_packet ('verack', '')

    def cmd_verack (self, data):
        pass

    def cmd_ping (self, data):
        self.send_packet ('pong', data)

    def cmd_pong (self, data):
        pass

            