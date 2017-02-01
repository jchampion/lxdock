import logging
import os
import subprocess
import time
from functools import wraps

from pylxd.exceptions import LXDAPIException, NotFound

from . import constants
from .exceptions import ContainerOperationFailed
from .network import EtcHosts, find_free_ip, get_ipv4_ip
from .provision import prepare_debian, provision_with_ansible, set_static_ip_on_debian
from .utils.identifier import folderid
from .utils.lxd import get_lxd_dir

logger = logging.getLogger(__name__)


def must_be_running(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        if not self.is_running:
            logger.error('The container is not running.')
            return
        return method(self, *args, **kwargs)

    return wrapper


class Container:
    """ Represents a specific container that is managed by LXD-Nomad. """

    # The default image server that will be used to pull images in "pull" mode.
    _default_image_server = 'https://images.linuxcontainers.org'

    def __init__(self, project_name, homedir, client, **options):
        self.project_name = project_name
        self.homedir = homedir
        self.client = client
        self.options = options

    #####################
    # CONTAINER ACTIONS #
    #####################

    def destroy(self):
        """ Destroys the container. """
        container = self._get_container(create=False)
        if container is None:
            logger.info("Container doesn't exist, nothing to destroy.")
            return

        # Halts the container...
        self.halt()
        # ... and destroy it!
        logger.info('Destroying container "{name}"...'.format(name=self.name))
        container.delete(wait=True)
        logger.info('Container "{name}" destroyed!'.format(name=self.name))

    def halt(self):
        """ Stops the container. """
        if self.is_stopped:
            logger.info('The container is already stopped.')
            return

        # Removes configurations related to container's hostnames if applicable.
        self._unsetup_hostnames()

        logger.info('Stopping...')
        try:
            self._container.stop(timeout=30, force=False, wait=True)
        except LXDAPIException:
            logger.warn("Can't stop the container. Forcing...")
            self._container.stop(force=True, wait=True)

    @must_be_running
    def provision(self, barebone=None):
        """ Provisions the container. """
        if barebone is None:  # None == only if the container isn't provisioned.
            barebone = not self.is_provisioned

        if barebone:
            logger.info('Doing bare bone setup on the machine...')
            prepare_debian(self._container)

        logger.info('Provisioning container "{name}"...'.format(name=self.name))
        for provisioning_item in self.options.get('provisioning', []):
            logger.info('Provisioning with {0}'.format(provisioning_item['type']))
            provision_with_ansible(self._container, provisioning_item)

        self._container.config['user.nomad.provisioned'] = 'true'
        self._container.save(wait=True)

    @must_be_running
    def shell(self):
        """ Opens a new interactive shell in the container. """
        # For now, it's much easier to call `lxc`, but eventually, we might want to contribute
        # to pylxd so it supports `interactive = True` in `exec()`.
        shellcfg = self.options.get('shell', {})
        shelluser = shellcfg.get('user')
        if shelluser:
            # This part is the result of quite a bit of `su` args trial-and-error.
            shellhome = shellcfg.get('home')
            homearg = '--env HOME={}'.format(shellhome) if shellhome else ''
            cmd = 'lxc exec {} {} -- su -m {}'.format(self.lxd_name, homearg, shelluser)
            subprocess.call(cmd, shell=True)
        else:
            cmd = 'lxc exec {} -- bash'.format(self.lxd_name)
            subprocess.call(cmd, shell=True)

    def up(self):
        """ Creates, starts and provisions the container. """
        if self.is_running:
            logger.info('Container "{name}" is already running'.format(name=self.name))
            return

        if 'shares' in self.options:
            logger.info("Setting up shares...")
            self._setup_shares()

        if self._has_static_ip:
            # If the container already previously received a static IP, we don't need to wait until
            # the container has started to assign it a new (and free) static IP. We do it now.
            self._assign_free_static_ip()

        logger.info('Starting container "{name}"...'.format(name=self.name))
        self._container.start(wait=True)
        if not self.is_running:
            logger.error('Something went wrong trying to start the container.')
            raise ContainerOperationFailed()

        ip = self._setup_ip()
        if not ip:
            return

        logger.info('Container "{name}" is up! IP: {ip}'.format(name=self.name, ip=ip))

        # Setup hostnames if applicable.
        self._setup_hostnames(ip)

        # Provisions the container if applicable.
        if not self.is_provisioned:
            self.provision(barebone=True)
        else:
            logger.info(
                'Container "{name}" already provisioned, not provisioning.'.format(name=self.name))

    ##################################
    # UTILITY METHODS AND PROPERTIES #
    ##################################

    @property
    def exists(self):
        """ Returns True if the considered container has already been created. """
        try:
            self.client.containers.get(self.lxd_name)
        except NotFound:
            return False
        else:
            return True

    @property
    def is_privileged(self):
        """ Returns a boolean indicating if the container is privileged. """
        return self._container.config.get('security.privileged') == 'true'

    @property
    def is_provisioned(self):
        """ Returns a boolean indicating if the container is provisioned. """
        return self._container.config.get('user.nomad.provisioned') == 'true'

    @property
    def is_running(self):
        """ Returns a boolean indicating if the container is running. """
        return self._container.status_code == constants.CONTAINER_RUNNING

    @property
    def is_stopped(self):
        """ Returns a boolean indicating if the container is stopped. """
        return self._container.status_code == constants.CONTAINER_STOPPED

    @property
    def lxd_name(self):
        """ Returns the name of the container that is used in the scope of LXD.

        This name id supposed to be unique among all the containers managed by LXD.
        """
        # Note: all container names must be a valid hostname! That is: maximum 63 characters, no
        # dots, no digit at first position, made entirely of letters/digits/hyphens, ...
        if not hasattr(self, '_lxd_name'):
            lxd_name_prefix = '{project_name}-{name}'.format(
                project_name=self.project_name, name=self.name)
            # We compute a project ID based on inode numbers in order to ensure that our LXD names
            # are unique.
            project_id = folderid(self.homedir)
            self._lxd_name = '{prefix}-{id}'.format(
                prefix=lxd_name_prefix[:63 - len(project_id)], id=project_id)
        return self._lxd_name

    @property
    def name(self):
        """ Returns the "local" name of the container. """
        return self.options['name']

    @property
    def status(self):
        """ Returns a string identifier representing the current status of the container. """
        default_status = 'undefined'  # Note: this status should not be displayed at all...
        container = self._get_container(create=False)
        if container is None:
            status = 'not-created'
        else:
            status = {
                constants.CONTAINER_RUNNING: 'running',
                constants.CONTAINER_STOPPED: 'stopped',
            }.get(container.status_code, default_status)
        return status

    ##################################
    # PRIVATE METHODS AND PROPERTIES #
    ##################################

    def _assign_free_static_ip(self):
        """ Assigns a free static IP to the considered container. """
        forced_ip, gateway = find_free_ip(self.client)
        set_static_ip_on_debian(self._container, forced_ip, gateway)
        self._container.config['user.nomad.static_ip'] = 'true'
        self._container.save(wait=True)

    def _get_container(self, create=True):
        """ Gets or creates the PyLXD container. """
        try:
            container = self.client.containers.get(self.lxd_name)
        except NotFound:
            container = None
        else:
            return container

        if not create:
            return

        logger.warn('Unable to find container "{name}" for directory "{homedir}"'.format(
            name=self.name, homedir=self.homedir))

        logger.info(
            'Creating new container "{name}" '
            'from image {image}'.format(name=self.lxd_name, image=self.options['image']))
        privileged = self.options.get('privileged', False)
        mode = self.options.get('mode', 'local')
        container_config = {
            'name': self.lxd_name,
            'source': {
                'alias': self.options['image'],
                # The 'mode' defines how the container will be retrieved. In "local" mode the image
                # will be determined using a local alias. In "pull" mode the image will be fetched
                # from a remote server using a remote alias.
                'mode': mode,
                # The 'protocol' to use. LXD supports two protocol: 'lxd' (RESTful API that is used
                # between the clients and a LXD daemon) and 'simplestreams' (an image server
                # description format, using JSON to describe a list of images and allowing to get
                # image information and import images). We use "simplestreams" by default (as the
                # lxc command do).
                'protocol': self.options.get('protocol', 'simplestreams'),
                # The 'server' that should be used to fetch the images. We use the default
                # linuxcontainers server for LXC and LXD when no value is provided (and if we are
                # not in "local" mode).
                'server': (self.options.get('server', self._default_image_server) if mode == 'pull'
                           else ''),
                'type': 'image',
            },
            'config': {
                'security.privileged': 'true' if privileged else 'false',
                'user.nomad.made': '1',
                'user.nomad.homedir': self.homedir,
            },
        }
        try:
            return self.client.containers.create(container_config, wait=True)
        except LXDAPIException as e:
            logger.error("Can't create container: {error}".format(error=e))
            raise ContainerOperationFailed()

    def _setup_hostnames(self, ip):
        """ Configure the potential hostnames associated with the container. """
        hostnames = self.options.get('hostnames', [])
        if not hostnames:
            return

        etchosts = EtcHosts()
        for hostname in hostnames:
            logger.info('Setting {hostname} to point to {ip}.'.format(
                hostname=hostname, ip=ip))
            etchosts.ensure_binding_present(hostname, ip)
        if etchosts.changed:
            logger.info("Saving host bindings to /etc/hosts. sudo may be needed")
            etchosts.save()

    def _setup_shares(self):
        container = self._container

        # First, let's make an inventory of shared sources that were already there.
        existing_shares = {k: d for k, d in container.devices.items() if k.startswith('nomadshare')}
        existing_sources = {d['source'] for d in existing_shares.values()}

        # Let's get rid of previously set up nomad shares.
        for k in existing_shares:
            del container.devices[k]

        # LXD uses user namespaces when running safe containers. This means that it maps a set of
        # uids and gids on the host to a set of uids and gids in the container.
        # When considering unprivileged containers we want to ensure that "root user" of such
        # containers have the proper rights to write in shared folders. To do so we have to retrieve
        # the UserID on the host-side that is mapped to the "root"'s UserID on the guest-side. This
        # will allow to set ACL on the host-side for this UID. By doing this we will also allow
        # "root" user on the guest-side to read/write in shared folders.
        host_root_uid = None
        if not self.is_privileged:
            container_path = os.path.join(get_lxd_dir(), 'containers', self.lxd_name, 'rootfs')
            container_path_stats = os.stat(container_path)
            host_root_uid = container_path_stats.st_uid

        for i, share in enumerate(self.options.get('shares', []), start=1):
            source = os.path.join(self.homedir, share['source'])
            if source not in existing_sources:
                logger.info('Setting host-side ACL for {}'.format(source))
                subprocess.Popen(
                    'setfacl -Rdm u:{}:rwX {}'.format(os.getuid(), source), shell=True).wait()
                if host_root_uid is not None:
                    # We are considering a safe container. So give the mapped root user permissions
                    # to read/write contents in the shared folders too.
                    subprocess.Popen(
                        'setfacl -Rm user:lxd:rwx,default:user:lxd:rwx,'
                        'user:{0}:rwx,default:user:{0}:rwx {1}'.format(host_root_uid, source),
                        shell=True).wait()

            shareconf = {
                'type': 'disk',
                'source': source,
                'path': share['dest'],
            }
            container.devices['nomadshare%s' % i] = shareconf
        container.save(wait=True)

    def _setup_ip(self):
        """ Setup the IP address of the considered container. """
        ip = get_ipv4_ip(self._container)
        if not ip:
            logger.info('No IP yet, waiting 10 seconds...')
            ip = self._wait_for_ipv4_ip()
        if not ip:
            logger.info('Still no IP! Forcing a static IP...')
            self._container.stop(wait=True)
            self._assign_free_static_ip()
            self._container.start(wait=True)
            ip = self._wait_for_ipv4_ip()
        if not ip:
            logger.warn('STILL no IP! Container is up, but probably broken.')
            logger.info('Maybe that restarting it will help? Not trying to provision.')
        return ip

    def _unsetup_hostnames(self):
        """ Removes the configuration associated with the hostnames of the container. """
        hostnames = self.options.get('hostnames', [])
        if not hostnames:
            return

        etchosts = EtcHosts()
        for hostname in hostnames:
            logger.info('Unsetting {hostname}. sudo needed.'.format(hostname=hostname))
            etchosts.ensure_binding_absent(hostname)
        if etchosts.changed:
            etchosts.save()

    def _wait_for_ipv4_ip(self, seconds=10):
        """ Waits some time before trying to get the IP of the container and returning it. """
        for i in range(seconds):
            time.sleep(1)
            ip = get_ipv4_ip(self._container)
            if ip:
                return ip
        return ''

    @property
    def _container(self):
        """ Returns the PyLXD Container instance associated with the considered container. """
        if not hasattr(self, '_pylxd_container'):
            self._pylxd_container = self._get_container()
        return self._pylxd_container

    @property
    def _has_static_ip(self):
        """ Returns a boolean indicating if the container has a static IP. """
        return self._container.config.get('user.nomad.static_ip') == 'true'
