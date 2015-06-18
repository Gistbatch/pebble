# This file is part of Pebble.

# Pebble is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License
# as published by the Free Software Foundation,
# either version 3 of the License, or (at your option) any later version.

# Pebble is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public License
# along with Pebble.  If not, see <http://www.gnu.org/licenses/>.


import os
import time

from itertools import count
from collections import namedtuple
from signal import SIG_IGN, SIGINT, signal

from pebble import thread
from pebble.task import Task
from pebble.utils import execute
from pebble.pool import RUNNING, SLEEP_UNIT
from pebble.pool import BasePool, run_initializer
from pebble.process.channel import channels
from pebble.process.decorators import spawn
from pebble.process.utils import stop, send_results
from pebble.exceptions import TimeoutError, TaskCancelled, ProcessExpired


NoMessage = namedtuple('NoMessage', ())
NewTask = namedtuple('NewTask', ('id', 'payload'))
Results = namedtuple('Results', ('task', 'results'))
Acknowledgement = namedtuple('Acknowledgement', ('worker', 'task'))


class Pool(BasePool):
    def __init__(self, workers=1, task_limit=0, queue=None, queueargs=None,
                 initializer=None, initargs=()):
        super(Pool, self).__init__(workers, task_limit, queue, queueargs,
                                   initializer, initargs)
        self._pool_manager = PoolManager(self._context)

    def _start_pool(self):
        self._pool_manager.start()
        self._loops = (task_scheduler_loop(self._pool_manager),
                       pool_manager_loop(self._pool_manager),
                       message_manager_loop(self._pool_manager))
        self._context.state = RUNNING

    def _stop_pool(self):
        self._pool_manager.stop()

    def stop(self):
        super(Pool, self).stop()
        self._context.task_queue.put(None)


@thread.spawn(daemon=True, name='task_scheduler')
def task_scheduler_loop(pool_manager):
    for task in pool_get_next_task(pool_manager):
        pool_manager.schedule(task)


def pool_get_next_task(pool_manager):
    context = pool_manager.context
    task_queue = context.task_queue

    while context.alive:
        task = task_queue.get()

        if isinstance(task, Task) and not task.cancelled:
            yield task
        else:
            task_queue.task_done()
            return


@thread.spawn(daemon=True, name='pool_manager')
def pool_manager_loop(pool_manager):
    context = pool_manager.context

    while context.alive:
        pool_manager.update_status()
        time.sleep(SLEEP_UNIT)


@thread.spawn(daemon=True, name='message_manager')
def message_manager_loop(pool_manager):
    for message in get_next_message(pool_manager):
        pool_manager.process_message(message)


def get_next_message(pool_manager):
    context = pool_manager.context
    channel = pool_manager.worker_manager.pool_channel

    while context.alive:
        if channel.poll(SLEEP_UNIT):
            yield channel.recv()
        else:
            yield NoMessage()


class PoolManager(object):
    def __init__(self, context):
        self.context = context
        self.task_manager = TaskManager(context.task_queue.task_done)
        self.worker_manager = WorkerManager(context.workers,
                                            context.worker_parameters)

    def start(self):
        self.worker_manager.create_workers()

    def stop(self):
        self.worker_manager.stop_workers()

    def schedule(self, task):
        self.task_manager.register(task)
        self.worker_manager.dispatch(task)

    def process_message(self, message):
        if isinstance(message, Acknowledgement):
            self.task_manager.task_start(message.task, message.worker)
        elif isinstance(message, Results):
            self.task_manager.task_done(message.task, message.results)

    def update_status(self):
        self.update_tasks()
        self.update_workers()

    def update_tasks(self):
        timeout, cancelled = self.task_manager.inspect_tasks()

        for task in timeout:
            self.task_manager.task_done(id(task), TimeoutError('Timeout'))
        for task in cancelled:
            self.task_manager.task_done(id(task), TaskCancelled('Cancelled'))

        for worker_id in (t._metadata for t in timeout + cancelled):
            self.worker_manager.stop_worker(worker_id)

    def update_workers(self):
        for worker_id, exitcode in self.worker_manager.inspect_workers():
            task = self.worker_id_lookup(worker_id)
            if task is not None:
                error = ProcessExpired('Abnormal termination', code=exitcode)
                self.task_manager.task_done(id(task), error)

        self.worker_manager.create_workers()

    def worker_id_lookup(self, worker_id):
        for task in tuple(self.task_manager.tasks.values()):
            if task._metadata == worker_id:
                return task


class TaskManager(object):
    def __init__(self, task_done_callback):
        self.tasks = {}
        self.task_done_callback = task_done_callback

    def register(self, task):
        self.tasks[id(task)] = task

    def task_start(self, task_id, worker_id):
        try:
            task = self.tasks[task_id]
        except KeyError:
            pass
        else:
            task._metadata = worker_id
            task._timestamp = time.time()

    def task_done(self, task_id, results):
        try:
            task = self.tasks.pop(task_id)
        except KeyError:
            pass
        else:
            task.set_results(results)
            self.task_done_callback()

    def inspect_tasks(self):
        tasks = tuple(self.tasks.values())

        return (tuple(t for t in tasks if self.has_timeout(t)),
                tuple(t for t in tasks if t.started and t.cancelled))

    @staticmethod
    def has_timeout(task):
        if task.timeout and task.started:
            return time.time() - task._timestamp > task.timeout
        else:
            return False


class WorkerManager(object):
    def __init__(self, workers, worker_parameters):
        self.workers = {}
        self.workers_number = workers
        self.worker_parameters = worker_parameters
        self.pool_channel, self.workers_channel = channels(SLEEP_UNIT / 2)

    def dispatch(self, task):
        self.pool_channel.send(NewTask(id(task), task._metadata))

    def inspect_workers(self):
        expired = tuple(w for w in self.workers.values() if not w.is_alive())

        for worker in expired:
            self.workers.pop(worker.pid)

        return ((w.pid, w.exitcode) for w in expired if w.exitcode != os.EX_OK)

    def create_workers(self):
        for _ in range(self.workers_number - len(self.workers)):
            self.new_worker()

    def stop_workers(self):
        for worker_id in tuple(self.workers.keys()):
            self.stop_worker(worker_id)

    def new_worker(self):
        worker = worker_process(self.worker_parameters, self.workers_channel)
        self.workers[worker.pid] = worker

    def stop_worker(self, worker_id):
        try:
            with self.workers_channel.lock:
                stop(self.workers.pop(worker_id))
        except TimeoutError:  # unable to acquire channel, busy
            pass
        except KeyError:  # worker already expired
            pass


@spawn(name='worker_process', daemon=True)
def worker_process(params, channel):
    """Runs the actual function in separate process."""
    signal(SIGINT, SIG_IGN)

    if params.initializer is not None:
        if not run_initializer(params.initializer, params.initargs):
            os._exit(os.EX_OK)

    try:
        for task in worker_get_next_task(channel, params.task_limit):
            results = execute_next_task(task.payload)
            send_results(channel, Results(task.id, results))
    except (EOFError, EnvironmentError) as error:
        return error.errno

    if params.deinitializer is not None:
        if not run_initializer(params.deinitializer, params.deinitargs):
            os._exit(os.EX_OK)

    return os._exit(os.EX_OK)


def worker_get_next_task(channel, task_limit):
    counter = count()

    while not task_limit or next(counter) < task_limit:
        yield fetch_task(channel)


def fetch_task(channel):
    task = None

    channel.poll()
    while task is None:
        try:
            task = task_transaction(channel)
        except TimeoutError:  # unable to acquire channel, busy
            pass

    return task


def task_transaction(channel):
    with channel.lock:
        task = channel.recv()
        channel.send(Acknowledgement(os.getpid(), task.id))

    return task


def execute_next_task(task):
    function, args, kwargs = task
    return execute(function, args, kwargs)
