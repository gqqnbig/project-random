import datetime
import os
import subprocess
import sys
import statistics
import time

from types import SimpleNamespace
from typing import List, Dict
from urllib.parse import urlparse

import django
import requests
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import HttpResponse
from django.http import JsonResponse
from django.views.decorators.cache import cache_control, cache_page

# import local packages
import GooglePlayAdvancedSearch.Errors
from GooglePlayAdvancedSearch.DBUtils import AppAccessor

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '../../..'))
import GooglePlayAdvancedSearch.DBUtils
import GooglePlayAdvancedSearch.django.web.apiHelper as apiHelper
from GooglePlayAdvancedSearch.Models import AppItem


@cache_control(max_age=3600)
def getAppCount(request):
	with connection.cursor() as cursor:
		count = GooglePlayAdvancedSearch.DBUtils.getAppCountInDatabase(cursor)
		response = HttpResponse(count, content_type="text/plain")

		return response


def getPermissions(request):
	with connection.cursor() as cursor:
		permissions = GooglePlayAdvancedSearch.DBUtils.getAllPermissions(cursor)

		# json doesn't allow integer keys. Remember to handle that in the html.
		response = JsonResponse(permissions, safe=False)
		response['Cache-Control'] = "public, max-age=" + str(len(permissions) * len(permissions))
		return response


def getCategories(request):
	with connection.cursor() as cursor:
		categories = GooglePlayAdvancedSearch.DBUtils.getAllCategories(cursor)
		response = JsonResponse(categories, safe=False)
		response['Cache-Control'] = "public, max-age=" + str(len(categories) * len(categories))
		return response


def logSearch(cursor, keyword, request):
	cursor.execute('insert into Search(keyword, query, ip) values(:keyword, :query,:ip)', {'keyword': keyword, 'query': request.GET.urlencode(), 'ip': getClientIP(request)})


def getClientIP(request) -> str:
	x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
	if x_forwarded_for:
		ip = x_forwarded_for.split(',')[-1].strip()
	else:
		ip = request.META.get('REMOTE_ADDR')
	return ip


def limitRate(ip):
	"""
	No more than 3 times every 30 seconds.
	"""
	d = cache.get('RateControl-' + ip)
	if d:
		# No more than 3 times every 30 seconds.
		if d.count >= 3 and d.count / (datetime.datetime.now() - d.time).total_seconds() > 0.1:
			return True
		else:
			d.count += 1
			cache.set('RateControl-' + ip, d, timeout=60)
	else:
		d = SimpleNamespace()
		d.count = 1
		d.time = datetime.datetime.now()
		cache.set('RateControl-' + ip, d, timeout=60)
	return False


@cache_page(60 * 5)
def search(request: django.http.HttpRequest):
	# If the user loads Google Analysis, let Nginx handle rating limit.
	if not request.COOKIES.get('_gaload') and limitRate(getClientIP(request)):
		return JsonResponse({'error': 'Rate limit reached. Wait 60 seconds.'})

	keyword = request.GET.get('q', '').strip()
	with connection.cursor() as cursor:
		try:
			logSearch(cursor, keyword, request)
		except django.db.utils.OperationalError:
			cursor.execute(apiHelper.getSqlCreateTableSearch())
			try:
				logSearch(cursor, keyword, request)
			except Exception as e:
				print(str(e))

	startTime = time.time()
	excludedPIds = [int(n) for n in request.GET.get('pids', '').split(',') if n != '']

	excludedCIds = [int(n) for n in request.GET.get('cids', '').split(',') if n != '']

	try:
		appAccessor = AppAccessor()
		appInfos = appAccessor.searchApps(keyword)

		needCompleteInfo = determineAppInfoCompleteness(request)

		if needCompleteInfo:
			appInfos = getCompleteAppInfo([a['id'] for a in appInfos])

		appInfos = filterApps(appInfos, excludedCIds, excludedPIds, request)

		# If we cannot find 200 matches from our database, we try to find more matches from Google.
		if len(appInfos) < 200 and cache.get('searchkey-' + keyword) is None:
			cache.set('searchkey-' + keyword, '', timeout=60 * 5)  # do not search the same keyword in 5 minutes
			appInfos2 = apiHelper.searchGooglePlay(keyword)
			if needCompleteInfo:
				appInfos2 = getCompleteAppInfo([a['id'] for a in appInfos2])

			appInfos2 = filterApps(appInfos2, excludedCIds, excludedPIds, request)

			appInfoIds = {a['id'] for a in appInfos}
			appInfos.extend([a for a in appInfos2 if a['id'] not in appInfoIds])

		sortType = request.GET.get('sort')
		if sortType == 'rlh':  # rating low to high
			appInfos = sorted(appInfos, key=lambda a: a['rating'])
		elif sortType == 'rhl':  # rating high to low
			appInfos = sorted(appInfos, key=lambda a: a['rating'], reverse=True)
		elif sortType == 'plh':  # number of permissions low to high
			appInfos = sorted(appInfos, key=lambda a: len(a['permissions']))
		elif sortType == 'phl':  # number of permissions low to high
			appInfos = sorted(appInfos, key=lambda a: len(a['permissions']), reverse=True)

		with connection.cursor() as cursor:
			d = int((time.time() - startTime) * 1000)
			GooglePlayAdvancedSearch.DBUtils.executeAndCreateTable(cursor, apiHelper.getSqlCreateTableSearchTiming, 'insert into SearchTiming values (:d)', {'d': d})

		response = JsonResponse({'apps': [dict(a) for a in appInfos]}, safe=False)
		response['Cache-Control'] = "public, max-age=3600"
		return response
	except requests.exceptions.SSLError as e:
		# In getCompleteAppInfo, we throw our own SSLError where we don't have request object.
		if e.request:
			url = urlparse(e.request.url)
			return JsonResponse({'error': f'Searching is aborted because secure connection to https://{url.netloc} is compromised.\nAttacker is attacking us, but we didn\'t leak your data!'})
		else:
			return JsonResponse({'error': f'Searching is aborted because secure connection is compromised.\nAttacker is attacking us, but we didn\'t leak your data!'})


def filterApps(appInfos: List[AppItem], excludedCategoryIds, excludedPermissionIds, request):
	if len(excludedPermissionIds):
		appInfos = [a for a in appInfos if isExcluded(a['permissions'], excludedPermissionIds) == False]
	if len(excludedCategoryIds):
		appInfos = [a for a in appInfos if isExcluded(a['categories'], excludedCategoryIds) == False]
	if request.GET.get('free') == 'true':
		appInfos = [a for a in appInfos if a['install_fee'] == 0]
	if request.GET.get('ap') == 'false':
		appInfos = [a for a in appInfos if a['inAppPurchases'] == 0]
	if request.GET.get('ad') == 'false':
		appInfos = [a for a in appInfos if a['containsAds'] == 0]
	return appInfos


def determineAppInfoCompleteness(request):
	"""
	If users filter by permissions or sort by the number of permission, we must return the complete app information.
	"""
	if request.GET.get('pids'):
		return True
	if request.GET.get('cids'):
		return True
	if request.GET.get('sort') == 'phl' or request.GET.get('sort') == 'plh':
		return True
	if request.GET.get('ap') == 'false':
		return True
	if request.GET.get('ad') == 'false':
		return True

	with connection.cursor() as cursor:
		permissions = GooglePlayAdvancedSearch.DBUtils.getAllPermissions(cursor)
		if len(permissions) == 0:
			return True
	return False


def isExcluded(d: Dict, ids: List[int]):
	return any(excludedId in d for excludedId in ids)


def getCompleteAppInfo(app_ids: List[str]) -> List[AppItem]:
	"""
	search the app_ids in database or retrieve from Google Play.

	run scraper against the apps that are not in our database
	if scraper failed for some app_id, their corresponding app_info will have only app_id

	:param app_ids: list of app_id
	:return:    list of dictionary(set). each set represents the app_info of the corresponding app_id,
                the return list have the same length as the input list
	"""

	if len(app_ids) == 0:
		return []

	# https://stackoverflow.com/a/39537308/746461
	# Python 3.6 and up keeps dict insertion order.
	# Python 3.7 formalizes it to a language specification.
	if sys.version_info < (3, 6):
		# sys.version_info is a named tuple. https://docs.python.org/3/glossary.html#term-named-tuple
		print(f'Python {tuple(sys.version_info)} may not keep dictionary insertion order. Upgrade to at least version 3.6.', file=sys.stderr)

	appAccessor = AppAccessor()
	with connection.cursor() as cursor:
		# very important!
		# connection.cursor() gives django cursor, connection.cursor().connection.cursor() gives underlying sqlite cursor.
		# django cursor and sqlite cursor uses different parameter style!
		if GooglePlayAdvancedSearch.DBUtils.getAppCountInDatabase(cursor) > 0:
			# search database first pass. If the app isn't in database, leave it none. We will fill in the second pass.
			app_infos = {id: appAccessor.getCompleteAppInfo(id) for id in app_ids}
		else:
			app_infos = {id: None for id in app_ids}

	# search database second pass
	# for first-pass non-found apps, pass into scraper
	appsMissingInDatabase = [k for k, v in app_infos.items() if v is None]
	if len(appsMissingInDatabase) > 0:
		code2 = os.system(('python ' if sys.platform == 'win32' else '') + "../scraper/Program.py -p %s" % ",".join(appsMissingInDatabase))
		if hasattr(os, 'WEXITSTATUS') and os.WEXITSTATUS(code2) == GooglePlayAdvancedSearch.Errors.sslErrorCode \
				or not hasattr(os, 'WEXITSTATUS') and code2 == GooglePlayAdvancedSearch.Errors.sslErrorCode:
			raise requests.exceptions.SSLError()

		appAccessor = AppAccessor()
		scraper_fail_id = []
		for id in appsMissingInDatabase:
			tmp = appAccessor.getCompleteAppInfo(id)
			if tmp:
				app_infos[id] = tmp
			else:
				assert id in app_infos
				app_infos[id] = {'id': id}  # if scraper fails, just pass "id" to appDetails to display
				scraper_fail_id.append(id)

		print("Scraper failed %d times: %s" % (len(scraper_fail_id), ", ".join(scraper_fail_id)))
		print("There were %d ids not in our database or stale. %d are now added" % (len(appsMissingInDatabase), len(appsMissingInDatabase) - len(scraper_fail_id)))

	print(f'total results: {len(app_ids)}')
	assert None not in app_infos.values(), "Every app id returned from Google should have an app detail."
	return list(app_infos.values())


@cache_page(60 * 60)
def version(request):
	try:
		branch = subprocess.check_output(['git', 'describe', '--dirty'], cwd=os.path.dirname(os.path.abspath(__file__)))
		branch = branch.decode("utf-8").strip()
	except:
		branch = None

	response = HttpResponse(branch)
	response['Cache-Control'] = "public, max-age=3600"
	return response


def buildRecentSearchResult(item, showIp):
	d = {'keyword': item[0], 'query': item[1], 'date': item[2]}
	if showIp:
		d['ip'] = item[3]
	return d


def recentSearches(request: django.http.HttpRequest):
	ip = getClientIP(request)

	showIp = False
	if any((k for k in request.GET.keys() if k.lower() == "ShowIP".lower())):
		if ip == "127.0.0.1" or (hasattr(settings, 'ADMIN_IP') and settings.ADMIN_IP is not None and ip.startswith(settings.ADMIN_IP)):
			showIp = True

	isSensitive = showIp

	if showIp:
		json = None
	else:
		# If we don't need to show IP, we can try to retrieve it from cache.
		json = cache.get('recentSearches')

	try:
		if not json:
			with connection.cursor() as cursor:
				cursor.execute('select keyword, query, date, ip from Search order by date desc limit 10')
				data = cursor.fetchall()
				json = [buildRecentSearchResult(item, showIp) for item in data]
	except django.db.utils.OperationalError as e:
		print(str(e))

	# In case json is None, change it to empty list. In this way, cache knows there is an object, and we don't have handle situation of None.
	if json is None:
		json = []

	if isSensitive == False:
		cache.set('recentSearches', json, 60)  # 60 seconds

	response = JsonResponse(json, safe=False, json_dumps_params=({'indent': '\t'} if isSensitive else None))

	if isSensitive:
		response['Cache-Control'] = "no-store"
	return response


def searchTiming(request: django.http.HttpRequest):
	with connection.cursor() as cursor:
		try:
			cursor.execute('select durationInMS from SearchTiming')
			data = cursor.fetchall()
			data = [d[0] for d in data]
			if len(data) > 1:
				mean = statistics.mean(data)
				std = statistics.stdev(data)  # stddev requires at least 2 samples.
			else:
				mean = 30000
				std = 0
		except:
			cursor.execute(apiHelper.getSqlCreateTableSearchTiming())
			mean = 30000
			std = 0

	return JsonResponse({'mean': mean, 'std': std})
