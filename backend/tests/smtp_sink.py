"""A tiny in-process SMTP server for tests: accepts every message (no TLS, no auth) and keeps them parsed."""

from __future__ import annotations

import email
import email.policy
import socketserver
import threading
from typing import Any


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:  # noqa: C901 - a linear SMTP dialogue
        sink: SmtpSink = self.server.sink  # type: ignore[attr-defined]
        w = self.wfile

        def say(line: str) -> None:
            w.write((line + '\r\n').encode())
            w.flush()

        say('220 sink ESMTP')
        rcpt: list[str] = []
        sender = ''
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode('utf-8', 'replace').rstrip('\r\n')
            cmd = line[:4].upper()
            if cmd in ('EHLO', 'HELO'):
                say('250-sink' if cmd == 'EHLO' else '250 sink')
                if cmd == 'EHLO':
                    say('250 8BITMIME')
            elif cmd == 'MAIL':
                sender = line.split(':', 1)[1].strip().strip('<>').split()[0] if ':' in line else ''
                rcpt = []
                say('250 OK')
            elif cmd == 'RCPT':
                addr = line.split(':', 1)[1].strip().strip('<>')
                if sink.reject and addr in sink.reject:
                    say('550 no such mailbox')
                else:
                    rcpt.append(addr)
                    say('250 OK')
            elif cmd == 'DATA':
                say('354 end with .')
                buf = []
                while True:
                    ln = self.rfile.readline()
                    if not ln or ln in (b'.\r\n', b'.\n'):
                        break
                    buf.append(ln[1:] if ln.startswith(b'..') else ln)
                msg = email.message_from_bytes(b''.join(buf), policy=email.policy.default)
                with sink.lock:
                    sink.messages.append({'from': sender, 'to': list(rcpt), 'subject': str(msg['Subject']), 'body': msg.get_content(), 'msg': msg})
                say('250 queued')
            elif cmd == 'RSET' or cmd == 'NOOP':
                say('250 OK')
            elif cmd == 'QUIT':
                say('221 bye')
                return
            else:
                say('502 not implemented')


class SmtpSink:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.reject: set[str] = set()
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), _Handler)
        self.server.daemon_threads = True
        self.server.sink = self  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f'smtp://127.0.0.1:{self.port}'

    def to(self, addr: str) -> list[dict[str, Any]]:
        with self.lock:
            return [m for m in self.messages if addr in m['to']]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
