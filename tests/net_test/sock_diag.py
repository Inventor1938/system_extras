#!/usr/bin/python
#
# Copyright 2015 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Partial Python implementation of sock_diag functionality."""

# pylint: disable=g-bad-todo

import errno
from socket import *  # pylint: disable=wildcard-import
import struct

import cstruct
import net_test
import netlink

### Base netlink constants. See include/uapi/linux/netlink.h.
NETLINK_SOCK_DIAG = 4

### sock_diag constants. See include/uapi/linux/sock_diag.h.
# Message types.
SOCK_DIAG_BY_FAMILY = 20
SOCK_DESTROY = 21

### inet_diag_constants. See include/uapi/linux/inet_diag.h
# Message types.
TCPDIAG_GETSOCK = 18

# Request attributes.
INET_DIAG_REQ_BYTECODE = 1

# Extensions.
INET_DIAG_NONE = 0
INET_DIAG_MEMINFO = 1
INET_DIAG_INFO = 2
INET_DIAG_VEGASINFO = 3
INET_DIAG_CONG = 4
INET_DIAG_TOS = 5
INET_DIAG_TCLASS = 6
INET_DIAG_SKMEMINFO = 7
INET_DIAG_SHUTDOWN = 8
INET_DIAG_DCTCPINFO = 9

# Bytecode operations.
INET_DIAG_BC_NOP = 0
INET_DIAG_BC_JMP = 1
INET_DIAG_BC_S_GE = 2
INET_DIAG_BC_S_LE = 3
INET_DIAG_BC_D_GE = 4
INET_DIAG_BC_D_LE = 5
INET_DIAG_BC_AUTO = 6
INET_DIAG_BC_S_COND = 7
INET_DIAG_BC_D_COND = 8

# Data structure formats.
# These aren't constants, they're classes. So, pylint: disable=invalid-name
InetDiagSockId = cstruct.Struct(
    "InetDiagSockId", "!HH16s16sI8s", "sport dport src dst iface cookie")
InetDiagReqV2 = cstruct.Struct(
    "InetDiagReqV2", "=BBBxIS", "family protocol ext states id",
    [InetDiagSockId])
InetDiagMsg = cstruct.Struct(
    "InetDiagMsg", "=BBBBSLLLLL",
    "family state timer retrans id expires rqueue wqueue uid inode",
    [InetDiagSockId])
InetDiagMeminfo = cstruct.Struct(
    "InetDiagMeminfo", "=IIII", "rmem wmem fmem tmem")
InetDiagBcOp = cstruct.Struct("InetDiagBcOp", "BBH", "code yes no")
InetDiagHostcond = cstruct.Struct("InetDiagHostcond", "=BBxxi",
                                  "family prefix_len port")

SkMeminfo = cstruct.Struct(
    "SkMeminfo", "=IIIIIIII",
    "rmem_alloc rcvbuf wmem_alloc sndbuf fwd_alloc wmem_queued optmem backlog")
TcpInfo = cstruct.Struct(
    "TcpInfo", "=BBBBBBBxIIIIIIIIIIIIIIIIIIIIIIII",
    "state ca_state retransmits probes backoff options wscale "
    "rto ato snd_mss rcv_mss "
    "unacked sacked lost retrans fackets "
    "last_data_sent last_ack_sent last_data_recv last_ack_recv "
    "pmtu rcv_ssthresh rtt rttvar snd_ssthresh snd_cwnd advmss reordering "
    "rcv_rtt rcv_space "
    "total_retrans")  # As of linux 3.13, at least.

TCP_TIME_WAIT = 6
ALL_NON_TIME_WAIT = 0xffffffff & ~(1 << TCP_TIME_WAIT)


class SockDiag(netlink.NetlinkSocket):

  FAMILY = NETLINK_SOCK_DIAG
  NL_DEBUG = []

  def _Decode(self, command, msg, nla_type, nla_data):
    """Decodes netlink attributes to Python types."""
    if msg.family == AF_INET or msg.family == AF_INET6:
      name = self._GetConstantName(__name__, nla_type, "INET_DIAG")
    else:
      # Don't know what this is. Leave it as an integer.
      name = nla_type

    if name in ["INET_DIAG_SHUTDOWN", "INET_DIAG_TOS", "INET_DIAG_TCLASS"]:
      data = ord(nla_data)
    elif name == "INET_DIAG_CONG":
      data = nla_data.strip("\x00")
    elif name == "INET_DIAG_MEMINFO":
      data = InetDiagMeminfo(nla_data)
    elif name == "INET_DIAG_INFO":
      # TODO: Catch the exception and try something else if it's not TCP.
      data = TcpInfo(nla_data)
    elif name == "INET_DIAG_SKMEMINFO":
      data = SkMeminfo(nla_data)
    else:
      data = nla_data

    return name, data

  def MaybeDebugCommand(self, command, data):
    name = self._GetConstantName(__name__, command, "SOCK_")
    if "ALL" not in self.NL_DEBUG and "SOCK" not in self.NL_DEBUG:
      return
    parsed = self._ParseNLMsg(data, InetDiagReqV2)
    print "%s %s" % (name, str(parsed))

  @staticmethod
  def _EmptyInetDiagSockId():
    return InetDiagSockId(("\x00" * len(InetDiagSockId)))

  def PackBytecode(self, instructions):
    """Compiles instructions to inet_diag bytecode.

    The input is a list of (INET_DIAG_BC_xxx, yes, no, arg) tuples, where yes
    and no are relative jump offsets measured in instructions. The yes branch
    is taken if the instruction matches.

    To accept, jump 1 past the last instruction. To reject, jump 2 past the
    last instruction.

    The target of a no jump is only valid if it is reachable by following
    only yes jumps from the first instruction - see inet_diag_bc_audit and
    valid_cc. This means that if cond1 and cond2 are two mutually exclusive
    filter terms, it is not possible to implement cond1 OR cond2 using:

      ...
      cond1 2 1 arg
      cond2 1 2 arg
      accept
      reject

    but only using:

      ...
      cond1 1 2 arg
      jmp   1 2
      cond2 1 2 arg
      accept
      reject

    The jmp instruction ignores yes and always jumps to no, but yes must be 1
    or the bytecode won't validate. It doesn't have to be jmp - any instruction
    that is guaranteed not to match on real data will do.

    Args:
      instructions: list of instruction tuples

    Returns:
      A string, the raw bytecode.
    """
    args = []
    positions = [0]

    for op, yes, no, arg in instructions:

      if yes <= 0 or no <= 0:
        raise ValueError("Jumps must be > 0")

      if op in [INET_DIAG_BC_NOP, INET_DIAG_BC_JMP, INET_DIAG_BC_AUTO]:
        arg = ""
      elif op in [INET_DIAG_BC_S_GE, INET_DIAG_BC_S_LE,
                  INET_DIAG_BC_D_GE, INET_DIAG_BC_D_LE]:
        arg = "\x00\x00" + struct.pack("=H", arg)
      elif op in [INET_DIAG_BC_S_COND, INET_DIAG_BC_D_COND]:
        addr, prefixlen, port = arg
        family = AF_INET6 if ":" in addr else AF_INET
        addr = inet_pton(family, addr)
        arg = InetDiagHostcond((family, prefixlen, port)).Pack() + addr
      else:
        raise ValueError("Unsupported opcode %d" % op)

      args.append(arg)
      length = len(InetDiagBcOp) + len(arg)
      positions.append(positions[-1] + length)

    # Reject label.
    positions.append(positions[-1] + 4)  # Why 4? Because the kernel uses 4.
    assert len(args) == len(instructions) == len(positions) - 2

    # print positions

    packed = ""
    for i, (op, yes, no, arg) in enumerate(instructions):
      yes = positions[i + yes] - positions[i]
      no = positions[i + no] - positions[i]
      instruction = InetDiagBcOp((op, yes, no)).Pack() + args[i]
      #print "%3d: %d %3d %3d %s %s" % (positions[i], op, yes, no,
      #                                 arg, instruction.encode("hex"))
      packed += instruction
    #print

    return packed

  def Dump(self, diag_req, bytecode=""):
    out = self._Dump(SOCK_DIAG_BY_FAMILY, diag_req, InetDiagMsg, bytecode)
    return out

  def DumpAllInetSockets(self, protocol, bytecode, sock_id=None, ext=0,
                         states=ALL_NON_TIME_WAIT):
    """Dumps IPv4 or IPv6 sockets matching the specified parameters."""
    # DumpSockets(AF_UNSPEC) does not result in dumping all inet sockets, it
    # results in ENOENT.
    if sock_id is None:
      sock_id = self._EmptyInetDiagSockId()

    if bytecode:
      bytecode = self._NlAttr(INET_DIAG_REQ_BYTECODE, bytecode)

    sockets = []
    for family in [AF_INET, AF_INET6]:
      diag_req = InetDiagReqV2((family, protocol, ext, states, sock_id))
      sockets += self.Dump(diag_req, bytecode)

    return sockets

  @staticmethod
  def GetRawAddress(family, addr):
    """Fetches the source address from an InetDiagMsg."""
    addrlen = {AF_INET:4, AF_INET6: 16}[family]
    return inet_ntop(family, addr[:addrlen])

  @staticmethod
  def GetSourceAddress(diag_msg):
    """Fetches the source address from an InetDiagMsg."""
    return SockDiag.GetRawAddress(diag_msg.family, diag_msg.id.src)

  @staticmethod
  def GetDestinationAddress(diag_msg):
    """Fetches the source address from an InetDiagMsg."""
    return SockDiag.GetRawAddress(diag_msg.family, diag_msg.id.dst)

  @staticmethod
  def RawAddress(addr):
    """Converts an IP address string to binary format."""
    family = AF_INET6 if ":" in addr else AF_INET
    return inet_pton(family, addr)

  @staticmethod
  def PaddedAddress(addr):
    """Converts an IP address string to binary format for InetDiagSockId."""
    padded = SockDiag.RawAddress(addr)
    if len(padded) < 16:
      padded += "\x00" * (16 - len(padded))
    return padded

  @staticmethod
  def DiagReqFromSocket(s):
    """Creates an InetDiagReqV2 that matches the specified socket."""
    family = s.getsockopt(net_test.SOL_SOCKET, net_test.SO_DOMAIN)
    protocol = s.getsockopt(net_test.SOL_SOCKET, net_test.SO_PROTOCOL)
    if net_test.LINUX_VERSION >= (3, 8):
      iface = s.getsockopt(SOL_SOCKET, net_test.SO_BINDTODEVICE,
                           net_test.IFNAMSIZ)
      iface = GetInterfaceIndex(iface) if iface else 0
    else:
      iface = 0
    src, sport = s.getsockname()[:2]
    try:
      dst, dport = s.getpeername()[:2]
    except error, e:
      if e.errno == errno.ENOTCONN:
        dport = 0
        dst = "::" if family == AF_INET6 else "0.0.0.0"
      else:
        raise e
    src = SockDiag.PaddedAddress(src)
    dst = SockDiag.PaddedAddress(dst)
    sock_id = InetDiagSockId((sport, dport, src, dst, iface, "\x00" * 8))
    return InetDiagReqV2((family, protocol, 0, 0xffffffff, sock_id))

  def FindSockDiagFromReq(self, req):
    for diag_msg, attrs in self.Dump(req):
      return diag_msg
    raise ValueError("Dump of %s returned no sockets" % req)

  def FindSockDiagFromFd(self, s):
    """Gets an InetDiagMsg from the kernel for the specified socket."""
    req = self.DiagReqFromSocket(s)
    return self.FindSockDiagFromReq(req)

  def GetSockDiag(self, req):
    """Gets an InetDiagMsg from the kernel for the specified request."""
    self._SendNlRequest(SOCK_DIAG_BY_FAMILY, req.Pack(), netlink.NLM_F_REQUEST)
    return self._GetMsg(InetDiagMsg)[0]

  @staticmethod
  def DiagReqFromDiagMsg(d, protocol):
    """Constructs a diag_req from a diag_msg the kernel has given us."""
    return InetDiagReqV2((d.family, protocol, 0, 1 << d.state, d.id))

  def CloseSocket(self, req):
    self._SendNlRequest(SOCK_DESTROY, req.Pack(),
                        netlink.NLM_F_REQUEST | netlink.NLM_F_ACK)

  def CloseSocketFromFd(self, s):
    diag_msg = self.FindSockDiagFromFd(s)
    protocol = s.getsockopt(SOL_SOCKET, net_test.SO_PROTOCOL)
    req = self.DiagReqFromDiagMsg(diag_msg, protocol)
    return self.CloseSocket(req)


if __name__ == "__main__":
  n = SockDiag()
  n.DEBUG = True
  bytecode = ""
  sock_id = n._EmptyInetDiagSockId()
  sock_id.dport = 443
  ext = 1 << (INET_DIAG_TOS - 1) | 1 << (INET_DIAG_TCLASS - 1)
  states = 0xffffffff
  diag_msgs = n.DumpAllInetSockets(IPPROTO_TCP, "",
                                   sock_id=sock_id, ext=ext, states=states)
  print diag_msgs
