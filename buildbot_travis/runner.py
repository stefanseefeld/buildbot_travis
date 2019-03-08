from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from builtins import range

import argparse
import hashlib
import math
import os
import re
import readline
from subprocess import PIPE, STDOUT, Popen
from threading import Lock

import urwid
from twisted.internet import reactor
from twisted.internet.threads import deferToThread

from buildbot_travis.steps.create_steps import SetupVirtualEnv
from buildbot_travis.config import Config, flatten_env
from buildbot_travis.travisyml import TravisConfig
# Fix Python 2.x.
try: input = raw_input
except NameError: pass

[readline]  # is imported for side effect (i.e get decent raw_input)


def loadConfig():
    for filename in ['meta.yml', ".bbtravis.yml", ".travis.yml"]:
        if os.path.exists(filename):
            config = Config() if filename == 'meta.yml' else TravisConfig()
            with open(filename) as f:
                config.parse(f.read())
            return config


class Runner(object):
    def __init__(self, args, ui, window):
        self.ui = ui
        self.window = window
        self.pwd = os.getcwd()

    def runAndSendOutput(self, cmd):
        if reactor._stopped:
            return 1, ""
        popen = Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=STDOUT)
        all_text = ""
        for stdout_line in iter(lambda: popen.stdout.readline().decode('utf-8'), ""):
            all_text += stdout_line
            if reactor._stopped:
                popen.terminate()
            self.ui.addTextForWindow(self.window, stdout_line)
        popen.stdout.close()
        return_code = popen.wait()
        return return_code, all_text

    def run(self, shellscript):
        cmd = ["bash", "-c", shellscript]
        return self.runAndSendOutput(cmd)

    def close(self):
        pass


class DockerRunner(Runner):
    def __init__(self, args, ui, window):
        Runner.__init__(self, args, ui, window)
        self.pwd = args.docker_pwd
        cwd = os.getcwd()
        volume = cwd + ":" + args.docker_pwd
        image = args.docker_image
        cmd = ['docker', 'run', '--network', 'host', '-d', '-v', volume, '-w', args.docker_pwd]
        for env in ['http_proxy', 'https_proxy', 'no_proxy']:
            if env in os.environ:
                cmd.extend(['-e', env + '=' + os.environ[env]])
        cmd.extend([image, "sleep", "200000"])
        rv, self.containerid = self.runAndSendOutput(cmd)
        self.containerid = self.containerid.strip()
        self.ui.addTextForWindow(
            self.window,
            "started container " + image + " " + self.containerid[:10] + "\n")

    def run(self, shellscript):
        cmd = ['docker', 'exec', '-t', self.containerid, "bash", "-c",
               shellscript]
        return self.runAndSendOutput(cmd)

    def close(self):
        self.runAndSendOutput(['docker', 'rm', '-f', self.containerid])


class MyTerminal(urwid.Terminal):
    """This is a hack class to use urwid Terminal class without actually spawning process
    """

    def __init__(self):
        urwid.Terminal.__init__(self, None)
        self.original_top = None

    def spawn(self):
        self.pid = 'foo'

    def feed(self):
        pass

    def set_termsize(self, h, w):
        pass

    def add_text(self, data):
        self.term.modes.lfnl = True
        self.term.addstr(data.encode("utf8"))

    def keypress(self, size, key):
        if key == 'esc':
            self.add_text("\nstopping!\n" + str(reactor._stopped) + "\n")
            if not reactor._stopped:
                reactor.callFromThread(reactor.stop)

    def mouse_event(self, size, event, button, col, row, focus):
        if button == 1:
            if self.original_top:
                self.loop.widget = self.original_top
                self.original_top = None
            else:
                self.original_top = self.loop.widget
                self.loop.widget = self
        if button == 4:
            self.term.scroll_buffer(up=True)
        if button == 5:
            self.term.scroll_buffer(up=False)


class Ui(object):
    """urwid UI which splits the screen into so many screens, and display each parallel job in that screen"""

    def __init__(self, maxwindow):
        self.maxwindow = maxwindow
        self.windows = []
        self.widgets = []
        numcolumns = min(maxwindow, 2)
        columns = [[] for i in range(numcolumns)]
        for i in range(maxwindow):
            window = MyTerminal()
            self.windows.append(window)
            widget = urwid.Frame(urwid.LineBox(window))
            self.widgets.append(widget)
            columns[i % len(columns)].append(widget)
        columns = [urwid.Pile(x) for x in columns]
        self.top = urwid.Columns(columns)
        evl = urwid.TwistedEventLoop(manage_reactor=True)
        self.loop = urwid.MainLoop(self.top, event_loop=evl)
        # now that the loop is there, we inform the terminals
        for window in self.windows:
            window.loop = self.loop
        self.lock = Lock()
        self.curwindow = 0
        self.redrawing = False

    def registerWindow(self, title):
        with self.lock:
            n = self.curwindow
            self.curwindow += 1
            self.widgets[n].contents['header'] = (urwid.Text(title), None)
        self.redraw()
        return n

    def addTextForWindow(self, n, text):
        with self.lock:
            output_widget = self.windows[n]
            output_widget.add_text(text)
        self.redraw()

    def redraw(self):
        # redraw with 100ms debounce
        with self.lock:
            if not self.redrawing:
                self.redrawing = True
                reactor.callLater(0.1, self._redraw)

    def _redraw(self):
        self.loop.draw_screen()
        self.redrawing = False


def run(args):
    config = loadConfig()
    config.filter(args)
    if not config.matrix:
        print("nothing in matrix (everything filtered?)")
        return
    all_configs = ""
    for env in config.matrix:
        all_configs += " ".join(
            ["%s=%s" % (k, v) for k, v in flatten_env(env).items()]) + "\n"
    print("will run:\n" + all_configs)
    print(
        "Once running: Hit 'esc' to quit. Use mouse scroll wheel to scroll buffer. Use mouse click to zoom/unzoom")
    res = input("OK? [Y/n]")
    if res.lower()[:1] == "n":
        return
    ui = Ui(len(config.matrix))

    def runOneEnv(env):
        results = 0
        script = "set -v; set -e\n"
        final_env = {"TRAVIS_PULL_REQUEST": 1}
        final_env.update(flatten_env(env))
        matrix = " ".join(["%s=%s" % (k, v) for k, v in final_env.items()])
        window = ui.registerWindow(matrix)

        def print_to_window(*args):
            text = " ".join([str(a) for a in args])
            ui.addTextForWindow(window, text + "\n")

        if not args.dryrun:
            if config.platform and not args.docker_image:
                args.docker_image = config.platform
            if args.docker_image:
                runner = DockerRunner(args, ui, window)
            else:
                runner = Runner(args, ui, window)
        envtitle = []

        for k, v in final_env.items():
            script += "export %s='%s'\n" % (k, v)
            envtitle.append("%s='%s'" % (k, v))

        if 'python' in config.language and not args.docker_image:
            ve = SetupVirtualEnv(final_env['python'])
            ve.sandboxname = "sandbox" + hashlib.sha1(matrix).hexdigest()
            vecmd = ve.buildCommand()
            if not args.dryrun:
                rc, out = runner.run(vecmd)
                _, path = runner.run("echo -n $PATH")
                script += 'export PATH="{}/{}/bin:{}"'.format(
                    runner.pwd, ve.sandboxname, path)

        print_to_window("running matrix", matrix)
        print_to_window("========================")
        for t in config.tasks(final_env):
            print_to_window('title:', f'{t.name}')
            print_to_window(f'{t.command}')
            if not args.dryrun:
                rc, out = runner.run(script + "\n" + t.command)
                print_to_window("results:", rc)
                if rc:
                    results = rc
        if not args.dryrun:
            runner.close()
        print_to_window("DONE! results:", results)
        return results

    reactor.suggestThreadPoolSize(args.num_threads)

    def start():
        # make sure the screen has drawn once
        ui.loop.draw_screen()
        for env in config.matrix:
            deferToThread(runOneEnv, env)

    reactor.callWhenRunning(start)
    ui.loop.run()
