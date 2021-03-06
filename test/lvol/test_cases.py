import io
import math
import time
import sys
import random
import signal
import subprocess
import pprint
import socket
import threading
import os

from errno import ESRCH
from os import kill, path, unlink, path, listdir, remove
from rpc_commands_lib import Commands_Rpc
from time import sleep
from uuid import uuid4


MEGABYTE = 1024 * 1024


current_fio_pid = -1

# ## Objective
# The purpose of these tests is to verify the possibility of using lvol configuration in SPDK.
#
# ## Methodology
# Configuration in test is to be done using example stub application.
# All management is done using RPC calls, including logical volumes management.
# All tests are performed using malloc backends.
# One exception to malloc backends are tests for logical volume
# tasting - these require persistent merory like NVMe backend.
#
# Tests will be executed as scenarios - sets of smaller test step
# in which return codes from RPC calls is validated.
# Some configuration calls may also be validated by use of
# "get_*" RPC calls, which provide additional information for verifying
# results.
#
# Tests with thin provisioned lvol bdevs, snapshots and clones are using nbd devices.
# Before writing/reading to lvol bdev, bdev is installed with rpc nbd_start_disk.
# After finishing writing/reading, rpc nbd_stop_disk is used.


def is_process_alive(pid):
    try:
        os.kill(pid, 0)
    except Exception as e:
        return 1

    return 0


def get_fio_cmd(nbd_disk, offset, size, rw, pattern, extra_params=""):
    fio_template = "fio --name=fio_test --filename=%(file)s --offset=%(offset)s --size=%(size)s"\
                   " --rw=%(rw)s --direct=1 %(extra_params)s %(pattern)s"
    pattern_template = ""
    if pattern:
        pattern_template = "--do_verify=1 --verify=pattern --verify_pattern=%s"\
                           " --verify_state_save=0" % pattern
    fio_cmd = fio_template % {"file": nbd_disk, "offset": offset, "size": size,
                              "rw": rw, "pattern": pattern_template,
                              "extra_params": extra_params}

    return fio_cmd


def run_fio(fio_cmd, expected_ret_value):
    global current_fio_pid
    try:
        proc = subprocess.Popen([fio_cmd], shell=True)
        current_fio_pid = proc.pid
        proc.wait()
        rv = proc.returncode
    except Exception as e:
        print("ERROR: Fio test ended with unexpected exception.")
        rv = 1
    if expected_ret_value == rv:
        return 0

    if rv == 0:
        print("ERROR: Fio test ended with unexpected success")
    else:
        print("ERROR: Fio test ended with unexpected failure")
    return 1


class FioThread(threading.Thread):
    def __init__(self, nbd_disk, offset, size, rw, pattern, expected_ret_value,
                 extra_params=""):
        super(FioThread, self).__init__()
        self.fio_cmd = get_fio_cmd(nbd_disk, offset, size, rw, pattern,
                                   extra_params=extra_params)
        self.rv = 1
        self.expected_ret_value = expected_ret_value

    def run(self):
        print("INFO: Starting fio")
        self.rv = run_fio(self.fio_cmd, self.expected_ret_value)
        print("INFO: Fio test finished")


def test_counter():
    '''
    :return: the number of tests
    '''
    return ['test_case' in i for i in dir(TestCases)].count(True)


def case_message(func):
    def inner(*args, **kwargs):
        test_name = {
            # bdev_lvol_delete_lvstore - positive tests
            254: 'destroy_after_bdev_lvol_resize_positive',
            255: 'delete_lvol_store_persistent_positive',
            551: 'delete_lvol_bdev',
            552: 'bdev_lvol_delete_lvstore_with_clones',
            553: 'unregister_lvol_bdev',
            # logical volume clear_method test
            850: 'clear_method_none',
            851: 'clear_method_unmap',
        }
        num = int(func.__name__.strip('test_case')[:])
        print("************************************")
        print("START TEST CASE {name}".format(name=test_name[num]))
        print("************************************")
        fail_count = func(*args, **kwargs)
        print("************************************")
        if not fail_count:
            print("END TEST CASE {name} PASS".format(name=test_name[num]))
        else:
            print("END TEST CASE {name} FAIL".format(name=test_name[num]))
        print("************************************")
        return fail_count
    return inner


class TestCases(object):

    def __init__(self, rpc_py, total_size, block_size, base_dir_path, app_path):
        self.c = Commands_Rpc(rpc_py)
        self.total_size = total_size
        self.block_size = block_size
        self.cluster_size = None
        self.path = base_dir_path
        self.app_path = app_path
        self.lvs_name = "lvs_test"
        self.lbd_name = "lbd_test"
        self.vhost_config_path = path.join(path.dirname(sys.argv[0]), 'vhost.conf')

    def _gen_lvs_uuid(self):
        return str(uuid4())

    def _gen_lvb_uuid(self):
        return "_".join([str(uuid4()), str(random.randrange(9999999999))])

    def compare_two_disks(self, disk1, disk2, expected_ret_value):
        cmp_cmd = "cmp %s %s" % (disk1, disk2)
        try:
            process = subprocess.check_output(cmp_cmd, stderr=subprocess.STDOUT, shell=True)
            rv = 0
        except subprocess.CalledProcessError as ex:
            rv = 1
        except Exception as e:
            print("ERROR: Cmp ended with unexpected exception.")
            rv = 1

        if expected_ret_value == rv:
            return 0
        elif rv == 0:
            print("ERROR: Cmp ended with unexpected success")
        else:
            print("ERROR: Cmp ended with unexpected failure")

        return 1

    def run_fio_test(self, nbd_disk, offset, size, rw, pattern, expected_ret_value=0):
        fio_cmd = get_fio_cmd(nbd_disk, offset, size, rw, pattern)
        return run_fio(fio_cmd, expected_ret_value)

    def _stop_vhost(self, pid_path):
        with io.open(pid_path, 'r') as vhost_pid:
            pid = int(vhost_pid.readline())
            if pid:
                try:
                    kill(pid, signal.SIGTERM)
                    for count in range(30):
                        sleep(1)
                        kill(pid, 0)
                except OSError as err:
                    if err.errno == ESRCH:
                        pass
                    else:
                        return 1
                else:
                    return 1
            else:
                return 1
        return 0

    def _start_vhost(self, vhost_path, pid_path):
        subprocess.call("{app} -f "
                        "{pid} &".format(app=vhost_path,
                                         pid=pid_path), shell=True)
        for timeo in range(10):
            if timeo == 9:
                print("ERROR: Timeout on waiting for app start")
                return 1
            if not path.exists(pid_path):
                print("Info: Waiting for PID file...")
                sleep(1)
                continue
            else:
                break

        # Wait for RPC to open
        sock = socket.socket(socket.AF_UNIX)
        for timeo in range(30):
            if timeo == 29:
                print("ERROR: Timeout on waiting for RPC start")
                return 1
            try:
                sock.connect("/var/tmp/spdk.sock")
                break
            except socket.error as e:
                print("Info: Waiting for RPC Unix socket...")
                sleep(1)
                continue
            else:
                sock.close()
                break

        with io.open(pid_path, 'r') as vhost_pid:
            pid = int(vhost_pid.readline())
            if not pid:
                return 1
        return 0

    def get_lvs_size(self, lvs_name="lvs_test"):
        lvs = self.c.bdev_lvol_get_lvstores(lvs_name)[0]
        return int(int(lvs['free_clusters'] * lvs['cluster_size']) / MEGABYTE)

    def get_lvs_divided_size(self, split_num, lvs_name="lvs_test"):
        # Actual size of lvol bdevs on creation is rounded up to multiple of cluster size.
        # In order to avoid over provisioning, this function returns
        # lvol store size in MB divided by split_num - rounded down to multiple of cluster size."
        lvs = self.c.bdev_lvol_get_lvstores(lvs_name)[0]
        return int(int(lvs['free_clusters'] / split_num) * lvs['cluster_size'] / MEGABYTE)

    def get_lvs_cluster_size(self, lvs_name="lvs_test"):
        lvs = self.c.bdev_lvol_get_lvstores(lvs_name)[0]
        return int(int(lvs['cluster_size']) / MEGABYTE)

    @case_message
    def test_case254(self):
        """
        destroy_resize_logical_volume_positive

        Positive test for destroying a logical_volume after resizing.
        Call bdev_lvol_delete_lvstore with correct logical_volumes name.
        """
        # Create malloc bdev
        base_name = self.c.bdev_malloc_create(self.total_size,
                                              self.block_size)
        # Construct lvol store on create malloc bdev
        uuid_store = self.c.bdev_lvol_create_lvstore(base_name,
                                                     self.lvs_name)
        # Check correct uuid values in response bdev_lvol_get_lvstores command
        fail_count = self.c.check_bdev_lvol_get_lvstores(base_name, uuid_store,
                                                         self.cluster_size)
        size = self.get_lvs_divided_size(4)
        # bdev_lvol_create on correct lvs_uuid and size is
        # equal to one quarter of size malloc bdev
        uuid_bdev = self.c.bdev_lvol_create(uuid_store,
                                            self.lbd_name,
                                            size)
        # check size of the lvol bdev
        fail_count += self.c.check_bdev_get_bdevs_methods(uuid_bdev, size)
        sz = size + 4
        # Resize_lvol_bdev on correct lvs_uuid and size is
        # equal to one quarter of size malloc bdev plus 4 MB
        self.c.bdev_lvol_resize(uuid_bdev, sz)
        # check size of the lvol bdev by command RPC : bdev_get_bdevs
        fail_count += self.c.check_bdev_get_bdevs_methods(uuid_bdev, sz)
        # Resize_lvol_bdev on correct lvs_uuid and size is
        # equal half of size malloc bdev
        sz = size * 2
        self.c.bdev_lvol_resize(uuid_bdev, sz)
        # check size of the lvol bdev by command RPC : bdev_get_bdevs
        fail_count += self.c.check_bdev_get_bdevs_methods(uuid_bdev, sz)
        # Resize_lvol_bdev on correct lvs_uuid and size is
        # equal to three quarters of size malloc bdev
        sz = size * 3
        self.c.bdev_lvol_resize(uuid_bdev, sz)
        # check size of the lvol bdev by command RPC : bdev_get_bdevs
        fail_count += self.c.check_bdev_get_bdevs_methods(uuid_bdev, sz)
        # Resize_lvol_bdev on correct lvs_uuid and size is
        # equal to size if malloc bdev minus 4 MB
        sz = (size * 4) - 4
        self.c.bdev_lvol_resize(uuid_bdev, sz)
        # check size of the lvol bdev by command RPC : bdev_get_bdevs
        fail_count += self.c.check_bdev_get_bdevs_methods(uuid_bdev, sz)
        # Resize_lvol_bdev on the correct lvs_uuid and size is equal 0 MiB
        sz = 0
        self.c.bdev_lvol_resize(uuid_bdev, sz)
        # check size of the lvol bdev by command RPC : bdev_get_bdevs
        fail_count += self.c.check_bdev_get_bdevs_methods(uuid_bdev, sz)

        # Destroy lvol store
        self.c.bdev_lvol_delete_lvstore(uuid_store)
        if self.c.check_bdev_lvol_get_lvstores("", "", "") == 1:
            fail_count += 1
        self.c.bdev_malloc_delete(base_name)

        # Expected result:
        # - lvol bdev should change size after resize operations
        # - calls successful, return code = 0
        # - no other operation fails
        # - bdev_lvol_get_lvstores: response should be of no value after destroyed lvol store
        return fail_count

    @case_message
    def test_case255(self):
        """
        delete_lvol_store_persistent_positive

        Positive test for removing lvol store persistently
        """
        base_path = path.dirname(sys.argv[0])
        base_name = "aio_bdev0"
        aio_bdev0 = path.join(base_path, "aio_bdev_0")
        # Construct aio bdev
        self.c.bdev_aio_create(aio_bdev0, base_name, 4096)
        # Create lvol store on created aio bdev
        uuid_store = self.c.bdev_lvol_create_lvstore(base_name,
                                                     self.lvs_name)
        fail_count = self.c.check_bdev_lvol_get_lvstores(base_name, uuid_store,
                                                         self.cluster_size)
        # Destroy lvol store
        if self.c.bdev_lvol_delete_lvstore(self.lvs_name) != 0:
            fail_count += 1

        # Delete aio bdev
        self.c.bdev_aio_delete(base_name)
        # Create aio bdev on the same file
        self.c.bdev_aio_create(aio_bdev0, base_name, 4096)
        # Wait 1 second to allow time for lvolstore tasting
        sleep(1)

        # check if destroyed lvol store does not exist on aio bdev
        ret_value = self.c.check_bdev_lvol_get_lvstores(base_name, uuid_store,
                                                        self.cluster_size)
        if ret_value == 0:
            fail_count += 1
        self.c.bdev_aio_delete(base_name)

        # Expected result:
        # - bdev_lvol_get_lvstores should not report any existsing lvol stores in configuration
        #    after deleting and adding NVMe bdev
        # - no other operation fails
        return fail_count

    @case_message
    def test_case551(self):
        """
        bdev_lvol_delete_ordering

        Test for destroying lvol bdevs in particular order.
        Check destroying wrong one is not possible and returns error.
        """

        fail_count = 0
        snapshot_name = "snapshot"
        clone_name = "clone"

        # Create malloc bdev
        base_name = self.c.bdev_malloc_create(self.total_size,
                                              self.block_size)
        # Construct_lvol_store on correct, exisitng malloc bdev
        uuid_store = self.c.bdev_lvol_create_lvstore(base_name,
                                                     self.lvs_name,
                                                     self.cluster_size)
        # Check correct uuid values in response bdev_lvol_get_lvstores command
        fail_count = self.c.check_bdev_lvol_get_lvstores(base_name, uuid_store,
                                                         self.cluster_size)
        lvs = self.c.bdev_lvol_get_lvstores()
        size = int(int(lvs[0]['free_clusters'] * lvs[0]['cluster_size']) / 4 / MEGABYTE)

        # Construct thin provisioned lvol bdev
        uuid_bdev0 = self.c.bdev_lvol_create(uuid_store,
                                             self.lbd_name, size, thin=True)
        lvol_bdev = self.c.get_lvol_bdev_with_name(uuid_bdev0)

        # Create snapshot of thin provisioned lvol bdev
        fail_count += self.c.bdev_lvol_snapshot(lvol_bdev['name'], snapshot_name)
        snapshot_bdev = self.c.get_lvol_bdev_with_name(self.lvs_name + "/" + snapshot_name)

        # Create clone of snapshot and check if it ends with success
        fail_count += self.c.bdev_lvol_clone(self.lvs_name + "/" + snapshot_name, clone_name)
        clone_bdev = self.c.get_lvol_bdev_with_name(self.lvs_name + "/" + clone_name)

        # Try to destroy snapshot with clones and check if it fails
        ret_value = self.c.bdev_lvol_delete(snapshot_bdev['name'])
        if ret_value == 0:
            print("ERROR: Delete snapshot should fail but didn't")
            fail_count += 1

        # Destroy clone and then snapshot
        fail_count += self.c.bdev_lvol_delete(lvol_bdev['name'])
        fail_count += self.c.bdev_lvol_delete(clone_bdev['name'])
        fail_count += self.c.bdev_lvol_delete(snapshot_bdev['name'])

        # Destroy lvol store
        fail_count += self.c.bdev_lvol_delete_lvstore(uuid_store)

        #  Check response bdev_lvol_get_lvstores command
        if self.c.check_bdev_lvol_get_lvstores("", "", "") == 1:
            fail_count += 1

        # Delete malloc bdev
        self.c.bdev_malloc_delete(base_name)
        # Expected result:
        # - bdev_lvol_get_lvstores: response should be of no value after destroyed lvol store
        # - no other operation fails
        return fail_count

    @case_message
    def test_case552(self):
        """
        bdev_lvol_delete_lvstore_with_clones

        Test for destroying lvol store with clones present,
        without removing them first.
        """

        fail_count = 0
        snapshot_name = "snapshot"
        snapshot_name2 = "snapshot2"
        clone_name = "clone"

        # Create malloc bdev
        base_name = self.c.bdev_malloc_create(self.total_size,
                                              self.block_size)
        # Construct_lvol_store on correct, exisitng malloc bdev
        uuid_store = self.c.bdev_lvol_create_lvstore(base_name,
                                                     self.lvs_name,
                                                     self.cluster_size)
        # Check correct uuid values in response bdev_lvol_get_lvstores command
        fail_count = self.c.check_bdev_lvol_get_lvstores(base_name, uuid_store,
                                                         self.cluster_size)
        lvs = self.c.bdev_lvol_get_lvstores()
        size = int(int(lvs[0]['free_clusters'] * lvs[0]['cluster_size']) / 4 / MEGABYTE)

        # Create lvol bdev, snapshot it, then clone it and then snapshot the clone
        uuid_bdev0 = self.c.bdev_lvol_create(uuid_store, self.lbd_name, size, thin=True)
        lvol_bdev = self.c.get_lvol_bdev_with_name(uuid_bdev0)

        fail_count += self.c.bdev_lvol_snapshot(lvol_bdev['name'], snapshot_name)
        snapshot_bdev = self.c.get_lvol_bdev_with_name(self.lvs_name + "/" + snapshot_name)

        fail_count += self.c.bdev_lvol_clone(self.lvs_name + "/" + snapshot_name, clone_name)
        clone_bdev = self.c.get_lvol_bdev_with_name(self.lvs_name + "/" + clone_name)

        fail_count += self.c.bdev_lvol_snapshot(clone_bdev['name'], snapshot_name2)
        snapshot_bdev2 = self.c.get_lvol_bdev_with_name(self.lvs_name + "/" + snapshot_name2)

        # Try to destroy snapshot with 2 clones and check if it fails
        ret_value = self.c.bdev_lvol_delete(snapshot_bdev['name'])
        if ret_value == 0:
            print("ERROR: Delete snapshot should fail but didn't")
            fail_count += 1

        # Destroy lvol store without deleting lvol bdevs
        fail_count += self.c.bdev_lvol_delete_lvstore(uuid_store)

        #  Check response bdev_lvol_get_lvstores command
        if self.c.check_bdev_lvol_get_lvstores("", "", "") == 1:
            fail_count += 1

        # Delete malloc bdev
        self.c.bdev_malloc_delete(base_name)
        # Expected result:
        # - bdev_lvol_get_lvstores: response should be of no value after destroyed lvol store
        # - no other operation fails
        return fail_count

    @case_message
    def test_case553(self):
        """
        unregister_lvol_bdev

        Test for unregistering the lvol bdevs.
        Removing malloc bdev under an lvol store triggers unregister of
        all lvol bdevs. Verify it with clones present.
        """

        fail_count = 0
        snapshot_name = "snapshot"
        snapshot_name2 = "snapshot2"
        clone_name = "clone"

        # Create malloc bdev
        base_name = self.c.bdev_malloc_create(self.total_size,
                                              self.block_size)
        # Construct_lvol_store on correct, exisitng malloc bdev
        uuid_store = self.c.bdev_lvol_create_lvstore(base_name,
                                                     self.lvs_name,
                                                     self.cluster_size)
        # Check correct uuid values in response bdev_lvol_get_lvstores command
        fail_count = self.c.check_bdev_lvol_get_lvstores(base_name, uuid_store,
                                                         self.cluster_size)
        lvs = self.c.bdev_lvol_get_lvstores()
        size = int(int(lvs[0]['free_clusters'] * lvs[0]['cluster_size']) / 4 / MEGABYTE)

        # Create lvol bdev, snapshot it, then clone it and then snapshot the clone
        uuid_bdev0 = self.c.bdev_lvol_create(uuid_store, self.lbd_name, size, thin=True)
        lvol_bdev = self.c.get_lvol_bdev_with_name(uuid_bdev0)

        fail_count += self.c.bdev_lvol_snapshot(lvol_bdev['name'], snapshot_name)
        snapshot_bdev = self.c.get_lvol_bdev_with_name(self.lvs_name + "/" + snapshot_name)

        fail_count += self.c.bdev_lvol_clone(self.lvs_name + "/" + snapshot_name, clone_name)
        clone_bdev = self.c.get_lvol_bdev_with_name(self.lvs_name + "/" + clone_name)

        fail_count += self.c.bdev_lvol_snapshot(clone_bdev['name'], snapshot_name2)
        snapshot_bdev2 = self.c.get_lvol_bdev_with_name(self.lvs_name + "/" + snapshot_name2)

        # Delete malloc bdev
        self.c.bdev_malloc_delete(base_name)

        #  Check response bdev_lvol_get_lvstores command
        if self.c.check_bdev_lvol_get_lvstores("", "", "") == 1:
            fail_count += 1

        # Expected result:
        # - bdev_lvol_get_lvstores: response should be of no value after destroyed lvol store
        # - no other operation fails
        return fail_count

    @case_message
    def test_case850(self):
        """"
        Clear_method

        Test for clear_method equals to none
        """
        # Create malloc bdev
        base_name = self.c.bdev_malloc_create(self.total_size,
                                              self.block_size)
        # Construct lvol store on created malloc bddev
        lvs_uuid = self.c.bdev_lvol_create_lvstore(base_name,
                                                   self.lvs_name)
        # Check correct uuid values in response bdev_lvol_get_lvstores command
        fail_count = self.c.check_bdev_lvol_get_lvstores(base_name, lvs_uuid,
                                                         self.cluster_size)
        lvs = self.c.bdev_lvol_get_lvstores(self.lvs_name)[0]
        # Construct lvol bdev on lvol store
        lbd_size = int(lvs['cluster_size'] / MEGABYTE)
        bdev_uuid = self.c.bdev_lvol_create(lvs_uuid,
                                            self.lbd_name,
                                            lbd_size,
                                            clear_method='none')
        lvol_bdev = self.c.get_lvol_bdev_with_name(bdev_uuid)

        nbd_name = "/dev/nbd0"
        fail_count += self.c.nbd_start_disk(bdev_uuid, nbd_name)
        # Write pattern to lvol bdev starting from offset 0.
        fail_count += self.run_fio_test(nbd_name, 0, lvs['cluster_size'],
                                        "write", "0xdd")
        fail_count += self.c.nbd_stop_disk(nbd_name)

        # Delete lvol bdev
        fail_count += self.c.bdev_lvol_delete(bdev_uuid)

        # Delete lvol store. We need to do this so that we can attach the underlying malloc
        # bdev to nbd to examine its contents.
        fail_count += self.c.bdev_lvol_delete_lvstore(lvs_uuid)

        fail_count += self.c.nbd_start_disk(base_name, nbd_name)
        metadata_pages = 1 + lvs['total_data_clusters'] + (math.ceil(5 + math.ceil(lvs['total_data_clusters'] / 8) / 4096)) * 3
        last_metadata_lba = int(metadata_pages * 4096 / self.block_size)
        offset_metadata_end = int(last_metadata_lba * self.block_size)
        last_cluster_of_metadata = math.ceil(metadata_pages / lvs['cluster_size'] / 4096)
        offset = last_cluster_of_metadata * lvs['cluster_size']
        size_metadata_end = offset - offset_metadata_end

        # Check if data on area between end of metadata
        # and first cluster of lvol bdev remained unchaged
        fail_count += self.run_fio_test("/dev/nbd0", offset_metadata_end,
                                        size_metadata_end, "read", "0x00")
        # Check if data on first lvol bdevs remains unchanged.
        fail_count += self.run_fio_test("/dev/nbd0", offset, lvs['cluster_size'], "read", "0xdd")
        fail_count += self.c.nbd_stop_disk(nbd_name)

        self.c.bdev_malloc_delete(base_name)

        # Expected result:
        # - calls successful, return code = 0
        # - get_bdevs: no change
        # - no other operation fails
        return fail_count

    @case_message
    def test_case851(self):
        """"
        Clear_method

        Test lvol bdev with clear_method equals to unmap
        """
        # Create malloc bdev
        base_name = self.c.bdev_malloc_create(self.total_size,
                                              self.block_size)

        nbd_name = "/dev/nbd0"
        fail_count = self.c.nbd_start_disk(base_name, nbd_name)

        # Write data to malloc bdev starting from offset 0.
        fail_count += self.run_fio_test(nbd_name, 0, self.total_size * MEGABYTE,
                                        "write", "0xdd")
        fail_count += self.c.nbd_stop_disk(nbd_name)

        # Construct lvol store on created malloc bddev
        lvs_uuid = self.c.bdev_lvol_create_lvstore(base_name,
                                                   self.lvs_name,
                                                   clear_method='none')
        # Check correct uuid values in response bdev_lvol_get_lvstores command
        fail_count = self.c.check_bdev_lvol_get_lvstores(base_name, lvs_uuid,
                                                         self.cluster_size)
        lvs = self.c.bdev_lvol_get_lvstores(self.lvs_name)[0]

        # Construct lvol bdev on lvol store
        lbd_size = int(lvs['cluster_size'] / MEGABYTE)
        bdev_uuid = self.c.bdev_lvol_create(lvs_uuid,
                                            self.lbd_name,
                                            lbd_size,
                                            clear_method='unmap')
        # Check that data on lvol bdev remains unchanged
        fail_count += self.c.nbd_start_disk(bdev_uuid, nbd_name)
        fail_count += self.run_fio_test(nbd_name, 0, lvs['cluster_size'],
                                        "read", "0xdd")
        fail_count += self.c.nbd_stop_disk(nbd_name)

        # Delete lvol bdev
        fail_count += self.c.bdev_lvol_delete(bdev_uuid)

        # Delete lvol store
        fail_count += self.c.bdev_lvol_delete_lvstore(lvs_uuid)

        fail_count += self.c.nbd_start_disk(base_name, nbd_name)
        metadata_pages = 1 + lvs['total_data_clusters'] + \
            (math.ceil(5 + math.ceil(lvs['total_data_clusters'] / 8) / 4096)) * 3
        last_metadata_lba = int(metadata_pages * 4096 / self.block_size)
        offset_metadata_end = int(last_metadata_lba * self.block_size)
        last_cluster_of_metadata = math.ceil(metadata_pages / lvs['cluster_size'] / 4096)
        offset = last_cluster_of_metadata * lvs['cluster_size']
        size_metadata_end = offset - offset_metadata_end

        # Check if data on area between end of metadata
        # and first cluster of lvol bdev remained unchaged
        fail_count += self.run_fio_test("/dev/nbd0", offset_metadata_end,
                                        size_metadata_end, "read", "0xdd")
        # Check if data on lvol bdev was zeroed.
        # Malloc bdev should zero any data that is unmapped.
        fail_count += self.run_fio_test("/dev/nbd0", offset, lvs['cluster_size'], "read", "0x00")

        self.c.bdev_malloc_delete(base_name)

        # Expected result:
        # - calls successful, return code = 0
        # - get_bdevs: no change
        # - no other operation fails
        return fail_count
