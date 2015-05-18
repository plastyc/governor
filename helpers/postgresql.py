import logging
import os
import psycopg2
import re
import sys
import time
from OpenSSL import crypto

is_py3 = sys.hexversion >= 0x03000000

if is_py3:
    from urllib.parse import urlparse
else:
    from urlparse import urlparse


logger = logging.getLogger(__name__)


def parseurl(url):
    r = urlparse(url)
    return {
        'hostname': r.hostname,
        'port': r.port or 5432,
        'username': r.username,
        'password': r.password,
    }


class Postgresql:

    def __init__(self, config):
        self.name = config['name']
        self.host, self.port = config['listen'].split(':')
        self.libpq_parameters = {
            'host': self.host,
            'port': self.port,
            'fallback_application_name': 'Governor',
            'connect_timeout': 5,
            'options': '-c statement_timeout=2000'
        }
        self.data_dir = config['data_dir']
        self.replication = config['replication']
        self.superuser = config['superuser']
        self.admin = config['admin']
        self.recovery_conf = os.path.join(self.data_dir, 'recovery.conf')
        self._pg_ctl = 'pg_ctl -w -D ' + self.data_dir

        self.config = config

        self.connection_string = 'postgres://{username}:{password}@{connect_address}/postgres'.format(
            connect_address=self.config['connect_address'], **self.replication)

        self.conn = None
        self.cursor_holder = None
        self.members = []  # list of already existing replication slots

    def cursor(self):
        if not self.cursor_holder:
            self.conn = psycopg2.connect(**self.libpq_parameters)
            self.conn.autocommit = True
            self.cursor_holder = self.conn.cursor()

        return self.cursor_holder

    def disconnect(self):
        try:
            self.conn.close()
        except:
            logger.exception('Error disconnecting')

    def query(self, sql, *params):
        max_attempts = 0
        while True:
            try:
                self.cursor().execute(sql, params)
                break
            except psycopg2.OperationalError as e:
                if self.conn:
                    self.disconnect()
                self.cursor_holder = None
                if max_attempts > 4:
                    raise e
                max_attempts += 1
                time.sleep(5)
        return self.cursor()

    def data_directory_empty(self):
        return not os.path.exists(self.data_dir) or os.listdir(self.data_dir) == []

    def initialize(self):
        if os.system(self._pg_ctl + ' initdb -o --encoding=UTF8') == 0:
            self.generate_dummy_certificate()
            self.write_pg_hba()

            return True

        return False

    def sync_from_leader(self, leader):
        r = parseurl(leader.address)

        pgpass = 'pgpass'
        with open(pgpass, 'w') as f:
            os.fchmod(f.fileno(), 0o600)
            f.write('{hostname}:{port}:*:{username}:{password}\n'.format(**r))

        try:
            os.environ['PGPASSFILE'] = pgpass
            return os.system('pg_basebackup -R -D {data_dir} --host={hostname} --port={port} -U {username}'.format(
                data_dir=self.data_dir, **r)) == 0
        finally:
            os.environ.pop('PGPASSFILE')

    def is_leader(self):
        return not self.query('SELECT pg_is_in_recovery()').fetchone()[0]

    def is_running(self):
        return os.system(self._pg_ctl + ' status > /dev/null') == 0

    def start(self):
        if self.is_running():
            self.load_replication_slots()
            logger.error('Cannot start PostgreSQL because one is already running.')
            return False

        pid_path = os.path.join(self.data_dir, 'postmaster.pid')
        if os.path.exists(pid_path):
            os.remove(pid_path)
            logger.info('Removed %s', pid_path)

        ret = os.system(self._pg_ctl + ' start -o "{}"'.format(self.server_options())) == 0
        ret and self.load_replication_slots()
        return ret

    def stop(self):
        return os.system(self._pg_ctl + ' stop -m fast') != 0

    def reload(self):
        return os.system(self._pg_ctl + ' reload') == 0

    def restart(self):
        return os.system(self._pg_ctl + ' restart -m fast') == 0

    def server_options(self):
        options = '--listen_addresses={} --port={}'.format(self.host, self.port)
        for setting, value in self.config['parameters'].items():
            options += " --{}='{}'".format(setting, value)
        return options

    def generate_dummy_certificate(self):
        key = crypto.PKey()
        key.generate_key(crypto.TYPE_RSA, 4096)

        cert = crypto.X509()
        cert.get_subject().C   = 'EU'
        cert.get_subject().ST  = 'Cirrocumulus'
        cert.get_subject().L   = 'Sky'
        cert.get_subject().O   = 'Zalando'
        cert.get_subject().OU  = 'ACID'
        cert.get_subject().CN  = 'Spilo PostgreSQL Appliance Dummy'
        cert.set_serial_number(1000)
        cert.gmtime_adj_notBefore(0)
        cert.gmtime_adj_notAfter(10*365*24*60*60)

        cert.set_issuer( cert.get_subject() )
        cert.set_pubkey(key)
        cert.sign(key, 'sha512')

        with open(self.data_dir+'/server.crt', 'w') as pub:
            pub.write( crypto.dump_certificate(crypto.FILETYPE_PEM, cert) )

        with open(self.data_dir+'/server.key', 'w') as private:
            private.write( crypto.dump_privatekey(crypto.FILETYPE_PEM, key) )
            os.fchmod(private.fileno(), 0o600)

    def is_healthy(self):
        if not self.is_running():
            logger.warning('Postgresql is not running.')
            return False
        return True

    def is_healthiest_node(self, last_leader_operation, members):
        if (last_leader_operation or 0) - self.xlog_position() > self.config.get('maximum_lag_on_failover', 0):
            return False

        for member in members:
            if member.hostname == self.name:
                continue
            try:
                member_conn = psycopg2.connect(member.address)
                member_conn.autocommit = True
                member_cursor = member_conn.cursor()
                member_cursor.execute(
                    "SELECT %s - (pg_last_xlog_replay_location() - '0/0000000'::pg_lsn)", (self.xlog_position(), ))
                xlog_diff = member_cursor.fetchone()[0]
                logger.info([self.name, member.hostname, xlog_diff])
                if xlog_diff < 0:
                    member_cursor.close()
                    return False
                member_cursor.close()
            except psycopg2.OperationalError:
                continue
        return True

    def replication_slot_name(self):
        member = os.environ.get("MEMBER")
        (member, _) = re.subn(r'[^a-z0-9]+', r'_', member)
        return member

    def write_pg_hba(self):
        with open(os.path.join(self.data_dir, 'pg_hba.conf'), 'a') as f:
            f.write('host replication {username} {network} md5'.format(**self.replication))
            # allow TCP connections from the host's own address
            f.write("\nhost postgres postgres samehost trust\n")
            # allow TCP connections from the rest of the world with a password
            f.write("\nhost all all 0.0.0.0/0 md5\n")

    @staticmethod
    def primary_conninfo(leader_url):
        r = parseurl(leader_url)
        return 'user={username} password={password} host={hostname} port={port} sslmode=prefer sslcompression=1'.format(**r)

    def check_recovery_conf(self, leader):
        if not os.path.isfile(self.recovery_conf):
            return False

        pattern = leader and leader.address and self.primary_conninfo(leader.address)

        with open(self.recovery_conf, 'r') as f:
            for line in f:
                if line.startswith('primary_conninfo'):
                    if not pattern:
                        return False
                    return pattern in line

        return not pattern

    def write_recovery_conf(self, leader):
        with open(self.recovery_conf, 'w') as f:
            f.write("""standby_mode = 'on'
recovery_target_timeline = 'latest'
""")
            if leader and leader.address:
                f.write("""
primary_slot_name = '{}'
primary_conninfo = '{}'
""".format(self.name, self.primary_conninfo(leader.address)))
                for name, value in self.config.get('recovery_conf', {}).items():
                    f.write("{} = '{}'\n".format(name, value))

    def follow_the_leader(self, leader):
        if self.check_recovery_conf(leader):
            return
        self.write_recovery_conf(leader)
        self.restart()

    def promote(self):
        return os.system(self._pg_ctl + ' promote') == 0

    def demote(self, leader):
        self.follow_the_leader(leader)

    def create_replication_user(self):
        self.query('CREATE USER "{}" WITH REPLICATION ENCRYPTED PASSWORD %s'.format(
            self.replication['username']), self.replication['password'])

    def create_connection_users(self):
        if self.superuser:
            if 'username' in self.superuser:
                self.query('CREATE ROLE "{0}" WITH LOGIN SUPERUSER PASSWORD %s'.format(
                    self.superuser['username']), self.superuser['password'])
            else:
                self.query('ALTER ROLE postgres WITH PASSWORD %s', self.superuser['password'])
        if self.admin:
            self.query('CREATE ROLE "{0}" WITH LOGIN CREATEDB CREATEROLE PASSWORD %s'.format(
                self.admin['username']), self.admin['password'])

    def xlog_position(self):
        return self.query("SELECT pg_last_xlog_replay_location() - '0/0000000'::pg_lsn").fetchone()[0]

    def load_replication_slots(self):
        cursor = self.query("SELECT slot_name FROM pg_replication_slots WHERE slot_type='physical'")
        self.members = [r[0] for r in cursor]

    def create_replication_slots(self, members):
        # drop unused slots
        for slot in set(self.members) - set(members):
            self.query("""SELECT pg_drop_replication_slot(%s)
                           WHERE EXISTS(SELECT 1 FROM pg_replication_slots
                           WHERE slot_name = %s)""", slot, slot)

        # create new slots
        for slot in set(members) - set(self.members):
            self.query("""SELECT pg_create_physical_replication_slot(%s)
                           WHERE NOT EXISTS (SELECT 1 FROM pg_replication_slots
                           WHERE slot_name = %s)""", slot, slot)
        self.members = members

    def last_operation(self):
        return self.query("SELECT pg_current_xlog_location() - '0/00000'::pg_lsn").fetchone()[0]
