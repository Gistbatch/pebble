import os
import sys
import time
import pickle
import signal
import unittest
import threading
import multiprocessing
from concurrent.futures import CancelledError, TimeoutError

from pebble import concurrent, ProcessExpired


# set start method
supported = False

if sys.version_info.major > 2 and sys.version_info.minor > 3:
    methods = multiprocessing.get_all_start_methods()
    if 'fork' in methods:
        try:
            multiprocessing.set_start_method('fork')

            if multiprocessing.get_start_method() == 'fork':
                supported = True
        except RuntimeError:  # child process
            pass
else:
    supported = True


@concurrent.process
def decorated(argument, keyword_argument=0):
    """A docstring."""
    return argument + keyword_argument


@concurrent.process
def error_decorated():
    raise RuntimeError("BOOM!")


@concurrent.process
def pickling_error_decorated():
    event = threading.Event()
    return event


@concurrent.process
def critical_decorated():
    os._exit(123)


@concurrent.process
def decorated_cancel():
    time.sleep(10)


@concurrent.process(timeout=0.1)
def long_decorated():
    time.sleep(10)


@concurrent.process(timeout=0.1)
def sigterm_decorated():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(10)

@concurrent.process()
def name_keyword_argument(name='function_kwarg'):
    return name

@concurrent.process(name='concurrent_process_name')
def name_keyword_decorated():
    return multiprocessing.current_process().name


@concurrent.process(name='decorator_kwarg')
def name_keyword_decorated_and_argument(name='bar'):
    return (multiprocessing.current_process().name, name)
class ProcessConcurrentObj:
    a = 0

    def __init__(self):
        self.b = 1

    @classmethod
    @concurrent.process
    def clsmethod(cls):
        return cls.a

    @concurrent.process
    def instmethod(self):
        return self.b

    @staticmethod
    @concurrent.process
    def stcmethod():
        return 2


@unittest.skipIf(not supported, "Start method is not supported")
class TestProcessConcurrent(unittest.TestCase):
    def setUp(self):
        self.results = 0
        self.exception = None
        self.event = threading.Event()
        self.event.clear()
        self.concurrentobj = ProcessConcurrentObj()

    def callback(self, future):
        try:
            self.results = future.result()
        except (ProcessExpired, RuntimeError, TimeoutError) as error:
            self.exception = error
        finally:
            self.event.set()

    def test_docstring(self):
        """Process Fork docstring is preserved."""
        self.assertEqual(decorated.__doc__, "A docstring.")

    def test_wrong_timeout(self):
        """Process Fork TypeError is raised if timeout is not number."""
        with self.assertRaises(TypeError):
            @concurrent.process(timeout='Foo')
            def function():
                return

    def test_class_method(self):
        """Process Fork decorated classmethods."""
        future = ProcessConcurrentObj.clsmethod()
        self.assertEqual(future.result(), 0)

    def test_instance_method(self):
        """Process Fork decorated instance methods."""
        future = self.concurrentobj.instmethod()
        self.assertEqual(future.result(), 1)

    def test_static_method(self):
        """Process Fork decorated static methods (Fork startmethod only)."""
        future = self.concurrentobj.stcmethod()
        self.assertEqual(future.result(), 2)

    def test_decorated_results(self):
        """Process Fork results are produced."""
        future = decorated(1, 1)
        self.assertEqual(future.result(), 2)

    def test_decorated_results_callback(self):
        """Process Fork results are forwarded to the callback."""
        future = decorated(1, 1)
        future.add_done_callback(self.callback)
        self.event.wait(timeout=1)
        self.assertEqual(self.results, 2)

    def test_error_decorated(self):
        """Process Fork errors are raised by future.result."""
        future = error_decorated()
        with self.assertRaises(RuntimeError):
            future.result()

    def test_error_decorated_callback(self):
        """Process Fork errors are forwarded to callback."""
        future = error_decorated()
        future.add_done_callback(self.callback)
        self.event.wait(timeout=1)
        self.assertTrue(isinstance(self.exception, RuntimeError),
                        msg=str(self.exception))

    def test_pickling_error_decorated(self):
        """Process Fork pickling errors are raised by future.result."""
        future = pickling_error_decorated()
        with self.assertRaises((pickle.PicklingError, TypeError)):
            future.result()

    def test_timeout_decorated(self):
        """Process Fork raises TimeoutError if so."""
        future = long_decorated()
        with self.assertRaises(TimeoutError):
            future.result()

    def test_timeout_decorated_callback(self):
        """Process Fork TimeoutError is forwarded to callback."""
        future = long_decorated()
        future.add_done_callback(self.callback)
        self.event.wait(timeout=1)
        self.assertTrue(isinstance(self.exception, TimeoutError),
                        msg=str(self.exception))

    def test_decorated_dead_process(self):
        """Process Fork ProcessExpired is raised if process dies."""
        future = critical_decorated()
        with self.assertRaises(ProcessExpired):
            future.result()

    def test_timeout_decorated_callback(self):
        """Process Fork ProcessExpired is forwarded to callback."""
        future = critical_decorated()
        future.add_done_callback(self.callback)
        self.event.wait(timeout=1)
        self.assertTrue(isinstance(self.exception, ProcessExpired),
                        msg=str(self.exception))

    def test_cancel_decorated(self):
        """Process Fork raises CancelledError if future was cancelled."""
        future = decorated_cancel()
        future.cancel()
        self.assertRaises(CancelledError, future.result)

    @unittest.skipIf(os.name == 'nt', "Test won't run on Windows.")
    def test_decorated_ignoring_sigterm(self):
        """Process Fork Concurrent ignored SIGTERM signal are handled on Unix."""
        future = sigterm_decorated()
        with self.assertRaises(TimeoutError):
            future.result()

    def test_name_keyword_argument(self):
        """name keyword can be passed to a decorated function process without name"""
        f = name_keyword_argument()
        fn_out = f.result()
        self.assertEqual(fn_out, "function_kwarg")

    def test_name_keyword_decorated(self):
        """
        Check that a simple use case of the name keyword passed to the decorator works
        """
        f = name_keyword_decorated()
        dec_out = f.result()
        self.assertEqual(dec_out, "concurrent_process_name")

    def test_name_keyword_decorated_result_colision(self):
        """name kwarg is handled  without modifying the function kwargs"""
        f = name_keyword_decorated_and_argument(name="function_kwarg")
        dec_out, fn_out = f.result()
        self.assertEqual(dec_out, "decorator_kwarg")
        self.assertEqual(fn_out, "function_kwarg")
    