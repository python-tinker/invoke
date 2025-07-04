"""
Command-line completion mechanisms, executed by the core ``--complete`` flag.
"""

import glob
import os
import re
import shlex
from typing import TYPE_CHECKING

from ..exceptions import Exit, ParseError
from ..util import debug, task_name_sort_key

if TYPE_CHECKING:
    from ..collection import Collection
    from ..parser import Parser, ParserContext, ParseResult


def complete(
    names: list[str],
    core: "ParseResult",
    initial_context: "ParserContext",
    collection: "Collection",
    parser: "Parser",
) -> Exit:
    # Strip out program name (scripts give us full command line)
    # TODO: this may not handle path/to/script though?
    invocation = re.sub(r"^({}) ".format("|".join(names)), "", core.remainder)
    debug("Completing for invocation: %r", invocation)
    # Tokenize (shlex will have to do)
    tokens = shlex.split(invocation)
    # Handle flags (partial or otherwise)
    if tokens and tokens[-1].startswith("-"):
        tail = tokens[-1]
        debug("Invocation's tail %r is flag-like", tail)
        # Gently parse invocation to obtain 'current' context.
        # Use last seen context in case of failure (required for
        # otherwise-invalid partial invocations being completed).

        contexts: list[ParserContext]
        try:
            debug("Seeking context name in tokens: %r", tokens)
            contexts = parser.parse_argv(tokens)
        except ParseError as err:
            debug(
                "Got parser error (%r), grabbing its last-seen context %r",
                err,
                err.context,
            )
            contexts = [err.context] if err.context is not None else []
        # Fall back to core context if no context seen.
        debug("Parsed invocation, contexts: %r", contexts)
        if not contexts or not contexts[-1]:
            context = initial_context
        else:
            context = contexts[-1]
        debug("Selected context: %r", context)
        # Unknown flags (could be e.g. only partially typed out; could be
        # wholly invalid; doesn't matter) complete with flags.
        debug("Looking for %r in %r", tail, context.flags)
        if tail not in context.flags:
            debug("Not found, completing with flag names")
            # Long flags - partial or just the dashes - complete w/ long flags
            if tail.startswith("--"):
                for name in filter(
                    lambda x: x.startswith("--"), context.flag_names()
                ):
                    print(name)
            # Just a dash, completes with all flags
            elif tail == "-":
                for name in context.flag_names():
                    print(name)
            # Otherwise, it's something entirely invalid (a shortflag not
            # recognized, or a java style flag like -foo) so return nothing
            # (the shell will still try completing with files, but that doesn't
            # hurt really.)
            else:
                pass
        # Known flags complete w/ nothing or tasks, depending
        else:
            # Flags expecting values: do nothing, to let default (usually
            # file) shell completion occur (which we actively want in this
            # case.)
            if context.flags[tail].takes_value:
                debug("Found, and it takes a value, so no completion")
            # Not taking values (eg bools): print task names
            else:
                debug("Found, takes no value, printing task names")
                print_task_names(collection)
    # If not a flag, is either task name or a flag value, so just complete
    # task names.
    else:
        debug("Last token isn't flag-like, just printing task names")
        print_task_names(collection)
    raise Exit


def print_task_names(collection: "Collection") -> None:
    for name in sorted(collection.task_names, key=task_name_sort_key):
        print(name)
        # Just stick aliases after the thing they're aliased to. Sorting isn't
        # so important that it's worth bending over backwards here.
        for alias in collection.task_names[name]:
            print(alias)


def print_completion_script(shell: str, names: list[str]) -> None:
    # Grab all .completion files in invoke/completion/. (These used to have no
    # suffix, but surprise, that's super fragile.
    completions = {
        os.path.splitext(os.path.basename(x))[0]: x
        for x in glob.glob(
            os.path.join(
                os.path.dirname(os.path.realpath(__file__)), "*.completion"
            )
        )
    }
    try:
        path = completions[shell]
    except KeyError as exc:
        err = 'Completion for shell "{}" not supported (options are: {}).'
        raise ParseError(
            err.format(shell, ", ".join(sorted(completions)))
        ) from exc
    debug("Printing completion script from %s", path)
    # Choose one arbitrary program name for script's own internal invocation
    # (also used to construct completion function names when necessary)
    binary = names[0]
    with open(path, "r", encoding="utf-8") as script:
        print(
            script.read().format(binary=binary, spaced_names=" ".join(names))
        )
