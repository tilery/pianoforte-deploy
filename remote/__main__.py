import os
import re
import sys
from io import BytesIO
from pathlib import Path

import minicli
import requests
from usine import (cd, chown, config, connect, env, exists, get, mkdir, mv,
                   put, run, screen, sudo, template)


def wget(url, dest):
    run(f'wget {url} -O {dest}')


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


@minicli.cli
def whoami():
    run('whoami')


@minicli.cli
def system():
    """Install the system deps."""
    # Not installed in minimized 18.04.
    run('which sudo || apt install sudo')
    with sudo():
        run('apt update')
        run('apt install -y postgresql postgis gdal-bin screen nginx '
            'software-properties-common wget unzip autoconf libtool g++ '
            'libmapnik-dev libleveldb1v5 libgeos-dev goaccess '
            'libprotobuf-dev unifont curl zlib1g-dev uuid-dev python-psycopg2 '
            'munin-node libdbd-pg-perl libwww-perl apache2 apache2-dev')
        run('useradd -N tilery -d /srv/tilery/ || exit 0')
        mkdir('/srv/tilery/src')
        mkdir('/srv/tilery/tmp')
        mkdir('/srv/tilery/letsencrypt/.well-known/acme-challenge')
        chown('tilery:users', '/srv/tilery/')
        run('chsh -s /bin/bash tilery')
    install_imposm()
    install_mod_tile()
    configure_mod_tile()
    configure_munin()
    install_goaccess()


@minicli.cli
def install_imposm(force=False, release='0.6.0-alpha.4'):
    """Install imposm from binary.

    :force: install even if the binary already exists.
    :release: optionnal release to install
    """
    if exists('/usr/bin/imposm') and not force:
        print('imposm already installed')
        return
    # Cf https://github.com/omniscale/imposm3/issues/165#issuecomment-395993259
    wget(f'https://github.com/omniscale/imposm3/releases/download/v{release}/imposm-{release}-linux-x86-64.tar.gz',   # noqa
         '/tmp/imposm.tar.gz')
    run('tar -xzf /tmp/imposm.tar.gz --directory /srv/tilery/src')
    with sudo():
        run(f'ln --symbolic --force '
            f'/srv/tilery/src/imposm-{release}-linux-x86-64/imposm '
            f'/usr/bin/imposm')
    with sudo(user='tilery'):
        mkdir('/srv/tilery/tmp/imposm')


@minicli.cli
def install_mod_tile(force=False):
    if not exists('/usr/bin/renderd') or force:
        with sudo():
            run('add-apt-repository --yes ppa:osmadmins/ppa')
            run('apt-get update')
            run('apt install --yes libapache2-mod-tile')


@minicli.cli
def configure_mod_tile():
    with sudo(user='tilery'):
        mkdir('/srv/tilery/tmp/tiles')
        mkdir('/srv/tilery/renderd')
    with sudo(), cd('/etc/apache2/'):
        put('remote/tile.load', 'mods-available/tile.load')
        put('remote/tile.conf', 'mods-available/tile.conf')
        put('remote/apache.conf', 'sites-enabled/000-default.conf')
        put('remote/ports.conf', 'ports.conf')
        run('a2enmod tile')


def configure_munin():
    psql_plugins = [
        'postgres_autovacuum', 'postgres_bgwriter', 'postgres_checkpoints',
        'postgres_connections_db', 'postgres_users', 'postgres_xlog',
        'nginx_status', 'nginx_request']
    with sudo(), cd('/etc/munin'):
        put('remote/munin.conf', 'munin.conf')
        for plugin in Path('remote/munin').glob('*'):
            put(plugin, f'plugins/{plugin.name}')
            run(f'chmod +x plugins/{plugin.name}')
        for name in psql_plugins:
            run(f'ln --symbolic --force /usr/share/munin/plugins/{name} '
                f'plugins/{name}')
        run('ln --symbolic --force /usr/share/munin/plugins/postgres_size_ '
            'plugins/postgres_size_tilery')
    restart(services='munin-node')


def install_goaccess():
    put('remote/run-goaccess', '/etc/cron.hourly/run-goaccess')
    run('chmod +x /etc/cron.hourly/run-goaccess')


@minicli.cli
def install_matomo():
    token = os.environ.get('MATOMO_TOKEN')
    if not token:
        sys.exist('You need to provide $MATOMO_TOKEN env var')
    wget('https://raw.githubusercontent.com/matomo-org/matomo-log-analytics'
         '/master/import_logs.py',
         '/srv/tilery/src/matomo.py')
    cron = template('remote/run-matomo', matomo_token=token)
    put(cron, '/etc/cron.daily/run-matomo')
    run('chmod +x /etc/cron.daily/run-matomo')


@minicli.cli
def http():
    """Configure Nginx and letsencrypt."""
    # When we'll have a domain.
    put('remote/piano.conf', '/etc/nginx/snippets/piano.conf')
    put('remote/forte.conf', '/etc/nginx/snippets/forte.conf')
    put('remote/letsencrypt.conf', '/etc/nginx/snippets/letsencrypt.conf')
    put('remote/ssl.conf', '/etc/nginx/snippets/ssl.conf')
    domain = config.piano_domains[0]
    pempath = f'/etc/letsencrypt/live/{domain}/fullchain.pem'
    if exists(pempath):
        print(f'{pempath} found, using https configuration')
        conf = template('remote/nginx-https.conf',
                        piano_domains=' '.join(config.piano_domains),
                        forte_domains=' '.join(config.forte_domains),
                        domain=domain)
    else:
        print(f'{pempath} not found, using http configuration')
        # Before letsencrypt.
        conf = template('remote/nginx-http.conf',
                        piano_domains=' '.join(config.piano_domains),
                        forte_domains=' '.join(config.forte_domains),
                        domain=domain)
    put(conf, '/etc/nginx/sites-enabled/pianoforte.conf')
    restart(services='nginx')


@minicli.cli
def db():
    """Create the database and the needed extensions."""
    dest = '/ssd/postgresql'
    if not exists(dest):
        src = '/var/lib/postgresql'
        mv(src, dest)
        run(f'ln --symbolic --force {dest} {src}')
        chown('postgres:postgres', src)
    with sudo(user='postgres'):
        conf = template('remote/postgresql.conf', **config)
        put(conf,
            f'/etc/postgresql/{config.psql_version}/main/postgresql.conf')
        run('createuser tilery || exit 0')
        run('createdb tilery -O tilery || exit 0')
        run('psql tilery -c "CREATE EXTENSION IF NOT EXISTS postgis"')


@minicli.cli
def bootstrap():
    """Bootstrap a new server."""
    system()
    db()
    services()
    http()
    if config.ssl:
        letsencrypt()
        # Now put the https ready Nginx conf.
        http()
    ssh_keys()


@minicli.cli
def access_logs():
    """See the nginx access logs."""
    run('tail -F /var/log/nginx/access.log')


@minicli.cli
def error_logs():
    """See the nginx access logs."""
    run('tail -F /var/log/nginx/error.log')


@minicli.cli
def letsencrypt():
    """Configure letsencrypt."""
    with sudo():
        run('add-apt-repository --yes ppa:certbot/certbot')
        run('apt update')
        run('apt install -y certbot')
    mkdir('/var/www/letsencrypt/.well-known/acme-challenge')
    domains = ','.join(list(config.piano_domains) + list(config.forte_domains))
    certbot_conf = template('remote/certbot.ini', domains=domains)
    put(certbot_conf, '/var/www/certbot.ini')
    run('certbot certonly -c /var/www/certbot.ini --non-interactive '
        '--agree-tos')


def service(name):
    with sudo():
        put(f'remote/{name}.service',
            f'/etc/systemd/system/{name}.service')
        systemctl(f'enable {name}.service')


@minicli.cli
def services():
    """Install services."""
    service('renderd')
    service('imposm')


@minicli.cli
def deploy():
    """Send config files."""
    with sudo(user='tilery'):
        mkdir('/srv/tilery/pianoforte/data')
        put(config.source_dir / 'mapping.yml', '/srv/tilery/mapping.yml')
        imposm_conf = template('remote/imposm.conf', **config)
        put(imposm_conf, '/srv/tilery/imposm.conf')
        put('remote/renderd.conf', '/etc/renderd.conf')
        put(config.source_dir / 'dist/', '/srv/tilery/pianoforte/')
        put(config.source_dir / 'fonts/', '/srv/tilery/pianoforte/fonts')
        put(config.source_dir / 'icon/', '/srv/tilery/pianoforte/icon')


def download_shapefile(name, url, force):
    datapath = '/srv/tilery/data/'
    if not exists(datapath + name) or force:
        with sudo(user='tilery'):
            wget(url, '/tmp/data.zip')
            run(f'unzip -n /tmp/data.zip -d {datapath}')


@minicli.cli
def download(force=False):
    """Download OSM data and shapefiles."""
    # path = '/srv/tilery/tmp/data.osm.pbf'
    # if not exists(path) or force:
    #     url = config.download_url
    #     with sudo(user='tilery'):
    #         wget(url, path)
    domain = 'http://data.openstreetmapdata.com/'
    download_shapefile(
        'simplified-land-polygons-complete-3857/simplified_land_polygons.shp',
        f'{domain}simplified-land-polygons-complete-3857.zip', force)
    download_shapefile('land-polygons-split-3857/land_polygons.shp',
                       f'{domain}land-polygons-split-3857.zip', force)


@minicli.cli(name='import')
def import_data(remove_backup=False, push_mapping=False, no_screen=False):
    """Import OSM data."""
    with sudo(user='tilery'), env(PGHOST='/var/run/postgresql/'):
        if push_mapping:
            put('mapping.yml', '/srv/tilery/mapping.yml')
        run('ls --full-time --time-style locale /srv/tilery/mapping.yml')
        if remove_backup:
            run('imposm import -config /srv/tilery/imposm.conf -removebackup')
        cmd = ('imposm import -diff -config /srv/tilery/imposm.conf '
               '-read /srv/tilery/tmp/data.osm.pbf -overwritecache '
               '-write -deployproduction 2>&1 | tee /tmp/imposm.log')
        if no_screen:
            run(cmd)
        else:
            with screen(name='import'):
                run(cmd)
            run('tail /tmp/imposm.log')


def import_sql_file(path):
    # Remove transaction management, as it does not cover the DROP TABLE;
    # we'll cover the transaction manually with "psql --single-transaction"
    run(f'sed -i.bak "/BEGIN;/d" {path}')
    run(f'sed -i.bak "/END;/d" {path}')
    run(f'sed -i.bak "/COMMIT;/d" {path}')
    with sudo(user='tilery'):
        run(f'psql --single-transaction -d tilery --file {path}')


@minicli.cli
def import_custom_data():
    """Send and import boundary and city SQL."""
    wget('http://nuage.yohanboniface.me/boundary.json', '/tmp/boundary.json')
    run("""ogr2ogr --config PG_USE_COPY YES -lco GEOMETRY_NAME=geometry \
        -lco DROP_TABLE=IF_EXISTS -f PGDump /tmp/boundary.sql /tmp/boundary.json -sql \
        \\'SELECT name,"name:en","name:fr","name:ar","name:es","name:de","name:ru",iso FROM boundary\\' -nln itl_boundary""")
    import_sql_file('/tmp/boundary.sql')
    wget('https://raw.githubusercontent.com/tilery/mae-boundaries/master/city.csv',
         '/tmp/city.csv')
    run("""ogr2ogr --config PG_USE_COPY YES -lco GEOMETRY_NAME=geometry \
        -lco DROP_TABLE=IF_EXISTS -f PGDump /tmp/city.sql /tmp/city.csv \
        -select name,'name:en','name:fr','name:ar',capital,type,prio,ldir \
        -nln city -oo X_POSSIBLE_NAMES=Lon* -oo Y_POSSIBLE_NAMES=Lat* \
        -oo KEEP_GEOM_COLUMNS=NO -a_srs EPSG:4326""")
    import_sql_file('/tmp/city.sql')
    wget('https://raw.githubusercontent.com/tilery/mae-boundaries/master/country.csv',
         '/tmp/country.csv')
    run("""ogr2ogr --config PG_USE_COPY YES -lco GEOMETRY_NAME=geometry \
        -lco DROP_TABLE=IF_EXISTS -f PGDump /tmp/country.sql /tmp/country.csv \
        -select name,'name:en','name:fr','name:ar',prio,iso,sov -nln country \
        -oo X_POSSIBLE_NAMES=Lon* -oo Y_POSSIBLE_NAMES=Lat* \
        -oo KEEP_GEOM_COLUMNS=NO -a_srs EPSG:4326""")
    import_sql_file('/tmp/country.sql')
    with sudo(user='tilery'):
        wget('http://nuage.yohanboniface.me/disputed.json',
             '/srv/tilery/pianoforte/data/disputed.json')


@minicli.cli
def create_index():
    """Create custom DB index."""
    psql("CREATE INDEX IF NOT EXISTS idx_road_label ON osm_roads "
         "USING GIST(geometry) WHERE name!=\\'\\' OR ref!=\\'\\'")
    psql("CREATE INDEX IF NOT EXISTS idx_boundary_low ON osm_admin "
         "USING GIST(geometry) WHERE admin_level IN (3, 4)")


@minicli.cli
def logs(lines=50, services=None):
    """See the system logs."""
    if not services:
        services = 'renderd apache2 imposm'
    services = ' --unit '.join(services.split())
    run(f'journalctl --lines {lines} --unit {services}')


@minicli.cli
def render_logs(lines=50):
    """See the renderd tile creation logs."""
    run(f'journalctl --lines {lines} --unit renderd --follow '
        '| fgrep "DONE TILE"')


@minicli.cli
def db_logs():
    """See the renderd tile creation logs."""
    run('tail -F '
        f'/var/log/postgresql/postgresql-{config.psql_version}-main.log')


@minicli.cli
def psql(query):
    """Run a psql command."""
    with sudo(user='postgres'):
        run(f'psql tilery -c "{query}"')


@minicli.cli
def clear_cache():
    """Clear tile cache."""
    run('rm -rf /srv/tilery/tmp/tiles/*')


@minicli.cli
def render(map='piano', min=1, max=10, threads=8):
    """Run a render command."""
    with sudo(user='tilery'), screen(name='render'):
        run(f'render_list --map {map} --all --force --num-threads {threads} '
            f'--socket /var/run/renderd/renderd.sock '
            f'--tile-dir /srv/tilery/tmp/tiles '
            f' --min-zoom {min} --max-zoom {max}')


@minicli.cli
def slow_query_stats(sort='date'):
    """Compile slow queries

    sort: one of "duration", "total", "date" (default: date)
    """
    pattern = re.compile(
        r'(?P<date>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (?:CES?T|UTC)) '
        r'\[\d+-\d+\] '
        r'tilery@tilery LOG:  duration: (?P<duration>\d+\.\d+) ms  execute '
        r'<unnamed>: (?P<query>SELECT ST_AsBinary\("geometry"\) AS '
        r'geom,[^\[]*AS data)', re.DOTALL)
    clean_bbox = re.compile(r'BOX3D\([^)]+\)')  # Allow to join same queries.
    queries = {}
    data = BytesIO()
    get(f'/var/log/postgresql/postgresql-{config.psql_version}-main.log', data)
    matches = pattern.findall(data.read().decode())
    for date, duration, query in matches:
        id_ = clean_bbox.sub('BBOX', query)
        if id_ not in queries:
            queries[id_] = {'date': date, 'duration': float(duration),
                            'total': 1, 'example': query}
        else:
            stats = queries[id_]
            if date > stats['date']:
                stats['date'] = date
            stats['duration'] += float(duration)
            stats['total'] += 1
    # Compute duration so we can sort by.
    for query in queries.values():
        query['duration'] = query['duration'] / query['total']
    queries = sorted(queries.values(), key=lambda d: d[sort])
    for query in queries:
        print('-'*80)
        print(query['example'])
        print('Total:', query['total'],
              '● Average duration:', int(query['duration']),
              '● Last seen', query['date'])

    print('—'*80)
    print('Total requests:', len(queries), f'(sorted by {sort})')


@minicli.wrap
def wrapper(hostname, configpath, source_dir):
    config.source_dir = Path(source_dir or os.environ.get('SOURCE_DIR')
                             or config.source_dir or 'src')
    with connect(hostname=hostname, configpath=configpath):
        yield


if __name__ == '__main__':
    minicli.run(hostname='pfmae', configpath='remote/config.yml',
                source_dir=None)
