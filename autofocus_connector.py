# --
# File: autofocus/autofocus_connector.py
#
# Copyright (c) Phantom Cyber Corporation, 2016-2018
#
# This unpublished material is proprietary to Phantom Cyber.
# All rights reserved. The methods and
# techniques described herein are considered trade secrets
# and/or confidential. Reproduction or distribution, in whole
# or in part, is forbidden except by express written permission
# of Phantom Cyber.
#
# --

# pylint: disable=W0614,W0212,W0201,W0703,W0401,W0403

# Phantom imports
import phantom.app as phantom
from phantom.base_connector import BaseConnector
from phantom.action_result import ActionResult

# Local imports
from autofocus_consts import *

import simplejson as json
import requests
import os
os.sys.path.insert(0, '{}/pan-python/lib'.format(os.path.dirname(os.path.abspath(__file__))))  # noqa
import pan.afapi  # pylint: disable=E0611,E0401


# There is an error with python and requests. The pan API puts all the data into a dictionary,
#  then just calls 'requests.post(**kwargs)'. This will throw an exception, but explicitly taking out the url param and
#  doing the call like so fixes this
def patch_requests():
    requests_post_old = requests.post
    requests_get_old = requests.get

    # The only thing that should appear in *args (if request.post is being called correctly) is the url value
    def new_requests_post(*args, **kwargs):
        if 'url' in kwargs:
            url = kwargs.pop('url')
            return requests_post_old(url, **kwargs)
        return requests_post_old(*args, **kwargs)


    def new_requests_get(*args, **kwargs):
        if 'url' in kwargs:
            url = kwargs.pop('url')
            return requests_get_old(url, **kwargs)
        return requests_get_old(*args, **kwargs)


    requests.post = new_requests_post
    requests.get = new_requests_get


class AutoFocusConnector(BaseConnector):

    SCOPE_MAP = {'all samples': 'global', 'my samples': 'private', 'public samples': 'public'}
    # MAX_SIZE = 4000
    ACTION_ID_HUNT_FILE = "hunt_file"
    ACTION_ID_HUNT_IP = "hunt_ip"
    ACTION_ID_HUNT_DOMAIN = "hunt_domain"
    ACTION_ID_HUNT_URL = "hunt_url"
    ACTION_ID_GET_REPORT = "get_report"

    def __init__(self):
        super(AutoFocusConnector, self).__init__()
        return

    def initialize(self):
        patch_requests()
        return phantom.APP_SUCCESS

    def _validate_api_call(self, response, action_result):
        """ Validate that an api call was successful """
        try:
            response.raise_for_status()
        except Exception as e:
            return action_result.set_status(phantom.APP_ERROR, str(e))
        return phantom.APP_SUCCESS

    def _init_api(self, action_result):
        api_key = self.get_config()[AF_JSON_API_KEY]
        try:
            self._afapi = pan.afapi.PanAFapi(panrc_tag="autofocus", api_key=api_key)  # pylint: disable=E1101
        except Exception as e:
            return action_result.set_status(phantom.APP_ERROR, str(e))
        return phantom.APP_SUCCESS

    def _construct_body(self, value, field, start, size, scope="global"):
        body = {}
        body['scope'] = scope
        body['from'] = start
        body['size'] = size
        body['sort'] = {"create_date": {"order": "desc"}}
        body['query'] = {'operator': 'all', 'children': [{'field': field, 'operator': 'contains', 'value': value}]}
        return body

    def _samples_search_tag(self, body, action_result):
        """ Do a search specified by query and then create a list of tags """
        body = json.dumps(body)
        # This method calls both the /sample/search and the /sample/result
        #  endpoints, which is pretty nifty
        tag_set = set()
        try:
            # Truthfully I'm not sure what could cause either of these first two loops to iterate more than once
            # But they return lists so it must be possible somehow
            for r in self._afapi.samples_search_results(data=body):
                for i in r.json['hits']:
                    if 'tag' in i['_source']:
                        for tag in i['_source']['tag']:
                            tag_set.add(tag)
        except Exception as e:
            return action_result.set_status(phantom.APP_ERROR, str(e))

        for tag in tag_set:
            r = self._afapi.tag(tagname=tag)
            if not self._validate_api_call(r, action_result):
                # Something wrong is going on if it reaches here
                continue
            tag_data = {}
            tag_data['description'] = r.json['tag']['description']
            tag_data['tag_name'] = r.json['tag']['tag_name']
            tag_data['public_tag_name'] = r.json['tag']['public_tag_name']
            tag_data['count'] = r.json['tag']['count']
            action_result.add_data(tag_data)

        action_result.update_summary({'total_tags_matched': action_result.get_data_size()})

        return phantom.APP_SUCCESS

    def _test_connectivity(self):
        action_result = ActionResult()

        self.save_progress("Starting connectivity test")
        ret_val = self._init_api(action_result)
        if (phantom.is_fail(ret_val)):
            return self.set_status_save_progress(phantom.APP_ERROR, "Connectivity test failed")

        # Now we need to send a command to test if creds are valid
        self.save_progress("Making a request to PAN AutoFocus")
        r = self._afapi.export()
        ret_val = self._validate_api_call(r, action_result)
        if (phantom.is_fail(ret_val)):
            self.save_progress(action_result.get_message())
            self.save_progress("Test Connectivity failed")
            return self.set_status(phantom.APP_ERROR)
        j = r.json['bucket_info']
        self.save_progress("{}/{} daily points remaining".format(j['daily_points_remaining'], j['daily_points']))
        return self.set_status_save_progress(phantom.APP_SUCCESS, "Connectivity test passed")

    def _hunt_action(self, field, value_type, param):
        action_result = self.add_action_result(ActionResult(param))

        ret_val = self._init_api(action_result)
        if (phantom.is_fail(ret_val)):
            return action_result.get_status()

        scope = param.get(AF_JSON_SCOPE, 'All Samples').lower()
        try:
            scope = self.SCOPE_MAP[scope]
        except KeyError:
            # You can also just use "global", "private", or "public" if you want
            if scope in self.SCOPE_MAP.values():
                pass
            return action_result.set_status(phantom.APP_ERROR, AF_ERR_INVALID_SCOPE.format(scope))

        value = param[value_type]
        # start = int(param.get(AF_JSON_FROM, "0"))
        # size = int(param.get(AF_JSON_SIZE, "50"))
        start = 0
        size = 4000
        # This is not wrong. MAX_SIZE isn't the most entries you can retrieve,
        # but it is the largest index that you can retrieve form. from = 3999 and size = 2 would be invalid
        # if (start + size > self.MAX_SIZE):
        #     return action_result.set_status(phantom.APP_ERROR, AF_ERR_TOO_BIG.format(self.MAX_SIZE))
        body = self._construct_body(value, field, start, size, scope=scope)

        self.save_progress("Querying AutoFocus")
        ret_val = self._samples_search_tag(body, action_result)
        if (phantom.is_fail(ret_val)):
            return action_result.get_status()

        return action_result.set_status(phantom.APP_SUCCESS)

    def _hunt_file(self, param):
        return self._hunt_action("alias.hash", AF_JSON_HASH, param)

    def _hunt_ip(self, param):
        return self._hunt_action("alias.ip_address", AF_JSON_IP, param)

    def _hunt_domain(self, param):
        return self._hunt_action("alias.domain", AF_JSON_DOMAIN, param)

    def _hunt_url(self, param):
        return self._hunt_action("alias.url", AF_JSON_URL, param)

    def _get_report(self, param):
        action_result = self.add_action_result(ActionResult(param))

        ret_val = self._init_api(action_result)
        if (phantom.is_fail(ret_val)):
            return action_result.get_status()

        tag = param[AF_JSON_TAG]

        r = self._afapi.tag(tagname=tag)
        ret_val = self._validate_api_call(r, action_result)
        if (phantom.is_fail(ret_val)):
            return action_result.get_status()

        action_result.add_data(r.json)
        return action_result.set_status(phantom.APP_SUCCESS, "Successfully retrieved report info")

    def handle_action(self, param):
        action = self.get_action_identifier()
        ret_val = phantom.APP_SUCCESS

        if (action == phantom.ACTION_ID_TEST_ASSET_CONNECTIVITY):
            ret_val = self._test_connectivity()
        elif (action == self.ACTION_ID_HUNT_FILE):
            ret_val = self._hunt_file(param)
        elif (action == self.ACTION_ID_HUNT_IP):
            ret_val = self._hunt_ip(param)
        elif (action == self.ACTION_ID_HUNT_DOMAIN):
            ret_val = self._hunt_domain(param)
        elif (action == self.ACTION_ID_HUNT_URL):
            ret_val = self._hunt_url(param)
        elif (action == self.ACTION_ID_GET_REPORT):
            ret_val = self._get_report(param)

        return ret_val


if __name__ == '__main__':
    import sys
    import pudb
    pudb.set_trace()
    with open(sys.argv[1]) as f:
        in_json = f.read()
        in_json = json.loads(in_json)
        print(json.dumps(in_json, indent=' ' * 4))
        connector = AutoFocusConnector()
        connector.print_progress_message = True
        r_val = connector._handle_action(json.dumps(in_json), None)
        print r_val
    exit(0)
