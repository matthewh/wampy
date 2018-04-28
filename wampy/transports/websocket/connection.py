# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import asyncio
import logging
import socket
import ssl
import uuid
from base64 import encodestring
from socket import error as socket_error

from async_timeout import timeout

from wampy.async import async_adapter
from wampy.async.errors import WampyTimeOut
from wampy.constants import WEBSOCKET_SUBPROTOCOLS, WEBSOCKET_VERSION
from wampy.errors import (
    IncompleteFrameError, ConnectionError, WampProtocolError, WampyError)
from wampy.interfaces import Transport
from wampy.mixins import ParseUrlMixin
from wampy.serializers import json_serialize

from . frames import FrameFactory, Pong, Text

logger = logging.getLogger(__name__)


class WebSocket(Transport, ParseUrlMixin):

    def __init__(self, server_url, ipv=4):  # TODO: not pass in the loop
        self.url = server_url
        self.ipv = ipv

        self.host = None
        self.port = None
        self.resource = None

        self.parse_url()
        self.websocket_location = self.resource
        self.key = encodestring(uuid.uuid4().bytes).decode('utf-8').strip()

        self.connected = False

    async def _connect(self):
        # examples https://www.programcreek.com/python/example/85340/asyncio.open_connection
        reader, writer = await asyncio.open_connection(self.host, self.port)
        self.reader = reader
        self.writer = writer
        logger.debug("socket connected")

    async def connect(self, upgrade=True):
        # TCP connection
        await self._connect()
        await self._handshake(upgrade=upgrade)
        return self  # weird

    def disconnect(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except socket.error:
            pass

        self.socket.close()

    def send(self, message):
        frame = Text(payload=json_serialize(message))
        websocket_message = frame.frame
        self._send_raw(websocket_message)

    def _send_raw(self, websocket_message):
        logger.debug('send raw: %s', websocket_message)
        self.writer.write(websocket_message)

    async def receive(self, bufsize=1):
        frame = None
        received_bytes = bytearray()

        while True:
            try:
                print('read bytes')
                line = await self.reader.readline()
            except Exception as exc:
                print('here')
                raise ConnectionError('Connection closed: "{}"'.format(exc))
            except socket.timeout as e:
                print('here')
                message = str(e)
                raise ConnectionError('timeout: "{}"'.format(message))
            except Exception as exc:
                print('here')
                raise ConnectionError('Connection lost: "{}"'.format(exc))
            print('here')
            if not line:
                break
            print(line)
            if not line.endswith(b'\r\n'):
                raise ValueError("Line without CRLF")

            received_bytes.extend(line)

            try:
                frame = FrameFactory.from_bytes(received_bytes)
            except IncompleteFrameError as exc:
                bufsize = exc.required_bytes
            else:
                if frame.opcode == frame.OPCODE_PING:
                    # Opcode 0x9 marks a ping frame. It does not contain wamp
                    # data, so the frame is not returned.
                    # Still it must be handled or the server will close the
                    # connection.
                    async_adapter.spawn(self.handle_ping(ping_frame=frame))
                    received_bytes = bytearray()
                    continue
                if frame.opcode == frame.OPCODE_BINARY:
                    break

                if frame.opcode == frame.OPCODE_CLOSE:
                    async_adapter.spawn(self.handle_close(close_frame=frame))
                    break

                break

        if frame is None:
            raise WampProtocolError("No frame returned")

        return frame

    async def _handshake(self, upgrade):
        handshake_headers = await self._get_handshake_headers(upgrade=upgrade)
        handshake = '\r\n'.join(handshake_headers) + "\r\n\r\n"
        self.writer.write(handshake.encode())

        try:
            with timeout(2):
                self.status, self.headers = await self._read_handshake_response()
        except WampyTimeOut:
            raise WampyError(
                'No response after handshake "{}"'.format(handshake)
            )

        logger.debug("connection upgraded")

    async def _get_handshake_headers(self, upgrade):
        """ Do an HTTP upgrade handshake with the server.

        Websockets upgrade from HTTP rather than TCP largely because it was
        assumed that servers which provide websockets will always be talking to
        a browser. Maybe a reasonable assumption once upon a time...

        The headers here will go a little further and also agree the
        WAMP websocket JSON subprotocols.

        """
        headers = []
        # https://tools.ietf.org/html/rfc6455
        headers.append("GET /{} HTTP/1.1".format(self.websocket_location))
        headers.append("Host: {}:{}".format(self.host, self.port))
        headers.append("Upgrade: websocket")
        headers.append("Connection: Upgrade")
        # Sec-WebSocket-Key header containing base64-encoded random bytes,
        # and the server replies with a hash of the key in the
        # Sec-WebSocket-Accept header. This is intended to prevent a caching
        # proxy from re-sending a previous WebSocket conversation and does not
        # provide any authentication, privacy or integrity
        headers.append("Sec-WebSocket-Key: {}".format(self.key))
        headers.append("Origin: ws://{}:{}".format(self.host, self.port))
        headers.append("Sec-WebSocket-Version: {}".format(WEBSOCKET_VERSION))

        if upgrade:
            headers.append("Sec-WebSocket-Protocol: {}".format(
                WEBSOCKET_SUBPROTOCOLS)
            )

        logger.debug("connection headers: %s", headers)

        return headers

    async def _read_handshake_response(self):
        # each header ends with \r\n and there's an extra \r\n after the last
        # one
        status = None
        headers = {}

        while True:
            received_bytes = await self.reader.readline()
            if received_bytes == b'\r\n':
                # end of the response
                break

            bytes_as_str = received_bytes.decode()
            line = bytes_as_str.strip()
            #print(line)
            if not status:
                status_info = line.split(" ", 2)
                try:
                    status = int(status_info[1])
                except IndexError:
                    logger.warning('unexpected handshake resposne')
                    logger.error('%s', status_info)
                    raise

                headers['status_info'] = status_info
                headers['status'] = status
                continue

            kv = line.split(":", 1)
            if len(kv) != 2:
                raise Exception(
                    'Invalid header: "{}"'.format(line)
                )

            key, value = kv
            headers[key.lower()] = value.strip().lower()

        logger.info("handshake complete: %s : %s", status, headers)
        self.connected = True

        return status, headers

    def handle_ping(self, ping_frame):
        pong_frame = Pong(payload=ping_frame.payload)
        bytes = pong_frame.frame
        logger.info('sending pong: %s', bytes)
        self._send_raw(bytes)

    def handle_close(self, close_frame):
        message = close_frame.payload
        logger.warning('server has closed down: %s', message)
        raise ConnectionError('connection closed: {}'.format(message))


class SecureWebSocket(WebSocket):
    def __init__(self, server_url, certificate_path, ipv=4):
        super(SecureWebSocket, self).__init__(server_url=server_url, ipv=ipv)

        # PROTOCOL_TLSv1_1 and PROTOCOL_TLSv1_2 are only available if Python is
        # linked with OpenSSL 1.0.1 or later.
        try:
            self.ssl_version = ssl.PROTOCOL_TLSv1_2
        except AttributeError:
            raise WampyError("Your Python Environment does not support TLS")

        self.certificate = certificate_path

    def _connect(self):
        _socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        wrapped_socket = ssl.wrap_socket(
            _socket,
            ssl_version=self.ssl_version,
            ciphers="ECDH+AESGCM:DH+AESGCM:ECDH+AES256:DH+AES256:ECDH+AES128:\
            DH+AES:ECDH+3DES:DH+3DES:RSA+AES:RSA+3DES:!ADH:!AECDH:!MD5:!DSS",
            cert_reqs=ssl.CERT_REQUIRED,
            ca_certs=self.certificate,
        )

        try:
            wrapped_socket.connect((self.host, self.port))
        except socket_error as exc:
            if exc.errno == 61:
                logger.error(
                    'unable to connect to %s:%s', self.host, self.port
                )

            raise

        self.socket = wrapped_socket
