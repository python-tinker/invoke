"""
This module contains the core `.Task` class & convenience decorators used to
generate new tasks.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from copy import deepcopy
from functools import singledispatchmethod, update_wrapper
from types import FunctionType
from typing import (  # Generic,; TypeVar,
    TYPE_CHECKING,
    Any,
    Optional,
    Union,
    cast,
)

from .context import Context
from .parser import Argument, translate_underscores

if TYPE_CHECKING:
    from inspect import Signature

    from .collection import Collection
    from .config import Config

# T = TypeVar("T", bound="Callable")


class Task:
    """
    Core object representing an executable task & its argument specification.

    For the most part, this object is a clearinghouse for all of the data that
    may be supplied to the `@task <invoke.tasks.task>` decorator, such as
    ``name``, ``aliases``, ``positional`` etc, which appear as attributes.

    In addition, instantiation copies some introspection/documentation friendly
    metadata off of the supplied ``body`` object, such as ``__doc__``,
    ``__name__`` and ``__module__``, allowing it to "appear as" ``body`` for
    most intents and purposes.

    May be called without any parentheses if no extra options need to be
    specified. Otherwise, the following keyword arguments are allowed in the
    parenthese'd form:

    * ``name``: Default name to use when binding to a `.Collection`. Useful for
      avoiding Python namespace issues (i.e. when the desired CLI level name
      can't or shouldn't be used as the Python level name.)
    * ``aliases``: Specify one or more aliases for this task, allowing it to be
      invoked as multiple different names. For example, a task named ``mytask``
      with a simple ``@task`` wrapper may only be invoked as ``"mytask"``.
      Changing the decorator to be ``@task(aliases=['myothertask'])`` allows
      invocation as ``"mytask"`` *or* ``"myothertask"``.
    * ``positional``: Iterable overriding the parser's automatic "args with no
      default value are considered positional" behavior. If a list of arg
      names, no args besides those named in this iterable will be considered
      positional. (This means that an empty list will force all arguments to be
      given as explicit flags.)
    * ``optional``: Iterable of argument names, declaring those args to
      have :ref:`optional values <optional-values>`. Such arguments may be
      given as value-taking options (e.g. ``--my-arg=myvalue``, wherein the
      task is given ``"myvalue"``) or as Boolean flags (``--my-arg``, resulting
      in ``True``).
    * ``iterable``: Iterable of argument names, declaring them to :ref:`build
      iterable values <iterable-flag-values>`.
    * ``incrementable``: Iterable of argument names, declaring them to
      :ref:`increment their values <incrementable-flag-values>`.
    * ``default``: Boolean option specifying whether this task should be its
      collection's default task (i.e. called if the collection's own name is
      given.)
    * ``auto_shortflags``: Whether or not to automatically create short
      flags from task options; defaults to True.
    * ``help``: dict mapping argument names to their help strings. Will be
      displayed in ``--help`` output. For arguments containing underscores
      (which are transformed into dashes on the CLI by default), either the
      dashed or underscored version may be supplied here.
    * ``pre``, ``post``: lists of task objects to execute prior to, or after,
      the wrapped task whenever it is executed.
    * ``autoprint``: Boolean determining whether to automatically print this
      task's return value to standard output when invoked directly via the CLI.
      Defaults to False.
    * ``klass``: Class to instantiate/return. Defaults to `.Task`.

    If any non-keyword arguments are given, they are taken as the value of the
    ``pre`` kwarg for convenience's sake. (It is an error to give both
    ``*args`` and ``pre`` at the same time.)

    .. versionadded:: 1.0
    .. versionchanged:: 1.1
        Added the ``klass`` keyword argument.
    .. versionchanged:: 3.0
        Consolidated ``Task`` and ``task`` capability.
    """

    def __new__(cls, *args: Union[Call, Task], **kwargs: Any) -> Task:
        # XXX: need to move to metaclass to really pop kwargs
        return super().__new__(kwargs.pop("klass", cls))

    # TODO: store these kwarg defaults centrally
    # TODO: allow central per-session / per-taskmodule control over some of
    # them, e.g. (auto_)positional, auto_shortflags.
    # NOTE: we shadow __builtins__.help here on purpose - obfuscating to avoid
    # it feels bad, given the builtin will never actually be in play anywhere
    # except a debug shell whose frame is exactly inside this class.
    def __init__(
        self,
        body: Optional[Callable] = None,
        *args: Union[Call, Task],
        **kwargs: Any,
    ) -> None:
        # XXX: handle pre-tasks provided as args
        if isinstance(body, Call) or isinstance(body, Task):
            args = (body, *args)
            body = None
        # Real callable
        self.body = body
        if self.body:
            # XXX: update_wrapper not working well here
            update_wrapper(self, self.body)
            self.__doc__ = getattr(self.body, "__doc__", "")
            self.__name__ = getattr(self.body, "__name__", "")
            self.__module__ = getattr(self.body, "__module__", "")

        # Default name, alternate names, and whether it should act as the
        # default for its parent collection
        self._name: str = kwargs.pop("name", None)
        self.aliases: tuple[str, ...] = tuple(kwargs.pop("aliases", ()))
        self.is_default: bool = bool(kwargs.pop("default", False))
        self.optional: tuple[str, ...] = tuple(kwargs.pop("optional", ()))
        self.iterable: Iterable[str] = kwargs.pop("iterable", [])
        self.incrementable: Iterable[str] = kwargs.pop("incrementable", [])
        self.auto_shortflags: bool = bool(kwargs.pop("auto_shortflags", True))
        self.help: dict[str, Any] = (kwargs.pop("help", {})).copy()
        # Call chain bidness
        if args:
            if "pre" in kwargs:
                raise TypeError(
                    "May not give *args and 'pre' kwarg simultaneously!"
                )
            kwargs["pre"] = args
        self.pre: list[Union[Call, Task]] = kwargs.pop("pre", [])
        self.post: list[Union[Call, Task]] = kwargs.pop("post", [])
        # Whether to print return value post-execution
        self.autoprint: bool = bool(kwargs.pop("autoprint", False))
        # Arg/flag/parser hints
        self.positional = (
            self.fill_implicit_positionals(kwargs.pop("positional", None))
            if self.body is not None
            else kwargs.pop("positional", None)
        )
        kwargs.pop("klass", None)  # XXX move to metaclass
        if kwargs != {}:
            raise TypeError
        self.times_called = 0

    def __repr__(self) -> str:
        aliases = ""
        if self.aliases:
            aliases = f" ({', '.join(self.aliases)})"
        return f"<Task {self.name!r}{aliases}>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Task) or self.name != other.name:
            return False
        # Functions do not define __eq__ but func_code objects apparently do.
        # (If we're wrapping some other callable, they will be responsible for
        # defining equality on their end.)
        if self.body:
            if self.body == other.body:
                return True
            if hasattr(self.body, "__code__"):
                return self.body.__code__ == other.body.__code__  # type: ignore
        return False

    def __hash__(self) -> int:
        # Presumes name and body will never be changed. Hrm.
        # Potentially cleaner to just not use Tasks as hash keys, but let's do
        # this for now.
        return hash(self.name) + hash(self.body)

    @singledispatchmethod
    def __call__(
        self, ctx: Union[Context, Callable], /, *args: Any, **kwargs: Any
    ) -> Optional[Any]:
        # TODO: raise a custom subclass _of_ TypeError instead
        raise TypeError(
            f"Task expected a Context as first arg, got {type(ctx)} instead!"
        )

    @__call__.register
    def _(self, ctx: Context, /, *args: Any, **kwargs: Any) -> Optional[Any]:
        if self.body:
            result = self.body(ctx, *args, **kwargs)
            self.times_called += 1
            return result
        raise AttributeError("task body is undefined")

    @__call__.register
    def _(self, body: Callable, /, *args: Any, **kwargs: Any) -> Any:
        # print('func', body, args, kwargs)
        if self.body is None:
            update_wrapper(self, body)  # type: ignore
            self.body = body
            self.positional = self.fill_implicit_positionals(self.positional)
        # XXX: need to register "Task" somehow but singledispath cannot see it
        return self

    @property
    def called(self) -> bool:
        return self.times_called > 0

    @property
    def name(self) -> str:
        return self._name or self.__name__

    def argspec(self, body: Callable) -> Signature:
        """
        Returns a modified `inspect.Signature` based on that of ``body``.

        :returns:
            an `inspect.Signature` matching that of ``body``, but with the
            initial context argument removed.
        :raises TypeError:
            if the task lacks an initial positional `.Context` argument.

        .. versionadded:: 1.0
        .. versionchanged:: 2.0
            Changed from returning a two-tuple of ``(arg_names, spec_dict)`` to
            returning an `inspect.Signature`.
        """
        # Rebuild signature with first arg dropped, or die usefully(ish trying
        sig = inspect.signature(
            # Handle callable-but-not-function objects
            body
            if isinstance(body, FunctionType)
            else body.__call__  # type: ignore
        )
        params = list(sig.parameters.values())
        # TODO: this ought to also check if an extant 1st param _was_ a Context
        # arg, and yell similarly if not.
        if not params:
            # TODO: see TODO under __call__, this should be same type
            raise TypeError("Tasks must have an initial Context argument!")
        return sig.replace(parameters=params[1:])

    def fill_implicit_positionals(
        self, positional: Optional[Iterable[str]]
    ) -> Iterable[str]:
        if self.body is None:
            raise AttributeError("task body is undefined")
        # If positionals is None, everything lacking a default
        # value will be automatically considered positional.
        if positional is None:
            positional = [
                x.name
                for x in self.argspec(self.body).parameters.values()
                if x.default is inspect.Signature.empty
            ]
        return positional

    def arg_opts(
        self, name: str, default: str, taken_names: set[str]
    ) -> dict[str, Any]:
        opts: dict = {}
        # Whether it's positional or not
        opts["positional"] = name in self.positional
        # Whether it is a value-optional flag
        opts["optional"] = name in self.optional
        # Whether it should be of an iterable (list) kind
        if name in self.iterable:
            opts["kind"] = list
            # If user gave a non-None default, hopefully they know better
            # than us what they want here (and hopefully it offers the list
            # protocol...) - otherwise supply useful default
            opts["default"] = default if default is not None else []
        # Whether it should increment its value or not
        if name in self.incrementable:
            opts["incrementable"] = True
        # Argument name(s) (replace w/ dashed version if underscores present,
        # and move the underscored version to be the attr_name instead.)
        original_name = name  # For reference in eg help=
        if "_" in name:
            opts["attr_name"] = name
            name = translate_underscores(name)
        names = [name]
        if self.auto_shortflags:
            # Must know what short names are available
            for char in name:
                if not (char == name or char in taken_names):
                    names.append(char)
                    break
        opts["names"] = names
        # Handle default value & kind if possible
        if default not in (None, inspect.Signature.empty):
            # TODO: allow setting 'kind' explicitly.
            # NOTE: skip setting 'kind' if optional is True + type(default) is
            # bool; that results in a nonsensical Argument which gives the
            # parser grief in a few ways.
            kind = type(default)
            if not (opts["optional"] and kind is bool):
                opts["kind"] = kind
            opts["default"] = default
        # Help
        for possibility in name, original_name:
            if possibility in self.help:
                opts["help"] = self.help.pop(possibility)
                break
        return opts

    def get_arguments(
        self, ignore_unknown_help: Optional[bool] = None
    ) -> list[Argument]:
        """
        Return a list of Argument objects representing this task's signature.

        :param bool ignore_unknown_help:
            Controls whether unknown help flags cause errors. See the config
            option by the same name for details.

        .. versionadded:: 1.0
        .. versionchanged:: 1.7
            Added the ``ignore_unknown_help`` kwarg.
        """
        if self.body is None:
            raise AttributeError("task body is undefined")
        # Core argspec
        sig = self.argspec(self.body)
        # Prime the list of all already-taken names (mostly for help in
        # choosing auto shortflags)
        taken_names = set(sig.parameters.keys())
        # Build arg list (arg_opts will take care of setting up shortnames,
        # etc)
        args = []
        for param in sig.parameters.values():
            new_arg = Argument(
                **self.arg_opts(param.name, param.default, taken_names)
            )
            args.append(new_arg)
            # Update taken_names list with new argument's full name list
            # (which may include new shortflags) so subsequent Argument
            # creation knows what's taken.
            taken_names.update(set(new_arg.names))
        # If any values were leftover after consuming a 'help' dict, it implies
        # the user messed up & had a typo or similar. Let's explode.
        if self.help and not ignore_unknown_help:
            raise ValueError(
                "Help field was set for param(s) that don't exist: {}".format(
                    list(self.help.keys())
                )
            )
        # Now we need to ensure positionals end up in the front of the list, in
        # order given in self.positionals, so that when Context consumes them,
        # this order is preserved.
        for posarg in reversed(list(self.positional)):
            for i, arg in enumerate(args):
                if arg.name == posarg:
                    args.insert(0, args.pop(i))
                    break
        return args


task = Task


class Call:
    """
    Represents a call/execution of a `.Task` with given (kw)args.

    Similar to `~functools.partial` with some added functionality (such as the
    delegation to the inner task, and optional tracking of the name it's being
    called by.)

    .. versionadded:: 1.0
    """

    def __init__(
        self,
        task: Task,
        /,
        called_as: Optional[str] = None,
        args: Optional[tuple[str, ...]] = None,
        kwargs: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        Create a new `.Call` object.

        :param task: The `.Task` object to be executed.

        :param str called_as:
            The name the task is being called as, e.g. if it was called by an
            alias or other rebinding. Defaults to ``None``, aka, the task was
            referred to by its default name.

        :param tuple args:
            Positional arguments to call with, if any. Default: ``None``.

        :param dict kwargs:
            Keyword arguments to call with, if any. Default: ``None``.
        """
        self.task = task
        self.called_as = called_as
        self.args = args or ()
        self.kwargs = kwargs or {}

    # TODO: just how useful is this? feels like maybe overkill magic
    def __getattr__(self, name: str) -> Any:
        return getattr(self.task, name)

    def __deepcopy__(self, memo: object) -> Call:
        return self.clone()

    def __repr__(self) -> str:
        aka = ""
        if self.called_as is not None and self.called_as != self.task.name:
            aka = f" (called as: {self.called_as!r})"
        return "<{} {!r}{}, args: {!r}, kwargs: {!r}>".format(
            self.__class__.__name__,
            self.task.name,
            aka,
            self.args,
            self.kwargs,
        )

    def __eq__(self, other: object) -> bool:
        # NOTE: Not comparing 'called_as'; a named call of a given Task with
        # same args/kwargs should be considered same as an unnamed call of the
        # same Task with the same args/kwargs (e.g. pre/post task specified w/o
        # name). Ditto tasks with multiple aliases.
        for attr in "task args kwargs".split():
            if getattr(self, attr) != getattr(other, attr):
                return False
        return True

    def clone_data(self) -> dict:
        """
        Return keyword args suitable for cloning this call into another.

        .. versionadded:: 1.1
        """
        return {
            "called_as": self.called_as,
            "args": deepcopy(self.args),
            "kwargs": deepcopy(self.kwargs),
        }

    def clone(
        self,
        into: Optional[type[Call]] = None,
        with_: Optional[dict[str, Any]] = None,
    ) -> "Call":
        """
        Return a standalone copy of this Call.

        Useful when parameterizing task executions.

        :param into:
            A subclass to generate instead of the current class. Optional.

        :param dict with_:
            A dict of additional keyword arguments to use when creating the new
            clone; typically used when cloning ``into`` a subclass that has
            extra args on top of the base class. Optional.

            .. note::
                This dict is used to ``.update()`` the original object's data
                (the return value from its `clone_data`), so in the event of
                a conflict, values in ``with_`` will win out.

        .. versionadded:: 1.0
        .. versionchanged:: 1.1
            Added the ``with_`` kwarg.
        """
        klass = into if into is not None else self.__class__
        data = self.clone_data()
        if with_ is not None:
            data.update(with_)
        return klass(self.task, **data)


def call(task: Task, /, *args: Any, **kwargs: Any) -> Call:
    """
    Describes execution of a `.Task`, typically with pre-supplied arguments.

    Useful for setting up :ref:`pre/post task invocations
    <parameterizing-pre-post-tasks>`. It's actually just a convenient wrapper
    around the `.Call` class, which may be used directly instead if desired.

    For example, here's two build-like tasks that both refer to a ``setup``
    pre-task, one with no baked-in argument values (and thus no need to use
    `.call`), and one that toggles a boolean flag::

        @task
        def setup(c, clean=False):
            if clean:
                c.run("rm -rf target")
            # ... setup things here ...
            c.run("tar czvf target.tgz target")

        @task(pre=[setup])
        def build(c):
            c.run("build, accounting for leftover files...")

        @task(pre=[call(setup, clean=True)])
        def clean_build(c):
            c.run("build, assuming clean slate...")

    Please see the constructor docs for `.Call` for details - this function's
    ``args`` and ``kwargs`` map directly to the same arguments as in that
    method.

    .. versionadded:: 1.0
    """
    return Call(task, args=args, kwargs=kwargs)
