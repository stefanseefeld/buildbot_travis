from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from future.utils import string_types
from collections import namedtuple
import re

import yaml
from buildbot.plugins import util
from buildbot.plugins.db import get_plugins


class Invalid(Exception):
    pass


def flatten_env(env):
    flatten_env = {}
    for k, v in env.items():
        if k == "env":
            flatten_env.update(v)
        else:
            flatten_env[k] = v
    return flatten_env


def parse_env_string(env, global_env=None):
    props = {}
    if global_env:
        props.update(global_env)
    if not env.strip():
        return props

    _vars = env.split(" ")
    for v in _vars:
        k, v = v.split("=", 1)
        props[k] = v

    return props


def interpolate_constructor(loader, node):
    value = loader.construct_scalar(node)
    return util.Interpolate(value)


class Loader(yaml.SafeLoader):
    pass

Loader.add_constructor(u'!Interpolate', interpolate_constructor)
Loader.add_constructor(u'!i', interpolate_constructor)


def registerStepClass(name, step):
    def step_constructor(loader, node):
        args = []
        kwargs = {}
        exceptions = []
        try:
            args = [loader.construct_scalar(node)]
        except Exception as e:
            exceptions.append(e)
        try:
            args = loader.construct_sequence(node)
        except Exception as e:
            exceptions.append(e)
        try:
            kwargs = loader.construct_mapping(node)
        except Exception as e:
            exceptions.append(e)

        if len(exceptions) == 3:
            raise Exception("Could not parse steps arguments: {}".format(
                " ".join([str(x) for x in exceptions])))
        return step(*args, **kwargs)

    Loader.add_constructor(u'!' + name, step_constructor)

steps = get_plugins('steps', None, load_now=True)
for step in steps.names:
    registerStepClass(step, steps.get(step))


Stage = namedtuple('Stage', ['name', 'sources', 'packages', 'tasks'])
Task = namedtuple('Task', ['name', 'command'])

class Config(object):
    """
    Loads a yml config file and parses it.
    """

    def __init__(self):
        self.platform = None
        self.language = []
        self.environments = [{}]
        self.global_env = {}
        self.matrix = [{}]
        self.branch_whitelist = None
        self.branch_blacklist = None
        self.config = None
        self.label_mapping = {}

    def tasks(self, variant):
        """Return a list of commands associated with the given variant"""
        for s in self.stages:
            yield from self._make_source_steps(s)
            yield from self._make_package_steps(s)
            yield from self._make_task_steps(s)

    def parse(self, config_input):
        try:
            d = yaml.load(config_input, Loader=Loader)
        except Exception as e:
            raise Invalid("Invalid YAML data\n" + str(e))
        self.config = d
        self._parse_platform()
        self._parse_stages()

    def filter(self, args):
        pass

    def _make_source_steps(self, stage):
        tasks = [Task(f'{stage.name} update', 'sudo apt-get update')]
        if stage.sources:
            tasks = [Task(f'{stage.name} setup repo {s}',
                          f'sudo DEBIAN_FRONTEND=noninteractive add-apt-repository {s}')
                     for s in stage.sources] + tasks
        return tasks

    def _make_package_steps(self, stage):
        tasks = []
        if stage.packages:
            pkgs = ' '.join(stage.packages)
            tasks = [Task(f'{stage.name} install packages',
                          f'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y {pkgs}')]
        return tasks

    def _make_task_steps(self, stage):
        tasks = []
        if stage.tasks:
            tasks = [Task(f'{stage.name} step {i}', f'{t}')
                     for i, t in enumerate(stage.tasks)]
        return tasks

    def _parse_platform(self):
        try:
            self.platform = self.config['platform']
        except:
            raise Invalid("'platform' parameter is missing")

    def _parse_stages(self):
        self.stages = []
        for s in ['base', 'build', 'test', 'package']:
            stage = self.config.get(s, {})
            sources = stage.get('sources', [])
            packages = stage.get('packages', [])
            script = stage.get('script', [])
            self.stages.append(Stage(name=s, sources=sources, packages=packages, tasks=script))

    def _parse_branches(self):
        branches = self.config.get("branches", None)
        if not branches:
            return

        if "only" in branches:
            if not isinstance(branches['only'], list):
                raise Invalid('branches.only should be a list')
            self.branch_whitelist = branches['only']
            return

        if "except" in branches:
            if not isinstance(branches['except'], list):
                raise Invalid('branches.except should be a list')
            self.branch_blacklist = branches['except']
            return

        raise Invalid(
            "'branches' parameter contains neither 'only' nor 'except'")

    def _match_branch(self, branch, lst):
        for b in lst:
            if b.startswith("/") and b.endswith("/"):
                if re.search(b[1:-1], branch):
                    return True
            else:
                if b == branch:
                    return True
        return False

    def can_build_branch(self, branch):
        if self.branch_whitelist is not None:
            if self._match_branch(branch, self.branch_whitelist):
                return True
            return False
        if self.branch_blacklist is not None:
            if self._match_branch(branch, self.branch_blacklist):
                return False
            return True
        return True
