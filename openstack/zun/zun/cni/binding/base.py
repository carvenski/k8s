#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import abc
import errno

import os_vif
from oslo_config import cfg
from oslo_log import log as logging
import pyroute2
from pyroute2 import netns as pyroute_netns
from stevedore import driver as stv_driver

from zun.cni import utils
from zun.common import consts
from zun.common import privileged


_BINDING_NAMESPACE = 'zun.cni.binding'
LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class BaseBindingDriver(object, metaclass=abc.ABCMeta):
    """Interface to attach ports to capsules/containers."""

    @abc.abstractmethod
    def connect(self, vif, ifname, netns, container_id):
        raise NotImplementedError()

    @abc.abstractmethod
    def disconnect(self, vif, ifname, netns, container_id):
        raise NotImplementedError()


def _get_binding_driver(vif):
    mgr = stv_driver.DriverManager(namespace=_BINDING_NAMESPACE,
                                   name=type(vif).__name__,
                                   invoke_on_load=True)
    return mgr.driver


def get_ipdb(netns=None):
    if netns:
        ipdb = pyroute2.IPDB(nl=pyroute2.NetNS(netns))
    else:
        ipdb = pyroute2.IPDB()
    return ipdb


def _enable_ipv6(netns):
    # Docker disables IPv6 for --net=none containers
    # TODO(apuimedo) remove when it is no longer the case
    try:
        path = utils.convert_netns('/proc/self/ns/net')
        self_ns_fd = open(path)
        pyroute_netns.setns(netns)
        path = utils.convert_netns('/proc/sys/net/ipv6/conf/all/disable_ipv6')
        with open(path, 'w') as disable_ipv6:
            disable_ipv6.write('0')
    except Exception:
        raise
    finally:
        pyroute_netns.setns(self_ns_fd)


@privileged.cni.entrypoint
def _configure_l3(vif_dict, ifname, netns, is_default_gateway):
    with get_ipdb(netns) as ipdb:
        with ipdb.interfaces[ifname] as iface:
            for subnet in vif_dict['network']['subnets']:
                if subnet['cidr']['version'] == 6:
                    _enable_ipv6(netns)
                for fip in subnet['ips']:
                    iface.add_ip('%s/%s' % (fip['address'],
                                            subnet['cidr']['prefixlen']))

        routes = ipdb.routes
        for subnet in vif_dict['network']['subnets']:
            for route in subnet['routes']:
                routes.add(gateway=str(route['gateway']),
                           dst=str(route['cidr'])).commit()
            if is_default_gateway and 'gateway' in subnet:
                try:
                    routes.add(gateway=str(subnet['gateway']),
                               dst='default').commit()
                except pyroute2.NetlinkError as ex:
                    if ex.code != errno.EEXIST:
                        raise
                    LOG.debug("Default route already exists in "
                              "capsule/container for vif=%s. Did not "
                              "overwrite with requested gateway=%s",
                              vif_dict, subnet['gateway'])


def _need_configure_l3(vif):
    if not hasattr(vif, 'physnet'):
        return True
    physnet = vif.physnet
    mapping_res = CONF.sriov_physnet_resource_mappings
    try:
        resource = mapping_res[physnet]
    except KeyError:
        LOG.exception("No resource name for physnet %s", physnet)
        raise
    mapping_driver = CONF.sriov_resource_driver_mappings
    try:
        driver_name = mapping_driver[resource]
    except KeyError:
        LOG.exception("No driver for resource_name %s", resource)
        raise
    if driver_name in consts.USERSPACE_DRIVERS:
        LOG.info("_configure_l3 will not be called for vif %s "
                 "because of it's driver", vif)
        return False
    return True


def connect(vif, instance_info, ifname, netns=None,
            is_default_gateway=True, container_id=None):
    netns = utils.convert_netns(netns)
    driver = _get_binding_driver(vif)
    os_vif.plug(vif, instance_info)
    driver.connect(vif, ifname, netns, container_id)
    if _need_configure_l3(vif):
        vif_dict = utils.osvif_vif_to_dict(vif)
        _configure_l3(vif_dict, ifname, netns, is_default_gateway)


def disconnect(vif, instance_info, ifname, netns=None,
               container_id=None, **kwargs):
    driver = _get_binding_driver(vif)
    driver.disconnect(vif, ifname, netns, container_id)
    os_vif.unplug(vif, instance_info)
