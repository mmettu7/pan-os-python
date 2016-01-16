#!/usr/bin/env python

# Copyright (c) 2014, Palo Alto Networks
#
# Permission to use, copy, modify, and/or distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

# Author: Brian Torres-Gil <btorres-gil@paloaltonetworks.com>


"""Palo Alto Networks device and firewall objects.

For performing common tasks on Palo Alto Networks devices.
"""


# import modules
import re
import logging
import inspect
import xml.etree.ElementTree as ET
import time
from copy import deepcopy
from decimal import Decimal

# import Palo Alto Networks api modules
# available at https://live.paloaltonetworks.com/docs/DOC-4762
import pan.xapi
import pan.commit
from pan.config import PanConfig

import pandevice
from pandevice import panorama
from pandevice import device
from pandevice import objects
from pandevice import network

# import other parts of this pandevice package
import errors as err
from network import Interface
from base import PanObject, PanDevice, Root, ENTRY
from base import VarPath as Var
from updater import Updater
import userid

# set logging to nullhandler to prevent exceptions if logging not enabled
logging.getLogger(__name__).addHandler(logging.NullHandler())
logger = logging.getLogger(__name__)


class Firewall(PanDevice):

    XPATH = "/devices"
    ROOT = Root.MGTCONFIG
    SUFFIX = ENTRY
    NAME = "serial"
    CHILDTYPES = (
        device.Vsys,
        VsysResources,
        objects.AddressObject,
        network.VirtualRouter,
    )

    def __init__(self,
                 hostname=None,
                 api_username=None,
                 api_password=None,
                 api_key=None,
                 serial=None,
                 port=443,
                 vsys='vsys1',  # vsys# or 'shared'
                 is_virtual=None,
                 classify_exceptions=True,
                 ):
        """Initialize PanDevice"""
        super(Firewall, self).__init__(hostname, api_username, api_password, api_key,
                                       port=port,
                                       is_virtual=is_virtual,
                                       classify_exceptions=classify_exceptions,
                                       )
        # create a class logger
        self._logger = logging.getLogger(__name__ + "." + self.__class__.__name__)

        self.serial = serial
        self._vsys = vsys
        self.vsys_name = None
        self.multi_vsys = None

        # Create a User-ID subsystem
        self.userid = userid.UserId(self)

    @property
    def vsys(self):
        return self._vsys

    @vsys.setter
    def vsys(self, value):
        self._vsys = value

    def xpath_vsys(self):
        if self.vsys == "shared":
            return "/config/shared"
        else:
            return "/config/devices/entry[@name='localhost.localdomain']/vsys/entry[@name='%s']" % self.vsys

    def xpath_panorama(self):
        raise err.PanDeviceError("Attempt to modify Panorama configuration on non-Panorama device")

    def _parent_xpath(self):
        if self.parent is None:
            # self with no parent
            if self.vsys == "shared":
                parent_xpath = self.xpath_root(Root.DEVICE)
            else:
                parent_xpath = self.xpath_root(Root.VSYS)
        elif isinstance(self.parent, panorama.Panorama):
            # Parent is Firewall or Panorama
            parent_xpath = self.parent.xpath_root(self.ROOT)
        else:
            try:
                # Bypass xpath of HAPairs
                parent_xpath = self.parent.xpath_bypass()
            except AttributeError:
                parent_xpath = self.parent.xpath()
        return parent_xpath

    def op(self, cmd=None, vsys=None, cmd_xml=True, extra_qs=None):
        if vsys is None:
            vsys = self.vsys
        self.xapi.op(cmd, vsys, cmd_xml, extra_qs)
        return self.xapi.element_root

    def generate_xapi(self):
        """Override super class to connect to Panorama

        Connect to this firewall via Panorama with 'target' argument set
        to this firewall's serial number.  This happens when panorama and serial
        variables are set in this firewall prior to the first connection.
        """
        try:
            self.panorama()
        except err.PanDeviceNotSet:
            return super(Firewall, self).generate_xapi()
        if self.serial is not None and self.hostname is None:
            if self.classify_exceptions:
                xapi_constructor = PanDevice.XapiWrapper
                kwargs = {'pan_device': self,
                          'api_key': self.panorama().api_key,
                          'hostname': self.panorama().hostname,
                          'port': self.panorama().port,
                          'timeout': self.timeout,
                          'serial': self.serial,
                          }
            else:
                xapi_constructor = pan.xapi.PanXapi
                kwargs = {'api_key': self.panorama().api_key,
                          'hostname': self.panorama().hostname,
                          'port': self.panorama().port,
                          'timeout': self.timeout,
                          'serial': self.serial,
                          }
            return xapi_constructor(**kwargs)
        else:
            return super(Firewall, self).generate_xapi()

    def refresh_system_info(self):
        """Refresh system information variables

        Returns:
            system information like version, platform, etc.
        """
        system_info = self.show_system_info()

        self.version = system_info['system']['sw-version']
        self.platform = system_info['system']['model']
        self.serial = system_info['system']['serial']
        self.multi_vsys = True if system_info['system']['multi-vsys'] == "on" else False

        return self.version, self.platform, self.serial

    def element(self):
        if self.serial is None:
            raise ValueError("Serial number must be set to generate element")
        entry = ET.Element("entry", {"name": self.serial})
        if self.parent == self.panorama() and self.serial is not None:
            # This is a firewall under a panorama
            if not self.multi_vsys:
                vsys = ET.SubElement(entry, "vsys")
                ET.SubElement(vsys, "entry", {"name": "vsys1"})
        elif self.parent == self.devicegroup() and self.multi_vsys:
            # This is a firewall under a device group
            if self.vsys.startswith("vsys"):
                vsys = ET.SubElement(entry, "vsys")
                ET.SubElement(vsys, "entry", {"name": self.vsys})
            else:
                vsys = ET.SubElement(entry, "vsys")
                all_vsys = self.findall(device.Vsys)
                for a_vsys in all_vsys:
                    ET.SubElement(vsys, "entry", {"name": a_vsys})
        return entry

    def apply(self):
        return

    def create(self):
        if self.parent is None:
            self.create_vsys()
            return
        # This is a firewall under a panorama or devicegroup
        panorama = self.panorama()
        logger.debug(panorama.hostname + ": create called on %s object \"%s\"" % (type(self), self.name))
        panorama.set_config_changed()
        element = self.element_str()
        panorama.xapi.set(self.xpath_short(), element)

    def delete(self):
        if self.parent is None:
            self.delete_vsys()
            return
        panorama = self.panorama()
        logger.debug(panorama.hostname + ": delete called on %s object \"%s\"" % (type(self), self.serial))
        if self.parent == self.devicegroup() and self.multi_vsys:
            # This is a firewall under a devicegroup
            # Refresh device-group first to see if this is the only vsys
            devices_xpath = self.devicegroup().xpath() + self.XPATH
            panorama.xapi.get(devices_xpath)
            devices_xml = panorama.xapi.element_root
            dg_vsys = devices_xml.findall("entry[@name='%s']/vsys/entry" % self.serial)
            if dg_vsys:
                if len(dg_vsys) == 1:
                    # Only vsys, so delete whole entry
                    panorama.set_config_changed()
                    panorama.xapi.delete(self.xpath())
                else:
                    # It's not the only vsys, just delete the vsys
                    panorama.set_config_changed()
                    panorama.xapi.delete(self.xpath() + "/vsys/entry[@name='%s']" % self.vsys)
        else:
            # This is a firewall under a panorama
            panorama.set_config_changed()
            panorama.xapi.delete(self.xpath())
        if self.parent is not None:
            self.parent.remove_by_name(self.name, type(self))

    def create_vsys(self):
        if self.vsys.startswith("vsys"):
            element = ET.Element("entry", {"name": self.vsys})
            if self.vsys_name is not None:
                ET.SubElement(element, "display-name").text = self.vsys_name
            self.set_config_changed()
            self.xapi.set(self.xpath_device() + "/vsys", ET.tostring(element))

    def delete_vsys(self):
        if self.vsys.startswith("vsys"):
            self.set_config_changed()
            self.xapi.delete(self.xpath_device() + "/vsys/entry[@name='%s']" % self.vsys)

    def show_system_resources(self):
        self.xapi.op(cmd="show system resources", cmd_xml=True)
        result = self.xapi.xml_root()
        regex = re.compile(r"load average: ([\d.]+).* ([\d.]+)%id.*Mem:.*?([\d.]+)k total.*?([\d]+)k free", re.DOTALL)
        match = regex.search(result)
        if match:
            """
            return cpu, mem_free, load
            """
            return {
                'load': Decimal(match.group(1)),
                'cpu': 100 - Decimal(match.group(2)),
                'mem_total': int(match.group(3)),
                'mem_free': int(match.group(4)),
            }
        else:
            raise err.PanDeviceError("Problem parsing show system resources",
                                     pan_device=self)

    def get_interface_counters(self, interface):
        """Pull the counters for an interface

        :param interface: interface object or str with name of interface
        :return: Dictionary of counters, or None if no counters for interface
        """
        interface_name = self._interface_name(interface)

        self.xapi.op("<show><counter><interface>%s</interface></counter></show>" % (interface_name,))
        pconf = PanConfig(self.xapi.element_result)
        response = pconf.python()
        counters = response['result']
        if counters:
            entry = {}
            # Check for entry in ifnet
            if 'entry' in counters.get('ifnet', {}):
                entry = counters['ifnet']['entry'][0]
            elif 'ifnet' in counters.get('ifnet', {}):
                if 'entry' in counters['ifnet'].get('ifnet', {}):
                    entry = counters['ifnet']['ifnet']['entry'][0]

            # Convert strings to integers, if they are integers
            entry.update((k, pandevice.convert_if_int(v)) for k, v in entry.iteritems())
            # If empty dictionary (no results) it usually means the interface is not
            # configured, so return None
            return entry if entry else None

    def commit_device_and_network(self, sync=False, exception=False):
        return self._commit(sync=sync, exclude="device-and-network",
                            exception=exception)

    def commit_policy_and_objects(self, sync=False, exception=False):
        return self._commit(sync=sync, exclude="policy-and-objects",
                            exception=exception)

