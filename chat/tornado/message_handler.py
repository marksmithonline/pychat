import json
import logging
import re
import urllib

from django.core.exceptions import ValidationError
from django.db.models import Q
from tornado.gen import engine, Task
from tornado.httpclient import AsyncHTTPClient
from tornado.web import asynchronous
from tornadoredis import Client
from chat import settings
from chat.log_filters import id_generator
from chat.models import Message, Room, RoomUsers
from chat.py2_3 import str_type, quote
from chat.settings import ALL_ROOM_ID, TORNADO_REDIS_PORT, WEBRTC_CONNECTION, GIPHY_URL, GIPHY_REGEX
from chat.tornado.constants import VarNames, HandlerNames, Actions, RedisPrefix, WebRtcRedisStates
from chat.tornado.image_utils import process_images, prepare_img, save_images, get_message_images
from chat.tornado.message_creator import WebRtcMessageCreator, MessagesCreator
from chat.utils import get_max_key, do_db, validate_edit_message, get_or_create_room, \
	create_room

parent_logger = logging.getLogger(__name__)
base_logger = logging.LoggerAdapter(parent_logger, {
	'id': 0,
	'ip': '000.000.000.000'
})

# TODO https://github.com/leporo/tornado-redis#connection-pool-support
# CONNECTION_POOL = tornadoredis.ConnectionPool(
# max_connections=500,
# wait_for_available=True)

GIPHY_API_KEY = getattr(settings, "GIPHY_API_KEY", None)

class MessagesHandler(MessagesCreator):

	def __init__(self, *args, **kwargs):
		self.closed_channels = None
		self.parsable_prefix = 'p'
		super(MessagesHandler, self).__init__()
		self.webrtc_ids = {}
		self.id = None  # child init
		self.sex = None
		self.sender_name = None
		self.user_id = 0  # anonymous by default
		self.ip = None
		from chat import global_redis
		self.async_redis_publisher = global_redis.async_redis_publisher
		self.sync_redis = global_redis.sync_redis
		self.channels = []
		self._logger = None
		self.async_redis = Client(port=TORNADO_REDIS_PORT)
		self.patch_tornadoredis()
		self.pre_process_message = {
			Actions.GET_MESSAGES: self.process_get_messages,
			Actions.SEND_MESSAGE: self.process_send_message,
			Actions.CREATE_DIRECT_CHANNEL: self.create_user_channel,
			Actions.DELETE_ROOM: self.delete_channel,
			Actions.EDIT_MESSAGE: self.edit_message,
			Actions.CREATE_ROOM_CHANNEL: self.create_new_room,
			Actions.INVITE_USER: self.invite_user,
			Actions.PING: self.respond_ping
		}
		self.post_process_message = {
			Actions.CREATE_DIRECT_CHANNEL: self.send_client_new_channel,
			Actions.CREATE_ROOM_CHANNEL: self.send_client_new_channel,
			Actions.DELETE_ROOM: self.send_client_delete_channel,
			Actions.INVITE_USER: self.send_client_new_channel
		}

	def patch_tornadoredis(self):  # TODO remove this
		fabric = type(self.async_redis.connection.readline)
		self.async_redis.connection.old_read = self.async_redis.connection.readline

		def new_read(new_self, callback=None):
			try:
				return new_self.old_read(callback=callback)
			except Exception as e:
				current_online = self.get_online_from_redis(RedisPrefix.DEFAULT_CHANNEL)
				self.logger.error(e)
				self.logger.error(
					"Exception info: "
					"self.id: %s ;;; "
					"self.connected = '%s';;; "
					"Redis default channel online = '%s';;; "
					"self.channels = '%s';;; "
					"self.closed_channels  = '%s';;;",
					self.id, self.connected, current_online, self.channels, self.closed_channels
				)
				raise e

		self.async_redis.connection.readline = fabric(new_read, self.async_redis.connection)

	@property
	def connected(self):
		raise NotImplemented

	@connected.setter
	def connected(self, value):
		raise NotImplemented

	@engine
	def listen(self, channels):
		yield Task(
			self.async_redis.subscribe, channels)
		self.async_redis.listen(self.pub_sub_message)

	@property
	def logger(self):
		return self._logger if self._logger else base_logger

	@engine
	def add_channel(self, channel):
		self.channels.append(channel)
		yield Task(self.async_redis.subscribe, (channel,))

	def get_online_from_redis(self, channel):
		return self.get_online_and_status_from_redis(channel)[1]

	def get_online_and_status_from_redis(self, channel):
		"""
		:rtype : (bool, list)
		"""
		online = self.sync_redis.ssmembers(channel)
		self.logger.debug('!! channel %s redis online: %s', channel, online)
		return self.parse_redis_online(online) if online else (False, [])

	def parse_redis_online(self, online):
		"""
		:rtype : (bool, list)
		"""
		result = set()
		user_is_online = False
		for decoded in online:  # py2 iteritems
			# : char specified in cookies_middleware.py.create_id
			user_id = int(decoded.split(':')[0])
			if user_id == self.user_id and decoded != self.id:
				user_is_online = True
			result.add(user_id)
		return user_is_online, list(result)

	def add_online_user(self, room_id, offline_messages=None):
		"""
		adds to redis
		online_users = { connection_hash1 = stored_redis_user1, connection_hash_2 = stored_redis_user2 }
		:return:
		"""
		self.async_redis_publisher.sadd(room_id, self.id)
		# since we add user to online first, latest trigger will always show correct online
		is_online, online = self.get_online_and_status_from_redis(room_id)
		if is_online:  # Send user names to self
			online_user_names_mes = self.room_online(online, Actions.REFRESH_USER, room_id)
			self.logger.info('!! Second tab, retrieving online for self')
			self.ws_write(online_user_names_mes)
		else:  # if a new tab has been opened
			online.append(self.user_id)
			online_user_names_mes = self.room_online(online, Actions.LOGIN, room_id)
			self.logger.info('!! First tab, sending refresh online for all')
			self.publish(online_user_names_mes, room_id)
			if offline_messages:
				self.ws_write(self.load_offline_message(offline_messages, room_id))

	def publish(self, message, channel, parsable=False):
		jsoned_mess = json.dumps(message)
		self.logger.debug('<%s> %s', channel, jsoned_mess)
		if parsable:
			jsoned_mess = self.encode(jsoned_mess)
		self.async_redis_publisher.publish(channel, jsoned_mess)

	def encode(self, message):
		"""
		Marks message with prefix to specify that
		it should be decoded and proccesed before sending to client
		@param message: message to mark
		@return: marked message
		"""
		return self.parsable_prefix + message

	def remove_parsable_prefix(self, message):
		if message.startswith(self.parsable_prefix):
			return message[1:]

	def pub_sub_message(self, message):
		data = message.body
		if isinstance(data, str_type):  # subscribe event
			prefixless_str = self.remove_parsable_prefix(data)
			if prefixless_str:
				dict_message = json.loads(prefixless_str)
				res = self.post_process_message[dict_message[VarNames.EVENT]](dict_message)
				if not res:
					self.ws_write(prefixless_str)
			else:
				self.ws_write(data)

	def ws_write(self, message):
		raise NotImplementedError('WebSocketHandler implements')

	@asynchronous
	def search_giphy(self, message, query, cb):
		self.logger.debug("!! Asking giphy for: %s", query)
		def on_giphy_reply(response):
			try:
				self.logger.debug("!! Got giphy response: " + str(response.body))
				res =  json.loads(response.body)
				giphy = res['data'][0]['embed_url']
			except:
				giphy = None
			cb(message, giphy)
		http_client = AsyncHTTPClient()
		url = GIPHY_URL.format(GIPHY_API_KEY, quote(query, safe=''))
		http_client.fetch(url, callback=on_giphy_reply)

	def isGiphy(self, content):
		if GIPHY_API_KEY is not None:
			giphy_match = re.search(GIPHY_REGEX, content)
			return giphy_match.group(1) if giphy_match is not None else None

	def process_send_message(self, message):
		"""
		:type message: dict
		"""
		content = message.get(VarNames.CONTENT)
		giphy_match = self.isGiphy(content)
		def send_message(message, giphy=None):
			raw_imgs = message.get(VarNames.IMG)
			channel = message[VarNames.CHANNEL]
			message_db = Message(
				sender_id=self.user_id,
				content=message[VarNames.CONTENT],
				symbol=get_max_key(raw_imgs),
				giphy=giphy
			)
			message_db.room_id = channel
			do_db(message_db.save)
			db_images = save_images(raw_imgs, message_db.id)
			prepared_message = self.create_send_message(
				message_db,
				Actions.PRINT_MESSAGE,
				prepare_img(db_images, message_db.id)
			)
			self.publish(prepared_message, channel)
		if giphy_match is not None:
			self.search_giphy(message, giphy_match, send_message)
		else:
			send_message(message)

	def create_new_room(self, message):
		room_name = message[VarNames.ROOM_NAME]
		if not room_name or len(room_name) > 16:
			raise ValidationError('Incorrect room name "{}"'.format(room_name))
		room = Room(name=room_name)
		do_db(room.save)
		RoomUsers(room_id=room.id, user_id=self.user_id).save()
		subscribe_message = self.subscribe_room_channel_message(room.id, room_name)
		self.publish(subscribe_message, self.channel, True)

	def invite_user(self, message):
		room_id = message[VarNames.ROOM_ID]
		user_id = message[VarNames.USER_ID]
		room = get_or_create_room(self.channels, room_id, user_id)
		users_in_room = {
			user.id: RedisPrefix.set_js_user_structure(user.username, user.sex)
			for user in room.users.all()
		}
		self.publish(self.add_user_to_room(room_id, user_id, users_in_room[user_id]), room_id)
		subscribe_message = self.invite_room_channel_message(room_id, user_id, room.name, users_in_room)
		self.publish(subscribe_message, RedisPrefix.generate_user(user_id), True)

	def respond_ping(self, message):
		self.ws_write(self.responde_pong())

	def create_user_channel(self, message):
		user_id = message[VarNames.USER_ID]
		room_id = create_room(self.user_id, user_id)
		subscribe_message = self.subscribe_direct_channel_message(room_id, user_id)
		self.publish(subscribe_message, self.channel, True)
		other_channel = RedisPrefix.generate_user(user_id)
		if self.channel != other_channel:
			self.publish(subscribe_message, other_channel, True)

	def delete_channel(self, message):
		room_id = message[VarNames.ROOM_ID]
		if room_id not in self.channels or room_id == ALL_ROOM_ID:
			raise ValidationError('You are not allowed to exit this room')
		room = do_db(Room.objects.get, id=room_id)
		if room.disabled:
			raise ValidationError('Room is already deleted')
		if room.name is None:  # if private then disable
			room.disabled = True
		else:  # if public -> leave the room, delete the link
			RoomUsers.objects.filter(room_id=room.id, user_id=self.user_id).delete()
			online = self.get_online_from_redis(room_id)
			online.remove(self.user_id)
			self.publish(self.room_online(online, Actions.LOGOUT, room_id), room_id)
		room.save()
		message = self.unsubscribe_direct_message(room_id)
		self.publish(message, room_id, True)


	def edit_message(self, data):
		message_id = data[VarNames.MESSAGE_ID]
		message = do_db(Message.objects.get, id=message_id)
		validate_edit_message(self.user_id, message)
		message.content = data[VarNames.CONTENT]
		selector = Message.objects.filter(id=message_id)
		giphy_match = self.isGiphy(data[VarNames.CONTENT])
		if message.content is None:
			action = Actions.DELETE_MESSAGE
			prep_imgs = None
			selector.update(deleted=True)
		elif giphy_match is not None:
			def edit_glyphy(message, giphy):
				do_db(selector.update, content=message.content, symbol=message.symbol, giphy=giphy)
				message.giphy = giphy
				self.publish(self.create_send_message(message, Actions.EDIT_MESSAGE, None), message.room_id)
			self.search_giphy(message, giphy_match, edit_glyphy)
			return
		else:
			action = Actions.EDIT_MESSAGE
			message.giphy = None
			prep_imgs = process_images(data.get(VarNames.IMG), message)
			selector.update(content=message.content, symbol=message.symbol, giphy=None)
		self.publish(self.create_send_message(message, action, prep_imgs), message.room_id)

	def send_client_new_channel(self, message):
		room_id = message[VarNames.ROOM_ID]
		self.add_channel(room_id)
		self.add_online_user(room_id)

	def send_client_delete_channel(self, message):
		room_id = message[VarNames.ROOM_ID]
		self.async_redis.unsubscribe((room_id,))
		self.async_redis_publisher.hdel(room_id, self.id)
		self.channels.remove(room_id)

	def process_get_messages(self, data):
		"""
		:type data: dict
		"""
		header_id = data.get(VarNames.GET_MESSAGES_HEADER_ID, None)
		count = int(data.get(VarNames.GET_MESSAGES_COUNT, 10))
		room_id = data[VarNames.CHANNEL]
		self.logger.info('!! Fetching %d messages starting from %s', count, header_id)
		if header_id is None:
			messages = Message.objects.filter(Q(room_id=room_id), Q(deleted=False)).order_by('-pk')[:count]
		else:
			messages = Message.objects.filter(Q(id__lt=header_id), Q(room_id=room_id), Q(deleted=False)).order_by('-pk')[:count]
		images = do_db(get_message_images, messages)
		response = self.get_messages(messages, room_id, images)
		self.ws_write(response)


class WebRtcMessageHandler(MessagesHandler, WebRtcMessageCreator):

	def __init__(self, *args, **kwargs):
		super(WebRtcMessageHandler, self).__init__(*args, **kwargs)
		self.pre_process_message.update({
			Actions.WEBRTC: self.proxy_webrtc,
			Actions.CLOSE_FILE_CONNECTION: self.close_file_connection,
			Actions.CLOSE_CALL_CONNECTION: self.close_call_connection,
			Actions.CANCEL_CALL_CONNECTION: self.cancel_call_connection,
			Actions.ACCEPT_CALL: self.accept_call,
			Actions.ACCEPT_FILE: self.accept_file,
			Actions.OFFER_FILE_CONNECTION: self.offer_webrtc_connection,
			Actions.OFFER_CALL_CONNECTION: self.offer_webrtc_connection,
			Actions.REPLY_FILE_CONNECTION: self.reply_file_connection,
			Actions.RETRY_FILE_CONNECTION: self.retry_file_connection,
			Actions.REPLY_CALL_CONNECTION: self.reply_call_connection,
		})
		self.post_process_message.update({
			Actions.OFFER_FILE_CONNECTION: self.set_opponent_call_channel,
			Actions.OFFER_CALL_CONNECTION: self.set_opponent_call_channel
		})

	def set_opponent_call_channel(self, message):
		connection_id = message[VarNames.CONNECTION_ID]
		if message[VarNames.WEBRTC_OPPONENT_ID] == self.id:
			return True
		self.sync_redis.hset(connection_id, self.id, WebRtcRedisStates.OFFERED)

	def offer_webrtc_connection(self, in_message):
		room_id = in_message[VarNames.CHANNEL]
		content = in_message.get(VarNames.CONTENT)
		qued_id = in_message[VarNames.WEBRTC_QUED_ID]
		connection_id = id_generator(RedisPrefix.CONNECTION_ID_LENGTH)
		# use list because sets dont have 1st element which is offerer
		self.async_redis_publisher.hset(WEBRTC_CONNECTION, connection_id, self.id)
		self.async_redis_publisher.hset(connection_id, self.id, WebRtcRedisStates.READY)
		opponents_message = self.offer_webrtc(content, connection_id, room_id, in_message[VarNames.EVENT])
		self_message = self.set_connection_id(qued_id, connection_id)
		self.ws_write(self_message)
		self.logger.info('!! Offering a webrtc, connection_id %s', connection_id)
		self.publish(opponents_message, room_id, True)

	def retry_file_connection(self, in_message):
		connection_id = in_message[VarNames.CONNECTION_ID]
		opponent_ws_id = in_message[VarNames.WEBRTC_OPPONENT_ID]
		sender_ws_id = self.sync_redis.shget(WEBRTC_CONNECTION, connection_id)
		receiver_ws_status = self.sync_redis.shget(connection_id, opponent_ws_id)
		if receiver_ws_status == WebRtcRedisStates.READY and self.id == sender_ws_id:
			self.publish(self.retry_file(connection_id), opponent_ws_id)
		else:
			raise ValidationError("Invalid channel status.")

	def reply_file_connection(self, in_message):
		connection_id = in_message[VarNames.CONNECTION_ID]
		sender_ws_id = self.sync_redis.shget(WEBRTC_CONNECTION, connection_id)
		sender_ws_status = self.sync_redis.shget(connection_id, sender_ws_id)
		self_ws_status = self.sync_redis.shget(connection_id, self.id)
		if sender_ws_status == WebRtcRedisStates.READY and self_ws_status == WebRtcRedisStates.OFFERED:
			self.async_redis_publisher.hset(connection_id, self.id, WebRtcRedisStates.RESPONDED)
			self.publish(self.reply_webrtc(
				Actions.REPLY_FILE_CONNECTION,
				connection_id,
				HandlerNames.WEBRTC_TRANSFER,
				in_message[VarNames.CONTENT]
			), sender_ws_id)
		else:
			raise ValidationError("Invalid channel status.")

	def reply_call_connection(self, in_message):
		self.send_call_answer(
			in_message,
			WebRtcRedisStates.RESPONDED,
			Actions.REPLY_CALL_CONNECTION,
			[WebRtcRedisStates.OFFERED],
			HandlerNames.WEBRTC_TRANSFER
		)

	def proxy_webrtc(self, in_message):
		"""
		:type in_message: dict
		"""
		connection_id = in_message[VarNames.CONNECTION_ID]
		channel = in_message.get(VarNames.WEBRTC_OPPONENT_ID)
		self_channel_status = self.sync_redis.shget(connection_id, self.id)
		opponent_channel_status = self.sync_redis.shget(connection_id, channel)
		if not (self_channel_status == WebRtcRedisStates.READY and opponent_channel_status == WebRtcRedisStates.READY):
			raise ValidationError('Error in connection status, your status is {} while opponent is {}'.format(
				self_channel_status, opponent_channel_status
			))  # todo receiver should only accept proxy_webrtc from sender, sender can accept all
		# I mean somebody if there're 3 ppl in 1 channel and first is initing transfer to 2nd and 3rd,
		# 2nd guy can fraud 3rd guy webrtc traffic, which is allowed during the call, but not while transering file
		in_message[VarNames.WEBRTC_OPPONENT_ID] = self.id
		in_message[VarNames.HANDLER_NAME] = HandlerNames.PEER_CONNECTION
		self.logger.debug(
			"!! Forwarding message to channel %s, self %s, other status %s",
			channel,
			self_channel_status,
			opponent_channel_status
		)
		self.publish(in_message, channel)

	def close_file_connection(self, in_message):
		connection_id = in_message[VarNames.CONNECTION_ID]
		self_channel_status = self.sync_redis.shget(connection_id, self.id)
		if not self_channel_status:
			raise Exception("Access Denied")
		if self_channel_status != WebRtcRedisStates.CLOSED:
			sender_id = self.sync_redis.shget(WEBRTC_CONNECTION, connection_id)
			if sender_id == self.id:
				self.close_file_sender(connection_id)
			else:
				self.close_file_receiver(connection_id, in_message, sender_id)
			self.async_redis_publisher.hset(connection_id, self.id, WebRtcRedisStates.CLOSED)

	def close_call_connection(self, in_message):
		self.send_call_answer(
			in_message,
			WebRtcRedisStates.CLOSED,
			Actions.CLOSE_CALL_CONNECTION,
			[WebRtcRedisStates.READY, WebRtcRedisStates.RESPONDED],
			HandlerNames.PEER_CONNECTION
		)

	def cancel_call_connection(self, in_message):
		self.send_call_answer(
			in_message,
			WebRtcRedisStates.CLOSED,
			Actions.CANCEL_CALL_CONNECTION,
			[WebRtcRedisStates.OFFERED],
			HandlerNames.WEBRTC_TRANSFER
		)

	def close_file_receiver(self, connection_id, in_message, sender_id):
		sender_status = self.sync_redis.shget(connection_id, sender_id)
		if not sender_status:
			raise Exception("Access denied")
		if sender_status != WebRtcRedisStates.CLOSED:
			in_message[VarNames.WEBRTC_OPPONENT_ID] = self.id
			in_message[VarNames.HANDLER_NAME] = HandlerNames.PEER_CONNECTION
			self.publish(in_message, sender_id)

	def close_file_sender(self, connection_id):
		values = self.sync_redis.shgetall(connection_id)
		del values[self.id]
		message = self.get_close_file_sender_message(connection_id)
		for ws_id in values:
			if values[ws_id] == WebRtcRedisStates.CLOSED:
				continue
			self.publish(message, ws_id)

	def accept_file(self, in_message):
		connection_id = in_message[VarNames.CONNECTION_ID]
		content = in_message[VarNames.CONTENT]
		sender_ws_id = self.sync_redis.shget(WEBRTC_CONNECTION, connection_id)
		sender_ws_status = self.sync_redis.shget(connection_id, sender_ws_id)
		self_ws_status = self.sync_redis.shget(connection_id, self.id)
		if sender_ws_status == WebRtcRedisStates.READY \
				and self_ws_status in [WebRtcRedisStates.RESPONDED, WebRtcRedisStates.READY]:
			self.async_redis_publisher.hset(connection_id, self.id, WebRtcRedisStates.READY)
			self.publish(self.get_accept_file_message(connection_id, content), sender_ws_id)
		else:
			raise ValidationError("Invalid channel status")

	# todo
	# we can use channel_status = self.sync_redis.shgetall(connection_id)
	# and then self.async_redis_publisher.hset(connection_id, self.id, WebRtcRedisStates.READY)
	# if we shgetall and only then do async hset
	# we can catch an issue when 2 concurrent users accepted the call
	# but we didn't  send them ACCEPT_CALL as they both were in status 'offered'
	def accept_call(self, in_message):
		connection_id = in_message[VarNames.CONNECTION_ID]
		self_status = self.sync_redis.shget(connection_id, self.id)
		if self_status == WebRtcRedisStates.RESPONDED:
			conn_users = self.sync_redis.shgetall(connection_id)
			self.publish_call_answer(
				conn_users,
				connection_id,
				HandlerNames.WEBRTC_TRANSFER,
				Actions.ACCEPT_CALL,
				WebRtcRedisStates.READY,
				{}
			)
		else:
			raise ValidationError("Invalid channel status")

	def send_call_answer(self, in_message, status_set, reply_action, allowed_state, message_handler):
		connection_id = in_message[VarNames.CONNECTION_ID]
		content = in_message[VarNames.CONTENT]
		conn_users = self.sync_redis.shgetall(connection_id)
		if conn_users[self.id] in allowed_state:
			self.publish_call_answer(conn_users, connection_id, message_handler, reply_action, status_set, content)
		else:
			raise ValidationError("Invalid channel status.")

	def publish_call_answer(self, conn_users, connection_id, message_handler, reply_action, status_set, content):
		self.async_redis_publisher.hset(connection_id, self.id, status_set)
		del conn_users[self.id]
		message = self.reply_webrtc(reply_action, connection_id, message_handler, content)
		for user in conn_users:
			if conn_users[user] != WebRtcRedisStates.CLOSED:
				self.publish(message, user)