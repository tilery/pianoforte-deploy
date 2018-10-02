import os
from pathlib import Path

import minicli
import requests
from usine import config, connect, run, sudo


def append_line(path, line):
    run(f'grep -q -r "{line}" {path} || echo "{line}" | tee --append {path}')


@minicli.cli
def ssh_keys():
    """Install ssh keys from remote urls."""
    with sudo():
        for name, url in config.get('ssh_key_urls', {}).items():
            key = requests.get(url).text.replace('\n', '')
            run('grep -q -r "{key}" .ssh/authorized_keys || echo "{key}" '
                '| tee --append .ssh/authorized_keys'.format(key=key))


@minicli.cli
def systemctl(*args):
    """Run a systemctl command on the remote server.

    :command: the systemctl command to run.
    """
    run(f'systemctl {" ".join(args)}')


@minicli.cli
def restart(services=None):
    """Restart services."""
    services = services or 'renderd apache2 imposm munin-node'
    with sudo():
        systemctl(f'restart {services}')


@minicli.wrap
def wrapper(hostname, configpath, source_dir):
    config.source_dir = Path(source_dir or os.environ.get('SOURCE_DIR')
                             or config.source_dir or 'src')
    with connect(hostname=hostname, configpath=configpath):
        yield


def main(hostname='tilery', configpath='remote/config/prod.yml'):
    minicli.run(hostname=hostname, configpath=configpath,
                source_dir=None)
