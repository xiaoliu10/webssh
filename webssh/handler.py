import io
import json
import logging
import socket
import struct
import traceback
import weakref
import paramiko
import tornado.web

from concurrent.futures import ThreadPoolExecutor
from tornado.ioloop import IOLoop
from tornado.options import options
from tornado.process import cpu_count
from webssh.utils import (
    is_valid_ip_address, is_valid_port, is_valid_hostname, to_bytes, to_str,
    to_int, to_ip_address, UnicodeType, is_ip_hostname, is_same_primary_domain
)
from webssh.worker import Worker, recycle_worker, clients

try:
    from json.decoder import JSONDecodeError
except ImportError:
    JSONDecodeError = ValueError

try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse


DELAY = 3
KEY_MAX_SIZE = 16384
DEFAULT_PORT = 22

swallow_http_errors = True
redirecting = None


class InvalidValueError(Exception):
    pass


class MixinHandler(object):

    custom_headers = {
        'Server': 'TornadoServer'
    }

    html = ('<html><head><title>{code} {reason}</title></head><body>{code} '
            '{reason}</body></html>')

    def initialize(self, loop=None):
        self.check_request()
        self.loop = loop
        self.origin_policy = self.settings.get('origin_policy')

    def check_request(self):
        context = self.request.connection.context
        result = self.is_forbidden(context, self.request.host_name)
        self._transforms = []
        if result:
            self.set_status(403)
            self.finish(
                self.html.format(code=self._status_code, reason=self._reason)
            )
        elif result is False:
            to_url = self.get_redirect_url(
                self.request.host_name, options.sslport, self.request.uri
            )
            self.redirect(to_url, permanent=True)
        else:
            self.context = context

    def check_origin(self, origin):
        if self.origin_policy == '*':
            return True

        parsed_origin = urlparse(origin)
        netloc = parsed_origin.netloc.lower()
        logging.debug('netloc: {}'.format(netloc))

        host = self.request.headers.get('Host')
        logging.debug('host: {}'.format(host))

        if netloc == host:
            return True

        if self.origin_policy == 'same':
            return False
        elif self.origin_policy == 'primary':
            return is_same_primary_domain(netloc.rsplit(':', 1)[0],
                                          host.rsplit(':', 1)[0])
        else:
            return origin in self.origin_policy

    def is_forbidden(self, context, hostname):
        ip = context.address[0]
        lst = context.trusted_downstream
        ip_address = None

        if lst and ip not in lst:
            logging.warning(
                'IP {!r} not found in trusted downstream {!r}'.format(ip, lst)
            )
            return True

        if context._orig_protocol == 'http':
            if redirecting and not is_ip_hostname(hostname):
                ip_address = to_ip_address(ip)
                if not ip_address.is_private:
                    # redirecting
                    return False

            if options.fbidhttp:
                if ip_address is None:
                    ip_address = to_ip_address(ip)
                if not ip_address.is_private:
                    logging.warning('Public plain http request is forbidden.')
                    return True

    def get_redirect_url(self, hostname, port, uri):
        port = '' if port == 443 else ':%s' % port
        return 'https://{}{}{}'.format(hostname, port, uri)

    def set_default_headers(self):
        for header in self.custom_headers.items():
            self.set_header(*header)

    def get_value(self, name):
        value = self.get_argument(name)
        if not value:
            raise InvalidValueError('Missing value {}'.format(name))
        return value

    def get_client_addr(self):
        if options.xheaders:
            return self.get_real_client_addr() or self.context.address
        else:
            return self.context.address

    def get_real_client_addr(self):
        ip = self.request.remote_ip

        if ip == self.request.headers.get('X-Real-Ip'):
            port = self.request.headers.get('X-Real-Port')
        elif ip in self.request.headers.get('X-Forwarded-For', ''):
            port = self.request.headers.get('X-Forwarded-Port')
        else:
            # not running behind an nginx server
            return

        port = to_int(port)
        if port is None or not is_valid_port(port):
            # fake port
            port = 65535

        return (ip, port)


class NotFoundHandler(MixinHandler, tornado.web.ErrorHandler):

    def initialize(self):
        super(NotFoundHandler, self).initialize()

    def prepare(self):
        raise tornado.web.HTTPError(404)


class IndexHandler(MixinHandler, tornado.web.RequestHandler):

    executor = ThreadPoolExecutor(max_workers=cpu_count()*5)

    def initialize(self, loop, policy, host_keys_settings):
        super(IndexHandler, self).initialize(loop)
        self.policy = policy
        self.host_keys_settings = host_keys_settings
        self.ssh_client = self.get_ssh_client()
        self.privatekey_filename = None
        self.debug = self.settings.get('debug', False)
        self.result = dict(id=None, status=None, encoding=None)

    def write_error(self, status_code, **kwargs):
        if swallow_http_errors and self.request.method == 'POST':
            exc_info = kwargs.get('exc_info')
            if exc_info:
                reason = getattr(exc_info[1], 'log_message', None)
                if reason:
                    self._reason = reason
            self.result.update(status=self._reason)
            self.set_status(200)
            self.finish(self.result)
        else:
            super(IndexHandler, self).write_error(status_code, **kwargs)

    def get_ssh_client(self):
        ssh = paramiko.SSHClient()
        ssh._system_host_keys = self.host_keys_settings['system_host_keys']
        ssh._host_keys = self.host_keys_settings['host_keys']
        ssh._host_keys_filename = self.host_keys_settings['host_keys_filename']
        ssh.set_missing_host_key_policy(self.policy)
        return ssh

    def get_privatekey(self):
        name = 'privatekey'
        lst = self.request.files.get(name)
        if lst:
            # multipart form
            self.privatekey_filename = lst[0]['filename']
            data = lst[0]['body']
            value = self.decode_argument(data, name=name).strip()
        else:
            # urlencoded form
            value = self.get_argument(name, u'')

        if len(value) > KEY_MAX_SIZE:
            raise InvalidValueError(
                'Invalid private key: {}'.format(self.privatekey_filename)
            )
        return value

    @classmethod
    def get_specific_pkey(cls, pkeycls, privatekey, password):
        logging.info('Trying {}'.format(pkeycls.__name__))
        try:
            pkey = pkeycls.from_private_key(io.StringIO(privatekey),
                                            password=password)
        except paramiko.PasswordRequiredException:
            raise
        except paramiko.SSHException:
            pass
        else:
            return pkey

    @classmethod
    def get_pkey_obj(cls, privatekey, password, filename):
        bpass = to_bytes(password) if password else None

        pkey = cls.get_specific_pkey(paramiko.RSAKey, privatekey, bpass)\
            or cls.get_specific_pkey(paramiko.DSSKey, privatekey, bpass)\
            or cls.get_specific_pkey(paramiko.ECDSAKey, privatekey, bpass)\
            or cls.get_specific_pkey(paramiko.Ed25519Key, privatekey, bpass)

        if not pkey:
            if not password:
                error = 'Invalid private key: {}'.format(filename)
            else:
                error = (
                    'Wrong password {!r} for decrypting the private key.'
                ) .format(password)
            raise InvalidValueError(error)

        return pkey

    def get_hostname(self):
        value = self.get_value('hostname')
        if not (is_valid_hostname(value) or is_valid_ip_address(value)):
            raise InvalidValueError('Invalid hostname: {}'.format(value))
        return value

    def get_port(self):
        value = self.get_argument('port', u'')
        if not value:
            return DEFAULT_PORT

        port = to_int(value)
        if port is None or not is_valid_port(port):
            raise InvalidValueError('Invalid port: {}'.format(value))
        return port

    def lookup_hostname(self, hostname, port):
        key = hostname if port == 22 else '[{}]:{}'.format(hostname, port)

        if self.ssh_client._system_host_keys.lookup(key) is None:
            if self.ssh_client._host_keys.lookup(key) is None:
                raise tornado.web.HTTPError(
                        403, 'Connection to {}:{} is not allowed.'.format(
                            hostname, port)
                    )

    def get_args(self):
        hostname = self.get_hostname()
        port = self.get_port()
        if isinstance(self.policy, paramiko.RejectPolicy):
            self.lookup_hostname(hostname, port)
        username = self.get_value('username')
        password = self.get_argument('password', u'')
        privatekey = self.get_privatekey()
        if privatekey:
            pkey = self.get_pkey_obj(
                privatekey, password, self.privatekey_filename
            )
            password = None
        else:
            pkey = None
        args = (hostname, port, username, password, pkey)
        logging.debug(args)
        return args

    def get_default_encoding(self, ssh):
        try:
            _, stdout, _ = ssh.exec_command('locale charmap')
        except paramiko.SSHException:
            result = None
        else:
            result = to_str(stdout.read().strip())

        return result if result else 'utf-8'

    def ssh_connect(self, args):
        ssh = self.ssh_client
        dst_addr = args[:2]
        logging.info('Connecting to {}:{}'.format(*dst_addr))

        try:
            ssh.connect(*args, timeout=6)
        except socket.error:
            raise ValueError('Unable to connect to {}:{}'.format(*dst_addr))
        except paramiko.BadAuthenticationType:
            raise ValueError('Bad authentication type.')
        except paramiko.AuthenticationException:
            raise ValueError('Authentication failed.')
        except paramiko.BadHostKeyException:
            raise ValueError('Bad host key.')

        chan = ssh.invoke_shell(term='xterm')
        chan.setblocking(0)
        worker = Worker(self.loop, ssh, chan, dst_addr, self.src_addr)
        worker.encoding = self.get_default_encoding(ssh)
        return worker

    def check_origin(self):
        event_origin = self.get_argument('_origin', u'')
        header_origin = self.request.headers.get('Origin')
        origin = event_origin or header_origin

        if origin:
            if not super(IndexHandler, self).check_origin(origin):
                raise tornado.web.HTTPError(
                    403, 'Cross origin operation is not allowed.'
                )

            if not event_origin and self.origin_policy != 'same':
                self.set_header('Access-Control-Allow-Origin', origin)

    def head(self):
        pass

    def get(self):
        self.render('index.html', debug=self.debug)

    @tornado.gen.coroutine
    def post(self):
        if self.debug and self.get_argument('error', u''):
            # for testing purpose only
            raise ValueError('Uncaught exception')

        self.src_addr = self.get_client_addr()
        if len(clients.get(self.src_addr[0], {})) >= options.maxconn:
            raise tornado.web.HTTPError(403, 'Too many live connections.')

        self.check_origin()

        try:
            args = self.get_args()
        except InvalidValueError as exc:
            raise tornado.web.HTTPError(400, str(exc))

        future = self.executor.submit(self.ssh_connect, args)

        try:
            worker = yield future
        except (ValueError, paramiko.SSHException) as exc:
            logging.error(traceback.format_exc())
            self.result.update(status=str(exc))
        else:
            workers = clients.setdefault(worker.src_addr[0], {})
            workers[worker.id] = worker
            self.loop.call_later(DELAY, recycle_worker, worker)
            self.result.update(id=worker.id, encoding=worker.encoding)

        self.write(self.result)


class WsockHandler(MixinHandler, tornado.websocket.WebSocketHandler):

    def initialize(self, loop):
        super(WsockHandler, self).initialize(loop)
        self.worker_ref = None

    def open(self):
        self.src_addr = self.get_client_addr()
        logging.info('Connected from {}:{}'.format(*self.src_addr))

        workers = clients.get(self.src_addr[0])
        if not workers:
            self.close(reason='Websocket authentication failed.')
            return

        try:
            worker_id = self.get_value('id')
        except (tornado.web.MissingArgumentError, InvalidValueError) as exc:
            self.close(reason=str(exc))
        else:
            worker = workers.get(worker_id)
            if worker:
                workers[worker_id] = None
                if self.stream:
                    self.set_nodelay(True)
                worker.set_handler(self)
                self.worker_ref = weakref.ref(worker)
                self.loop.add_handler(worker.fd, worker, IOLoop.READ)
            else:
                self.close(reason='Websocket authentication failed.')

    def on_message(self, message):
        logging.debug('{!r} from {}:{}'.format(message, *self.src_addr))
        worker = self.worker_ref()
        try:
            msg = json.loads(message)
        except JSONDecodeError:
            return

        if not isinstance(msg, dict):
            return

        resize = msg.get('resize')
        if resize and len(resize) == 2:
            try:
                worker.chan.resize_pty(*resize)
            except (TypeError, struct.error, paramiko.SSHException):
                pass

        data = msg.get('data')
        if data and isinstance(data, UnicodeType):
            worker.data_to_dst.append(data)
            worker.on_write()

    def on_close(self):
        if self.close_reason:
            logging.info(
                'Disconnecting to {}:{} with reason: {reason}'.format(
                    *self.src_addr, reason=self.close_reason
                )
            )
        else:
            self.close_reason = 'client disconnected'
            logging.info('Disconnected from {}:{}'.format(*self.src_addr))

        worker = self.worker_ref() if self.worker_ref else None
        if worker:
            worker.close(reason=self.close_reason)
