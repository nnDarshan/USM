import random
import logging
import uuid

from usm_rest_api.models import Cluster
from usm_rest_api.v1.serializers.serializers import ClusterSerializer
from usm_rest_api.models import Host
from usm_rest_api.v1.serializers.serializers import HostSerializer
from usm_rest_api.models import StorageDevice
from usm_rest_api.models import DiscoveredNode

from usm_wrappers import salt_wrapper
from usm_wrappers import utils as usm_wrapper_utils


log = logging.getLogger('django.request')


CLUSTER_TYPE_GLUSTER = 1
CLUSTER_TYPE_CEPH = 2
STORAGE_TYPE_BLOCK = 1
STORAGE_TYPE_FILE = 2
STORAGE_TYPE_OBJECT = 3
STATUS_INACTIVE = 1
STATUS_ACTIVE_NOT_AVAILABLE = 2
STATUS_ACTIVE_AND_AVAILABLE = 3
HOST_TYPE_MONITOR = 1
HOST_TYPE_OSD = 2
HOST_TYPE_MIXED = 3
HOST_TYPE_GLUSTER = 4
HOST_STATUS_INACTIVE = 1
HOST_STATUS_ACTIVE = 2
ACCEPT_MINION_TIMEOUT = 3
CONFIG_PUSH_TIMEOUT = 15


def add_gluster_host(hostlist, newNode):
    if hostlist:
        host = random.choice(hostlist)

        rc = salt_wrapper.peer(host, newNode)
        if rc is True:
            return rc
        #
        # random host is not able to peer probe
        # Now try to iterate through the list of hosts
        # in the cluster to peer probe until
        # the peer probe is successful
        #

        # No need to send it to host which we already tried
        hostlist.remove(host)
        for host in hostlist:
            rc = salt_wrapper.peer(host, newNode)
            if rc is True:
                return rc
        return rc
    else:
        return True


def create_cluster(cluster_data):
    # create the cluster
    cluster_data['cluster_id'] = str(uuid.uuid4())
    try:
        clusterSerilaizer = ClusterSerializer(data=cluster_data)
        if clusterSerilaizer.is_valid():
            clusterSerilaizer.save()
        else:
                log.error(
                    "Cluster Creation failed. Invalid clusterSerilaizer")
                log.error(
                    "clusterSerilaizer Err: %s" % clusterSerilaizer.errors)
                raise Exception(
                    "Cluster Creation failed. Invalid clusterSerilaizer",
                    clusterSerilaizer.errors)
    except Exception, e:
        log.exception(e)
        raise e


def setup_transport_and_update_db(cluster_data, nodelist):
    log.debug("Inside setup_transport_and_update_db function")
    failedNodes = []

    # Create the Nodes in the cluster
    # Setup the host communication channel
    minionIds = {}
    for node in nodelist:
        try:
            if 'ssh_username' in node and 'ssh_password' in node:
                ssh_fingerprint = usm_wrapper_utils.get_host_ssh_key(
                    node['management_ip'])
                minionIds[node['management_ip']] = salt_wrapper.setup_minion(
                    node['management_ip'], ssh_fingerprint[0],
                    node['ssh_username'], node['ssh_password'])
            else:
                # Discovered node, add hostname to the minionIds list
                log.debug("Discovered Node: %s" % node)
                minionIds[node['management_ip']] = node['node_name']
        except Exception, e:
            log.exception(e)
            failedNodes.append(node)
    log.debug(minionIds)
    log.debug("Accepting the minions keys")

    # Accept the keys of the successful minions and add to the DB
    for node in nodelist:
        try:
            if node not in failedNodes:
                salt_wrapper.accept_minion(minionIds[node['management_ip']])
        except Exception, e:
            log.exception(e)
            failedNodes.append(node)
    log.debug(minionIds)
    #
    # Wait till the communication chanel is ready
    #
    started_minions = salt_wrapper.get_started_minions(minionIds.values())
    log.debug("Started Minions %s" % started_minions)
    # if any of the minions are not ready, move ito the failed Nodes
    failedNodes.extend(
        [item for item in nodelist if item['node_name'] not in
         started_minions])
    # Persist the hosts into DB
    for node in nodelist:
        #
        # Get the host uuid from  host and update in DB
        #
        try:
            if node not in failedNodes:
                node['node_id'] = salt_wrapper.get_machine_id(
                    minionIds[node['management_ip']])
                node['cluster'] = str(cluster_data['cluster_id'])
                hostSerilaizer = HostSerializer(data=node)

                if hostSerilaizer.is_valid():
                    # Delete all the fields those are not
                    # reqired to be persisted
                    if 'ssh_password' in hostSerilaizer.validated_data:
                        del hostSerilaizer.validated_data['ssh_password']
                    if 'ssh_key_fingerprint' in hostSerilaizer.validated_data:
                        del hostSerilaizer.validated_data[
                            'ssh_key_fingerprint']
                    if 'ssh_username' in hostSerilaizer.validated_data:
                        del hostSerilaizer.validated_data['ssh_username']
                    if 'ssh_port' in hostSerilaizer.validated_data:
                        del hostSerilaizer.validated_data['ssh_port']

                    hostSerilaizer.save()
                else:
                    log.error("Host Creation failed. Invalid hostSerilaizer")
                    log.error("hostSerilaizer Err: %s" % hostSerilaizer.errors)
                    raise Exception("Add to DB Failed", hostSerilaizer.errors)
        except Exception, e:
            log.exception(e)
            failedNodes.append(node)
        # Remove the node from discovered nodes if present
        try:
            discovered = DiscoveredNode.objects.get(
                node_name__exact=node['node_name'])
            if discovered:
                # delete from discovered nodes table
                log.debug("Clearing the entry from DiscoveredNodes")
                discovered.delete()
        except Exception, e:
            log.exception(e)

    # Push the cluster configuration to nodes
    log.debug("Push Cluster config to Nodes")
    failed_minions = salt_wrapper.setup_cluster_grains(
        minionIds.values(), cluster_data)
    if failed_minions:
        log.debug('Config Push failed for minions %s' % failed_minions)
        # Add the failed minions to failed Nodes list
        for node in nodelist:
            if usm_wrapper_utils.resolve_ip_address(
                    node['management_ip']) in failed_minions:
                failedNodes.append(node)
    log.debug("failedNodes %s" % failedNodes)

    return minionIds, failedNodes


def update_host_status(nodes, status):
    for node in nodes:
        glusterNode = Host.objects.get(pk=str(node['node_id']))
        glusterNode.node_status = status
        glusterNode.save()


def update_cluster_status(cluster_id, status):
    cluster = Cluster.objects.get(pk=cluster_id)
    cluster.cluster_status = status
    cluster.save()


def create_ceph_cluster(nodelist, cluster_data, minionIds):
    status = True
    if nodelist:
        minions = {}
        for node in nodelist:
            if node['node_type'] == HOST_TYPE_OSD:
                continue
            nodeInfo = {'public_ip': node['public_ip'],
                        'cluster_ip': node['cluster_ip']}
            minions[minionIds[node['management_ip']]] = nodeInfo
        log.debug("cluster_name %s" % cluster_data['cluster_name'])
        log.debug("cluster_id %s" % cluster_data['cluster_id'])
        log.debug("minions %s" % minions)
        try:
            status = salt_wrapper.setup_ceph_cluster(
                cluster_data['cluster_name'],
                cluster_data['cluster_id'],
                minions)
            return status
        except Exception, e:
            log.exception(e)
            # Should we return failure from here?
            return False

    return status


def add_ceph_osd(node, cluster_name, minionId):
    failedNodes = []
    try:
        failed = salt_wrapper.add_ceph_osd(
            cluster_name, {minionId: node})
        if failed:
            log.debug("Failed:%s" % failed)
            if minionId in failed.keys():
                failedNodes.append(minionId)
    except Exception, e:
        log.exception(e)
        failedNodes.append(node)

    return failedNodes


def add_ceph_monitors(nodelist, cluster_data, minionIds):
    failedNodes = []
    if nodelist:
        for node in nodelist:
            nodeInfo = {'public_ip': node['public_ip']}
            try:
                failed = salt_wrapper.add_ceph_mon(
                    cluster_data['cluster_name'],
                    {minionIds[node['management_ip']]: nodeInfo})
                if failed:
                    failedNodes += _map_failed_nodes(
                        failed, nodelist, minionIds)
            except Exception, e:
                log.exception(e)
                failedNodes.append(node)
    return failedNodes


def create_gluster_cluster(nodelist):
    failedNodes = []
    if nodelist:
        rootNode = nodelist[0]['management_ip']
        for node in nodelist[1:]:
            rc = salt_wrapper.peer(rootNode, node['management_ip'])
            if rc is False:
                log.critical("Peer Probe Failed for node %s" % node)
                failedNodes.append(node)
    return failedNodes


def _map_failed_nodes(failed, nodelist, minionIds):
    failedNodes = []
    if failed:
        log.debug("Failed:" % failed)
        failedlist = failed.keys()
        for node in nodelist:
            if minionIds[node['management_ip']] in failedlist:
                failedNodes.append(node)
    return failedNodes


def discover_disks(nodelist, minionIds):
    minions = []
    if nodelist:
        for node in nodelist:
            minions.append(minionIds[node['management_ip']])
        log.debug("Monions %s" % minions)
        try:
            disksInfo = salt_wrapper.get_minion_disk_info(minions)
            for node in nodelist:
                diskInfo = disksInfo[minionIds[node['management_ip']]]
                for k in diskInfo:
                    log.debug("Disks: %s" % diskInfo[k])
                    log.debug("node UUID: %s" % node['node_id'])
                    sd = StorageDevice(
                        storage_device_name=diskInfo[k]['PKNAME'],
                        device_uuid=diskInfo[k]['UUID'],
                        node_id=node['node_id'],
                        device_type=diskInfo[k]['TYPE'],
                        device_path=diskInfo[k]['KNAME'],
                        filesystem_type=diskInfo[k]['FSTYPE'],
                        device_mount_point=diskInfo[k]['MOUNTPOINT'])
                    log.debug("Saving")
                    sd.save()
        except Exception, e:
            log.exception(e)
            return False
    return True


class ClusterCreationFailed(Exception):
    message = "Cluster creation failed"

    def __init__(self, nodelist, reason):
        self.failedNodes = nodelist
        self.reason = reason

    def __str__(self):
        nodeNames = []
        for node in self.failedNodes:
            nodeNames.append(node['node_name'])
        s = ("%s\nfailednodes: %s\nreason: %s\n")
        return s % (self.message, nodeNames, self.reason)


class HostAdditionFailed(Exception):
    message = "Host Addition failed"

    def __init__(self, nodelist, reason):
        self.failedNodes = nodelist
        self.reason = reason

    def __str__(self):
        nodeNames = []
        for node in self.failedNodes:
            nodeNames.append(node['node_name'])
        s = ("%s\nfailednodes: %s\nreason: %s\n")
        return s % (self.message, nodeNames, self.reason)