from __future__ import (absolute_import, division,
                        print_function)
import time
import logging
import os
import shutil
import re
import sys
try:
    from watchdog.observers import Observer
    from watchdog.observers.polling import PollingObserver
    from watchdog.events import PatternMatchingEventHandler
    HAS_WATCHDOG = True
except ImportError:
    PatternMatchingEventHandler = object
    HAS_WATCHDOG = False
import warnings
import subprocess as sp
import glob
from threading import Timer, Thread
from queue import Queue
from http.server import HTTPServer, SimpleHTTPRequestHandler, HTTPStatus

from srttools.read_config import read_config
from srttools.scan import product_path_from_file_name
from srttools.imager import main_preprocess
try:
    import matplotlib.pyplot as plt
    plt.switch_backend('Agg')
except ImportError:
    pass


class MyEventHandler(PatternMatchingEventHandler):
    patterns = ["*/*.fits"]

    def __init__(self, nosave=False):
        super().__init__()
        self.nosave = nosave
        self.conf = getattr(sys.modules[__name__], 'conf')
        self.ext = self.conf['debug_file_format']
        create_index_file(self.ext)
        self.filequeue = Queue()
        self.timers = {}
        t = Thread(target=self._dequeue)
        t.daemon = True
        t.start()

    def _enqueue(self, infile):
        if self.timers.get(infile):
            del self.timers[infile]
        if infile not in self.filequeue.queue:
            self.filequeue.put(infile)

    def _dequeue(self):
        while True:
            self._process(self.filequeue.get())

    def _process(self, infile):
        productdir, fname = product_path_from_file_name(
            infile,
            productdir=self.conf['productdir'],
            workdir=self.conf['workdir']
        )
        root = os.path.join(productdir, fname.replace('.fits', ''))

        pp_args = ['--debug', '-c', self.conf['configuration_file_name']]
        if self.nosave:
            pp_args.append('--nosave')
        pp_args.append(infile)
        try:
            main_preprocess(pp_args)
        except:
            return

        newfiles = []
        for debugfile in glob.glob(root + '*.{}'.format(self.ext)):
            newfile = debugfile.replace(root, 'latest')
            newfiles.append(newfile)
            cmd_string = ''
            if self.nosave:
                cmd_string = 'mv {} {}'
            else:
                cmd_string = 'cp {} {}'
            sp.check_call(cmd_string.format(debugfile, newfile).split())
        if self.nosave and self.conf['productdir'] \
                and self.conf['workdir'] not in self.conf['productdir']:
            prodpath = os.path.relpath(root, self.conf['productdir'])
            prodpath = prodpath.split('/')[0]
            prodpath = os.path.join(self.conf['productdir'], prodpath)
            if os.path.exists(prodpath):
                shutil.rmtree(prodpath)

        oldfiles = glob.glob('latest*.{}'.format(self.ext))
        for oldfile in oldfiles:
            if oldfile not in newfiles:
                os.remove(oldfile)

    def on_modified(self, event):
        self._start_timer(event.src_path)

    def on_created(self, event):
        self._start_timer(event.src_path)

    def _start_timer(self, infile):
        if self.timers.get(infile):
            self.timers[infile].cancel()

        self.timers[infile] = Timer(0.05, self._enqueue, args=[infile])
        self.timers[infile].start()


class MyRequestHandler(SimpleHTTPRequestHandler):

    def __init__(self, request, client_address, server):
        self.allowed_paths = ['index.html', 'index.htm']
        conf = getattr(sys.modules[__name__], 'conf')
        self.re_pattern = '^latest_([0-9]*).%s$' % conf['debug_file_format']
        SimpleHTTPRequestHandler.__init__(self, request, client_address, server)

    def do_GET(self):
        path = self.path.lstrip('/')
        if path == '':
            path = 'index.html'
        elif '?' in path:
            path = path.split('?')[0]
        if path not in self.allowed_paths \
                and not re.match(self.re_pattern, path):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        SimpleHTTPRequestHandler.do_GET(self)

    def log_message(self, format, *args):
        return


def main_monitor(args=None):
    import argparse

    description = ('Run the SRT quicklook in a given directory.')
    parser = argparse.ArgumentParser(description=description)

    parser.add_argument(
        "directories",
        help="Directories to monitor",
        default=None,
        nargs='+',
        type=str
    )
    parser.add_argument(
        "-c", "--config",
        help="Config file",
        default=None,
        type=str
    )
    parser.add_argument(
        "--test",
        help="Only to be used in tests!",
        action='store_true',
        default=False
    )
    parser.add_argument(
        "--nosave",
        help="Do not save the hdf5 intermediate files",
        action='store_true',
        default=False
    )
    parser.add_argument(
        "-p", "--polling",
        help="Use a platform-independent, polling watchdog",
        action='store_true',
        default=False
    )
    parser.add_argument(
        "--http-server-port",
        help="Share the results via HTTP server on given HTTP_SERVER_PORT",
        type=int
    )
    args = parser.parse_args(args)

    if not HAS_WATCHDOG:
        raise ImportError('To use SDTmonitor, you need to install watchdog: \n'
                          '\n   > pip install watchdog')
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')

    if args.config is None:
        config_file = create_dummy_config()
    else:
        config_file = args.config
    conf = read_config(config_file)
    conf['configuration_file_name'] = config_file
    setattr(sys.modules[__name__], 'conf', conf)

    event_handler = MyEventHandler(nosave=args.nosave)
    observer = None
    if args.polling:
        observer = PollingObserver()
    else:
        observer = Observer()

    for path in args.directories:
        observer.schedule(event_handler, path, recursive=True)

    observer.start()

    if args.http_server_port:
        http_server = HTTPServer(('', args.http_server_port), MyRequestHandler)
        t = Thread(target=http_server.serve_forever)
        t.daemon = True
        t.start()

    try:
        count = 0
        while count < 10:
            time.sleep(1)
            if args.test:
                count += 1
        raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass

    if args.http_server_port:
        http_server.shutdown()

    observer.stop()


def create_dummy_config():
    config_str = """
    [local]
    [analysis]
    [debugging]
    debug_file_format : jpg
    """
    with open('monitor_config.ini', 'w') as fobj:
        print(config_str, file=fobj)

    return 'monitor_config.ini'


def create_index_file(extension, max_images=50, interval=500):
    """
    :param extension: the file extension of the image files to look for.
    :param max_images: the maximum number of images to monitor. It should be
        enough to set it to twice the number of feeds of the receiver having
        the highest number of feeds (twice because of L and R channels).
        Its default value is set to 50, a number high enough to account for all
        the file images but not too high to represent a computational
        overhead. Since javascript cannot perform any client filesystem
        operation other than loading a local file from its path, the script
        below tries to access every image and just hides the not found ones,
        displaying only the images coming out of the monitor processing phase.
    :param interval: expressed in milliseconds, it represents the time between
        two subsequent calls to the `updatePage` function. Since the images
        get reloaded without any flickering (as opposed to when the whole page
        gets reloaded from the browser), its value can be set to a fraction of
        a second without any visible issue.
    """
    html_string = \
    """<html>
    <script type="text/javascript">
        window.onload = function()
        {
            var extension = '""" + extension + """';
            var maxImages = """ + str(max_images) + """;
			var interval = """ + str(interval) + """;

            document.body.innerHTML = "";

            for(i = 0; i < maxImages; i++)
            {
                document.body.innerHTML += '<div id="div_' + i + '" style="width:50%; float:left;"/></div>';
            }

            function update(index)
            {
                image_id = "img_" + index.toString();

                var image = document.getElementById(image_id);

                if(image == null)
                {
                    image = new Image();
                    image.id = image_id;
                    image.style.width = "100%";

                    image.addEventListener("load", function()
                    {
                        if(this.parentElement == null)
                        {
                            index = parseInt(this.id.split("_")[1]);

                            var div = document.getElementById("div_" + index.toString());

                            while(div.firstChild)
                            {
                                div.removeChild(div.firstChild);
                            }
                            div.appendChild(this);
                        }

                        update(index + 1);
                    });

                    image.addEventListener("error", function()
                    {
                        index = parseInt(this.id.split("_")[1]);

                        for(i = index; i < maxImages; i++)
                        {
                            var div = document.getElementById("div_" + index.toString());
                            div.innerHTML = "";
                        }
                    });
                }

                image.src = "latest_" + index.toString() + "." + extension + "?" + new Date().getTime();
            }

            update(0);
            setInterval(update, interval, 0);
        }
    </script>/n</html>"""

    with open('index.html', 'w') as fobj:
        print(html_string, file=fobj)
