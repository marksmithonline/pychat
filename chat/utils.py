import base64
import json
import logging
import re
import sys
from io import BytesIO

import requests
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.template import RequestContext
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe

from chat import local
from chat import settings
from chat.models import User, UserProfile, Verification, RoomUsers, IpAddress
from chat.settings import ISSUES_REPORT_LINK, SITE_PROTOCOL, ALL_ROOM_ID

try:  # py2
	from urllib import urlopen
except ImportError:  # py3
	from urllib.request import urlopen

USERNAME_REGEX = str(settings.MAX_USERNAME_LENGTH).join(['^[a-zA-Z-_0-9]{1,', '}$'])
API_URL = getattr(settings, "IP_API_URL", None)
RECAPTCHA_SECRET_KEY = getattr(settings, "RECAPTCHA_SECRET_KEY", None)
GOOGLE_OAUTH_2_CLIENT_ID = getattr(settings, "GOOGLE_OAUTH_2_CLIENT_ID", None)
GOOGLE_OAUTH_2_HOST = getattr(settings, "GOOGLE_OAUTH_2_HOST", None)
FACEBOOK_ACCESS_TOKEN = getattr(settings, "FACEBOOK_ACCESS_TOKEN", None)

logger = logging.getLogger(__name__)


def is_blank(check_str):
	if check_str and check_str.strip():
		return False
	else:
		return True


def hide_fields(post, fields, huge=False, fill_with='****'):
	"""
	:param post: Object that will be copied
	:type post: QueryDict
	:param fields: fields that will be removed
	:param fill_with: replace characters for hidden fields
	:param huge: if true object will be cloned and then fields will be removed
	:return: a shallow copy of dictionary without specified fields
	"""
	if not huge:
		# hide *fields in shallow copy
		res = post.copy()
		for field in fields:
			if field in post:  # check if field was absent
				res[field] = fill_with
	else:
		# copy everything but *fields
		res = {}
		for field in post:
			# _______________________if this is field to remove
			res[field] = post[field] if field not in fields else fill_with
	return res


def check_password(password):
	"""
	Checks if password is secure
	:raises ValidationError exception if password is not valid
	"""
	if is_blank(password):
		raise ValidationError("password can't be empty")
	if not re.match(u'^\S.+\S$', password):
		raise ValidationError("password should be at least 3 symbols")


def check_email(email):
	"""
	:raises ValidationError if specified email is registered or not valid
	"""
	if not email:
		return
	try:
		validate_email(email)
		# theoretically can throw returning 'more than 1' error
		UserProfile.objects.get(email=email)
		raise ValidationError('Email {} is already used'.format(email))
	except User.DoesNotExist:
		pass


def check_user(username):
	"""
	Checks if specified username is free to register
	:type username str
	:raises ValidationError exception if username is not valid
	"""
	# Skip javascript validation, only summary message
	if is_blank(username):
		raise ValidationError("Username can't be empty")
	if not re.match(USERNAME_REGEX, username):
		raise ValidationError("Username {} doesn't match regex {}".format(username, USERNAME_REGEX))
	try:
		# theoretically can throw returning 'more than 1' error
		User.objects.get(username=username)
		raise ValidationError("Username {} is already used. Please select another one".format(username))
	except User.DoesNotExist:
		pass


def get_client_ip(request):
	x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
	return x_forwarded_for.split(',')[-1].strip() if x_forwarded_for else request.META.get('REMOTE_ADDR')


def check_captcha(request):
	"""
	:type request: WSGIRequest
	:raises ValidationError: if captcha is not valid or not set
	If RECAPTCHA_SECRET_KEY is enabled in settings validates request with it
	"""
	if not RECAPTCHA_SECRET_KEY:
		logger.debug('Skipping captcha validation')
		return
	try:
		captcha_rs = request.POST.get('g-recaptcha-response')
		url = "https://www.google.com/recaptcha/api/siteverify"
		params = {
			'secret': RECAPTCHA_SECRET_KEY,
			'response': captcha_rs,
			'remoteip': local.client_ip
		}
		raw_response = requests.post(url, params=params, verify=True)
		response = raw_response.json()
		if not response.get('success', False):
			logger.debug('Captcha is NOT valid, response: %s', raw_response)
			raise ValidationError(
				response['error-codes'] if response.get('error-codes', None) else 'This captcha already used')
		logger.debug('Captcha is valid, response: %s', raw_response)
	except Exception as e:
		raise ValidationError('Unable to check captcha because {}'.format(e))


def send_sign_up_email(user, site_address, request):
	if user.email is not None:
		verification = Verification(user=user, type_enum=Verification.TypeChoices.register)
		verification.save()
		user.email_verification = verification
		user.save(update_fields=['email_verification'])
		link = "{}://{}/confirm_email?token={}".format(SITE_PROTOCOL, site_address, verification.token)
		text = ('Hi {}, you have registered pychat'
				  '\nTo complete your registration please click on the url bellow: {}'
				  '\n\nIf you find any bugs or propositions you can post them {}').format(
			user.username, link, ISSUES_REPORT_LINK)
		start_message = mark_safe((
			"You have registered in <b>Pychat</b>. If you find any bugs or propositions you can post them"
			" <a href='{}'>here</a>. To complete your registration please click on the link below.").format(
				ISSUES_REPORT_LINK))
		context = {
			'username': user.username,
			'magicLink': link,
			'btnText': "CONFIRM SIGN UP",
			'greetings': start_message
		}
		html_message = render_to_string('sign_up_email.html', context, context_instance=RequestContext(request))
		logger.info('Sending verification email to userId %s (email %s)', user.id, user.email)
		send_mail("Confirm chat registration", text, site_address, [user.email, ], html_message=html_message)
		logger.info('Email %s has been sent', user.email)


def send_reset_password_email(request, user_profile, verification):
	link = "{}://{}/restore_password?token={}".format(SITE_PROTOCOL, request.get_host(), verification.token)
	message = "{},\n" \
				 "You requested to change a password on site {}.\n" \
				 "To proceed click on the link {}\n" \
				 "If you didn't request the password change just ignore this mail" \
		.format(user_profile.username, request.get_host(), link)
	ip_info = get_or_create_ip(get_client_ip(request), logger)
	start_message = mark_safe(
		"You have requested to send you a magic link to quickly restore password to <b>Pychat</b>. "
		"If it wasn't you, you can safely ignore this email")
	context = {
		'username': user_profile.username,
		'magicLink': link,
		'ipInfo': ip_info.info,
		'ip': ip_info.ip,
		'btnText': "CHANGE PASSWORD",
		'timeCreated': verification.time,
		'greetings': start_message
	}
	html_message = render_to_string('reset_pass_email.html', context, context_instance=RequestContext(request))
	send_mail("Pychat: restore password", message, request.get_host(), (user_profile.email,), fail_silently=False,
				 html_message=html_message)


def extract_photo(image_base64, filename=None):
	base64_type_data = re.search(r'data:(\w+/(\w+));base64,(.*)$', image_base64)
	logger.debug('Parsing base64 image')
	image_data = base64_type_data.group(3)
	f = BytesIO(base64.b64decode(image_data))
	content_type = base64_type_data.group(1)
	name = filename or ".{}".format(base64_type_data.group(2))
	logger.debug('Base64 filename extension %s, content_type %s', name, content_type)
	image = InMemoryUploadedFile(
		f,
		field_name='photo',
		name=name,
		content_type=content_type,
		size=sys.getsizeof(f),
		charset=None)
	return image


def get_max_key(dictionary):
	return max(dictionary.keys()) if dictionary else None


def create_user_model(user):
	user.save()
	RoomUsers(user_id=user.id, room_id=ALL_ROOM_ID).save()
	logger.info('Signed up new user %s, subscribed for channels with id %d', user, ALL_ROOM_ID)
	return user


def get_or_create_ip(ip, logger):
	"""

	@param ip: ip to fetch info from
	@param logger initialized logger:
	@type IpAddress
	"""
	try:
		ip_address = IpAddress.objects.get(ip=ip)
	except IpAddress.DoesNotExist:
		try:
			if not API_URL:
				raise Exception('api url is absent')
			logger.debug("Creating ip record %s", ip)
			f = urlopen(API_URL % ip)
			raw_response = f.read().decode("utf-8")
			response = json.loads(raw_response)
			if response['status'] != "success":
				raise Exception("Creating iprecord failed, server responded: %s" % raw_response)
			ip_address = IpAddress.objects.create(
				ip=ip,
				isp=response['isp'],
				country=response['country'],
				region=response['regionName'],
				city=response['city'],
				country_code=response['countryCode']
			)
		except Exception as e:
			logger.error("Error while creating ip with country info, because %s", e)
			ip_address = IpAddress.objects.create(ip=ip)
	return ip_address


class EmailOrUsernameModelBackend(object):
	"""
	This is a ModelBacked that allows authentication with either a username or an email address.
	"""

	def authenticate(self, username=None, password=None):
		try:
			if '@' in username:
				user = UserProfile.objects.get(email=username)
			else:
				user = UserProfile.objects.get(username=username)
			if user.check_password(password):
				return user
		except User.DoesNotExist:
			return None

	def get_user(self, username):
		try:
			return get_user_model().objects.get(pk=username)
		except get_user_model().DoesNotExist:
			return None
