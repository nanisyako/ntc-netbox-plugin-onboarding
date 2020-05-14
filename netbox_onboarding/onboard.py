"""Worker code for processing inbound OnboardingTasks.

(c) 2020 Network To Code
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
  http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import logging
import os
import re
import socket

from first import first
from napalm import get_network_driver
from napalm.base.exceptions import ConnectionException, CommandErrorException

from django.conf import settings
from dcim.models import Manufacturer, Device, Interface, DeviceType, Platform, DeviceRole
from ipam.models import IPAddress

from .constants import NETMIKO_TO_NAPALM
from netmiko.ssh_autodetect import SSHDetect
from netmiko.ssh_exception import NetMikoAuthenticationException
from netmiko.ssh_exception import NetMikoTimeoutException
from paramiko.ssh_exception import SSHException

__all__ = []


class OnboardException(Exception):
    """A failure occurred during the onboarding process.

    The exception includes a reason "slug" as defined below as well as a humanized message.
    """

    REASONS = (
        "fail-config",  # config provided is not valid
        "fail-connect",  # device is unreachable at IP:PORT
        "fail-execute",  # unable to execute device/API command
        "fail-login",  # bad username/password
        "fail-general",  # other error
    )

    def __init__(self, reason, message, **kwargs):
        super(OnboardException, self).__init__(kwargs)
        self.reason = reason
        self.message = message

    def __str__(self):
        return f"{self.__class__.__name__}: {self.reason}: {self.message}"


# -----------------------------------------------------------------------------
#
#                            Network Device Keeper
#
# -----------------------------------------------------------------------------


class NetdevKeeper:
    """Used to maintain information about the network device during the onboarding process."""

    def __init__(self, onboarding_task, username=None, password=None):
        """Initialize the network device keeper instance and ensure the required configuration parameters are provided.

        Args:
          onboarding_task (OnboardingTask): Task being processed
          username (str): Device username (if unspecified, NAPALM_USERNAME environment variable will be used)
          password (str): Device password (if unspecified, NAPALM_PASSWORD environment variable will be used)

        Raises:
          OnboardException('fail-config'):
            When any required config options are missing.
        """
        self.ot = onboarding_task

        # Attributes that are set when reading info from device

        self.hostname = None
        self.vendor = None
        self.model = None
        self.serial_number = None
        self.mgmt_ifname = None
        self.mgmt_pflen = None
        self.username = username or os.environ.get("NAPALM_USERNAME", None)
        self.password = password or os.environ.get("NAPALM_PASSWORD", None)

    def check_reachability(self):
        """Ensure that the device at the mgmt-ipaddr provided is reachable.

        We do this check before attempting other "show" commands so that we know we've got a
        device that can be reached.

        Raises:
          OnboardException('fail-connect'):
            When device unreachable
        """
        ip_addr = self.ot.ip_address
        port = self.ot.port
        timeout = self.ot.timeout

        logging.info("CHECK: IP %s:%s", ip_addr, port)

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((ip_addr, port))

        except (socket.error, socket.timeout, ConnectionError):
            raise OnboardException(reason="fail-connect", message=f"ERROR device unreachable: {ip_addr}:{port}")

    @staticmethod
    def guess_netmiko_device_type(**kwargs):
        """
        Guess the device type of host, based on Netmiko
        """
        guessed_device_type = None

        remote_device = {
            "device_type": "autodetect",
            "host": kwargs.get("host"),
            "username": kwargs.get("username"),
            "password": kwargs.get("password"),
        }

        try:
            logging.info("INFO guessing device type: {}".format(str(kwargs.get("host"))))
            guesser = SSHDetect(**remote_device)
            guessed_device_type = guesser.autodetect()
            logging.info("INFO guessed device type: {}".format(str(guessed_device_type)))

        except NetMikoAuthenticationException as err:
            logging.error("ERROR {}".format(str(err)))
            raise OnboardException(reason="fail-login", message="ERROR {}".format(str(err)))

        except (NetMikoTimeoutException, SSHException) as err:
            logging.error("ERROR {}".format(str(err)))
            raise OnboardException(reason="fail-connect", message="ERROR {}".format(str(err)))

        except Exception as err:
            logging.error("ERROR {}".format(str(err)))
            raise OnboardException(reason="fail-general", message="ERROR {}".format(str(err)))

        logging.info("INFO device type is {}".format(str(guessed_device_type)))

        return guessed_device_type

    def get_platform_name(self):
        """
        Get platform name in netmiko format (ie cisco_ios, cisco_xr etc)
        """
        if self.ot.platform:
            platform_name = self.ot.platform.name
        else:
            platform_name = self.guess_netmiko_device_type(
                host=self.ot.ip_address, username=self.username, password=self.password
            )

        logging.info(f"PLATFORM NAME is {platform_name}")

        return platform_name

    @staticmethod
    def get_platform_object_from_netbox(platform_name):
        """
        Get platform object from NetBox filtered by platform_name

        Lookup is performed based on the object's slug field (not the name field)
        """
        try:
            platform_object = Platform.objects.get(slug=platform_name)
            logging.info(f"PLATFORM: found in NetBox {platform_name}")
        except Platform.DoesNotExist:

            if not settings.PLUGINS_CONFIG["netbox_onboarding"].get("create_platform_if_missing"):
                raise OnboardException(
                    reason="fail-general", message=f"ERROR platform not found in NetBox: {platform_name}"
                )

            if platform_name not in NETMIKO_TO_NAPALM.keys():
                raise OnboardException(
                    reason="fail-general",
                    message=f"ERROR platform not found in NetBox and it's not part eligible to auto-creation: {platform_name}  ",
                )

            platform_object = Platform.objects.create(
                name=platform_name, slug=platform_name, napalm_driver=NETMIKO_TO_NAPALM[platform_name]
            )
            platform_object.save()

        return platform_object

    def get_required_info(self):
        """Gather information from the network device that is needed to onboard the device into the NetBox system.

        Raises:
          OnboardException('fail-login'):
            When unable to login to device

          OnboardException('fail-execute'):
            When unable to run commands to collect device information

          OnboardException('fail-general'):
            Any other unexpected device comms failure.
        """
        self.check_reachability()
        mgmt_ipaddr = self.ot.ip_address

        logging.info("COLLECT: device information %s", mgmt_ipaddr)

        try:
            platform_name = self.get_platform_name()
            platform_object = self.get_platform_object_from_netbox(platform_name=platform_name)

            driver_name = platform_object.napalm_driver

            if not driver_name:
                raise OnboardException(
                    reason="fail-general", message=f"Onboarding for platform {platform_name} not supported"
                )

            driver = get_network_driver(driver_name)
            dev = driver(hostname=mgmt_ipaddr, username=self.username, password=self.password, timeout=self.ot.timeout)

            dev.open()
            logging.info("COLLECT: device facts")
            facts = dev.get_facts()

            logging.info("COLLECT: device interface IPs")
            ip_ifs = dev.get_interfaces_ip()

        except ConnectionException as exc:
            raise OnboardException(reason="fail-login", message=exc.args[0])

        except CommandErrorException as exc:
            raise OnboardException(reason="fail-execute", message=exc.args[0])

        except Exception as exc:
            raise OnboardException(reason="fail-general", message=str(exc))

        # locate the interface assigned with the mgmt_ipaddr value and retain
        # the interface name and IP prefix-length so that we can use it later
        # when creating the IPAM IP-Address instance.

        try:
            mgmt_ifname, mgmt_pflen = first(
                (if_name, if_addr_data["prefix_length"])
                for if_name, if_data in ip_ifs.items()
                for if_addr, if_addr_data in if_data["ipv4"].items()
                if if_addr == mgmt_ipaddr
            )

        except Exception as exc:
            raise OnboardException(reason="fail-general", message=str(exc))

        # retain the attributes that will be later used by NetBox processing.

        self.hostname = facts["hostname"]
        self.vendor = facts["vendor"].title()
        self.model = facts["model"].lower()
        self.serial_number = facts["serial_number"]
        self.mgmt_ifname = mgmt_ifname
        self.mgmt_pflen = mgmt_pflen


# -----------------------------------------------------------------------------
#
#                            NetBox Device Keeper
#
# -----------------------------------------------------------------------------


class NetboxKeeper:
    """Used to manage the information relating to the network device within the NetBox server."""

    def __init__(self, netdev):
        """Create an instance and initialize the managed attributes that are used throughout the onboard processing.

        Args:
          netdev (NetdevKeeper): instance
        """
        self.netdev = netdev

        # these attributes are netbox model instances as discovered/created
        # through the course of processing.

        self.manufacturer = None
        self.device_type = None
        self.device_role = None
        self.device = None
        self.interface = None
        self.primary_ip = None

    def ensure_device_type(self):
        """Ensure the Device Type (slug) exists in NetBox associated to the netdev "model" and "vendor" (manufacturer).

        Raises:
          OnboardException('fail-config'):
            When the device vendor value does not exist as a Manufacturer in
            NetBox.

          OnboardException('fail-config'):
            When the device-type exists by slug, but is assigned to a different
            manufacturer.  This should *not* happen, but guard-rail checking
            regardless in case two vendors have the same model name.
        """
        # First ensure that the vendor, as extracted from the network device exists
        # in NetBox.  We need the ID for this vendor when ensuring the DeviceType
        # instance.

        try:
            self.manufacturer = Manufacturer.objects.get(name=self.netdev.vendor)
        except Manufacturer.DoesNotExist:
            if not settings.PLUGINS_CONFIG["netbox_onboarding"].get("create_manufacturer_if_missing"):
                raise OnboardException(
                    reason="fail-config", message=f"ERROR manufacturer not found: {self.netdev.vendor}"
                )

            self.manufacturer = Manufacturer.objects.create(name=self.netdev.vendor, slug=self.netdev.vendor)
            self.manufacturer.save()

        # Now see if the device type (slug) already exists,
        #  if so check to make sure that it is not assigned as a different manufacturer
        # if it doesn't exist, create it if the flag 'create_device_type_if_missing' is defined

        slug = self.netdev.model
        if re.search(r"[^a-zA-Z0-9\-_]+", slug):
            logging.warning("device model is not sluggable: %s", slug)
            self.netdev.model = slug.replace(" ", "-")
            logging.warning("device model is now: %s", self.netdev.model)

        try:
            self.device_type = DeviceType.objects.get(slug=self.netdev.model)
        except DeviceType.DoesNotExist:
            if not settings.PLUGINS_CONFIG["netbox_onboarding"].get("create_device_type_if_missing"):
                raise OnboardException(
                    reason="fail-config", message=f"ERROR device type not found: {self.netdev.model}"
                )

            logging.info("CREATE: device-type: %s", self.netdev.model)
            self.device_type = DeviceType.objects.create(
                slug=self.netdev.model, model=self.netdev.model.upper(), manufacturer=self.manufacturer
            )
            self.device_type.save()
            return

        if self.device_type.manufacturer.id != self.manufacturer.id:
            raise OnboardException(
                reason="fail-config",
                message=f"ERROR device type {self.netdev.model}" f"already exists for vendor {self.netdev.vendor}",
            )

    def ensure_device_role(self):
        """Ensure that the device role is defined / exist in NetBox or create it if it doesn't exist"""

        if self.netdev.ot.role:
            return

        default_device_role = settings.PLUGINS_CONFIG["netbox_onboarding"].get("default_device_role")

        try:
            device_role = DeviceRole.objects.get(slug=default_device_role)
        except DeviceRole.DoesNotExist:
            if not settings.PLUGINS_CONFIG["netbox_onboarding"].get("create_device_role_if_missing"):
                raise OnboardException(
                    reason="fail-config", message=f"ERROR device role not found: {default_device_role}"
                )

            self.netdev.ot.role = DeviceRole.objects.create(name=default_device_role, slug=default_device_role)
            self.netdev.ot.role.save()
            self.netdev.ot.save()
            return

    def ensure_device_instance(self):
        """Ensure that the device instance exists in NetBox and is assigned the provided device role or DEFAULT_ROLE."""

        device, _ = Device.objects.get_or_create(
            name=self.netdev.hostname,
            device_type=self.device_type,
            device_role=self.netdev.ot.role,
            platform=self.netdev.ot.platform,
            site=self.netdev.ot.site,
        )

        device.serial = self.netdev.serial_number
        device.save()

        self.netdev.ot.device = device
        self.netdev.ot.save()

        self.device = device

    def ensure_interface(self):
        """Ensure that the interface associated with the mgmt_ipaddr exists and is assigned to the device."""
        self.interface, _ = Interface.objects.get_or_create(name=self.netdev.mgmt_ifname, device=self.device)

    def ensure_primary_ip(self):
        """Ensure mgmt_ipaddr exists in IPAM, has the device interface, and is assigned as the primary IP address."""
        mgmt_ipaddr = self.netdev.ot.ip_address

        # see if the primary IP address exists in IPAM
        self.primary_ip, created = IPAddress.objects.get_or_create(address=f"{mgmt_ipaddr}/{self.netdev.mgmt_pflen}")

        if created or not self.primary_ip.interface:
            logging.info("ASSIGN: IP address %s to %s", self.primary_ip.address, self.interface.name)
            self.primary_ip.interface = self.interface

        self.primary_ip.save()

        # Ensure the primary IP is assigned to the device
        self.device.primary_ip4 = self.primary_ip
        self.device.save()

    def ensure_device(self):
        """Ensure that the device represented by the dev_info data exists in the NetBox system.

        This means the following is true:

            1. The device 'hostname' exists and is a member of 'site'
            2. The 'serial_number' is assigned to the device
            3. The 'model' is an existing DevType and assigned to the device.
            4. The 'mgmt_ifname' exists as an interface of the device
            5. The 'mgmt_ipaddr' is assigned to the mgmt_ifname
            6. The 'mgmt_ipaddr' is assigned as the primary IP address to the device.

        If the device previously exists and is not a member of the give site, then raise
        an OnboardException.

        """
        self.ensure_device_type()
        self.ensure_device_role()
        self.ensure_device_instance()
        self.ensure_interface()
        self.ensure_primary_ip()
