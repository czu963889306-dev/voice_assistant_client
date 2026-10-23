"""Read-only Wi-Fi MCP tool and local routing for IP address questions."""
import ipaddress
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path


NETWORK_TOOLS = [{
    'name': 'get_wifi_ip',
    'description': '实时查询本设备 Wi-Fi 无线网卡的 IPv4 地址。询问当前 Wi-Fi IP、本机 IP 或要求播报 IP 时调用；不返回有线、回环或 VPN 地址。优先返回具有 Wi-Fi 默认路由的地址。speech_text 提供逐位朗读内容。未连接或未获得 IPv4 时明确报错，不能猜测地址。',
    'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    'annotations': {'readOnlyHint': True, 'idempotentHint': True, 'openWorldHint': False},
}]


class NetworkToolError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _ip_json(command, *args):
    try:
        result = subprocess.run([command, '-j', '-4', *args], capture_output=True, text=True,
                                timeout=3, check=False)
        data = json.loads(result.stdout) if result.returncode == 0 else None
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ValueError('Invalid ip output')
        return data
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise NetworkToolError('NETWORK_QUERY_FAILED', '无法读取当前网络地址，请检查 iproute2 工具和系统网络状态。') from None


def get_wifi_ip(sysfs_root=Path('/sys/class/net')):
    """Inspect actual wireless devices, not a hostname or internet route guess."""
    try:
        wireless = {path.name for path in Path(sysfs_root).iterdir()
                    if (path / 'wireless').is_dir() or (path / 'phy80211').exists()}
    except OSError:
        raise NetworkToolError('NETWORK_QUERY_FAILED', '无法读取本机网络设备。') from None
    if not wireless:
        raise NetworkToolError('WIFI_NOT_FOUND', '没有发现 Wi-Fi 网卡，无法播报 Wi-Fi IP。')
    command = shutil.which('ip')
    if not command:
        raise NetworkToolError('IPROUTE2_NOT_FOUND', '系统未安装 iproute2，暂时无法查询 Wi-Fi IP。')
    interfaces = []
    for row in _ip_json(command, 'address', 'show'):
        if row.get('ifname') not in wireless or 'UP' not in row.get('flags', []):
            continue
        if row.get('operstate') != 'UP' and 'LOWER_UP' not in row.get('flags', []):
            continue
        addresses = []
        for address in row.get('addr_info', []):
            if address.get('family') != 'inet' or address.get('scope') != 'global':
                continue
            try:
                value = ipaddress.IPv4Address(address.get('local', ''))
            except ipaddress.AddressValueError:
                continue
            if value.is_loopback or value.is_link_local or value.is_unspecified or value.is_multicast:
                continue
            if str(value) not in addresses:
                addresses.append(str(value))
        if addresses:
            interfaces.append({'interface': row['ifname'], 'ipv4_addresses': addresses})
    if not interfaces:
        raise NetworkToolError('WIFI_NO_IPV4', 'Wi-Fi 未连接，或尚未获得可用的 IPv4 地址。')
    # A missing default route does not invalidate a local Wi-Fi address.
    metrics = {}
    if len(interfaces) > 1:
        try:
            for route in _ip_json(command, 'route', 'show', 'default'):
                if route.get('dev') in wireless:
                    try:
                        metric = int(route.get('metric', 0))
                    except (ValueError, TypeError):
                        continue
                    name = route['dev']
                    metrics[name] = min(metrics.get(name, metric), metric)
        except NetworkToolError:
            pass
    interfaces.sort(key=lambda row: (metrics.get(row['interface'], float('inf')), row['interface']))
    primary = interfaces[0]
    address = primary['ipv4_addresses'][0]
    digits = dict(zip('0123456789', '零一二三四五六七八九'))
    spoken = '，点，'.join(''.join(digits[digit] for digit in part) for part in address.split('.'))
    return {'success': True, 'interface': primary['interface'], 'ipv4': address,
            'ipv4_addresses': primary['ipv4_addresses'], 'interfaces': interfaces,
            'message': f'当前 Wi-Fi 的 IP 地址是 {address}。',
            'speech_text': f'当前 Wi-Fi 的 IP 地址是，{spoken}。'}


def wifi_ip_query(text):
    compact = unicodedata.normalize('NFKC', text).casefold()
    compact = re.sub(r'[\s，,。.!！?？、：:;；“”"‘’\-]', '', compact)
    if 'ip' not in compact:
        return False
    if re.search(r'什么是ip|ip是什么东西|如何|怎么|设置|修改|更改|更换|公网|外网|网关|掩码|dns', compact):
        return False
    return bool(re.search(r'wifi|无线|本机|当前|现在|目前|设备|机器人|你的|你现在', compact)
                or re.fullmatch(r'(?:请|请你)?(?:查询|查看|播报|报一下|告诉我)?ip(?:地址)?(?:是多少|是什么|多少|呢|是啥)?', compact))


def network_reply(text, bridge):
    """Answer common IP questions through MCP without a cloud chat request."""
    if not wifi_ip_query(text):
        return None
    if bridge is None:
        return 'MCP 工具未启用，无法查询 Wi-Fi IP。'
    print('调用 MCP 工具：get_wifi_ip {}', file=sys.stderr, flush=True)
    try:
        result = bridge.call('get_wifi_ip', {})
        data = result.get('structuredContent')
        if not isinstance(data, dict):
            data = json.loads(next(part['text'] for part in result.get('content', []) if part.get('type') == 'text'))
        if result.get('isError') or data.get('success') is not True:
            return 'Wi-Fi IP 查询未成功：' + str(data.get('message') or '工具没有返回有效结果。')
        ipaddress.IPv4Address(data['ipv4'])
        if not isinstance(data.get('speech_text'), str) or not data['speech_text'].strip():
            raise ValueError('Missing speech text')
        return data['speech_text']
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, StopIteration):
        return '暂时无法读取 Wi-Fi IP，请检查网络工具服务。'
