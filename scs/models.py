from __future__ import absolute_import, with_statement

import errno
import logging
import os

from threading import Lock

from anyjson import deserialize
from celery import current_app as celery
from celery import platforms
from celery.bin.celeryd_multi import MultiTool
from kombu.utils import cached_property

from django.db import models
from django.utils.translation import ugettext_lazy as _

from scs.managers import BrokerManager, NodeManager, QueueManager
from scs.utils import shellquote

CWD = "/var/run/scs"

logger = logging.getLogger("Node")


class Broker(models.Model):
    objects = BrokerManager()

    hostname = models.CharField(_(u"hostname"), max_length=128)
    port = models.IntegerField(_(u"port"))
    userid = models.CharField(_(u"userid"), max_length=128)
    password = models.CharField(_(u"password"), max_length=128)
    virtual_host = models.CharField(_(u"virtual host"), max_length=128)

    _pool = None

    class Meta:
        verbose_name = _(u"broker")
        verbose_name_plural = _(u"brokers")
        unique_together = ("hostname", "port", "virtual_host")

    def __unicode__(self):
        return unicode(self.connection().as_uri())

    def connection(self):
        return celery.broker_connection(hostname=self.hostname,
                                        userid=self.userid,
                                        password=self.password,
                                        virtual_host=self.virtual_host,
                                        port=self.port)
    @property
    def pool(self):
        if self._pool is None:
            self._pool = self.connection().Pool(celery.conf.BROKER_POOL_LIMIT)
        return self._pool


class Queue(models.Model):
    objects = QueueManager()

    name = models.CharField(_(u"name"), max_length=128, unique=True)
    exchange = models.CharField(_(u"exchange"), max_length=128,
                                default=None, null=True, blank=True)
    exchange_type = models.CharField(_(u"exchange type"), max_length=128,
                                     default=None, null=True, blank=True)
    routing_key = models.CharField(_(u"routing key"), max_length=128,
                                   default=None, null=True, blank=True)
    is_enabled = models.BooleanField(_(u"is enabled"), default=True)
    created_at = models.DateTimeField(_(u"created at"), auto_now_add=True)

    #: Additional JSON encoded queue options.
    options = models.TextField(null=True, blank=True)

    class Meta:
        verbose_name = _(u"queue")
        verbose_name_plural = _(u"queues")

    def __unicode__(self):
        return self.name


class Node(models.Model):
    Broker = Broker
    Queue = Queue
    MultiTool = MultiTool

    objects = NodeManager()
    mutex = Lock()
    cwd = CWD

    name = models.CharField(_(u"name"), max_length=128, unique=True)
    queues = models.ManyToManyField(Queue, null=True, blank=True)
    max_concurrency = models.IntegerField(_(u"max concurrency"), default=1)
    min_concurrency = models.IntegerField(_(u"min concurrency"), default=1)
    is_enabled = models.BooleanField(_(u"is enabled"), default=True)
    created_at = models.DateTimeField(_(u"created at"), auto_now_add=True)
    _broker = models.ForeignKey(Broker, null=True, blank=True)

    class Meta:
        verbose_name = _(u"node")
        verbose_name_plural = _(u"nodes")

    def __unicode__(self):
        return self.name

    def enable(self):
        self.is_enabled = True
        self.save()

    def disable(self):
        self.is_enabled = False
        self.save()

    def start(self, **kwargs):
        """Starts the instance."""
        return self._action("start", **kwargs)

    def stop(self, **kwargs):
        """Shuts down the instance."""
        return self._action("stop", **kwargs)

    def restart(self, **kwargs):
        """Restarts the instance."""
        return self._action("restart", **kwargs)

    def alive(self, **kwargs):
        """Returns :const:`True` if the pid responds to signals,
        and the instance responds to ping broadcasts."""
        return self.responds_to_signal() and self.responds_to_ping(**kwargs)

    def stats(self, **kwargs):
        """Returns node statistics (as ``celeryctl inspect stats``)."""
        return self._query("stats", **kwargs)

    def autoscale(self, max=None, min=None, **kwargs):
        """Set min/max autoscale settings."""
        if max is not None:
            self.max_concurrency = max
        if min is not None:
            self.min_concurrency = min
        self.save()
        return self._query("autoscale", dict(max=max, min=min), **kwargs)

    def responds_to_ping(self, **kwargs):
        """Returns :const:`True` if the instance responds to
        broadcast ping."""
        return True if self._query("ping", **kwargs) else False

    def responds_to_signal(self):
        """Returns :const:`True` if the pidfile exists and the pid
        responds to signals."""
        pid = self.getpid()
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except OSError, exc:
            if exc.errno == errno.ESRCH:
                return False
            raise
        return True

    def consuming_from(self, **kwargs):
        """Return the queues the instance is currently consuming from."""
        queues = self._query("active_queues", **kwargs)
        if queues:
            return dict((q["name"], q) for q in queues)
        return {}

    def add_queue(self, q, **kwargs):
        """Add queue for this instance by name."""
        if not isinstance(q, self.Queue):
            q = self.Queue.objects.get(name=q)
        options = deserialize(q.options) if q.options else {}
        exchange = q.exchange if q.exchange else q.name
        routing_key = q.routing_key if q.routing_key else q.name
        return self._query("add_consumer", dict(queue=q.name,
                                                exchange=exchange,
                                                exchange_type=q.exchange_type,
                                                routing_key=routing_key,
                                                **options), **kwargs)

    def cancel_queue(self, queue, **kwargs):
        """Cancel queue for this instance by :class:`Queue`."""
        if not isinstance(queue, self.Queue):
            queue = self.Queue.objects.get(name=queue)
        return self._query("cancel_consumer", dict(queue=queue.name), **kwargs)

    def getpid(self):
        """Get the process id for this instance by reading the pidfile.

        Returns :const:`None` if the pidfile does not exist.

        """
        pidfile = self.pidfile.replace("%n", self.name)
        return platforms.PIDFile(pidfile).read_pid()

    @property
    def extra_config(self):
        return ["--",
                "broker.host=%s"     % (self.broker.hostname, ),
                "broker.port=%s"     % (self.broker.port, ),
                "broker.user=%s"     % (self.broker.userid, ),
                "broker.password=%s" % (self.broker.password, ),
                "broker.vhost=%s"    % (self.broker.virtual_host, )]

    def _action(self, action, multi="celeryd-multi"):
        """Execute :program:`celeryd-multi` command."""
        with self.mutex:
            argv = ([multi, action, '--suffix=""', "--no-color", self.name]
                  + list(self.argv)
                  + list(self.default_args)
                  + self.extra_config)
            logger.info(" ".join(argv))
            return self.multi.execute_from_commandline(argv)

    def _query(self, cmd, args={}, **kwargs):
        """Send remote control command and wait for this instances reply."""
        name = self.name
        conn = None
        if "connection" not in kwargs:
            conn = kwargs["connection"] = self.broker.pool.acquire(block=True)
        try:
            r = celery.control.broadcast(cmd, arguments=args,
                       destination=[name], reply=True, **kwargs)
        finally:
            if conn:
                conn.release()
        if r:
            for reply in r:
                if name in reply:
                    return reply[name]

    @property
    def argv(self):
        acc = []
        for k, v in self.argtuple:
            if v:
                acc.append(k)
                acc.append(shellquote(str(v)))
        return acc

    @property
    def argtuple(self):
        return (("-Q", self.direct_queue, ), )

    @cached_property
    def multi(self):
        env = os.environ.copy()
        env.pop("CELERY_LOADER", None)
        return self.MultiTool(env=env)

    @property
    def direct_queue(self):
        return "dq.%s" % (self.name, )

    @property
    def pidfile(self):
        return os.path.join(self.cwd, "celeryd@%n.pid")

    @property
    def logfile(self):
        return os.path.join(self.cwd, "celeryd@%n.log")

    @property
    def default_args(self):
        return ("--workdir=%s" % (self.cwd, ),
                "--pidfile=%s" % (self.pidfile, ),
                "--logfile=%s" % (self.logfile, ),
                "--loglevel=DEBUG",
                "--autoscale=%s,%s" % (self.max_concurrency,
                                       self.min_concurrency))

    @property
    def broker(self):
        if self._broker is None:
            return self.Broker._default_manager.get_default()
        return self._broker
