from functools import wraps, partial
import os
import types
import pathlib

import trio
from trio._util import async_wraps, fspath

__all__ = ['Path']


# python3.5 compat: __fspath__ does not exist in 3.5, so unwrap any trio.Path
# being passed to any wrapped method
def unwrap_paths(args):
    new_args = []
    for arg in args:
        if isinstance(arg, Path):
            arg = arg._wrapped
        new_args.append(arg)
    return new_args


# re-wrap return value from methods that return new instances of pathlib.Path
def rewrap_path(value):
    if isinstance(value, pathlib.Path):
        value = Path(value)
    return value


def _forward_factory(cls, attr_name, attr):
    @wraps(attr)
    def wrapper(self, *args, **kwargs):
        args = unwrap_paths(args)
        attr = getattr(self._wrapped, attr_name)
        value = attr(*args, **kwargs)
        return rewrap_path(value)

    return wrapper


def _forward_magic(cls, attr):
    sentinel = object()

    @wraps(attr)
    def wrapper(self, other=sentinel):
        if other is sentinel:
            return attr(self._wrapped)
        if isinstance(other, cls):
            other = other._wrapped
        value = attr(self._wrapped, other)
        return rewrap_path(value)

    return wrapper


def thread_wrapper_factory(cls, meth_name):
    @async_wraps(cls, pathlib.Path, meth_name)
    async def wrapper(self, *args, **kwargs):
        args = unwrap_paths(args)
        meth = getattr(self._wrapped, meth_name)
        func = partial(meth, *args, **kwargs)
        value = await trio.run_sync_in_worker_thread(func)
        return rewrap_path(value)

    return wrapper


class AsyncAutoWrapperType(type):
    def __init__(cls, name, bases, attrs):
        super().__init__(name, bases, attrs)

        cls._forward = []
        type(cls).generate_forwards(cls, attrs)
        type(cls).generate_wraps(cls, attrs)
        type(cls).generate_magic(cls, attrs)

    def generate_forwards(cls, attrs):
        # forward functions of _forwards
        for attr_name, attr in cls._forwards.__dict__.items():
            if attr_name.startswith('_') or attr_name in attrs:
                continue

            if isinstance(attr, property):
                cls._forward.append(attr_name)
            elif isinstance(attr, types.FunctionType):
                wrapper = _forward_factory(cls, attr_name, attr)
                setattr(cls, attr_name, wrapper)
            else:
                raise TypeError(attr_name, type(attr))

    def generate_wraps(cls, attrs):
        # generate wrappers for functions of _wraps
        for attr_name, attr in cls._wraps.__dict__.items():
            if attr_name.startswith('_') or attr_name in attrs:
                continue

            if isinstance(attr, classmethod):
                setattr(cls, attr_name, attr)
            elif isinstance(attr, types.FunctionType):
                wrapper = thread_wrapper_factory(cls, attr_name)
                setattr(cls, attr_name, wrapper)
            else:
                raise TypeError(attr_name, type(attr))

    def generate_magic(cls, attrs):
        # generate wrappers for magic
        for attr_name in cls._forward_magic:
            attr = getattr(cls._forwards, attr_name)
            wrapper = _forward_magic(cls, attr)
            setattr(cls, attr_name, wrapper)


class Path(metaclass=AsyncAutoWrapperType):
    """A :class:`pathlib.Path` wrapper that executes blocking methods in
    :meth:`trio.run_sync_in_worker_thread`.

    """

    _wraps = pathlib.Path
    _forwards = pathlib.PurePath
    _forward_magic = [
        '__str__', '__bytes__', '__truediv__', '__rtruediv__', '__eq__',
        '__lt__', '__le__', '__gt__', '__ge__'
    ]

    def __init__(self, *args):
        args = unwrap_paths(args)

        self._wrapped = pathlib.Path(*args)

    def __getattr__(self, name):
        if name in self._forward:
            value = getattr(self._wrapped, name)
            return rewrap_path(value)
        raise AttributeError(name)

    def __dir__(self):
        return super().__dir__() + self._forward

    def __repr__(self):
        return 'trio.Path({})'.format(repr(str(self)))

    def __fspath__(self):
        return fspath(self._wrapped)

    @wraps(pathlib.Path.open)
    async def open(self, *args, **kwargs):
        """Open the file pointed to by the path, like the :func:`trio.open_file`
        function does.

        """

        func = partial(self._wrapped.open, *args, **kwargs)
        value = await trio.run_sync_in_worker_thread(func)
        return trio.wrap_file(value)


# The value of Path.absolute.__doc__ makes a reference to
# :meth:~pathlib.Path.absolute, which does not exist. Removing this makes more
# sense than inventing our own special docstring for this.
del Path.absolute.__doc__

# python3.5 compat
if hasattr(os, 'PathLike'):
    os.PathLike.register(Path)
