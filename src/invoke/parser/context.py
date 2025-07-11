import itertools
from typing import TYPE_CHECKING, Any, Optional, Union

from lexicon import Lexicon

from .argument import Argument

if TYPE_CHECKING:
    from collections.abc import Iterable


def translate_underscores(name: str) -> str:
    return name.lstrip("_").rstrip("_").replace("_", "-")


def to_flag(name: str) -> str:
    name = translate_underscores(name)
    if len(name) == 1:
        return "-" + name
    return "--" + name


def sort_candidate(arg: Argument) -> str:
    names = arg.names
    # TODO: is there no "split into two buckets on predicate" builtin?
    shorts = {x for x in names if len(x.strip("-")) == 1}
    longs = {x for x in names if x not in shorts}
    return str(sorted(shorts if shorts else longs)[0])


def flag_key(arg: Argument) -> list[Union[int, str]]:
    """
    Obtain useful key list-of-ints for sorting CLI flags.

    .. versionadded:: 1.0
    """
    # Setup
    ret: list[Union[int, str]] = []
    x = sort_candidate(arg)
    # Long-style flags win over short-style ones, so the first item of
    # comparison is simply whether the flag is a single character long (with
    # non-length-1 flags coming "first" [lower number])
    ret.append(1 if len(x) == 1 else 0)
    # Next item of comparison is simply the strings themselves,
    # case-insensitive. They will compare alphabetically if compared at this
    # stage.
    ret.append(x.lower())
    # Finally, if the case-insensitive test also matched, compare
    # case-sensitive, but inverse (with lowercase letters coming first)
    inversed = ""
    for char in x:
        inversed += char.lower() if char.isupper() else char.upper()
    ret.append(inversed)
    return ret


# Named slightly more verbose so Sphinx references can be unambiguous.
# Got real sick of fully qualified paths.
class ParserContext:
    """
    Parsing context with knowledge of flags & their format.

    Generally associated with the core program or a task.

    When run through a parser, will also hold runtime values filled in by the
    parser.

    .. versionadded:: 1.0
    """

    def __init__(
        self,
        name: Optional[str] = None,
        aliases: "Iterable[str]" = (),
        args: "Iterable[Argument]" = (),
    ) -> None:
        """
        Create a new ``ParserContext`` named ``name``, with ``aliases``.

        ``name`` is optional, and should be a string if given. It's used to
        tell ParserContext objects apart, and for use in a Parser when
        determining what chunk of input might belong to a given ParserContext.

        ``aliases`` is also optional and should be an iterable containing
        strings. Parsing will honor any aliases when trying to "find" a given
        context in its input.

        May give one or more ``args``, which is a quick alternative to calling
        ``for arg in args: self.add_arg(arg)`` after initialization.
        """
        self.args = Lexicon()
        self.positional_args: list[Argument] = []
        self.flags = Lexicon()
        self.inverse_flags: dict = {}  # No need for Lexicon here
        self.name = name
        self.aliases = aliases
        for arg in args:
            self.add_arg(arg)

    def __repr__(self) -> str:
        aliases = ""
        if self.aliases:
            aliases = f" ({', '.join(self.aliases)})"
        name = f" {self.name!r}{aliases}" if self.name else ""
        args = f": {self.args!r}" if self.args else ""
        return f"<parser/Context{name}{args}>"

    def add_arg(self, *args: Any, **kwargs: Any) -> None:
        """
        Adds given ``Argument`` (or constructor args for one) to this context.

        The Argument in question is added to the following dict attributes:

        * ``args``: "normal" access, i.e. the given names are directly exposed
          as keys.
        * ``flags``: "flaglike" access, i.e. the given names are translated
          into CLI flags, e.g. ``"foo"`` is accessible via ``flags['--foo']``.
        * ``inverse_flags``: similar to ``flags`` but containing only the
          "inverse" versions of boolean flags which default to True. This
          allows the parser to track e.g. ``--no-myflag`` and turn it into a
          False value for the ``myflag`` Argument.

        .. versionadded:: 1.0
        """
        # Normalize
        if len(args) == 1 and isinstance(args[0], Argument):
            arg = args[0]
        else:
            arg = Argument(*args, **kwargs)
        # Uniqueness constraint: no name collisions
        for name in arg.names:
            if name in self.args:
                msg = (
                    "Tried to add an argument named {!r} but one already "
                    + "exists!"
                )
                raise ValueError(msg.format(name))
        # First name used as "main" name for purposes of aliasing
        main = arg.names[0]  # NOT arg.name
        self.args[main] = arg
        # Note positionals in distinct, ordered list attribute
        if arg.positional:
            self.positional_args.append(arg)
        # Add names & nicknames to flags, args
        self.flags[to_flag(main)] = arg
        for name in arg.nicknames:
            self.args.alias(name, to=main)
            self.flags.alias(to_flag(name), to=to_flag(main))
        # Add attr_name to args, but not flags
        if arg.attr_name:
            self.args.alias(arg.attr_name, to=main)
        # Add to inverse_flags if required
        if arg.kind == bool and arg.default is True:
            # Invert the 'main' flag name here, which will be a dashed version
            # of the primary argument name if underscore-to-dash transformation
            # occurred.
            inverse_name = to_flag(f"no-{main}")
            self.inverse_flags[inverse_name] = to_flag(main)

    @property
    def missing_positional_args(self) -> list[Argument]:
        return [x for x in self.positional_args if x.value is None]

    @property
    def as_kwargs(self) -> dict:
        """
        This context's arguments' values keyed by their ``.name`` attribute.

        Results in a dict suitable for use in Python contexts, where e.g. an
        arg named ``foo-bar`` becomes accessible as ``foo_bar``.

        .. versionadded:: 1.0
        """
        ret = {}
        for arg in self.args.values():
            ret[arg.name] = arg.value
        return ret

    def names_for(self, flag: str) -> list[str]:
        # TODO: should probably be a method on Lexicon/Aliasdict
        return list(set([flag] + self.flags.aliases_of(flag)))

    def help_for(self, flag: str) -> tuple[str, str]:
        """
        Return 2-tuple of ``(flag-spec, help-string)`` for given ``flag``.

        .. versionadded:: 1.0
        """
        # Obtain arg obj
        if flag not in self.flags:
            err = (
                "{!r} is not a valid flag for this context! Valid flags are: "
                + "{!r}"
            )
            raise ValueError(err.format(flag, self.flags.keys()))
        arg = self.flags[flag]
        # Determine expected value type, if any
        value = {str: "STRING", int: "INT"}.get(arg.kind)
        # Format & go
        full_names = []
        for name in self.names_for(flag):
            if value:
                # Short flags are -f VAL, long are --foo=VAL
                # When optional, also, -f [VAL] and --foo[=VAL]
                if len(name.strip("-")) == 1:
                    value_ = f"[{value}]" if arg.optional else value
                    valuestr = f" {value_}"
                else:
                    valuestr = f"={value}"
                    if arg.optional:
                        valuestr = f"[{valuestr}]"
            else:
                # no value => boolean
                # check for inverse
                if name in self.inverse_flags.values():
                    name = f"--[no-]{name[2:]}"
                valuestr = ""
            # Tack together
            full_names.append(name + valuestr)
        namestr = ", ".join(sorted(full_names, key=len))
        helpstr = arg.help or ""
        return namestr, helpstr

    def help_tuples(self) -> list[tuple[str, Optional[str]]]:
        """
        Return sorted iterable of help tuples for all member Arguments.

        Sorts like so:

        * General sort is alphanumerically
        * Short flags win over long flags
        * Arguments with *only* long flags and *no* short flags will come
          first.
        * When an Argument has multiple long or short flags, it will sort using
          the most favorable (lowest alphabetically) candidate.

        This will result in a help list like so::

            --alpha, --zeta # 'alpha' wins
            --beta
            -a, --query # short flag wins
            -b, --argh
            -c

        .. versionadded:: 1.0
        """
        # TODO: argument/flag API must change :(
        # having to call to_flag on 1st name of an Argument is just dumb.
        # To pass in an Argument object to help_for may require moderate
        # changes?
        return list(
            map(
                lambda x: self.help_for(to_flag(x.name)),
                sorted(self.flags.values(), key=flag_key),
            )
        )

    def flag_names(self) -> tuple[str, ...]:
        """
        Similar to `help_tuples` but returns flag names only, no helpstrs.

        Specifically, all flag names, flattened, in rough order.

        .. versionadded:: 1.0
        """
        # Regular flag names
        flags = sorted(self.flags.values(), key=flag_key)
        names = [self.names_for(to_flag(x.name)) for x in flags]
        # Inverse flag names sold separately
        names.append(list(self.inverse_flags.keys()))
        return tuple(itertools.chain.from_iterable(names))
