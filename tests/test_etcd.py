import unittest
import requests
import time
import json

from helpers.etcd import Cluster, Etcd
from helpers.errors import EtcdError, CurrentLeaderError


class MockResponse:

    def __init__(self):
        self.status_code = 200
        self.content = '{}'
        self.ok = True

    def json(self):
        return json.loads(self.content)


class MockPostgresql:
    name = ''

    def last_operation(self):
        return 0


def requests_get(url, **kwargs):
    if url.startswith('http://local'):
        raise requests.exceptions.RequestException()
    response = MockResponse()
    if url.startswith('http://remote') or url.startswith('http://127.0.0.1'):
        response.content = '{"action":"get","node":{"key":"/service/batman5","dir":true,"nodes":[{"key":"/service/batman5/initialize","value":"postgresql0","modifiedIndex":1582,"createdIndex":1582},{"key":"/service/batman5/leader","value":"postgresql1","expiration":"2015-05-15T09:11:00.037397538Z","ttl":21,"modifiedIndex":20728,"createdIndex":20434},{"key":"/service/batman5/optime","dir":true,"nodes":[{"key":"/service/batman5/optime/leader","value":"2164261704","modifiedIndex":20729,"createdIndex":20729}],"modifiedIndex":20437,"createdIndex":20437},{"key":"/service/batman5/members","dir":true,"nodes":[{"key":"/service/batman5/members/postgresql1","value":"postgres://replicator:rep-pass@127.0.0.1:5434/postgres?application_name=http://127.0.0.1:8009/governor","expiration":"2015-05-15T09:10:59.949384522Z","ttl":21,"modifiedIndex":20727,"createdIndex":20727},{"key":"/service/batman5/members/postgresql0","value":"postgres://replicator:rep-pass@127.0.0.1:5433/postgres?application_name=http://127.0.0.1:8008/governor","expiration":"2015-05-15T09:11:09.611860899Z","ttl":30,"modifiedIndex":20730,"createdIndex":20730}],"modifiedIndex":1581,"createdIndex":1581}],"modifiedIndex":1581,"createdIndex":1581}}'
    elif url.startswith('http://other'):
        response.status_code = 404
    elif url.startswith('http://noleader'):
        response.content = '{"action":"get","node":{"key":"/service/batman5","dir":true,"nodes":[{"key":"/service/batman5/initialize","value":"postgresql0","modifiedIndex":1582,"createdIndex":1582},{"key":"/service/batman5/leader","value":"postgresql1","expiration":"2015-05-15T09:11:00.037397538Z","ttl":21,"modifiedIndex":20728,"createdIndex":20434},{"key":"/service/batman5/optime","dir":true,"nodes":[{"key":"/service/batman5/optime/leader","value":"2164261704","modifiedIndex":20729,"createdIndex":20729}],"modifiedIndex":20437,"createdIndex":20437},{"key":"/service/batman5/members","dir":true,"nodes":[{"key":"/service/batman5/members/postgresql0","value":"postgres://replicator:rep-pass@127.0.0.1:5433/postgres?application_name=http://127.0.0.1:8008/governor","expiration":"2015-05-15T09:11:09.611860899Z","ttl":30,"modifiedIndex":20730,"createdIndex":20730}],"modifiedIndex":1581,"createdIndex":1581}],"modifiedIndex":1581,"createdIndex":1581}}'
    else:
        response.status_code = 404
        response.ok = False
    return response


def requests_put(url, **kwargs):
    if url.startswith('http://local') or '/optime/leader' in url:
        raise requests.exceptions.RequestException()
    response = MockResponse()
    response.status_code = 201
    if url.startswith('http://other'):
        response.status_code = 404
    return response


def requests_delete(url):
    if url.startswith('http://local'):
        raise requests.exceptions.RequestException()
    response = MockResponse()
    response.status_code = 204
    return response


def time_sleep(_):
    pass


class TestEtcd(unittest.TestCase):

    def __init__(self, method_name='runTest'):
        self.setUp = self.set_up
        super(TestEtcd, self).__init__(method_name)

    def set_up(self):
        requests.get = requests_get
        requests.put = requests_put
        requests.delete = requests_delete
        time.sleep = time_sleep
        self.etcd = Etcd({'ttl': 30, 'host': 'localhost', 'scope': 'test'})

    def test_get_client_path(self):
        self.assertRaises(Exception, self.etcd.get_client_path, '', 2)

    def test_put_client_path(self):
        self.assertRaises(EtcdError, self.etcd.put_client_path, '')

    def test_delete_client_path(self):
        self.assertFalse(self.etcd.delete_client_path(''))

    def test_get_cluster(self):
        self.assertRaises(EtcdError, self.etcd.get_cluster)
        self.etcd.base_client_url = self.etcd.base_client_url.replace('local', 'remote')
        cluster = self.etcd.get_cluster()
        self.assertIsInstance(cluster, Cluster)
        self.etcd.base_client_url = self.etcd.base_client_url.replace('remote', 'other')
        self.etcd.get_cluster()
        self.etcd.base_client_url = self.etcd.base_client_url.replace('other', 'noleader')
        self.etcd.get_cluster()

    def test_current_leader(self):
        self.assertRaises(CurrentLeaderError, self.etcd.current_leader)

    def test_touch_member(self):
        self.assertFalse(self.etcd.touch_member('', ''))

    def test_take_leader(self):
        self.assertFalse(self.etcd.take_leader(''))

    def test_attempt_to_acquire_leader(self):
        self.assertFalse(self.etcd.attempt_to_acquire_leader(''))

    def test_update_leader(self):
        self.etcd.base_client_url = self.etcd.base_client_url.replace('local', 'remote')
        self.assertTrue(self.etcd.update_leader(MockPostgresql()))
        self.etcd.base_client_url = self.etcd.base_client_url.replace('remote', 'other')
        self.assertFalse(self.etcd.update_leader(MockPostgresql()))

    def test_race(self):
        self.assertFalse(self.etcd.race('', ''))

    def test_delete_member(self):
        self.assertFalse(self.etcd.delete_member(''))
