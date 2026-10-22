"""MCP navigation calls use ros2-web's saved points, never model-supplied coordinates."""
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from http_transport import urlopen


def navigation_intent(text):
    """Conservative command grammar. Negations/questions never become motion commands."""
    text = unicodedata.normalize('NFKC', text).strip()
    text = re.sub(r'[\s，,。.!！?？、：:;；“”"‘’]', '', text)
    text = re.sub(r'^(?:请帮我|请你|麻烦你|麻烦|帮我|请)', '', text)
    if re.fullmatch(r'(?:停止|取消|暂停|结束)(?:当前)?导航(?:吧)?', text):
        return 'cancel_navigation', {}
    if re.search(r'(有哪些|列出|查看|查询).*(导航点|预设点|点位)|(?:导航点|预设点|点位).*(有哪些|列表)', text):
        return 'list_navigation_points', {}
    if re.fullmatch(r'(?:导航(?:状态|进度|到哪里了|怎么样了)|还有多远|到(?:达)?了吗|离目标还有多远)', text):
        return 'get_navigation_status', {}
    nav_related = bool(re.search(r'导航|预设点|点位|(?:前往|去|到).*[点位]$', text))
    if re.search(r'不要|别|不许|不能|如果|假如|能否|怎么|如何|是否|为什么|能不能|吗|没(?:有)?', text):
        return ('help', {}) if nav_related else None
    match = re.fullmatch(r'(?:现在)?(?:导航到|导航去|前往|去往|带我到|带我去|移动到|去)(.+?)(?:吧)?', text)
    if match:
        target = match.group(1)
        if re.search(r'然后|再去|并且|或者|还是|以及', target):
            return 'help', {}
        return 'navigate_to_point', {'name': target}
    return ('help', {}) if nav_related else None


def navigation_reply(text, bridge):
    """Motion acknowledgements come from tool results, never generated model text."""
    intent = navigation_intent(text)
    if intent is None:
        return None
    name, arguments = intent
    if name == 'help':
        return '导航请说“导航到A点”，也可以说“有哪些导航点”“停止导航”或“导航状态”。这次没有下发移动指令。'
    if bridge is None:
        return '导航工具未启用，未下发指令。请检查助手 MCP 配置。'
    print('调用 MCP 工具：' + name + ' ' + json.dumps(arguments, ensure_ascii=False), file=sys.stderr, flush=True)
    try:
        result = bridge.call(name, arguments)
        data = result.get('structuredContent')
        if not isinstance(data, dict):
            data = json.loads(next(c['text'] for c in result.get('content', []) if c.get('type') == 'text'))
        if result.get('isError') or not data.get('success'):
            message = data.get('message') or data.get('error') or '工具未确认执行成功'
            print('导航 MCP 返回失败：' + str(message), file=sys.stderr, flush=True)
            heard = f'我听到的点名是“{arguments["name"]}”。' if name == 'navigate_to_point' else ''
            return heard + '导航请求未成功：' + str(message)
        print('导航 MCP 返回成功：' + json.dumps(data, ensure_ascii=False), file=sys.stderr, flush=True)
        if name == 'navigate_to_point':
            if data.get('submitted') is not True:
                return '导航服务未确认目标已下发，请查询导航状态。'
            return f'已下发前往{data.get("point", {}).get("name", arguments["name"])}的导航目标，尚未确认到达。'
        if name == 'cancel_navigation':
            return '已发送停止当前导航的指令。' if data.get('submitted') is True else '导航服务未确认停止指令，请检查导航状态。'
        if name == 'list_navigation_points':
            active = data.get('activeMap')
            points = [p for p in data.get('points', []) if not active or p.get('mapPath') == active]
            names = '、'.join(p['name'] for p in points)
            return ('当前地图的导航点有：' if active else '已保存的导航点有：') + names + '。' if names else '当前没有可用的预设导航点，请先在网页添加。'
        labels = {'OFFLINE': '导航节点未连接', 'UNKNOWN': '导航状态未知', 'IDLE': '正在等待设置初始位置',
                  'LOCALIZED': '定位已就绪，当前没有执行导航目标', 'GOAL_SUBMITTED': '目标已下发，等待导航节点反馈',
                  'PLANNING': '正在规划路线', 'TRACKING': '正在前往目标', 'ARRIVED': '导航节点报告已到达目标',
                  'PLAN_FAILED': '路线规划失败', 'SCAN_STALE_STOP': '激光数据过期，导航已停止',
                  'LOCAL_BLOCKED': '前方受阻', 'LOCAL_RECOVERY': '正在尝试避障'}
        answer = labels.get(data.get('status'), '导航状态暂不明确')
        distance = data.get('distance_remaining')
        if data.get('status') in {'TRACKING', 'LOCAL_BLOCKED', 'LOCAL_RECOVERY'} and isinstance(distance, (int, float)):
            answer += f'，剩余约{distance:.1f}米'
        return answer + '。'
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, StopIteration) as error:
        print('导航 MCP 异常：' + str(error), file=sys.stderr, flush=True)
        return '导航工具没有返回有效执行结果，请检查导航状态，不要假定指令已执行。'


def tool(name, description, properties=None, required=None):
    return {'name': name, 'description': description, 'inputSchema': {
        'type': 'object', 'properties': properties or {}, 'required': required or [], 'additionalProperties': False}}


NAVIGATION_TOOLS = [
    tool('list_navigation_points', '列出 ros2-web 保存的导航点及所属地图。导航前可用此工具确认真实点名，不能猜测坐标。'),
    tool('navigate_to_point', '用户明确要求导航到某个预设点时调用。按名称向当前地图的 ROS2 导航下发目标；成功仅代表已下发，不代表到达。未知点或定位未就绪会拒绝。',
         {'name': {'type': 'string', 'description': '用户指定的预设点完整名称，例如厨房、充电点。不要擅自选择相似点。'}}, ['name']),
    tool('cancel_navigation', '用户要求停止、取消当前导航时调用。停止当前目标，保留导航节点与定位。'),
    tool('get_navigation_status', '查询 ROS2 导航是否连接、是否就绪、实际状态和剩余距离。ARRIVED 才表示导航节点报告已到达。'),
]


def call_navigation(raw, name, arguments):
    if raw.get('NAVIGATION_MCP_ENABLED', True) is False:
        raise ValueError('导航 MCP 已在配置中禁用')
    if name == 'navigate_to_point':
        heard = unicodedata.normalize('NFKC', str(arguments.get('name', ''))).strip()
        # Correct only a clear Latin letter with a known ASR suffix error.
        # Prefer an exact saved name and only correct against the active map.
        match = re.fullmatch(r'([A-Za-z])(?:顶|定|典|店)', heard)
        numbered = re.fullmatch(r'([1-9]|10|[一二三四五六七八九十])号位', heard)
        if match or numbered:
            catalog = call_navigation(raw, 'list_navigation_points', {})
            active = catalog.get('activeMap')
            names = [p['name'] for p in catalog.get('points', []) if p.get('mapPath') == active] if active else []
            exact = [n for n in names if n.casefold() == heard.casefold()]
            def number_key(value):
                for index, digit in enumerate('一二三四五六七八九十', 1):
                    if value == digit + '号位':
                        return str(index) + '号位'
                return value
            candidates = ([n for n in names if n.casefold() == (match.group(1) + '点').casefold()] if match else
                          [n for n in names if number_key(n) == number_key(heard)])
            if not exact and len(candidates) == 1:
                arguments = {**arguments, 'name': candidates[0]}
                print(f'导航点名校正：{heard} → {candidates[0]}（当前地图已保存点位）', file=sys.stderr, flush=True)
    endpoint, payload = {
        'list_navigation_points': ('navigation/points', None),
        'get_navigation_status': ('navigation/state', None),
        'navigate_to_point': ('navigation/go-to', {'name': arguments.get('name')}),
        'cancel_navigation': ('navigation/cancel', {}),
    }[name]
    base = str(raw.get('ROS2_WEB_API_URL', 'http://127.0.0.1:3000/api')).rstrip('/')
    request = urllib.request.Request(base + '/' + endpoint, data=None if payload is None else json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=8, connect_timeout=2, prefer_ipv4=True) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            result = json.loads(error.read(8192))
        except ValueError:
            raise ValueError('导航服务返回错误，请检查 ros2-web') from None
        raise ValueError(result.get('error', '导航请求失败')) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError('无法连接 ros2-web 导航服务，请检查服务状态；不要假定目标已执行') from None
    if not result.get('success'):
        raise ValueError(result.get('error', '导航请求失败'))
    return result
