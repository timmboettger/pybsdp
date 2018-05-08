#!/usr/bin/python
from os import listdir, mkdir, chown
from os.path import isdir, join
from pwd import getpwnam
import plistlib
import socket
import select
import struct
import sys
import ConfigParser

sys.path.append('/usr/local/lib/pybsdp')
import dhcp
import bsdp
from interfaces import all_interfaces


#
# Need to make an nbi module and move the code related to working with
# those files into that module.
#
# Cheap and easy way to check for interface changes is to simply refresh
# the interface list every 5 minutes to see if anything has changed.
#

def imageList(path):
    nbis = [ f for f in listdir(path) if isdir(join(path, f)) and len(f) > 4 and str(f)[-4:] == '.nbi' ]

    items = [ ]
    for f in nbis:
        dir = join(path, f)
        nbi = plistlib.readPlist(join(dir, 'NBImageInfo.plist'))
        item = { }
        item['Name'] = str(nbi['Name'])
        item['Path'] = f
        item['Description'] = str(nbi['Description'])
        item['Index'] = int(nbi['Index'])
        item['BootFile'] = str(nbi['BootFile'])
        item['RootPath'] = str(nbi['RootPath'])
        item['IsEnabled'] = bool(nbi['IsEnabled'])
        item['IsInstall'] = bool(nbi['IsInstall'])
        item['IsDefault'] = bool(nbi['IsDefault'])
        item['Type'] = str(nbi['Type'])
        item['Kind'] = int(nbi['Kind'])
        item['Architectures'] = str(nbi['Architectures'])
        imageid = item['Index']
        imageid += (item['Kind'] << 24)
        if item['IsInstall']:
            imageid += (1 << 31)
        item['ID'] = imageid

        items.append(item)

    return items


#
# Handle a DHCP packet.
#
def handleDhcpPacket(sock, ip, dpacket):
    if dpacket.options[dhcp.OPTION_MESSAGE_TYPE] == dhcp.MESSAGE_INFORM and dpacket.options[dhcp.OPTION_VENDOR_CLASS][0:9] == 'AAPLBSDPC':
        bpacket = bsdp.BsdpPacket()
        bpacket.decode(dpacket.options[dhcp.OPTION_VENDOR_INFORMATION])

        if bpacket.getType() == bsdp.TYPE_LIST:
            response = handleImageList(ip, dpacket, bpacket)
            if response != None:
                sock.sendto(response.encode(), addr)
        elif bpacket.getType() == bsdp.TYPE_SELECT:
            response = handleImageSelect(ip, dpacket, bpacket)
            if response != None:
                sock.sendto(response.encode(), addr)


#
# Handle a DHCP/BSDP packet LIST command.
#
def handleImageList(ip, dpacket, bpacket):
    #
    # Build the basic packet.
    #
    bresponse = bsdp.BsdpPacket()
    bresponse.setType(bsdp.TYPE_LIST)
    bresponse.setServerID(ip)
    bresponse.setServerPriority(32768)

    #
    # Rerieve the list of NetBoot Images.
    #
    images = imageList(netbootimagepath)
    if len(images) == 0:
        return None

    #
    # Walk each image and add it to the list of images if needed.
    #
    for image in images:
        if image['IsEnabled'] == False:
            continue
        imageid = image['ID']
        if image['IsDefault'] and bresponse.getDefaultBootImage() == None:
            bresponse.setDefaultBootImage(imageid)
        bresponse.appendBootImageList(imageid, image['Name'])

    #
    # If no default boot image was specified then set the last one to
    # be the default.
    #
    if bresponse.getDefaultBootImage() == None:
        bresponse.setDefaultBootImage(imageid)

    #
    # Build the response DHCP packet.
    #
    dresponse = dpacket.newAckPacket()
    dresponse.options[dhcp.OPTION_SERVER_IDENTIFIER] = ip
    dresponse.options[dhcp.OPTION_VENDOR_CLASS] = 'AAPLBSDPC'
    dresponse.options[dhcp.OPTION_VENDOR_INFORMATION] = bresponse.encode(True)

    return dresponse


#
# Handle a DHCP/BSDP packet SELECT command.
#
def handleImageSelect(ip, dpacket, bpacket):
    #
    # Find the boot image the user selected.
    #
    images = imageList(netbootimagepath)
    bootimage = None
    for image in images:
        if image['ID'] == bpacket.getSelectedBootImage():
            bootimage = image
    if bootimage == None:
        return None

    #
    # Build the DHCP Ack packet.
    #
    dresponse = dpacket.newAckPacket()
    dresponse.sname = ip
    dresponse.bfile = '/nbi/' + bootimage['Path'] + '/i386/booter'
    dresponse.options[dhcp.OPTION_VENDOR_CLASS] = 'AAPLBSDPC'
    dresponse.options[dhcp.OPTION_SERVER_IDENTIFIER] = ip
    #dresponse.options[dhcp.OPTION_TFTP_SERVER_NAME] = ip
    #dresponse.options[dhcp.OPTION_BOOTFILE_NAME] = bootimage['Path'] + '/i386/booter'
    if image['Type'] == 'NFS':
        dresponse.options[dhcp.OPTION_ROOT_PATH] = 'nfs:' + ip + ':' + netbootimagepath + ':' + bootimage['Path'] + '/' + bootimage['RootPath']
    else:
        dresponse.options[dhcp.OPTION_ROOT_PATH] = 'http://' + ip + '/' + bootimage['Path'].replace(' ', '%20') + '/' + bootimage['RootPath'].replace(' ', '%20')

    #
    # Machine name will follow the pattern of: NetBootMAC
    #
    #name = 'NetBoot' + ''.join('{:02x}'.format(c) for c in dpacket.chaddr)
    bresponse = bsdp.BsdpPacket()
    bresponse.setType(2)
    #bresponse.setMachineName(name)

    #
    # If this is not an install image, we need to setup a shadow
    # path for them to use.
    #
    if bootimage['IsInstall'] == False:
        path = join(netbootclientpath, name)
        if isdir(path) == False:
            mkdir(path)
            chown(path, getpwnam(netbootuser).pw_uid, -1)

        bresponse.setShadowMountURL('afp://' + netbootuser + ':' + netbootpass + '@' + ip + '/NetBootClients')
        bresponse.setShadowFilePath(name + '/Shadow')
    bresponse.setSelectedBootImage(bpacket.getSelectedBootImage())

    dresponse.options[dhcp.OPTION_VENDOR_INFORMATION] = bresponse.encode(True)

    return dresponse


#
# Begin main.
#
config = ConfigParser.RawConfigParser()
config.read('pybsdp.conf')
netbootimagepath = config.get('pybsdp', 'imagepath')
netbootclientpath = config.get('pybsdp', 'clientpath')
netbootuser = config.get('pybsdp', 'netbootuser')
netbootpass = config.get('pybsdp', 'netbootpass')

#
# Listen for all DHCP packets.
#
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(('0.0.0.0', 67))
interfaces = all_interfaces()
input = [sock]

while True:
    inputready,outputready,exceptready = select.select(input, [], [], 5)
    for s in inputready:
        #
        # Receive data from the socket.
        #
        data = None
        try:
            data, addr = s.recvfrom(8192)
        except:
            pass
        if data == None:
            continue

        #
        # If this packet did not come from a valid IP address, ignore.
        #
        if addr[0] == '0.0.0.0' or len(addr[0].split('.')) != 4:
            continue

        #
        # Find the likely IP address (interface) that received this packet.
        #
        match = None
        for (dev, ip) in interfaces:
            devseg = ip.split('.')
            fromseg = addr[0].split('.')
            if devseg[0] == fromseg[0]:
                if match == None:
                    match = (1, ip)
                if devseg[1] == fromseg[1]:
                    if match[0] < 2:
                        match = (2, ip)
                    if devseg[2] == fromseg[2]:
                        if match[0] < 3:
                            match = (3, ip)
        if match == None:
            continue
        ip = match[1]

        #
        # Try to decode and handle the packet, ignoring errors.
        #
        try:
            dpacket = dhcp.DhcpPacket()
            dpacket.decode(data)
            handleDhcpPacket(s, ip, dpacket)
        except:
            pass
