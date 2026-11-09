"""The create request the real `docker run` sends for a sandbox (captured from Docker CLI 29.9, built by orchestrator/dockercmd.py)."""

import copy
import json
from typing import Any

from dockerproxy.policy import Policy

RUN = "898c1ae2-c330-454b-9bf6-274f4c305038"
NAME = f"memorylab-run-{RUN}"
POLICY = Policy(images=frozenset({"memorylab-worker:dev"}), network="memorylab-sandbox")

GOOD: dict[str, Any] = json.loads(r'''
{"Hostname":"","Domainname":"","User":"10001:10001","AttachStdin":true,"AttachStdout":true,"AttachStderr":true,"Tty":false,"OpenStdin":true,
"StdinOnce":true,"Env":null,"Cmd":null,"Image":"memorylab-worker:dev","Volumes":{},"WorkingDir":"","Entrypoint":null,
"Labels":{"memorylab.owner":"memorylab","memorylab.run":"898c1ae2-c330-454b-9bf6-274f4c305038"},"StopTimeout":5,
"HostConfig":{"Binds":null,"ContainerIDFile":"","LogConfig":{"Type":"none","Config":{}},"NetworkMode":"memorylab-sandbox","PortBindings":{},
"RestartPolicy":{"Name":"no","MaximumRetryCount":0},"AutoRemove":true,"VolumeDriver":"","VolumesFrom":null,"ConsoleSize":[0,0],"CapAdd":null,
"CapDrop":["ALL"],"CgroupnsMode":"","Dns":null,"DnsOptions":[],"DnsSearch":[],"ExtraHosts":null,"GroupAdd":null,"IpcMode":"","Cgroup":"",
"Links":null,"OomScoreAdj":0,"PidMode":"","Privileged":false,"PublishAllPorts":false,"ReadonlyRootfs":true,"SecurityOpt":["no-new-privileges"],
"Tmpfs":{"/tmp":"rw,noexec,nosuid,nodev,size=64m"},"UTSMode":"","UsernsMode":"","ShmSize":0,"Isolation":"","CpuShares":0,"Memory":1073741824,
"NanoCpus":1000000000,"CgroupParent":"","BlkioWeight":0,"BlkioWeightDevice":[],"BlkioDeviceReadBps":[],"BlkioDeviceWriteBps":[],
"BlkioDeviceReadIOps":[],"BlkioDeviceWriteIOps":[],"CpuPeriod":0,"CpuQuota":0,"CpuRealtimePeriod":0,"CpuRealtimeRuntime":0,"CpusetCpus":"",
"CpusetMems":"","Devices":[],"DeviceCgroupRules":null,"DeviceRequests":null,"MemoryReservation":0,"MemorySwap":1073741824,"MemorySwappiness":-1,
"OomKillDisable":false,"PidsLimit":128,"Ulimits":[{"Name":"core","Hard":0,"Soft":0},{"Name":"nofile","Hard":1024,"Soft":1024}],"CpuCount":0,
"CpuPercent":0,"IOMaximumIOps":0,"IOMaximumBandwidth":0,"MaskedPaths":null,"ReadonlyPaths":null,"Init":true},
"NetworkingConfig":{"EndpointsConfig":{}}}''')


def mutated(**changes: Any) -> dict[str, Any]:
    """GOOD with dotted keys replaced: mutated(**{"HostConfig.Privileged": True}); a value of KEEP-ABSENT removes the key."""
    doc = copy.deepcopy(GOOD)
    for dotted, value in changes.items():
        *path, last = dotted.split(".")
        node = doc
        for part in path:
            node = node[part]
        if value is ABSENT:
            node.pop(last, None)
        else:
            node[last] = value
    return doc


class _Absent:
    pass


ABSENT = _Absent()
