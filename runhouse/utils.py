import asyncio
import contextvars
import functools
import inspect
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Type, Union

logger = logging.getLogger(__name__)


def _find_directory_containing_any_file(dir_path, files, searched_dirs=None):
    if Path(dir_path) == Path.home() or dir_path == Path("/"):
        return None

    if any(Path(dir_path, file).exists() for file in files):
        return str(dir_path)

    searched_dirs.add(dir_path)
    parent_path = Path(dir_path).parent
    if parent_path in searched_dirs:
        return None
    return _find_directory_containing_any_file(
        parent_path, files, searched_dirs=searched_dirs
    )


def locate_working_dir(start_dir=None):
    if start_dir is None:
        start_dir = os.getcwd()

    # Search first for anything that represents a Python package
    target_files = [
        ".git",
        "setup.py",
        "setup.cfg",
        "pyproject.toml",
        "requirements.txt",
    ]

    dir_with_target = _find_directory_containing_any_file(
        start_dir, target_files, searched_dirs=set()
    )

    if dir_with_target is None:
        dir_with_target = _find_directory_containing_any_file(
            start_dir, ["rh"], searched_dirs=set()
        )

    return dir_with_target if dir_with_target is not None else start_dir


def extract_module_path(raw_cls_or_fn: Union[Type, Callable]):
    py_module = inspect.getmodule(raw_cls_or_fn)

    # Need to resolve in case just filename is given
    module_path = (
        str(Path(inspect.getfile(py_module)).resolve())
        if hasattr(py_module, "__file__")
        else None
    )

    return module_path


def get_module_import_info(raw_cls_or_fn: Union[Type, Callable]):
    """
    Given a class or function in Python, get all the information needed to import it in another Python process.
    """

    # Background on all these dunders: https://docs.python.org/3/reference/import.html
    py_module = inspect.getmodule(raw_cls_or_fn)

    # Need to resolve in case just filename is given
    module_path = extract_module_path(raw_cls_or_fn)

    # TODO better way of detecting if in a notebook or interactive Python env
    if not module_path or module_path.endswith("ipynb"):
        # The only time __file__ wouldn't be present is if the function is defined in an interactive
        # interpreter or a notebook. We can't import on the server in that case, so we need to cloudpickle
        # the fn to send it over. The __call__ function will serialize the function if we return it this way.
        # This is a short-term hack.
        # return None, "notebook", raw_fn.__name__
        root_path = os.getcwd()
        module_name = "notebook"
        cls_or_fn_name = raw_cls_or_fn.__name__
    else:
        root_path = os.path.dirname(module_path)
        module_name = inspect.getmodulename(module_path)
        # TODO __qualname__ doesn't work when fn is aliased funnily, like torch.sum
        cls_or_fn_name = getattr(raw_cls_or_fn, "__qualname__", raw_cls_or_fn.__name__)

        # Adapted from https://github.com/modal-labs/modal-client/blob/main/modal/_function_utils.py#L94
        if getattr(py_module, "__package__", None):
            module_path = os.path.abspath(py_module.__file__)
            package_paths = [
                os.path.abspath(p) for p in __import__(py_module.__package__).__path__
            ]
            base_dirs = [
                base_dir
                for base_dir in package_paths
                if os.path.commonpath((base_dir, module_path)) == base_dir
            ]

            if len(base_dirs) != 1:
                logger.debug(f"Module files: {module_path}")
                logger.debug(f"Package paths: {package_paths}")
                logger.debug(f"Base dirs: {base_dirs}")
                raise Exception("Wasn't able to find the package directory!")
            root_path = os.path.dirname(base_dirs[0])
            module_name = py_module.__spec__.name

    return root_path, module_name, cls_or_fn_name


def _thread_coroutine(coroutine, context):
    # Copy contextvars from the parent thread to the new thread
    for var, value in context.items():
        var.set(value)

    # Technically, event loop logic is not threadsafe. However, this event loop is only in this thread.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # The loop runs only for the duration of the thread
        return loop.run_until_complete(coroutine)
    finally:
        # We don't need to do asyncio.set_event_loop(None) since the thread will just end completely
        loop.close()


# We should minimize calls to this since each one will start a new thread.
# Technically we should not have many threads running async logic at once, however, the calling thread
# will actually block until the async logic that is spawned in the other thread is done.
def sync_function(coroutine_func):
    @functools.wraps(coroutine_func)
    def wrapper(*args, **kwargs):
        # Better API than using threading.Thread, since we just need the thread temporarily
        # and the resources are cleaned up
        with ThreadPoolExecutor() as executor:
            future = executor.submit(
                _thread_coroutine,
                coroutine_func(*args, **kwargs),
                contextvars.copy_context(),
            )
            return future.result()

    return wrapper


def get_pid():
    import os

    return os.getpid()


def get_node_ip():
    import socket

    return socket.gethostbyname(socket.gethostname())


async def arun_in_thread(method_to_run, *args, **kwargs):
    def _run_sync_fn_with_context(context_to_set, sync_fn, method_args, method_kwargs):
        for var, value in context_to_set.items():
            var.set(value)

        return sync_fn(*method_args, **method_kwargs)

    with ThreadPoolExecutor() as executor:
        return await asyncio.get_event_loop().run_in_executor(
            executor,
            functools.partial(
                _run_sync_fn_with_context,
                context_to_set=contextvars.copy_context(),
                sync_fn=method_to_run,
                method_args=args,
                method_kwargs=kwargs,
            ),
        )
