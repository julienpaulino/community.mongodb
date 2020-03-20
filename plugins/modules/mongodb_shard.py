#!/usr/bin/python

# (c) 2018, Rhys Campbell <rhys.james.campbell@googlemail.com>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type

ANSIBLE_METADATA = {'metadata_version': '1.1',
                    'status': ['preview'],
                    'supported_by': 'community'}

DOCUMENTATION = '''
---
module: mongodb_shard
version_added: "2.8"
short_description: Add or remove shards from a MongoDB Cluster
description:
    -  Add or remove shards from a MongoDB Cluster.
author: Rhys Campbell (@rhysmeister)
options:
  login_user:
    description:
      - The MongoDB user to login with.
    required: false
    type: str
  login_password:
    description:
      - The login user's password used to authenticate with.
    required: false
    type: str
  login_database:
    description:
      - The database where login credentials are stored.
    required: false
    type: str
    default: admin
  login_host:
    description:
      - The host to login to.
      - This must be a mongos.
    required: false
    type: str
    default: localhost
  login_port:
    description:
      - The MongoDB port to login to.
    required: false
    type: int
    default: 27017
  shard:
    description:
      - The shard connection string.
      - Should be supplied in the form <replicaset>/host:port as detailed in U(https://docs.mongodb.com/manual/tutorial/add-shards-to-shard-cluster/).
      - For example rs0/example1.mongodb.com:27017.
    required: true
    type: str
  ssl:
    description:
      - Whether to use an SSL connection when connecting to the database.
    default: False
    type: bool
  ssl_cert_reqs:
    description:
      - Specifies whether a certificate is required from the other side of the connection, and whether it will be validated if provided.
    required: false
    type: str
    default: CERT_REQUIRED
    choices: [CERT_NONE, CERT_OPTIONAL, CERT_REQUIRED]
  state:
    description:
      - Whether the shard should be present or absent from the Cluster.
    required: false
    type: str
    default: present
    choices: [absent, present]

notes:
    - Requires the pymongo Python package on the remote host, version 2.4.2+.
requirements: [ pymongo ]
'''

EXAMPLES = '''
- name: Add a replicaset shard named rs1 with a member running on port 27018 on mongodb0.example.net
  mongodb_shard:
    login_user: admin
    login_password: admin
    shard: "rs1/mongodb0.example.net:27018"
    state: present

- name: Add a standalone mongod shard running on port 27018 of mongodb0.example.net
  mongodb_shard:
    login_user: admin
    login_password: admin
    shard: "mongodb0.example.net:27018"
    state: present

- name: To remove a shard called 'rs1'
  mongodb_shard:
    login_user: admin
    login_password: admin
    shard: rs1
    state: absent

# Single node shard running on localhost
- name: Ensure shard rs0 exists
  mongodb_shard:
    login_user: admin
    login_password: secret
    shard: "rs0/localhost:3001"
    state: present

# Single node shard running on localhost
- name: Ensure shard rs1 exists
  mongodb_shard:
    login_user: admin
    login_password: secret
    shard: "rs1/localhost:3002"
    state: present
'''

RETURN = '''
mongodb_shard:
    description: The name of the shard to create.
    returned: success
    type: str
'''

import os
import ssl as ssl_lib
import traceback
from distutils.version import LooseVersion

try:
    from pymongo.errors import ConnectionFailure
    from pymongo.errors import OperationFailure
    from pymongo import version as PyMongoVersion
    from pymongo import MongoClient
except ImportError:
    try:  # for older PyMongo 2.2
        from pymongo import Connection as MongoClient
    except ImportError:
        pymongo_found = False
    else:
        pymongo_found = True
else:
    pymongo_found = True

from ansible.module_utils.basic import AnsibleModule, missing_required_lib
from ansible.module_utils.six import binary_type, text_type
from ansible.module_utils.six.moves import configparser
from ansible.module_utils._text import to_native


# =========================================
# MongoDB module specific support methods.
#

def check_compatibility(module, srv_version, driver_version):
    """Check the compatibility between the driver and the database.

    See: https://docs.mongodb.com/ecosystem/drivers/driver-compatibility-reference/#python-driver-compatibility

    Args:
        module: Ansible module.
        srv_version (LooseVersion): MongoDB server version.
        driver_version (LooseVersion): Pymongo version.
    """
    msg = 'pymongo driver version and MongoDB version are incompatible: '

    if srv_version >= LooseVersion('4.2') and driver_version < LooseVersion('3.9'):
        msg += 'you must use pymongo 3.9+ with MongoDB >= 4.2'
        module.fail_json(msg=msg)

    elif srv_version >= LooseVersion('4.0') and driver_version < LooseVersion('3.7'):
        msg += 'you must use pymongo 3.7+ with MongoDB >= 4.0'
        module.fail_json(msg=msg)

    elif srv_version >= LooseVersion('3.6') and driver_version < LooseVersion('3.6'):
        msg += 'you must use pymongo 3.6+ with MongoDB >= 3.6'
        module.fail_json(msg=msg)

    elif srv_version >= LooseVersion('3.4') and driver_version < LooseVersion('3.4'):
        msg += 'you must use pymongo 3.4+ with MongoDB >= 3.4'
        module.fail_json(msg=msg)

    elif srv_version >= LooseVersion('3.2') and driver_version < LooseVersion('3.2'):
        msg += 'you must use pymongo 3.2+ with MongoDB >= 3.2'
        module.fail_json(msg=msg)

    elif srv_version >= LooseVersion('3.0') and driver_version <= LooseVersion('2.8'):
        msg += 'you must use pymongo 2.8+ with MongoDB 3.0'
        module.fail_json(msg=msg)

    elif srv_version >= LooseVersion('2.6') and driver_version <= LooseVersion('2.7'):
        msg += 'you must use pymongo 2.7+ with MongoDB 2.6'
        module.fail_json(msg=msg)


def shard_find(client, shard):
    """Check if a shard exists.

    Args:
        client (cursor): Mongodb cursor on admin database.
        shard (str): shard to check.

    Returns:
        dict: when user exists, False otherwise.
    """
    if '/' in shard:
        s = shard.split('/')[0]
    else:
        s = shard
    for shard in client["config"].shards.find({"_id": s}):
        return shard
    return False


def shard_add(client, shard):
    try:
        sh = client["admin"].command('addShard', shard)
    except Exception as excep:
        raise excep
    return sh


def shard_remove(client, shard):
    try:
        sh = client["admin"].command('removeShard', shard)
    except Exception as excep:
        raise excep
    return sh


def load_mongocnf():
    config = configparser.RawConfigParser()
    mongocnf = os.path.expanduser('~/.mongodb.cnf')

    try:
        config.readfp(open(mongocnf))
        creds = dict(
            user=config.get('client', 'user'),
            password=config.get('client', 'pass')
        )
    except (configparser.NoOptionError, IOError):
        return False

    return creds

# =========================================
# Module execution.
#


def main():
    module = AnsibleModule(argument_spec=dict(login_user=dict(type='str', required=False),
                                              login_password=dict(type='str', required=False, no_log=True),
                                              login_database=dict(type='str', required=False, default='admin'),
                                              login_host=dict(type='str', required=False, default='localhost'),
                                              login_port=dict(type='int', default=27017, required=False),
                                              ssl=dict(type='bool', default=False, required=False),
                                              ssl_cert_reqs=dict(type='str', required=False, default='CERT_REQUIRED',
                                                                 choices=['CERT_NONE', 'CERT_OPTIONAL', 'CERT_REQUIRED']),
                                              shard=dict(type='str', required=True),
                                              state=dict(type='str', required=False, default='present', choices=['absent', 'present'])),
                           supports_check_mode=True)

    if not pymongo_found:
        module.fail_json(msg=missing_required_lib('pymongo'))

    login_user = module.params['login_user']
    login_password = module.params['login_password']
    login_database = module.params['login_database']
    login_host = module.params['login_host']
    login_port = module.params['login_port']
    ssl = module.params['ssl']
    shard = module.params['shard']
    state = module.params['state']

    try:
        connection_params = {
            "host": login_host,
            "port": int(login_port)
        }

        if ssl:
            connection_params["ssl"] = ssl
            connection_params["ssl_cert_reqs"] = getattr(ssl_lib, module.params['ssl_cert_reqs'])

        client = MongoClient(**connection_params)

        try:
            # Get server version:
            try:
                srv_version = LooseVersion(client.server_info()['version'])
            except Exception as e:
                module.fail_json(msg='Unable to get MongoDB server version: %s' % to_native(e))

            # Get driver version::
            driver_version = LooseVersion(PyMongoVersion)

            # Check driver and server version compatibility:
            check_compatibility(module, srv_version, driver_version)
        except Exception as excep:
            if "not authorized on" in str(excep) or "there are no users authenticated" in str(excep):
                if login_user is not None and login_password is not None:
                    client.admin.authenticate(login_user, login_password, source=login_database)
                    check_compatibility(module, client)
                else:
                    raise excep
            else:
                raise excep

        if login_user is None and login_password is None:
            mongocnf_creds = load_mongocnf()
            if mongocnf_creds is not False:
                login_user = mongocnf_creds['user']
                login_password = mongocnf_creds['password']
        elif login_password is None or login_user is None:
            module.fail_json(msg='when supplying login arguments, both login_user and login_password must be provided')

        try:
            client['admin'].command('listDatabases', 1.0)  # if this throws an error we need to authenticate
        except Exception as excep:
            if "not authorized on" in str(excep) or "command listDatabases requires authentication" in str(excep):
                if login_user is not None and login_password is not None:
                    client.admin.authenticate(login_user, login_password, source=login_database)
                else:
                    raise excep
            else:
                raise excep

    except Exception as e:
        module.fail_json(msg='unable to connect to database: %s' % to_native(e), exception=traceback.format_exc())

    try:
        if client["admin"].command("serverStatus")["process"] != "mongos":
            module.fail_json(msg="Process running on {0}:{1} is not a mongos".format(login_host, login_port))
        shard_created = False
        if module.check_mode:
            if state == "present":
                if not shard_find(client, shard):
                    changed = True
                else:
                    changed = False
            elif state == "absent":
                if not shard_find(client, shard):
                    changed = False
                else:
                    changed = True
        else:
            if state == "present":
                if not shard_find(client, shard):
                    shard_add(client, shard)
                    changed = True
                else:
                    changed = False
            elif state == "absent":
                if shard_find(client, shard):
                    shard_remove(client, shard)
                    changed = True
                else:
                    changed = False
    except Exception as e:
        action = "add"
        if state == "absent":
            action = "remove"
        module.fail_json(msg='Unable to {0} shard: %s'.format(action) % to_native(e), exception=traceback.format_exc())

    module.exit_json(changed=changed, shard=shard)


if __name__ == '__main__':
    main()