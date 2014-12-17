import logging
import shlex
import struct
import six

from docker.utils import kwargs_from_env
from cattle import default_value, Config

log = logging.getLogger('docker')

_ENABLED = True

DOCKER_COMPUTE_LISTENER = 'docker-compute-listener'
DOCKER_STREAM_STDOUT = 1
DOCKER_STREAM_STDERR = 2

STREAM_HEADER_SIZE_BYTES = 8

try:
    from docker import Client
except:
    log.info('Disabling docker, docker-py not found')
    _ENABLED = False


class DockerConfig:
    def __init__(self):
        pass

    @staticmethod
    def docker_enabled():
        return default_value('DOCKER_ENABLED', 'true') == 'true'

    @staticmethod
    def docker_host_ip():
        return default_value('DOCKER_HOST_IP', Config.agent_ip())

    @staticmethod
    def docker_home():
        return default_value('DOCKER_HOME', '/var/lib/docker')

    @staticmethod
    def docker_uuid_file():
        def_value = '{0}/.docker_uuid'.format(Config.home())
        return default_value('DOCKER_UUID_FILE', def_value)

    @staticmethod
    def docker_uuid():
        return Config.get_uuid_from_file('DOCKER_UUID',
                                         DockerConfig.docker_uuid_file())

    @staticmethod
    def url_base():
        return default_value('DOCKER_URL_BASE', None)

    @staticmethod
    def api_version():
        return default_value('DOCKER_API_VERSION', '1.15')

    @staticmethod
    def docker_required():
        return default_value('DOCKER_REQUIRED', 'true') == 'true'

    @staticmethod
    def delegate_timeout():
        return int(default_value('DOCKER_DELEGATE_TIMEOUT', '120'))

    @staticmethod
    def use_boot2docker_connection_env_vars():
        use_b2d = default_value('DOCKER_USE_BOOT2DOCKER', 'false')
        return use_b2d.lower() == 'true'


class ClientSock:
    def __init__(self, response, client, stream):
        self.response = response
        self.client = client
        self.stream = stream

    def read(self, stdout=True, stderr=True):
        res = self.response
        if self.stream:
            return self.client._multiplexed_socket_stream_helper(res,
                                                                 stdout,
                                                                 stderr)
        else:
            return str().join(
                [x for x in self.client._multiplexed_buffer_helper(res,
                                                                   stdout,
                                                                   stderr)]
            )

    def write(self, data):
        self.socket.sendall(data)


class RancherClient(Client):
    def __init__(self, base_url=None, version='1.15', tls=False, timeout=60):
        super(RancherClient, self).__init__(base_url=base_url,
                                            version=version,
                                            tls=tls,
                                            timeout=timeout)

    def _multiplexed_buffer_helper(self, response, stdout=True, stderr=True):
        """A generator of multiplexed data blocks read from a buffered
        response."""
        buf = self._result(response, binary=True)
        walker = 0
        while True:
            if len(buf[walker:]) < 8:
                break
            stream_type, length = struct.unpack_from('>BxxxL', buf[walker:])
            start = walker + STREAM_HEADER_SIZE_BYTES
            end = start + length
            walker = end
            if stream_type == DOCKER_STREAM_STDOUT and not stdout:
                continue
            if stream_type == DOCKER_STREAM_STDERR and not stderr:
                continue
            yield buf[start:end]

    def _multiplexed_socket_stream_helper(self, response, stdout=True,
                                          stderr=True):
        """A generator of multiplexed data blocks coming from a response
        socket."""
        socket = self._get_raw_response_socket(response)

        def recvall(socket, size):
            blocks = []
            while size > 0:
                block = socket.recv(size)
                if not block:
                    return None

                blocks.append(block)
                size -= len(block)

            sep = bytes()
            data = sep.join(blocks)
            return data

        while True:
            socket.settimeout(None)
            header = recvall(socket, STREAM_HEADER_SIZE_BYTES)
            if not header:
                break
            stream_type, length = struct.unpack('>BxxxL', header)
            if not length:
                break
            data = recvall(socket, length)
            if stream_type == DOCKER_STREAM_STDOUT and not stdout:
                continue
            if stream_type == DOCKER_STREAM_STDERR and not stderr:
                continue
            if not data:
                break
            yield data

    def execute(self, container, cmd, detach=False, stdout=True, stderr=True,
                stream=False, tty=False):
        cmd_id = self.executeCreate(container, cmd, detach, stdout, stderr,
                                    stream, False, tty)
        return self.executeStart(cmd_id, detach, tty, stream).read()

    def executeCreate(self, container, cmd, detach=False,
                      stdout=True, stderr=True, stream=False,
                      stdin=False, tty=False):
        if isinstance(container, dict):
            container = container.get('Id')
        if isinstance(cmd, six.string_types):
            cmd = shlex.split(str(cmd))

        data = {
            'Container': container,
            'User': '',
            'Privileged': False,
            'Tty': tty,
            'AttachStdin': stdin,
            'AttachStdout': stdout,
            'AttachStderr': stderr,
            'Detach': detach,
            'Cmd': cmd
        }

        # create the command
        url = self._url('/containers/{0}/exec'.format(container))
        res = self._post_json(url, data=data)
        self._raise_for_status(res)

        cmd_id = res.json().get('Id')
        return cmd_id

    def executeStart(self, cmd_id, detach=False, tty=False, stream=False):
        data = {
            'Detach': detach,
            'Tty': tty
        }

        res = self._post_json(self._url('/exec/{0}/start'.format(cmd_id)),
                              data=data, stream=stream)
        self._raise_for_status(res)
        return ClientSock(res, self, stream)


def docker_client(version=None):
    if DockerConfig.use_boot2docker_connection_env_vars():
        kwargs = kwargs_from_env(assert_hostname=False)
    else:
        kwargs = {'base_url': DockerConfig.url_base()}

    if version is None:
        version = DockerConfig.api_version()

    kwargs['version'] = version

    return RancherClient(**kwargs)


def pull_image(image, progress):
    _DOCKER_POOL.pull_image(image, progress)


def get_compute():
    return _DOCKER_COMPUTE


try:
    if _ENABLED:
        docker_client().info()
except Exception, e:
    log.exception('Disabling docker, could not contact docker')
    _ENABLED = False

if _ENABLED and DockerConfig.docker_enabled():
    from .storage import DockerPool
    from .compute import DockerCompute
    from .network.setup import NetworkSetup
    from .network.links import LinkSetup
    from .network.ipsec_tunnel import IpsecTunnelSetup
    from .network.ports import PortSetup
    from .delegate import DockerDelegate
    from cattle import type_manager

    _DOCKER_POOL = DockerPool()
    _DOCKER_COMPUTE = DockerCompute()
    _DOCKER_DELEGATE = DockerDelegate()
    type_manager.register_type(type_manager.STORAGE_DRIVER, _DOCKER_POOL)
    type_manager.register_type(type_manager.COMPUTE_DRIVER, _DOCKER_COMPUTE)
    type_manager.register_type(DOCKER_COMPUTE_LISTENER, _DOCKER_DELEGATE)
    type_manager.register_type(DOCKER_COMPUTE_LISTENER, NetworkSetup())
    type_manager.register_type(DOCKER_COMPUTE_LISTENER, LinkSetup())
    type_manager.register_type(DOCKER_COMPUTE_LISTENER, IpsecTunnelSetup())
    type_manager.register_type(DOCKER_COMPUTE_LISTENER, PortSetup())
    type_manager.register_type(type_manager.PRE_REQUEST_HANDLER,
                               _DOCKER_DELEGATE)

if not _ENABLED and DockerConfig.docker_required():
    raise Exception('Failed to initialize Docker')
