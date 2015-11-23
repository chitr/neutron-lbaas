# Copyright 2014: Citrix Systems, Inc.
#
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

from oslo_config import cfg
from oslo_log import log as logging
from oslo_serialization import jsonutils
from neutron import context
from neutron.i18n import _LI
from neutron.openstack.common import periodic_task
from neutron.openstack.common import service
from neutron.plugins.common import constants

from neutron_lbaas.db.loadbalancer import loadbalancer_db
from neutron_lbaas.services.loadbalancer.drivers import abstract_driver
from neutron_lbaas.services.loadbalancer.drivers.netscaler import ncc_client


DEFAULT_PERIODIC_TASK_INTERVAL = "2"
DEFAULT_STATUS_COLLECTION = "True"
DEFAULT_PAGE_SIZE = "300"

LOG = logging.getLogger(__name__)

NETSCALER_CC_OPTS = [
    cfg.StrOpt(
        'netscaler_ncc_uri',
        help=_('The URL to reach the NetScaler Control Center Server.'),
    ),
    cfg.StrOpt(
        'netscaler_ncc_username',
        help=_('Username to login to the NetScaler Control Center Server.'),
    ),
    cfg.StrOpt(
        'netscaler_ncc_password',
        help=_('Password to login to the NetScaler Control Center Server.'),
    ),
    cfg.StrOpt(
        'netscaler_ncc_cleanup_mode',
        help=_(
            'Setting to enable/disable cleanup mode for NetScaler Control '
            'Center Server'),
    ),
    cfg.StrOpt(
        'periodic_task_interval',
        default=DEFAULT_PERIODIC_TASK_INTERVAL,
        help=_('Setting for periodic task collection interval from'
               'NetScaler Control Center Server..'),
    ),
    cfg.StrOpt(
        'netscaler_status_collection',
        default=DEFAULT_STATUS_COLLECTION + "," + DEFAULT_PAGE_SIZE,
        help=_('Setting for member status collection from'
               'NetScaler Control Center Server.'),
    )
]

cfg.CONF.register_opts(NETSCALER_CC_OPTS, 'netscaler_driver')

VIP_IN_PENDING_CREATE = []
VIPS_RESOURCE = 'vips'
VIP_RESOURCE = 'vip'
POOLS_RESOURCE = 'pools'
POOL_RESOURCE = 'pool'
POOLMEMBERS_RESOURCE = 'members'
POOLMEMBER_RESOURCE = 'member'
MONITORS_RESOURCE = 'healthmonitors'
MONITOR_RESOURCE = 'healthmonitor'
STATS_RESOURCE = 'stats'
PROV_SEGMT_ID = 'provider:segmentation_id'
PROV_NET_TYPE = 'provider:network_type'
DRIVER_NAME = 'netscaler_driver'
RESOURCE_PREFIX = 'v2.0/lb'
STATUS_PREFIX = 'oca/v1'
MEMBER_STATUS = 'memberstatus'
PAGE = 'page'
SIZE = 'size'


class NetScalerPluginDriver(abstract_driver.LoadBalancerAbstractDriver):

    """NetScaler LBaaS Plugin driver class."""

    def __init__(self, plugin):
        self.plugin = plugin
        ncc_uri = cfg.CONF.netscaler_driver.netscaler_ncc_uri
        ncc_username = cfg.CONF.netscaler_driver.netscaler_ncc_username
        ncc_password = cfg.CONF.netscaler_driver.netscaler_ncc_password
        ncc_cleanup_mode = cfg.CONF.netscaler_driver.netscaler_ncc_cleanup_mode
        self.client = ncc_client.NSClient(ncc_uri,
                                          ncc_username,
                                          ncc_password,
                                          ncc_cleanup_mode)
        driver_conf = cfg.CONF.netscaler_driver
        status_conf = driver_conf.netscaler_status_collection
        self.periodic_task_interval = driver_conf.periodic_task_interval
        (self.enable_status_collection,
            self.status_collection_pagesize) = status_conf.split(",")
        if self.enable_status_collection.lower() == "true":
            self.enable_status_collection = True
        else:
            self.enable_status_collection = False
        self.launch_periodic_task(self.periodic_task_interval,
                                  self.enable_status_collection,
                                  self.status_collection_pagesize,
                                  self.client, self.plugin)

    def create_vip(self, context, vip):
        """Create a vip on a NetScaler device."""
        network_info = self._get_vip_network_info(context, vip)
        ncc_vip = self._prepare_vip_for_creation(vip)
        ncc_vip = dict(ncc_vip.items() + network_info.items())
        LOG.debug("NetScaler driver vip creation: %r", ncc_vip)
        status = constants.PENDING_CREATE
        try:
            resource_path = "%s/%s" % (RESOURCE_PREFIX, VIPS_RESOURCE)
            self.client.create_resource(context.tenant_id, resource_path,
                                        VIP_RESOURCE, ncc_vip)
        except ncc_client.NCCException:
            status = constants.ERROR
        else:
            VIP_IN_PENDING_CREATE.append(vip["id"])
        self.plugin.update_status(context, loadbalancer_db.Vip, vip["id"],
                                  status)

    def update_vip(self, context, old_vip, vip):
        """Update a vip on a NetScaler device."""
        update_vip = self._prepare_vip_for_update(vip)
        resource_path = "%s/%s/%s" % (RESOURCE_PREFIX, VIPS_RESOURCE,
                                      vip["id"])
        LOG.debug("NetScaler driver vip %(vip_id)s update: %(vip_obj)r",
                  {"vip_id": vip["id"], "vip_obj": vip})
        status = constants.ACTIVE
        try:
            self.client.update_resource(context.tenant_id, resource_path,
                                        VIP_RESOURCE, update_vip)
        except ncc_client.NCCException:
            status = constants.ERROR
        self.plugin.update_status(context, loadbalancer_db.Vip, old_vip["id"],
                                  status)

    def delete_vip(self, context, vip):
        """Delete a vip on a NetScaler device."""
        resource_path = "%s/%s/%s" % (RESOURCE_PREFIX, VIPS_RESOURCE,
                                      vip["id"])
        LOG.debug("NetScaler driver vip removal: %s", vip["id"])
        try:
            self.client.remove_resource(context.tenant_id, resource_path)
        except ncc_client.NCCException:
            self.plugin.update_status(context, loadbalancer_db.Vip,
                                      vip["id"],
                                      constants.ERROR)
        else:
            self.plugin._delete_db_vip(context, vip['id'])

    def create_pool(self, context, pool):
        """Create a pool on a NetScaler device."""
        network_info = self._get_pool_network_info(context, pool)
        ncc_pool = self._prepare_pool_for_creation(pool)
        ncc_pool = dict(ncc_pool.items() + network_info.items())
        LOG.debug("NetScaler driver pool creation: %r", ncc_pool)
        status = constants.ACTIVE
        try:
            resource_path = "%s/%s" % (RESOURCE_PREFIX, POOLS_RESOURCE)
            self.client.create_resource(context.tenant_id, resource_path,
                                        POOL_RESOURCE, ncc_pool)
        except ncc_client.NCCException:
            status = constants.ERROR
        self.plugin.update_status(context, loadbalancer_db.Pool,
                                  ncc_pool["id"], status)

    def update_pool(self, context, old_pool, pool):
        """Update a pool on a NetScaler device."""
        ncc_pool = self._prepare_pool_for_update(pool)
        resource_path = "%s/%s/%s" % (RESOURCE_PREFIX, POOLS_RESOURCE,
                                      old_pool["id"])
        LOG.debug("NetScaler driver pool %(pool_id)s update: %(pool_obj)r",
                  {"pool_id": old_pool["id"], "pool_obj": ncc_pool})
        status = constants.ACTIVE
        try:
            self.client.update_resource(context.tenant_id, resource_path,
                                        POOL_RESOURCE, ncc_pool)
        except ncc_client.NCCException:
            status = constants.ERROR
        self.plugin.update_status(context, loadbalancer_db.Pool,
                                  old_pool["id"], status)

    def delete_pool(self, context, pool):
        """Delete a pool on a NetScaler device."""
        resource_path = "%s/%s/%s" % (RESOURCE_PREFIX, POOLS_RESOURCE,
                                      pool['id'])
        LOG.debug("NetScaler driver pool removal: %s", pool["id"])
        try:
            self.client.remove_resource(context.tenant_id, resource_path)
        except ncc_client.NCCException:
            self.plugin.update_status(context, loadbalancer_db.Pool,
                                      pool["id"],
                                      constants.ERROR)
        else:
            self.plugin._delete_db_pool(context, pool['id'])

    def create_member(self, context, member):
        """Create a pool member on a NetScaler device."""
        ncc_member = self._prepare_member_for_creation(member)
        LOG.info(_LI("NetScaler driver poolmember creation: %r"),
                 ncc_member)
        status = constants.ACTIVE
        try:
            resource_path = "%s/%s" % (RESOURCE_PREFIX, POOLMEMBERS_RESOURCE)
            self.client.create_resource(context.tenant_id,
                                        resource_path,
                                        POOLMEMBER_RESOURCE,
                                        ncc_member)
        except ncc_client.NCCException:
            status = constants.ERROR
        self.plugin.update_status(context, loadbalancer_db.Member,
                                  member["id"], status)

    def update_member(self, context, old_member, member):
        """Update a pool member on a NetScaler device."""
        ncc_member = self._prepare_member_for_update(member)
        resource_path = "%s/%s/%s" % (RESOURCE_PREFIX, POOLMEMBERS_RESOURCE,
                                      old_member["id"])
        LOG.debug("NetScaler driver poolmember %(member_id)s update: "
                  "%(member_obj)r",
                  {"member_id": old_member["id"],
                   "member_obj": ncc_member})
        status = constants.ACTIVE
        try:
            self.client.update_resource(context.tenant_id, resource_path,
                                        POOLMEMBER_RESOURCE, ncc_member)
        except ncc_client.NCCException:
            status = constants.ERROR
        self.plugin.update_status(context, loadbalancer_db.Member,
                                  old_member["id"], status)

    def delete_member(self, context, member):
        """Delete a pool member on a NetScaler device."""
        resource_path = "%s/%s/%s" % (RESOURCE_PREFIX, POOLMEMBERS_RESOURCE,
                                      member['id'])
        LOG.debug("NetScaler driver poolmember removal: %s", member["id"])
        try:
            self.client.remove_resource(context.tenant_id, resource_path)
        except ncc_client.NCCException:
            self.plugin.update_status(context, loadbalancer_db.Member,
                                      member["id"],
                                      constants.ERROR)
        else:
            self.plugin._delete_db_member(context, member['id'])

    def create_pool_health_monitor(self, context, health_monitor, pool_id):
        """Create a pool health monitor on a NetScaler device."""
        ncc_hm = self._prepare_healthmonitor_for_creation(health_monitor,
                                                          pool_id)
        resource_path = "%s/%s/%s/%s" % (RESOURCE_PREFIX, POOLS_RESOURCE,
                                         pool_id, MONITORS_RESOURCE)
        LOG.debug("NetScaler driver healthmonitor creation for pool "
                  "%(pool_id)s: %(monitor_obj)r",
                  {"pool_id": pool_id, "monitor_obj": ncc_hm})
        status = constants.ACTIVE
        try:
            self.client.create_resource(context.tenant_id, resource_path,
                                        MONITOR_RESOURCE,
                                        ncc_hm)
        except ncc_client.NCCException:
            status = constants.ERROR
        self.plugin.update_pool_health_monitor(context,
                                               health_monitor['id'],
                                               pool_id,
                                               status, "")

    def update_pool_health_monitor(self, context, old_health_monitor,
                                   health_monitor, pool_id):
        """Update a pool health monitor on a NetScaler device."""
        ncc_hm = self._prepare_healthmonitor_for_update(health_monitor)
        resource_path = "%s/%s/%s" % (RESOURCE_PREFIX, MONITORS_RESOURCE,
                                      old_health_monitor["id"])
        LOG.debug("NetScaler driver healthmonitor %(monitor_id)s update: "
                  "%(monitor_obj)r",
                  {"monitor_id": old_health_monitor["id"],
                   "monitor_obj": ncc_hm})
        status = constants.ACTIVE
        try:
            self.client.update_resource(context.tenant_id, resource_path,
                                        MONITOR_RESOURCE, ncc_hm)
        except ncc_client.NCCException:
            status = constants.ERROR
        self.plugin.update_pool_health_monitor(context,
                                               old_health_monitor['id'],
                                               pool_id,
                                               status, "")

    def delete_pool_health_monitor(self, context, health_monitor, pool_id):
        """Delete a pool health monitor on a NetScaler device."""
        resource_path = "%s/%s/%s/%s/%s" % (RESOURCE_PREFIX, POOLS_RESOURCE,
                                            pool_id, MONITORS_RESOURCE,
                                            health_monitor["id"])
        LOG.debug("NetScaler driver healthmonitor %(monitor_id)s"
                  "removal for pool %(pool_id)s",
                  {"monitor_id": health_monitor["id"],
                   "pool_id": pool_id})
        try:
            self.client.remove_resource(context.tenant_id, resource_path)
        except ncc_client.NCCException:
            self.plugin.update_pool_health_monitor(context,
                                                   health_monitor['id'],
                                                   pool_id,
                                                   constants.ERROR, "")
        else:
            self.plugin._delete_db_pool_health_monitor(context,
                                                       health_monitor['id'],
                                                       pool_id)

    def stats(self, context, pool_id):
        """Retrieve pool statistics from the NetScaler device."""
        resource_path = "%s/%s/%s/%s" % (RESOURCE_PREFIX, POOLS_RESOURCE,
                                         pool_id, STATS_RESOURCE)
        LOG.debug("NetScaler driver pool stats retrieval: %s", pool_id)
        try:
            result = self.client.retrieve_resource(context.tenant_id,
                                                   resource_path)[1]
            result['body'] = jsonutils.loads(result['body'])
            stats = result['body']['stats']

        except ncc_client.NCCException:
            LOG.debug("Stats of pool:%s errored out. Returning default stats", pool_id)
        else:
            return stats

    def _prepare_vip_for_creation(self, vip):
        creation_attrs = {
            'id': vip['id'],
            'tenant_id': vip['tenant_id'],
            'protocol': vip['protocol'],
            'address': vip['address'],
            'protocol_port': vip['protocol_port'],
        }
        update_attrs = self._prepare_vip_for_update(vip)
        creation_attrs.update(update_attrs)
        return creation_attrs

    def _prepare_vip_for_update(self, vip):
        updation_attrs = {
            'name': vip['name'],
            'description': vip['description'],
            'pool_id': vip['pool_id'],
            'connection_limit': vip['connection_limit'],
            'admin_state_up': vip['admin_state_up']
        }
        if 'session_persistence' in vip:
            updation_attrs['session_persistence'] = vip['session_persistence']
        return updation_attrs

    def _prepare_pool_for_creation(self, pool):
        creation_attrs = {
            'id': pool['id'],
            'tenant_id': pool['tenant_id'],
            'vip_id': pool['vip_id'],
            'protocol': pool['protocol'],
            'subnet_id': pool['subnet_id'],
        }
        update_attrs = self._prepare_pool_for_update(pool)
        creation_attrs.update(update_attrs)
        return creation_attrs

    def _prepare_pool_for_update(self, pool):
        return {
            'name': pool['name'],
            'description': pool['description'],
            'lb_method': pool['lb_method'],
            'admin_state_up': pool['admin_state_up']
        }

    def _prepare_member_for_creation(self, member):
        creation_attrs = {
            'id': member['id'],
            'tenant_id': member['tenant_id'],
            'address': member['address'],
            'protocol_port': member['protocol_port'],
        }
        update_attrs = self._prepare_member_for_update(member)
        creation_attrs.update(update_attrs)
        return creation_attrs

    def _prepare_member_for_update(self, member):
        return {
            'pool_id': member['pool_id'],
            'weight': member['weight'],
            'admin_state_up': member['admin_state_up']
        }

    def _prepare_healthmonitor_for_creation(self, health_monitor, pool_id):
        creation_attrs = {
            'id': health_monitor['id'],
            'tenant_id': health_monitor['tenant_id'],
            'type': health_monitor['type'],
        }
        update_attrs = self._prepare_healthmonitor_for_update(health_monitor)
        creation_attrs.update(update_attrs)
        return creation_attrs

    def _prepare_healthmonitor_for_update(self, health_monitor):
        ncc_hm = {
            'delay': health_monitor['delay'],
            'timeout': health_monitor['timeout'],
            'max_retries': health_monitor['max_retries'],
            'admin_state_up': health_monitor['admin_state_up']
        }
        if health_monitor['type'] in ['HTTP', 'HTTPS']:
            ncc_hm['http_method'] = health_monitor['http_method']
            ncc_hm['url_path'] = health_monitor['url_path']
            ncc_hm['expected_codes'] = health_monitor['expected_codes']
        return ncc_hm

    def _get_network_info(self, context, entity):
        network_info = {}
        subnet_id = entity['subnet_id']
        subnet = self.plugin._core_plugin.get_subnet(context, subnet_id)
        network_id = subnet['network_id']
        network = self.plugin._core_plugin.get_network(context, network_id)
        network_info['network_id'] = network_id
        network_info['subnet_id'] = subnet_id
        if PROV_NET_TYPE in network:
            network_info['network_type'] = network[PROV_NET_TYPE]
        if PROV_SEGMT_ID in network:
            network_info['segmentation_id'] = network[PROV_SEGMT_ID]
        return network_info

    def _get_vip_network_info(self, context, vip):
        network_info = self._get_network_info(context, vip)
        network_info['port_id'] = vip['port_id']
        return network_info

    def _get_pool_network_info(self, context, pool):
        return self._get_network_info(context, pool)

    def _get_pools_on_subnet(self, context, tenant_id, subnet_id):
        filter_dict = {'subnet_id': [subnet_id], 'tenant_id': [tenant_id]}
        return self.plugin.get_pools(context, filters=filter_dict)

    def launch_periodic_task(self, periodic_task_interval,
                             enable_status_collection,
                             status_collection_pagesize, ncc_client,
                             plugin):

        class Collectors(periodic_task.PeriodicTasks):

            @periodic_task.periodic_task(spacing=int(periodic_task_interval))
            def execute_periodic_tasks(self, context):

                msg = _("periodic task interval: %s") % (
                    periodic_task_interval)
                LOG.debug(msg)
                self._refresh_vip_status()
                if enable_status_collection:
                    self._refresh_all_members_status()

            def _refresh_vip_status(self):
                """Retrieve vip status from Netscalr Control Center."""
                context_handle = context.get_admin_context()
                filter_dict = {'status':  ['PENDING_CREATE'] }
                pending_vips = plugin.get_vips(context_handle, filters=filter_dict)
                for vip_id in pending_vips:
                    resource_path = "%s/%s/%s" % (RESOURCE_PREFIX,
                                                  VIPS_RESOURCE, vip_id)
                    try:
                        status, result = (ncc_client.
                                          retrieve_resource("GLOBAL",
                                                            resource_path))
                    except Exception:
                        # ignore the exception if vip
                        # is not yet created on device
                        continue
                    msg = _("result is: %s") % (str(result))
                    LOG.debug(msg)
                    result = jsonutils.loads(result['body'])
                    vip_status = result['vip']['status']

                    if vip_status != constants.PENDING_CREATE:

                        context_handle = context.get_admin_context()
                        plugin.update_status(context_handle,
                                             loadbalancer_db.Vip, vip_id,
                                             vip_status)
                        VIP_IN_PENDING_CREATE.remove(vip_id)

            def _refresh_all_members_status(self):
                """Retrieve poolmember status from the NetScaler device."""
                page_no = 1
                while True:
                    resource_path = "%s/%s" % (STATUS_PREFIX, MEMBER_STATUS)
                    resource_path = ("%s?%s=%s&%s=%s"
                                     % (resource_path, PAGE,
                                        page_no, SIZE,
                                        status_collection_pagesize))
                    result = ncc_client.retrieve_resource("GLOBAL",
                                                          resource_path)[1]
                    msg = _("result is: %s") % (str(result))
                    LOG.debug(msg)
                    result['body'] = jsonutils.loads(result['body'])
                    statuses = result['body']['statuses']
                    context_handle = context.get_admin_context()
                    for status in statuses:
                        members = status["memberstatus"]
                        for member in members:
                            member_id = member["id"]
                            member_status = member["status"]
                            member_status_desc = member["status_description"]
                            plugin.update_status(context_handle,
                                                 loadbalancer_db.Member,
                                                 member_id,
                                                 member_status,
                                                 member_status_desc)
                    if len(statuses) < int(status_collection_pagesize):
                        return
                    else:
                        page_no += 1

        class AsyncStatusCollectorService(service.Service):

            def set_periodic_task_interval(self, periodic_task_interval):
                self.periodic_task_interval = periodic_task_interval

            def start(self):
                super(AsyncStatusCollectorService, self).start()
                self.tg.add_timer(
                    int(self.periodic_task_interval),
                    collectors.run_periodic_tasks,
                    None,
                    None
                )
        collectors = Collectors()
        svc = AsyncStatusCollectorService()
        svc.set_periodic_task_interval(self.periodic_task_interval)
        service.launch(svc)
