"""scs.managers

- These are the managers for our models in :mod:`models`.

- They are not to be used directly, but accessed through
  the ``objects`` attribute of a Model.


"""

from __future__ import absolute_import

from anyjson import serialize
from djcelery.managers import ExtendedManager

from ..utils import cached_property, uuid


class BrokerManager(ExtendedManager):
    default_url = "amqp://guest:guest@localhost:5672//"

    def get_default(self):
        return self.get_or_create(url=self.default_url)[0]


class AppManager(ExtendedManager):

    def from_json(self, name=None, broker=None):
        return {"name": name, "broker": self.get_broker(broker)}

    def recreate(self, name=None, broker=None):
        d = self.from_json(name, broker)
        return self.get_or_create(name=d["name"],
                                  defaults={"broker": d["broker"]})[0]

    def instance(self, name=None, broker=None):
        return self.model(**self.from_json(name, broker))

    def get_broker(self, url):
        return self.Brokers.get_or_create(url=url)[0]

    def add(self, name=None, broker=None):
        broker = self.get_broker(broker) if broker else None
        return self.get_or_create(name=name, defaults={"broker": broker})[0]

    def get_default(self):
        return self.get_or_create(name="scs")[0]

    @cached_property
    def Brokers(self):
        return self.model.Broker._default_manager


class NodeManager(ExtendedManager):

    def enabled(self):
        return self.filter(is_enabled=True)

    def _maybe_queues(self, queues):
        if isinstance(queues, basestring):
            queues = queues.split(",")
        return [(queue.name if isinstance(queue, self.model.Queue) else queue)
                    for queue in queues]

    def add(self, nodename=None, queues=None, max_concurrency=1,
            min_concurrency=1, broker=None, pool=None, app=None):
        node = self.create(name=nodename or uuid(),
                           max_concurrency=max_concurrency,
                           min_concurrency=min_concurrency,
                           pool=pool,
                           app=app)
        needs_save = False
        if queues:
            node.queues = self._maybe_queues(queues)
            needs_save = True
        if broker:
            node._broker = broker
            needs_save = True
        if needs_save:
            node.save()
        return node

    def _action(self, nodename, action, *args, **kwargs):
        node = self.get(name=nodename)
        getattr(node, action)(*args, **kwargs)
        return node

    def remove(self, nodename):
        return self._action(nodename, "delete")

    def enable(self, nodename):
        return self._action(nodename, "enable")

    def disable(self, nodename):
        return self._action(nodename, "disable")

    def remove_queue_from_nodes(self, queue, **query):
        nodes = []
        for node in self.filter(**query).iterator():
            if queue in node.queues:
                node.queues.remove(queue)
                node.save()
                nodes.append(node)
        return nodes

    def add_queue_to_nodes(self, queue, **query):
        nodes = []
        for node in self.filter(**query).iterator():
            node.queues.add(queue)
            node.save()
            nodes.append(node)
        return nodes


class QueueManager(ExtendedManager):

    def enabled(self):
        return self.filter(is_enabled=True)

    def _add(self, name, **declaration):
        return self.get_or_create(name=name, defaults=declaration)[0]

    def add(self, name, exchange=None, exchange_type=None,
            routing_key=None, **options):
        options = serialize(options) if options else None
        return self._add(name, exchange=exchange, exchange_type=exchange_type,
                               routing_key=routing_key, options=options)