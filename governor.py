#!/usr/bin/env python
import logging
import os
import sys
import time
import yaml

from helpers.api import RestApiServer
from helpers.etcd import Etcd
from helpers.postgresql import Postgresql
from helpers.ha import Ha
from helpers.utils import setup_signal_handlers, sleep
from helpers.aws import AWSConnection


class Governor:

    def __init__(self, config):
        assert config["etcd"]["ttl"] > 2 * config["loop_wait"]

        self.nap_time = config['loop_wait']
        self.etcd = Etcd(config['etcd'])
        self.aws = AWSConnection(config)
        self.postgresql = Postgresql(config['postgresql'], self.aws.on_role_change)
        self.ha = Ha(self.postgresql, self.etcd)
        host, port = config['restapi']['listen'].split(':')
        self.api = RestApiServer(self, config['restapi'])
        self.next_run = time.time()

    def touch_member(self, ttl=None):
        connection_string = self.postgresql.connection_string + '?application_name=' + self.api.connection_string
        return self.etcd.touch_member(self.postgresql.name, connection_string, ttl)

    def initialize(self):
        # FIXME: isn't there a better way testing if etcd is writable?
        # wait for etcd to be available
        while not self.touch_member():
            logging.info('waiting on etcd')
            sleep(5)

        # is data directory empty?
        if self.postgresql.data_directory_empty():
            # racing to initialize
            if self.etcd.race('/initialize', self.postgresql.name):
                self.postgresql.initialize()
                self.etcd.take_leader(self.postgresql.name)
                self.postgresql.start()
            else:
                # FIXME: touch_member?
                while True:
                    leader = self.etcd.current_leader()
                    if leader and self.postgresql.sync_from_leader(leader):
                        self.postgresql.write_recovery_conf(leader)
                        self.postgresql.start()
                        break
                    sleep(5)
        elif self.postgresql.is_running():
            self.postgresql.load_replication_slots()

    def schedule_next_run(self):
        self.next_run += self.nap_time
        current_time = time.time()
        nap_time = self.next_run - current_time
        if nap_time <= 0:
            self.next_run = current_time
        else:
            sleep(nap_time)

    def run(self):
        self.api.start()
        self.next_run = time.time()

        while True:
            self.touch_member()
            logging.info(self.ha.run_cycle())

            self.schedule_next_run()


def main():
    logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s', level=logging.INFO)
    setup_signal_handlers()

    if len(sys.argv) < 2 or not os.path.isfile(sys.argv[1]):
        print('Usage: {} config.yml'.format(sys.argv[0]))
        return

    with open(sys.argv[1], 'r') as f:
        config = yaml.load(f)

    governor = Governor(config)
    try:
        governor.initialize()
        governor.run()
    except KeyboardInterrupt:
        pass
    finally:
        governor.touch_member(300)  # schedule member removal
        governor.postgresql.stop()
        governor.etcd.delete_leader(governor.postgresql.name)


if __name__ == '__main__':
    main()
