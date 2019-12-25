#-*- coding:utf-8 -*-

import pyshark
import json
import base64
import time
import re
import traceback
import concurrent.futures
from ._logging import _logging
from cacheout import Cache

class tcp_http_sniff():

	def __init__(self, interface, display_filter, syslog_ip, syslog_port, custom_tag, return_deep_info, http_filter_json, cache_size, bpf_filter, timeout, debug):
		"""
		构造函数
		:param interface: 捕获流量的网卡名
		:param display_filter: 数据包显示过滤器
		:param syslog_ip: 接收数据用 Syslog 服务器地址
		:param syslog_port: 接收数据用 Syslog 服务器端口
		:param custom_tag: 数据标签，用于区分不同的采集引擎
		:param return_deep_info: 是否处理更多信息，包括原始请求、响应头和正文
		:param http_filter_json: HTTP过滤器配置，支持按状态和内容类型过滤
		:param cache_size: 缓存的已处理数据条数，120秒内重复的数据将不会发送Syslog
		:param bpf_filter: 数据包底层过滤器
		:param timeout: 采集程序的运行超时时间，默认为启动后1小时自动退出
		:param debug: 调试开关
		"""
		self.debug = debug
		self.timeout = timeout
		self.bpf_filter = bpf_filter
		self.cache_size = cache_size
		self.http_filter_json = http_filter_json
		self.return_deep_info = return_deep_info
		self.custom_tag = custom_tag
		self.syslog_ip = syslog_ip
		self.syslog_port = syslog_port
		self.log_obj = _logging(self.syslog_ip,self.syslog_port)
		self.interface = interface
		self.display_filter = display_filter
		self.pktcap = pyshark.LiveCapture(interface=self.interface, bpf_filter=self.bpf_filter, use_json=False, display_filter=self.display_filter, debug=self.debug)
		self.http_stream_cache = Cache(maxsize=1500, ttl=15, timer=time.time, default=None)
		self.tcp_stream_cache = Cache(maxsize=1500, ttl=15, timer=time.time, default=None)
		self.http_cache = Cache(maxsize=self.cache_size, ttl=120, timer=time.time, default=None)
		self.tcp_cache = Cache(maxsize=self.cache_size, ttl=120, timer=time.time, default=None)
		# 检测页面编码的正则表达式
		self.encode_regex = re.compile(b'<meta [^>]*?charset=["\']?([^"\']+)["\']?', re.I)

	def http_filter(self, key, value):
		"""
		检查字符串中是否包含特定的规则
		:param key: 规则键名，response_code（状态码）或 content_type（内容类型）
		:param value: 要检查的字符串
		:return: True - 包含， False - 不包含
		"""
		if key in self.http_filter_json:
			for rule in self.http_filter_json[key]:
				if rule in value:
					return True
		return False
	
	def run(self):
		"""
		入口函数
		"""
		try:
			self.pktcap.apply_on_packets(self.proc_packet,timeout=self.timeout)
		except concurrent.futures.TimeoutError:
			print("\nTimeoutError.")
	
	def proc_packet(self, pkt):
		"""
		全局数据包处理：识别、路由及结果发送
		:param pkt: 数据包
		:return: JSON or None
		"""
		try:
			pkt_json = None
			pkt_dict = dir(pkt)
			
			if 'ip' in pkt_dict:
				if 'http' in pkt_dict:
					pkt_json = self.proc_http(pkt)
				elif 'tcp' in pkt_dict:
					pkt_json = self.proc_tcp(pkt)

			if pkt_json:
				if self.debug:
					print(json.dumps(pkt_json))
				self.log_obj.info(json.dumps(pkt_json))

		except Exception as e:
			print(e)
			print(traceback.format_exc())
	
	def proc_http(self, pkt):
		"""
		处理 HTTP 包
		:param pkt: 数据包
		:return: JSON or None
		"""
		http_dict = dir(pkt.http)
		
		if 'request' in http_dict:
			req = {
				'url': pkt.http.request_full_uri if 'request_full_uri' in http_dict else pkt.http.request_uri,
				'method': pkt.http.request_method
			}
			
			self.http_stream_cache.set(pkt.tcp.stream, req)
	
		elif 'response' in http_dict:
			pkt_json = {}
			src_addr = pkt.ip.src
			src_port = pkt[pkt.transport_layer].srcport
			
			cache_req = self.http_stream_cache.get(pkt.tcp.stream)
			if cache_req:
				pkt_json['url'] = cache_req['url']
				pkt_json['method'] = cache_req['method']
				self.http_stream_cache.delete(pkt.tcp.stream)
			
			if 'url' not in pkt_json:
				if 'response_for_uri' in http_dict:
					pkt_json["url"] = pkt.http.response_for_uri
				else:
					pkt_json["url"] = '/'

			# 处理 URL 只有URI的情况
			if pkt_json["url"][0] == '/':
				if src_port == '80':
					pkt_json["url"] = "http://%s%s"%(src_addr, pkt_json["url"])
				else:
					pkt_json["url"] = "http://%s:%s%s"%(src_addr, src_port, pkt_json["url"])

			# 缓存机制，防止短时间大量处理重复响应
			exists = self.http_cache.get(pkt_json['url'])
			if exists:
				return None

			self.http_cache.set(pkt_json["url"], True)

			pkt_json["pro"] = 'HTTP'
			pkt_json["tag"] = self.custom_tag
			pkt_json["ip"] = src_addr
			pkt_json["port"] = src_port

			if 'response_code' in http_dict:
				if self.http_filter_json:
					return_status = self.http_filter('response_code', pkt.http.response_code)
					if return_status:
						return None
				pkt_json["code"] = pkt.http.response_code
			
			if 'content_type' in http_dict:
				if self.http_filter_json:
					return_status = self.http_filter('content_type', pkt.http.content_type)
					if return_status:
						return None
				pkt_json["type"] = pkt.http.content_type.lower()
			else:
				pkt_json["type"] = 'unkown'

			if 'server' in http_dict:
				pkt_json["server"] = pkt.http.server

			# -r on开启深度数据分析，返回header和body等数据
			if self.return_deep_info:
				charset = 'utf-8'
				# 检测 Content-Type 中的编码信息
				if 'type' in pkt_json and 'charset=' in pkt_json["type"]:
					charset = pkt_json["type"][pkt_json["type"].find('charset=')+8:].strip().lower()
					if not charset :
						charset = 'utf-8'
				if 'payload' in dir(pkt.tcp):
					payload = bytes.fromhex(str(pkt.tcp.payload).replace(':', ''))
					if payload.find(b'HTTP/') == 0:
						split_pos = payload.find(b'\r\n\r\n')
						if split_pos <= 0 or split_pos > 2048:
							split_pos = 2048
						pkt_json["header"] = str(payload[:split_pos], 'utf-8', 'ignore')
				
				if 'file_data' in http_dict and pkt.http.file_data.raw_value and pkt_json['type'] != 'application/octet-stream':
					data = bytes.fromhex(pkt.http.file_data.raw_value)
					# 检测页面 Meta 中的编码信息
					data_head = data[:500] if data.find(b'</head>', 0, 1024) == -1 else data[:data.find(b'</head>')]
					match = self.encode_regex.search(data_head)
					if match:
						charset = str(match.group(1).strip().lower(), 'utf-8', 'ignore')
					
					response_body = self.proc_body(str(data, charset, 'ignore'), 16*1024)
					pkt_json["body"] = response_body
				else:
					pkt_json["body"] = ''
			
			return pkt_json
		
		return None

	def proc_tcp(self, pkt):
		"""
		处理 TCP 包
		:param pkt: 数据包
		:return: JSON or None
		"""
		tcp_stream = pkt.tcp.stream
		
		pkt_json = {}
		pkt_json["pro"] = 'TCP'
		pkt_json["tag"] = self.custom_tag

		# SYN+ACK
		if pkt.tcp.flags == '0x00000012' : 
			server_ip = pkt.ip.src
			server_port = pkt[pkt.transport_layer].srcport
			tcp_info = '%s:%s' % (server_ip, server_port)

			exists = self.tcp_cache.get(tcp_info)
			if exists:
				return None
			
			self.tcp_cache.set(tcp_info, True)
			if self.return_deep_info:
				self.tcp_stream_cache.set(tcp_stream, tcp_info)
			else:
				pkt_json["ip"] = server_ip
				pkt_json["port"] = server_port
				
				return pkt_json
		
		# -r on开启深度数据分析，采集server第一个响应数据包
		if self.return_deep_info and pkt.tcp.seq == "1" and "payload" in dir(pkt.tcp) :
			tcp_info = self.tcp_stream_cache.get(tcp_stream)
			if tcp_info:
				self.tcp_stream_cache.delete(tcp_stream)
				
				pkt_json["ip"] = pkt.ip.src
				pkt_json["port"] = pkt[pkt.transport_layer].srcport
				payload_data = pkt.tcp.payload.replace(":","")
				if payload_data.startswith("48545450"): # ^HTTP
					return None
				
				# HTTPS Protocol
				# TODO: other https port support 
				if pkt_json["port"] == "443" and payload_data.startswith("1603"): # SSL
					pkt_json["pro"] = 'HTTPS'
					pkt_json["url"] = "https://{}/".format(pkt_json["ip"])
				else:
					pkt_json["data"] = payload_data
				
				return pkt_json
		return None

	def proc_body(self, data, length):
		"""
		防止转换为 JSON 后超长的数据截取方法
		:param data: 原始数据
		:param length: 截取的数据长度
		:return: 截断后的数据
		"""
		json_data = json.dumps(data)[:length]
		total_len = len(json_data)
		if total_len < length:
			return data
		
		pos = json_data.rfind("\\u")
		if pos + 6 > len(json_data):
			json_data = json_data[:pos]
		
		return json.loads(json_data.rstrip(r'\"') + '"')
