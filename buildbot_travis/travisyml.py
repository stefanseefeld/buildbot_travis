# Copyright 2012-2013 Isotoma Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from future.utils import string_types

import re

import yaml
from buildbot.plugins import util
from buildbot.plugins.db import get_plugins

from .config import Config, Stage, Invalid, Loader
from .config import flatten_env, parse_env_string

class TravisConfig(Config):

    def __init__(self):
        super(TravisConfig, self).__init__()
        self.email = TravisYmlEmail()
        self.irc = TravisYmlIrc()

    def parse(self, config_input):
        try:
            d = yaml.load(config_input, Loader=Loader)
        except Exception as e:
            raise Invalid("Invalid YAML data\n" + str(e))
        self.config = d
        self._parse_language()
        self._parse_label_mapping()
        self._parse_envs()
        self._parse_matrix()
        self._parse_stages()
        self._parse_branches()
        self._parse_notifications_email()
        self._parse_notifications_irc()

    def filter(self, args):
        if not args.filters:
            return
        new_matrix = []
        for env in self.matrix:
            final_env = flatten_env(env)
            for f in args.filters:
                k, op, v = f
                res = False
                if k in final_env:
                    if op == '==' or op == '=':
                        res = str(final_env[k]) == v
                    if op == '!=':
                        res = str(final_env[k]) != v
                if not res:
                    break
            if res:
                new_matrix.append(env)
        self.matrix = new_matrix

    def _parse_language(self):
        try:
            self.language = self.config['language']
        except:
            raise Invalid("'language' parameter is missing")

    def _parse_label_mapping(self):
        self.label_mapping = self.config.get('label_mapping', {})

    def _parse_envs(self):
        env = self.config.get("env", None)
        self.global_env = {}
        if env is None:
            return
        elif isinstance(env, string_types):
            self.environments = [parse_env_string(env)]
        elif isinstance(env, list):
            self.environments = [parse_env_string(e) for e in env]
        elif isinstance(env, dict):
            global_env_strings = env.get('global', [])
            if isinstance(global_env_strings, string_types):
                global_env_strings = [global_env_strings]
            for e in global_env_strings:
                self.global_env.update(parse_env_string(e))
            self.environments = [
                parse_env_string(e, self.global_env)
                for e in env.get('matrix', [''])
            ]
        else:
            raise Invalid("'env' parameter is invalid")

    def _parse_stages(self):
        self.stages = []
        for s in ("before_install", "install", "after_install", "before_script",
                  "script", "after_script"):
            commands = self.config.get(s, [])
            if isinstance(commands, string_types):
                commands = [commands]
            if not isinstance(commands, list):
                raise Invalid("'%s' parameter is invalid" % s)
            self.stages.append(Stage(name=s, sources=[], packages=[], tasks=commands))

    def _parse_matrix(self):
        matrix = []
        python = self.config.get("python", ["python2.6"])
        if not isinstance(python, list):
            python = [python]
        # First of all, build the implicit matrix
        for lang in python:
            for env in self.environments:
                matrix.append(dict(
                    python=lang,
                    env=env, ))

        cfg = self.config.get("matrix", {})

        def env_to_set(env):
            env = env.copy()
            env.update(env.get('env', {}))
            if 'env' in env:
                del env['env']
            return set("{}={}".format(k, v) for k, v in env.items())

        for env in cfg.get("exclude") or []:
            matchee = env.copy()
            matchee['env'] = parse_env_string(matchee.get('env', ''))
            matchee_set = env_to_set(matchee)
            for matrix_line in matrix:
                matrix_line_set = env_to_set(matrix_line)
                if matrix_line_set.issuperset(matchee_set):
                    matrix.remove(matrix_line)

        for env in cfg.get("include") or []:
            e = env.copy()
            e['env'] = parse_env_string(e.get('env', ''), self.global_env)
            matrix.append(e)

        self.matrix = matrix

    def _parse_notifications_irc(self):
        notifications = self.config.get("notifications", {})
        self.irc.parse(notifications.get("irc", {}))

    def _parse_notifications_email(self):
        notifications = self.config.get("notifications", {})
        self.email.parse(notifications.get("email", {}))


class _NotificationsMixin(object):
    success = 'never'
    failure = 'never'

    def parse_failure_success(self, settings):
        self.success = settings.get("on_success", self.success)
        if self.success not in ("always", "never", "change"):
            raise TravisYmlInvalid("Invalid value '%s' for on_success" %
                                   self.success)

        self.failure = settings.get("on_failure", self.failure)
        if self.failure not in ("always", "never", "change"):
            raise TravisYmlInvalid("Invalid value '%s' for on_failure" %
                                   self.failure)


class TravisYmlEmail(_NotificationsMixin):
    def __init__(self):
        self.enabled = True
        self.addresses = []
        self.success = "change"
        self.failure = "always"

    def parse(self, settings):
        if not settings:
            self.enabled = False
            return

        if isinstance(settings, list):
            self.addresses = settings
            return

        if not isinstance(settings, dict):
            raise TravisYmlInvalid(
                "Exepected a False, a list of addresses or a dictionary at noficiations.email")

        self.addresses = settings.get("recipients", self.addresses)

        self.parse_failure_success(settings)


class TravisYmlIrc(_NotificationsMixin):
    def __init__(self):
        self.enabled = False
        self.channels = []
        self.template = []
        self.success = "change"
        self.failure = "always"
        self.notice = False
        self.join = True

    def parse(self, settings):
        if not settings:
            return

        self.enabled = True
        self.channels = settings.get("channels", [])
        self.template = settings.get("template", [])
        self.notice = settings.get("use_notice", False)
        self.join = not settings.get("skip_join", False)

        self.parse_failure_success(settings)
