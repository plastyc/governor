import logging
import re
import requests
from requests.exceptions import RequestException
import types
import boto.ec2

logger = logging.getLogger(__name__)


class AWSConnection:
    def __init__(self, config):
        self.available = False
        self.config = config

        if 'cluster_name' in config:
            self.cluster_name = config.get('cluster_name')
        elif 'etcd' in config and type(config['etcd']) == types.DictType:
            self.cluster_name = config['etcd'].get('scope', 'unknown')
        else:
            self.cluster_name = 'unknown'
        try:
            # get the instance id
            r = requests.get('http://169.254.169.254/latest/meta-data/instance-id', timeout=0.1)
            if r.ok:
                self.instance_id = r.content.strip()
                r = requests.get('http://169.254.169.254/latest/meta-data/placement/availability-zone', timeout=0.1)
                if r.ok:
                    # get the region from the availability zone, i.e. eu-west-1 from eu-west-1c
                    m = re.match(r'(\w+-\w+-\d+)[a-z]+', r.content)
                    if m:
                        self.region = m.group(1)
                        self.available = True
        except RequestException:
            logger.info("cannot query AWS meta-data")
            pass

    def aws_available(self):
        return self.available

    def _tag_ebs(self, role):
        """ set tags, carrying the cluster name, instance role and instance id for the EBS storage """
        if not self.available:
            return False

        tags = {'Name': self.cluster_name, 'Role': role, 'Instance': self.instance_id}
        try:
            conn = boto.ec2.connect_to_region(self.region)
            # get all volumes attached to the current instance
            volumes = conn.get_all_volumes(filter={'attachment.instance-id': self.instance_id})
            if volumes:
                conn.create_tags([v.id for v in volumes], tags)
        except Exception as e:
            logger.info('could not set tags for EBS storage devices attached: {}'.format(e))
            return False
        return True

    def _tag_ec2(self, role):
        """ tag the current EC2 instance with a cluster role """
        if not self.available:
            return False
        tags = {'Role', role}
        try:
            conn = boto.ec2.connect_to_region(self.region)
            instances = conn.get_all_reservations(instance_ids=[self.instance_id])
            if instances:
                conn.create_tag([instances[0].id], tags)
        except Exception as e:
            logger.info("could not set tags for EC2 instance {}: {}".format(self.instance_id, e))
            return False
        return True

    def on_role_change(self, new_role):
        self._tag_ec2(new_role)
        self._tag_ebs('spilo_' + self.cluster_name, new_role)

if __name__ == '__main__':
    import yaml
    config_string = """
loop_wait: 10
restapi:
  listen: 0.0.0.0:8008
  connect_address: 127.0.0.1:5432
etcd:
  scope: test
  ttl: 30
  host: 127.0.0.1:8080
postgresql:
  name: postgresql_foo
  listen: 0.0.0.0:5432
  connect_address: 127.0.0.1:5432
  data_dir: /home/postgres/pgdata/data
  replication:
    username: standby
    password: standby
    network: 0.0.0.0/0
  superuser:
    password: zalando
  admin:
    username: admin
    password: admin
  parameters:
    archive_mode: "on"
    wal_level: hot_standby
    max_wal_senders: 5
    wal_keep_segments: 8
    archive_timeout: 1800s
    max_replication_slots: 5
    hot_standby: "on"
    ssl: "on"
"""
    awsconnection = AWSConnection(yaml.load(config_string))
    print "AWS available: {}, Cluster_name: {}".format(awsconnection.available, awsconnection.cluster_name)
    if awsconnection.available:
        print "AWS Region: {}, Instance_id: {}".format(awsconnection.region, awsconnection.instance_id)